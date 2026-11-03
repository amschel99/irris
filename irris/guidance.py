"""Guidance v2 (docs/GUIDANCE_SPEC.md): the step tracker, checklist verification and speech suggestions.

The model only proposes; code decides. A run keeps a tracker next to its log:

- a plan: the procedure's steps (normalised, with defaults) plus any inserted recovery steps, each with a stable uid,
  so inserting steps never breaks earlier history;
- per-step status (not_started | in_progress | complete | error | skipped), progress, the latest evidence and votes;
- rules: complete needs the prereqs complete; an automatic check needs two passes at least 3 s apart (a check the
  person asked for, decided by voting, or a person's own pass counts at once); the model never reopens a complete
  step; nothing happening is "idle", never a deviation;
- `say` items for the browser (it applies the interruption policy; the server never speaks by itself);
- metrics per run: completion times, abstentions, people overriding a fail, instrument reads vs what people confirmed.

Checklist verification asks the step's yes/no questions over the last few frames, votes 3 samples for decisions
(1 for previews), and abstains, before any model call, when the picture can't show the answer.
"""
from __future__ import annotations

import io
import re
import statistics
from typing import Any

from irris import llm
from irris.db import Database, dumps, loads, now

IDLE_S = 60.0
CONFIRM_GAP_S = 3.0
SETTLE_LOOKS = 3
SETTLE_CONF = 0.75
SAY_TTL = 120.0
SAY_GAP_S = 25.0
OVERDUE_GRACE_S = 15.0
DONE = ("complete", "skipped")


# ================================================================ steps: defaults
def default_questions(step: dict) -> list[dict]:
    """2-3 visible yes/no questions from the step's `check` (or its instruction)."""
    src = str(step.get("check") or "").strip()
    if not src:
        return [{"q": f"Is this done: {_short(step.get('instruction', ''), 18)}?", "must": True}]
    parts = [p.strip(" .") for p in re.split(r";|\band\b|, (?=[a-z])", src) if len(p.strip(" .")) > 2][:3]
    return [{"q": f"Can you see this: {p}?", "must": True} for p in (parts or [src])]


def normalize_step(s: dict, i: int) -> dict:
    out = dict(s)
    out.setdefault("questions", default_questions(s))
    out.setdefault("typical_errors", [])
    out.setdefault("kind", "measurement" if s.get("reading") or s.get("instrument_id") else "action")
    out.setdefault("severity", "major")
    out.setdefault("reversible", True)
    out.setdefault("prereqs", [i - 1] if i > 0 else [])
    out.setdefault("expected_objects", [])
    out.setdefault("intervention", "notify_if_missed" if out["reversible"] else "remind_before")
    return out


def _short(text: str, words: int = 12) -> str:
    w = str(text or "").split()
    return " ".join(w[:words]) + ("…" if len(w) > words else "")


# ================================================================ the tracker
def _new_step_state() -> dict:
    return {"status": "not_started", "passes": [], "fails": 0, "looks": [], "settled": False, "started_at": None,
            "completed_at": None, "votes": {}, "evidence": None, "last_check": None, "by": ""}


def init(db: Database, workspace_id: str, run_id: str, steps: list[dict]) -> dict:
    plan = []
    for i, s in enumerate(steps):
        st = normalize_step(s, i)
        st["uid"] = f"s{i}"
        st["after"] = [f"s{p}" for p in st["prereqs"] if 0 <= p < len(steps)]
        plan.append(st)
    t = now()
    state = {"plan": plan, "steps": {p["uid"]: _new_step_state() for p in plan}, "phase": "working",
             "plan_version": 1, "recovery": None, "say": [], "last_activity": t, "created": t,
             "metrics": {"vision_checks": 0, "abstained": 0, "overrides": 0, "readings": []}, "next_r": 1}
    if plan:
        state["steps"][plan[0]["uid"]]["started_at"] = t
    save(db, workspace_id, run_id, state)
    return state


def load(db: Database, workspace_id: str, run_id: str) -> dict | None:
    row = db.one("SELECT state FROM run_tracker WHERE run_id = ? AND workspace_id = ?", [run_id, workspace_id])
    return loads(row["state"], None) if row else None


def save(db: Database, workspace_id: str, run_id: str, state: dict) -> None:
    t = now()
    if db.one("SELECT run_id FROM run_tracker WHERE run_id = ?", [run_id]):
        db.execute("UPDATE run_tracker SET state = ?, updated = ? WHERE run_id = ?", [dumps(state), t, run_id])
    else:
        db.execute("INSERT INTO run_tracker (run_id, workspace_id, state, updated) VALUES (?,?,?,?)",
                   [run_id, workspace_id, dumps(state), t])


def ensure(db: Database, workspace_id: str, run_id: str, steps: list[dict]) -> dict:
    """The tracker of a run; runs started before Guidance v2 get one built from their procedure on first use."""
    return load(db, workspace_id, run_id) or init(db, workspace_id, run_id, steps)


def index_of(state: dict, uid: str) -> int:
    return next((i for i, p in enumerate(state["plan"]) if p["uid"] == uid), -1)


def prereqs_met(state: dict, uid: str) -> tuple[bool, list[int]]:
    step = state["plan"][index_of(state, uid)]
    missing = [index_of(state, a) for a in step.get("after", []) if state["steps"].get(a, {}).get("status") not in DONE]
    return not missing, [m for m in missing if m >= 0]


def focus(state: dict) -> int:
    """The step to work on: the first one not done whose prereqs are done (else the first not done)."""
    todo = [i for i, p in enumerate(state["plan"]) if state["steps"][p["uid"]]["status"] not in DONE]
    if not todo:
        return len(state["plan"])
    for i in todo:
        if prereqs_met(state, state["plan"][i]["uid"])[0]:
            return i
    return todo[0]


def all_done(state: dict) -> bool:
    return all(state["steps"][p["uid"]]["status"] in DONE for p in state["plan"])


def touch(state: dict, t: float | None = None) -> None:
    state["last_activity"] = t or now()
    state["phase"] = "working"


def apply_result(state: dict, idx: int, result: str, *, by: str, explicit: bool, t: float | None = None,
                 confidence: float | None = None) -> dict:
    """Record one observation of step `idx` and decide what it means. Returns events:
    {"completed", "confirming", "ignored", "rejected", "deviation": None|"error"|"order", "confirmed_error"}."""
    t = t or now()
    p = state["plan"][idx]
    s = state["steps"][p["uid"]]
    person = by != "vision"
    ev: dict[str, Any] = {"completed": False, "confirming": False, "ignored": "", "rejected": "", "deviation": None,
                          "confirmed_error": False}
    touch(state, t)
    if s["started_at"] is None:
        s["started_at"] = t
    if not person:
        state["metrics"]["vision_checks"] += 1
        s["looks"] = (s["looks"] + [{"result": result, "conf": confidence if confidence is not None else 0.5, "t": t}])[-6:]
        last = s["looks"][-SETTLE_LOOKS:]
        s["settled"] = len(last) == SETTLE_LOOKS and len({x["result"] for x in last}) == 1 \
            and min(x["conf"] for x in last) >= SETTLE_CONF
    if s["status"] in DONE and not person:
        ev["ignored"] = "already complete"           # the model never reopens a finished step
        return ev
    if result == "pass":
        met, missing = prereqs_met(state, p["uid"])
        if not met and not person:
            ev["rejected"] = "step " + ", ".join(str(m + 1) for m in missing) + " comes first"
            ev["deviation"] = "order"
            s["status"] = "in_progress"
            return ev
        if not met:
            ev["deviation"] = "order"                # a person can go out of order, but it's recorded
        if person and s["status"] == "error":
            state["metrics"]["overrides"] += 1       # a person passed a step the model had failed: a false alarm
        prior = [x for x in s["passes"] if t - x >= CONFIRM_GAP_S]
        s["passes"].append(t)
        if person or explicit or prior:
            s["status"], s["completed_at"], s["by"] = "complete", t, by
            ev["completed"] = True
        else:
            s["status"] = "in_progress"
            ev["confirming"] = True
    elif result == "skip":
        s["status"], s["completed_at"], s["by"] = "skipped", t, by
        ev["completed"] = True
        ev["deviation"] = "skip"
    elif result in ("fail", "danger"):
        s["fails"] += 1
        s["status"] = "error"
        ev["deviation"] = "error"
        ev["confirmed_error"] = person or explicit or s["fails"] >= 2 or result == "danger"
    else:                                            # unsure
        if s["status"] == "not_started":
            s["status"] = "in_progress"
    if ev["completed"]:
        nxt = focus(state)
        if nxt < len(state["plan"]):
            ns = state["steps"][state["plan"][nxt]["uid"]]
            if ns["started_at"] is None:
                ns["started_at"] = t
    return ev


def reopen(state: dict, idx: int) -> None:
    """A person stepping back reopens a step (the model never does)."""
    s = state["steps"][state["plan"][idx]["uid"]]
    s.update(status="in_progress", completed_at=None, passes=[], settled=False)
    touch(state)


def progress(state: dict, idx: int, t: float | None = None) -> float:
    t = t or now()
    p = state["plan"][idx]
    s = state["steps"][p["uid"]]
    if s["status"] in DONE:
        return 1.0
    parts = []
    votes = s.get("votes") or {}
    musts = [q for q in p.get("questions", []) if q.get("must", True)]
    if votes and musts:
        yes = sum(1 for q in musts if (votes.get(q["q"]) or {}).get("answer") == "yes")
        parts.append(0.9 * yes / len(musts))
    if p.get("median_s") and s.get("started_at") is not None:
        parts.append(min(0.9, 0.9 * (t - s["started_at"]) / max(p["median_s"], 1)))
    if s["status"] == "in_progress" and not parts:
        parts.append(0.1)
    return round(max(parts) if parts else 0.0, 2)


# ================================================================ speech suggestions
def add_say(state: dict, text: str, *, severity: str = "major", topic: str, kind: str = "warning",
            step: int | None = None, words: int = 12) -> bool:
    """Queue a line for the browser. One line per topic per 25 s; at most `words` words."""
    t = now()
    if any(x["topic"] == topic and t - x["t"] < SAY_GAP_S for x in state["say"]):
        return False
    item = {"text": _short(text, words), "severity": severity if severity in ("critical", "major", "minor") else "major",
            "topic": topic, "kind": kind, "step": step, "t": t}
    state["say"] = [x for x in state["say"] if t - x["t"] < SAY_TTL][-19:] + [item]
    return True


def reminders(state: dict, t: float | None = None) -> None:
    """Time-based suggestions for the step in focus: remind before irreversible steps, ask about overdue ones."""
    t = t or now()
    i = focus(state)
    if i >= len(state["plan"]):
        return
    p = state["plan"][i]
    s = state["steps"][p["uid"]]
    if p.get("intervention") == "remind_before" and s["status"] == "not_started":
        add_say(state, f"Careful, can't be undone: {p['instruction']}", severity=p.get("severity", "major"),
                topic=f"{p['uid']}:remind", kind="reminder", step=i + 1)
    elif p.get("intervention") == "notify_if_missed" and p.get("median_s") and s.get("started_at") is not None \
            and s["status"] not in DONE and t - s["started_at"] > p["median_s"] + OVERDUE_GRACE_S:
        add_say(state, f"Have you done this: {_short(p['instruction'], 8)}?", severity="minor",
                topic=f"{p['uid']}:missed", kind="question", step=i + 1)


def update_phase(state: dict, camera_changed: bool, t: float | None = None) -> str:
    t = t or now()
    if camera_changed:
        state["last_activity"] = t
    state["phase"] = "idle" if t - state.get("last_activity", t) > IDLE_S else "working"
    return state["phase"]


def view(state: dict, t: float | None = None) -> dict:
    """The tracker part of the run state the API returns."""
    t = t or now()
    steps = []
    for i, p in enumerate(state["plan"]):
        s = state["steps"][p["uid"]]
        steps.append({"status": s["status"], "progress": progress(state, i, t), "evidence": s.get("evidence"),
                      "votes": s.get("votes") or {}, "last_check": s.get("last_check"), "settled": s.get("settled", False),
                      "started_at": s.get("started_at"), "completed_at": s.get("completed_at")})
    return {"steps": steps, "phase": state.get("phase", "working"), "plan_version": state.get("plan_version", 1),
            "recovery": state.get("recovery"), "say": [x for x in state.get("say", []) if t - x["t"] < SAY_TTL],
            "metrics": metrics(state)}


def plan_with_indexes(state: dict) -> list[dict]:
    """Plan steps with `prereqs` as current indexes (they're stored as uids so inserts don't break them)."""
    out = []
    for p in state["plan"]:
        q = {k: v for k, v in p.items() if k != "after"}
        q["prereqs"] = [index_of(state, a) for a in p.get("after", []) if index_of(state, a) >= 0]
        out.append(q)
    return out


def metrics(state: dict) -> dict:
    m = state.get("metrics") or {}
    durations = []
    for p in state["plan"]:
        s = state["steps"][p["uid"]]
        if s.get("completed_at") and s.get("started_at"):
            durations.append(round(s["completed_at"] - s["started_at"], 1))
    checks = m.get("vision_checks", 0)
    reads = m.get("readings", [])
    errs = [abs(r["auto"] - r["confirmed"]) for r in reads if r.get("auto") is not None and r.get("confirmed") is not None]
    return {"vision_checks": checks, "abstained": m.get("abstained", 0),
            "abstention_rate": round(m.get("abstained", 0) / checks, 2) if checks else 0.0,
            "false_alarms": m.get("overrides", 0), "step_seconds": durations,
            "median_step_s": round(statistics.median(durations), 1) if durations else None,
            "readings": reads[-20:], "reading_error_mean": round(sum(errs) / len(errs), 3) if errs else None}


# ================================================================ inserting recovery steps
def insert_steps(state: dict, failed_idx: int, steps: list[dict]) -> list[int]:
    """Insert recovery steps just before the step that went wrong: do the fix-up steps, then redo that step.
    The recovery steps take over its prereqs, and it now depends on the last of them. Returns their indexes."""
    if not steps:
        return []
    anchor = state["plan"][failed_idx] if 0 <= failed_idx < len(state["plan"]) else None
    new_uids: list[str] = []
    for k, s in enumerate(steps):
        uid = f"r{state.get('next_r', 1)}"
        state["next_r"] = state.get("next_r", 1) + 1
        st = normalize_step({**s, "recovery": True}, 0)
        st["uid"] = uid
        st["after"] = [new_uids[-1]] if new_uids else (list(anchor.get("after", [])) if anchor else [])
        st["prereqs"] = []
        state["plan"].insert((failed_idx if anchor else len(state["plan"])) + k, st)
        state["steps"][uid] = _new_step_state()
        new_uids.append(uid)
    if anchor:
        anchor["after"] = [new_uids[-1]]
        a = state["steps"][anchor["uid"]]
        a.update(status="not_started", passes=[], fails=0, settled=False, looks=[], completed_at=None)
    state["steps"][new_uids[0]]["started_at"] = now()
    state["plan_version"] = state.get("plan_version", 1) + 1
    touch(state)
    return [index_of(state, u) for u in new_uids]


# ================================================================ evidence: can the picture show the answer?
HARD = ("blurry", "too dark", "too bright", "old frame", "covered by hands", "too small")


def evidence(jpeg: bytes, *, roi: dict | None = None, age_s: float | None = None,
             hands: list[dict] | None = None, expected: list[str] | None = None, scene: str = "") -> dict:
    """{"score", "reasons", "abstain", "say"} from the frame alone, before any model call.
    Frames smaller than 160 px can't be judged (score None, never abstains)."""
    reasons: list[str] = []
    try:
        import numpy as np
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg)).convert("L")
    except Exception:  # noqa: BLE001
        return {"score": None, "reasons": [], "abstain": False, "say": ""}
    W, H = img.size
    if min(W, H) < 160:
        return {"score": None, "reasons": [], "abstain": False, "say": ""}
    if roi:
        x0, y0 = int(roi["x"] * W), int(roi["y"] * H)
        x1, y1 = int((roi["x"] + roi["w"]) * W), int((roi["y"] + roi["h"]) * H)
        if (x1 - x0) * (y1 - y0) < 40 * 40:
            reasons.append("too small")
        else:
            img = img.crop((max(0, x0), max(0, y0), min(W, x1), min(H, y1)))
    img.thumbnail((512, 512))
    a = np.asarray(img, dtype=np.float32)
    mean, std = float(a.mean()), float(a.std())
    if a.shape[0] > 2 and a.shape[1] > 2:
        lap = (4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:])
        sharp = float(lap.var())
    else:
        sharp = 0.0
    if mean < 25:
        reasons.append("too dark")
    elif mean > 235 and std < 12:
        reasons.append("too bright")
    elif std > 8 and sharp < 12:
        reasons.append("blurry")
    if age_s is not None and age_s > 5:
        reasons.append("old frame")
    if roi and hands:
        rx0, ry0, rx1, ry1 = roi["x"], roi["y"], roi["x"] + roi["w"], roi["y"] + roi["h"]
        area = max(1e-6, roi["w"] * roi["h"])
        cover = 0.0
        for h in hands:
            try:
                ix = max(0.0, min(rx1, h["x"] + h["w"]) - max(rx0, h["x"]))
                iy = max(0.0, min(ry1, h["y"] + h["h"]) - max(ry0, h["y"]))
            except (KeyError, TypeError):
                continue
            cover += ix * iy
        if cover / area > 0.5:
            reasons.append("covered by hands")
    if expected and scene:
        low = scene.lower()
        missing = [o for o in expected if o.lower() not in low]
        if missing and len(missing) == len(expected):
            reasons.append(f"no {missing[0]} in view")
    hard = [r for r in reasons if r in HARD]
    score = round(max(0.0, 1.0 - 0.35 * len(hard) - 0.15 * (len(reasons) - len(hard))), 2)
    say = ""
    if hard:
        say = {"blurry": "It's blurry. Hold the camera still.",
               "too dark": "Too dark to see. Add some light.",
               "too bright": "Too much glare. Tilt the camera.",
               "old frame": "The camera stopped sending. Check it's on.",
               "covered by hands": "Your hands are covering it. Move them aside.",
               "too small": "Too far away. Move the camera closer."}[hard[0]]
    return {"score": score, "reasons": reasons, "abstain": bool(hard), "say": say}


def bbox_of_zone(zone: dict | None) -> dict | None:
    if not zone or not zone.get("polygon"):
        return None
    xs = [p[0] for p in zone["polygon"]]
    ys = [p[1] for p in zone["polygon"]]
    return {"x": min(xs), "y": min(ys), "w": max(xs) - min(xs), "h": max(ys) - min(ys)}


# ================================================================ checklist verification (yes/no questions, voted)
CHECKLIST_PROMPT = """You check one step of a procedure through a camera. You get a few frames from the last seconds,
oldest first. Answer every question about what is VISIBLE, mostly in the latest frame: "yes", "no", or "unsure" when
the frames don't clearly show it. Never guess. Also say whether anything is unsafe.
Reply as JSON: {"answers": [{"i": 1, "a": "yes|no|unsure"}], "danger": false,
"say": "one short sentence for the person, under 12 words"}"""


def _parse_answers(out: dict, n: int) -> tuple[list[str], bool, str]:
    """A model's answers as a list of yes/no/unsure (index-aligned with the questions). Models that answer in the
    older shape ({"result": "pass|fail|..."}) are mapped: pass -> all yes, fail -> the first must is no."""
    say = str(out.get("say") or "").strip()
    danger = out.get("danger") is True or out.get("result") == "danger"
    ans = ["unsure"] * n
    if isinstance(out.get("answers"), list):
        for k, a in enumerate(out["answers"]):
            if not isinstance(a, dict):
                continue
            try:
                i = int(a.get("i", k + 1)) - 1
            except (TypeError, ValueError):
                i = k
            v = str(a.get("a") or a.get("answer") or "").strip().lower()
            if 0 <= i < n and v in ("yes", "no", "unsure"):
                ans[i] = v
    elif out.get("result") in ("pass", "fail", "unsure", "danger"):
        r = out["result"]
        ans = ["yes"] * n if r == "pass" else (["no"] + ["unsure"] * (n - 1) if r in ("fail", "danger") else ans)
    return ans, danger, say


def vote(samples: list[list[str]], questions: list[dict]) -> dict:
    """Majority per question across samples; pass = every must is yes, fail = any must is no, else unsure."""
    n = len(samples) or 1
    votes, majority = {}, []
    for qi, q in enumerate(questions):
        counts = {"yes": 0, "no": 0, "unsure": 0}
        for s in samples:
            counts[s[qi] if qi < len(s) else "unsure"] += 1
        best = max(counts, key=lambda k: (counts[k], k == "unsure"))
        answer = best if counts[best] * 2 > n else "unsure"
        votes[q["q"]] = {**counts, "answer": answer, "must": q.get("must", True)}
        majority.append((answer, counts[best] / n))
    musts = [(a, c) for (a, c), q in zip(majority, questions) if q.get("must", True)] or majority
    if any(a == "no" for a, _ in musts):
        result = "fail"
    elif musts and all(a == "yes" for a, _ in musts):
        result = "pass"
    else:
        result = "unsure"
    conf = round(sum(c for _, c in majority) / len(majority), 2) if majority else 0.0
    return {"result": result, "votes": votes, "confidence": conf}


async def checklist(models: list[llm.Model], frames: list[bytes], step: dict, index: int, total: int, *,
                    workspace_id: str = "", decision: bool = True, background: bool = False, scene: str = "",
                    knowledge: str = "") -> dict:
    """Ask the step's questions over the frames. Decisions: up to 3 samples at temperature 0.7 (stop after 2 that agree
    completely); previews: 1 sample. Returns {result, say, votes, confidence, samples, model, where, ms}."""
    from irris import vision
    questions = step.get("questions") or default_questions(step)
    lines = [f"Step {index + 1} of {total}: {step['instruction']}"]
    if step.get("caution"):
        lines.append(f"Caution: {step['caution']}")
    if step.get("typical_errors"):
        lines.append("Common mistakes here: " + "; ".join(step["typical_errors"][:4]))
    if scene:
        lines.append("On-device detector, just now: " + scene[:500])
    if knowledge:
        lines.append("Team knowledge:\n" + knowledge[:1200])
    lines.append("Questions:\n" + "\n".join(f"{i + 1}. {q['q']}" for i, q in enumerate(questions)))
    side = min(448, vision.side_for(models))
    content: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    for f in frames[-4:]:
        content.append(llm.image_part(vision.shrink(f, side)))
    msgs = [{"role": "system", "content": CHECKLIST_PROMPT}, {"role": "user", "content": content}]
    samples, says, danger_votes, ms, info = [], [], 0, 0, {"model": "", "where": ""}
    want = 3 if decision else 1
    for k in range(want):
        msg, info = await vision.call(models, msgs, workspace_id=workspace_id, kind="check", max_tokens=200,
                                      background=background, temperature=0.7 if decision else 0.2)
        ms += info.get("ms", 0)
        ans, danger, say = _parse_answers(llm.parse_json(msg.get("content") or ""), len(questions))
        samples.append(ans)
        danger_votes += 1 if danger else 0
        if say:
            says.append(say)
        if k == 1 and samples[0] == samples[1]:
            break                                  # two identical samples: the third can't change the majority
    out = vote(samples, questions)
    if danger_votes * 2 > len(samples):
        out["result"] = "danger"
    say = says[-1] if says else ""
    if out["result"] == "fail" and not say:
        no = next((q for q, v in out["votes"].items() if v["answer"] == "no" and v["must"]), "")
        say = f"Not yet: {no.replace('Can you see this: ', '').rstrip('?')}" if no else "That doesn't look right yet."
    elif out["result"] == "unsure" and not say:
        say = "I can't tell yet. Show me the work closer."
    return {**out, "say": _short(say, 14), "samples": len(samples), "model": info.get("model", ""),
            "where": info.get("where", ""), "ms": ms}
