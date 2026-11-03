"""Guided runs: a person works through a procedure while Irris checks each step through a camera.

Every check is logged. Readings become `reading` records. When the last step is done, the run writes a
`batch_record` record: every step, who or what checked it, the result, the readings and the times.

Guidance v2 (docs/GUIDANCE_SPEC.md, irris/guidance.py) keeps a tracker per run: step statuses validated in code
(prereqs, double confirmation, never reopened by the model, idle is not a deviation), checklist votes and evidence,
speech suggestions for the browser, recovery plans, and per-run metrics.
"""
from __future__ import annotations

import asyncio
import os

from irris import ops, guidance, instruments, irf, llm, observe, perception, store, vision
from irris.db import Database, dumps, loads, new_id, now


class RunError(Exception):
    pass


def _row(db: Database, workspace_id: str, run_id: str) -> dict:
    row = db.one("SELECT * FROM runs WHERE id = ? AND workspace_id = ?", [run_id, workspace_id])
    if not row:
        raise store.NotFound("run not found")
    return row


def _proc(db: Database, workspace_id: str, procedure_id: str) -> dict:
    try:
        return store.procedure(db, workspace_id, procedure_id)
    except store.NotFound:
        return {"id": procedure_id, "name": "(deleted procedure)", "steps": [], "subject": ""}


def _tracker(db: Database, workspace_id: str, row: dict, proc: dict | None = None) -> dict:
    t = guidance.load(db, workspace_id, row["id"])
    if t is None:
        proc = proc or _proc(db, workspace_id, row["procedure_id"])
        t = guidance.init(db, workspace_id, row["id"], proc["steps"])
        # a run started before Guidance v2: rebuild statuses from its log
        for e in loads(row["log"], []):
            i = e.get("step", -1)
            if 0 <= i < len(t["plan"]) and e.get("result") in ("pass", "skip"):
                s = t["steps"][t["plan"][i]["uid"]]
                s.update(status="complete" if e["result"] == "pass" else "skipped", completed_at=e.get("time"))
        guidance.save(db, workspace_id, row["id"], t)
    return t


def _entries_for(log: list[dict], step: dict, index: int) -> list[dict]:
    uid = step.get("uid")
    return [e for e in log if (e.get("uid") == uid if e.get("uid") else e.get("step") == index)]


def state(db: Database, workspace_id: str, run_id: str) -> dict:
    row = _row(db, workspace_id, run_id)
    proc = _proc(db, workspace_id, row["procedure_id"])
    tr = _tracker(db, workspace_id, row, proc)
    if row["status"] == "active":
        cam = row["camera_id"]
        guidance.update_phase(tr, bool(cam) and vision.hub.last_change(cam) > tr.get("last_activity", 0))
        before = len(tr.get("say", []))
        guidance.reminders(tr)
        if len(tr.get("say", [])) != before:
            guidance.save(db, workspace_id, run_id, tr)
    log = loads(row["log"], [])
    plan = guidance.plan_with_indexes(tr)
    tv = guidance.view(tr)
    steps = []
    for i, s in enumerate(plan):
        checks = _entries_for(log, s, i)
        last = checks[-1] if checks else None
        st = {k: v for k, v in s.items() if k not in ("prototype", "uid")}
        steps.append({**st, "uid": s["uid"], "index": i, "result": last["result"] if last else None,
                      "say": last["say"] if last else "", "measured": last.get("reading") if last else None,
                      "snapshot": next((e["snapshot"] for e in reversed(checks) if e.get("snapshot")), None),
                      "checks": len(checks), **(tv["steps"][i] if i < len(tv["steps"]) else {})})
        steps[-1]["started"] = steps[-1].get("started_at")      # alias the Live screen reads
    clips = db.execute("SELECT id, data, created FROM records WHERE workspace_id = ? AND type = 'recording' AND source = ? "
                       "ORDER BY created", [workspace_id, f"runs/{run_id}/recordings"])
    recordings = [{"record_id": r["id"], "created": r["created"],
                   **{k: v for k, v in loads(r["data"], {}).items() if k in ("file_id", "mime", "size", "step")}}
                  for r in clips]
    focus = guidance.focus(tr) if row["status"] == "active" else row["step"]
    return {"id": row["id"], "status": row["status"], "step": min(focus, max(len(steps) - 1, 0)) if row["status"] == "active"
            else row["step"], "label": row["label"], "recordings": recordings,
            "procedure": {"id": proc["id"], "name": proc["name"]}, "camera_id": row["camera_id"],
            "started": row["started"], "ended": row["ended"], "steps": steps, "log": log[-50:],
            "phase": tv["phase"], "plan_version": tv["plan_version"], "recovery": tv["recovery"], "say": tv["say"],
            "metrics": tv["metrics"], **_signed(db, workspace_id, run_id, row["status"], steps, tv, focus)}


def _signed(db: Database, workspace_id: str, run_id: str, status: str, steps: list, tv: dict, focus: int) -> dict:
    sigs = ops.signatures(db, workspace_id, run_id)
    released = any(x["meaning"] == "approved" and x["status"] == "approved" for x in sigs)
    view = {"status": status, "step": min(focus, max(len(steps) - 1, 0)), "steps": steps, "phase": tv["phase"],
            "recovery": tv["recovery"]}
    return {"signatures": sigs, "released": released, "status_info": ops.run_status(view, sigs)}


def start(db: Database, workspace_id: str, procedure_id: str, *, camera_id: str = "", user_id: str = "",
          label: str = "") -> dict:
    proc = store.procedure(db, workspace_id, procedure_id)
    if not proc["steps"]:
        raise RunError("that procedure has no steps")
    run_id = new_id()
    db.execute("INSERT INTO runs (id, workspace_id, procedure_id, camera_id, user_id, status, step, log, label, started) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               [run_id, workspace_id, procedure_id, camera_id, user_id, "active", 0, "[]", label[:120], now()])
    tr = guidance.init(db, workspace_id, run_id, proc["steps"])
    guidance.reminders(tr)
    guidance.save(db, workspace_id, run_id, tr)
    return _pushed(state(db, workspace_id, run_id))


def _pushed(s: dict) -> dict:
    """Tell whoever watches the run's camera (the phone, Live) about the new run state."""
    if s.get("camera_id"):
        vision.hub.publish(s["camera_id"], {"type": "run", "run": push_view(s), "say": s["say"]})
    return s


def list_runs(db: Database, workspace_id: str, limit: int = 50) -> list[dict]:
    rows = db.execute("SELECT r.*, p.title AS procedure_name FROM runs r LEFT JOIN records p ON p.id = r.procedure_id "
                      "WHERE r.workspace_id = ? ORDER BY r.started DESC LIMIT ?", [workspace_id, limit])
    out = []
    for r in rows:
        log = loads(r["log"], [])
        out.append({"id": r["id"], "status": r["status"], "step": r["step"], "label": r["label"],
                    "procedure": {"id": r["procedure_id"], "name": r["procedure_name"] or "(deleted procedure)"},
                    "started": r["started"], "ended": r["ended"], "checks": len(log),
                    "deviations": sum(1 for e in log if e["result"] in ("fail", "danger", "skip")
                                      or e.get("deviation") == "order")})
        sigs = ops.signatures(db, workspace_id, r["id"])
        out[-1].update(released=any(x["meaning"] == "approved" and x["status"] == "approved" for x in sigs),
                       status_info=ops.run_status({"status": r["status"], "step": r["step"], "steps": [], "phase": "",
                                                   "recovery": None}, sigs))
    return out


def record(db: Database, workspace_id: str, run_id: str, result: str, *, say: str = "", reading: dict | None = None,
           by: str = "vision", note: str = "", snapshot: str | None = None, meta: dict | None = None,
           step: int | None = None, explicit: bool = True, confidence: float | None = None) -> dict:
    """Log one check of a step (the step in focus unless `step` is given) and let the tracker decide what it means.

    A person's pass or skip completes the step. The model's pass completes it when the check was one the person asked
    for (decided by voting / a verified reading), or when an automatic check passes a second time 3 s or more after
    the first. Complete steps are never reopened by the model. Every check keeps evidence: the frame that was judged
    (vision checks), or, for a check marked by hand while a camera is live, the frame at that moment."""
    if result not in ("pass", "fail", "unsure", "danger", "skip"):
        raise RunError("result must be pass, fail, unsure, danger or skip")
    row = _row(db, workspace_id, run_id)
    if row["status"] != "active":
        raise RunError(f"this run is {row['status']}")
    proc = _proc(db, workspace_id, row["procedure_id"])
    tr = _tracker(db, workspace_id, row, proc)
    total = len(tr["plan"])
    idx = guidance.focus(tr) if step is None else int(step)
    if not 0 <= idx < total:
        raise RunError(f"there's no step {idx + 1}")
    plan_step = tr["plan"][idx]
    if snapshot is None and row["camera_id"]:
        frame = vision.hub.get(row["camera_id"])
        if frame:
            snapshot = vision.save_snapshot(db, workspace_id, frame, f"run {run_id} step {idx + 1}.jpg")
    ev = guidance.apply_result(tr, idx, result, by=by, explicit=explicit, confidence=confidence)
    entry = {"step": idx, "uid": plan_step["uid"], "result": result, "say": say, "reading": reading, "by": by,
             "note": note, "time": now(), "snapshot": snapshot, **(meta or {})}
    if ev["deviation"] == "order":
        entry["deviation"] = "order"
    if ev["ignored"]:
        entry["ignored"] = ev["ignored"]
    log = loads(row["log"], []) + [entry]
    sev = plan_step.get("severity", "major")
    if ev["rejected"]:
        guidance.add_say(tr, f"Do {ev['rejected'].replace(' comes first', '')} first.", severity=sev,
                         topic=f"{plan_step['uid']}:order", kind="warning", step=idx + 1)
    elif result == "danger":
        guidance.add_say(tr, say or "Stop. That's unsafe.", severity="critical", topic=f"{plan_step['uid']}:danger",
                         kind="warning", step=idx + 1)
    elif ev["confirmed_error"] and by == "vision":
        guidance.add_say(tr, say or "That step doesn't look right.", severity=sev, topic=f"{plan_step['uid']}:error",
                         kind="warning", step=idx + 1)
    elif ev["completed"] and by == "vision" and result == "pass":
        guidance.add_say(tr, say or "Done.", severity="minor", topic=f"{plan_step['uid']}:done", kind="confirm",
                         step=idx + 1)
    if ev["confirmed_error"]:
        tr["recovery_needed"] = {"step": idx, "error": say or note or result, "t": now()}
    if reading and by != "vision":
        last = (tr.get("instrument_reads") or {}).get(plan_step["uid"])
        if last and last.get("value") is not None:
            tr["metrics"].setdefault("readings", []).append(
                {"step": idx + 1, "auto": last["value"], "confirmed": reading["value"], "method": last.get("method")})
    if reading:
        spec = plan_step.get("reading") or {}
        rec = irf.normalize({"type": "reading", "source": f"runs/{run_id}",
                             "subject": proc.get("subject") or f"procedure:{proc['name']}",
                             "title": spec.get("label") or f"{proc['name']}, step {idx + 1}",
                             "data": {**reading, "run_id": run_id, "step": idx + 1, "result": result,
                                      **({"min": spec["min"]} if "min" in spec else {}),
                                      **({"max": spec["max"]} if "max" in spec else {})},
                             "units": {"value": reading.get("unit", "")}}, now=now())
        store.write(db, workspace_id, [rec])
    done = guidance.all_done(tr)
    status, ended = ("done", now()) if done else ("active", None)
    nxt = guidance.focus(tr)
    guidance.reminders(tr)
    guidance.save(db, workspace_id, run_id, tr)
    db.execute("UPDATE runs SET log = ?, step = ?, status = ?, ended = ? WHERE id = ?",
               [dumps(log), total if done else min(nxt, total - 1), status, ended, run_id])
    if done:
        _batch_record(db, workspace_id, run_id)
    ev_data = {"run_id": run_id, "procedure": proc["name"], "label": row["label"], "step": idx + 1,
               "instruction": plan_step.get("instruction", ""), "result": result, "by": by, "say": say or note}
    if result in ("fail", "danger") and (by != "vision" or ev["confirmed_error"] or result == "danger"):
        ops.emit(db, workspace_id, "step.failed", run_id, ev_data)
    if ev["deviation"] or (ev["confirmed_error"] and result in ("fail", "danger")) or result == "skip":
        ops.emit(db, workspace_id, "deviation.opened", run_id,
                 {**ev_data, "kind": ev["deviation"] or ("skipped" if result == "skip" else "error")})
    if done:
        ops.emit(db, workspace_id, "run.completed", run_id, {"run_id": run_id, "procedure": proc["name"],
                                                             "label": row["label"]})
    out = state(db, workspace_id, run_id)
    if row["camera_id"]:
        moved = out["status"] == "active" and out["step"] != idx
        vision.hub.publish(row["camera_id"], {"type": "verdict", "run_id": run_id, "step": idx + 1,
                                              "result": result, "say": say, "reading": reading,
                                              "next": out["steps"][out["step"]]["instruction"] if moved else "",
                                              "status": out["steps"][idx]["status"] if idx < len(out["steps"]) else "",
                                              "speak": out["say"][-3:]})
        vision.hub.publish(row["camera_id"], {"type": "run", "run": push_view(out), "say": out["say"]})
    return out


STEP_PUSH = ("index", "uid", "instruction", "check", "caution", "reading", "status", "progress", "evidence", "votes",
             "questions", "typical_errors", "kind", "severity", "reversible", "prereqs", "zone", "expected_objects",
             "instrument_id", "median_s", "std_s", "intervention", "recovery", "result", "measured", "started", "settled")


def push_view(s: dict) -> dict:
    """The run state pushed over the camera WebSocket (everything the phone and Live need to draw the step)."""
    return {"id": s["id"], "status": s["status"], "step": s["step"], "phase": s["phase"], "plan_version": s["plan_version"],
            "recovery": s["recovery"], "say": s["say"], "procedure": s["procedure"], "label": s["label"],
            "camera_id": s["camera_id"], "steps": [{k: st.get(k) for k in STEP_PUSH} for st in s["steps"]]}


def move(db: Database, workspace_id: str, run_id: str, action: str) -> dict:
    row = _row(db, workspace_id, run_id)
    if action == "stop":
        if row["status"] == "active":
            db.execute("UPDATE runs SET status = 'stopped', ended = ? WHERE id = ?", [now(), run_id])
            _batch_record(db, workspace_id, run_id)
        return _pushed(state(db, workspace_id, run_id))
    if row["status"] != "active":
        raise RunError(f"this run is {row['status']}")
    if action == "back":
        tr = _tracker(db, workspace_id, row)
        f = guidance.focus(tr)
        prev = max(0, min(f, len(tr["plan"])) - 1)
        guidance.reopen(tr, prev)                      # a person stepping back reopens the previous step
        guidance.save(db, workspace_id, run_id, tr)
        db.execute("UPDATE runs SET step = ? WHERE id = ?", [guidance.focus(tr), run_id])
        return _pushed(state(db, workspace_id, run_id))
    raise RunError("action must be back or stop")


def deviations_of(s: dict) -> list[dict]:
    """Failures and dangers, skipped and out-of-order steps, and, once a run has ended, steps that were never done."""
    out = [e for e in s["log"] if e["result"] in ("fail", "danger", "skip") or e.get("deviation") == "order"]
    if s["status"] != "active":
        out += [{"step": st["index"], "result": "not done", "say": "This step was never completed.", "time": s["ended"]}
                for st in s["steps"] if st.get("status") not in guidance.DONE]
    return out


def _batch_record(db: Database, workspace_id: str, run_id: str) -> None:
    s = state(db, workspace_id, run_id)
    deviations = deviations_of(s)
    steps = [{"step": st["index"] + 1, "instruction": st["instruction"], "result": st["result"] or "not done",
              "status": st.get("status"), "recovery": bool(st.get("recovery")),
              "reading": st["measured"], "checks": st["checks"], "snapshot": st["snapshot"]} for st in s["steps"]]
    lines = [f"{s['procedure']['name']}{' · ' + s['label'] if s['label'] else ''}: {s['status']}, "
             f"{sum(1 for x in steps if x['result'] == 'pass')}/{len(steps)} steps passed, {len(deviations)} deviations."]
    lines += [f"Step {x['step']}{' (recovery)' if x['recovery'] else ''}: {x['result']}"
              + (f", reading {x['reading']['value']:g} {x['reading']['unit']}" if x["reading"] else "") for x in steps]
    rec = irf.normalize({"type": "batch_record", "source": "runs", "id": run_id,
                         "title": f"Batch record: {s['procedure']['name']}" + (f" ({s['label']})" if s["label"] else ""),
                         "subject": f"run:{run_id}", "text": "\n".join(lines),
                         "time": s["ended"] or now(),
                         "data": {"run_id": run_id, "procedure_id": s["procedure"]["id"], "status": s["status"],
                                  "started": s["started"], "ended": s["ended"], "steps": steps,
                                  "deviations": deviations, "log": s["log"], "recordings": s["recordings"],
                                  "metrics": s["metrics"], "recovery": s["recovery"],
                                  "signatures": s["signatures"], "released": s["released"]},
                         "links": [{"rel": "follows", "to": f"procedure:{s['procedure']['id']}"}]}, now=now())
    store.write(db, workspace_id, [rec])


def roi_for(db: Database, workspace_id: str, camera_id: str, procedure_id: str, step: int) -> dict | None:
    """The region to read for this step: one drawn for this step on this camera, else the camera's default."""
    if not camera_id:
        return None
    for key in (f"{procedure_id}:{step}", "default"):
        row = db.one("SELECT box FROM camera_rois WHERE camera_id = ? AND workspace_id = ? AND key = ?",
                     [camera_id, workspace_id, key])
        if row:
            return loads(row["box"], None)
    return None


_last_seen: dict[tuple[str, int], bytes] = {}       # (run, step) -> ROI thumbnail at the last model check


def refresh_batch_record(db: Database, workspace_id: str, run_id: str) -> None:
    """Rewrite a finished run's batch record (e.g. a recording was attached after the run ended)."""
    if _row(db, workspace_id, run_id)["status"] != "active":
        _batch_record(db, workspace_id, run_id)


def _hands(hands) -> list[dict]:
    out = []
    for h in hands or []:
        try:
            out.append({k: float(h[k]) for k in ("x", "y", "w", "h")})
        except (KeyError, TypeError, ValueError):
            continue
    return out[:4]


async def vision_check(db: Database, workspace: dict, run_id: str, jpeg: bytes, *, gated: bool = False,
                       scene: str = "", step: int | None = None, hands: list | None = None) -> dict:
    """Check a step (the one in focus unless `step` is given) and log it.

    Order of work, cheapest first: a settled step isn't re-checked automatically (critical steps excepted); with
    `gated` (auto-check) an unchanged region costs nothing; the frame's evidence (blur, light, age, hands over the
    region) is judged in code and the check abstains with a concrete reason before any model call; measurement steps
    with a calibrated instrument are read by geometry/OCR; the rest go to the vision model (a reading step: check plus
    verified reading; an action step: yes/no questions over the last few frames, voted when the person asked)."""
    ws_id = workspace["id"]
    s = state(db, ws_id, run_id)
    if s["status"] != "active":
        raise RunError(f"this run is {s['status']}")
    idx = s["step"] if step is None else int(step)
    if not 0 <= idx < len(s["steps"]):
        raise RunError(f"there's no step {idx + 1}")
    st = s["steps"][idx]
    cam = s["camera_id"]
    if gated and st.get("settled") and st.get("severity") != "critical":
        vision.gate_count(ws_id, False)
        return {"check": {"result": "unchanged", "say": "", "reading": None, "skipped": True, "settled": True}, "run": s}
    roi = roi_for(db, ws_id, cam, s["procedure"]["id"], idx) or guidance.bbox_of_zone(st.get("zone"))
    key = (run_id, idx)
    th = perception.roi_thumb(jpeg, roi)
    if gated and key in _last_seen and not vision.changed(_last_seen[key], th):
        vision.gate_count(ws_id, False)
        return {"check": {"result": "unchanged", "say": "", "reading": None, "skipped": True}, "run": s}
    ev = await asyncio.to_thread(guidance.evidence, jpeg, roi=roi, age_s=vision.hub.age(cam) if cam else None,
                                 hands=_hands(hands), expected=st.get("expected_objects"), scene=scene)
    row = _row(db, ws_id, run_id)
    tr = _tracker(db, ws_id, row)
    uid = tr["plan"][idx]["uid"]
    tr["steps"][uid]["evidence"] = {"score": ev["score"], "reasons": ev["reasons"]}
    if ev["abstain"]:
        tr["metrics"]["abstained"] = tr["metrics"].get("abstained", 0) + 1
        guidance.add_say(tr, ev["say"], severity="minor", topic=f"{uid}:evidence", kind="question", step=idx + 1)
        guidance.save(db, ws_id, run_id, tr)
        _last_seen[key] = th
        out = {"result": "unsure", "say": ev["say"], "reading": None, "abstained": True, "evidence": ev}
        return {"check": out, "run": record(db, ws_id, run_id, "unsure", say=ev["say"], step=idx, explicit=not gated,
                                            meta={"abstained": True, "evidence": ev["reasons"]})}
    guidance.save(db, ws_id, run_id, tr)
    snap = vision.save_snapshot(db, ws_id, jpeg, f"run {run_id} step {idx + 1}.jpg")
    if st.get("instrument_id") and st.get("reading"):
        out = await _instrument_check(db, ws_id, run_id, st, jpeg, cam)
        _last_seen[key] = th
    else:
        hits = store.search(db, ws_id, st["instruction"] + " " + st.get("check", ""), limit=3,
                            types=["document", "lesson", "row", "asset"])
        knowledge = "\n".join(f"- ({h['title'] or h['type']}{', p. ' + str(h['page']) if h['page'] else ''}) "
                              f"{h['text'][:400]}" for h in hits)
        models = llm.chain(workspace, "vlm")
        if not models:
            raise RunError("no vision model is set up. Add one in Settings → Models, or mark the step by hand.")
        frames = vision.hub.recent(cam, 4, 8.0) if cam else []
        out = await vision.check_step(models, jpeg, st, idx, len(s["steps"]), knowledge, workspace_id=ws_id, roi=roi,
                                      background=gated, scene=scene, frames=frames, decision=not gated)
        _last_seen[key] = th
    out["snapshot"] = snap
    out["evidence"] = ev
    tr = _tracker(db, ws_id, _row(db, ws_id, run_id))
    if out.get("votes"):
        tr["steps"][uid]["votes"] = out["votes"]
    tr["steps"][uid]["last_check"] = {"result": out["result"], "t": now(), "confidence": out.get("confidence"),
                                      "samples": out.get("samples"), "method": out.get("method") or "vision"}
    guidance.save(db, ws_id, run_id, tr)
    observe.record(db, ws_id, camera_id=cam, subject=f"{s['procedure']['name']} step {idx + 1}",
                   relation="check", value=out["result"], conf=0.8 if out["result"] in ("pass", "fail") else 0.4,
                   evidence=f"file:{snap}", source="run")
    if out.get("reading"):
        spec = st.get("reading") or {}
        observe.record(db, ws_id, camera_id=cam, subject=spec.get("label") or f"{s['procedure']['name']} step {idx + 1}",
                       relation="reading", value=out["reading"]["value"], unit=out["reading"].get("unit", ""),
                       conf=out["reading"].get("confidence", 0.5), evidence=f"file:{snap}", source="run")
    run = record(db, ws_id, run_id, out["result"], say=out["say"], reading=out["reading"], snapshot=snap, step=idx,
                 explicit=not gated, confidence=out.get("confidence"),
                 meta={"model": out.get("model", ""), "where": out.get("where", ""), "ms": out.get("ms"),
                       "votes": out.get("votes"), "samples": out.get("samples")})
    if run.get("recovery") is None and _wants_recovery(db, ws_id, run_id):
        _schedule_recovery(db, workspace, run_id)
    return {"check": out, "run": run}


async def _instrument_check(db: Database, ws_id: str, run_id: str, st: dict, jpeg: bytes, cam: str) -> dict:
    try:
        inst = instruments.get(db, ws_id, st["instrument_id"])
    except store.NotFound:
        return {"result": "unsure", "say": "The linked instrument was deleted. Read it by hand.", "reading": None,
                "method": "instrument"}
    frames = (vision.hub.recent(inst.get("camera_id") or cam, 2, 6.0) if (inst.get("camera_id") or cam) else []) + [jpeg]
    r = await asyncio.to_thread(instruments.read, inst, frames, limits=st.get("reading"))
    tr = _tracker(db, ws_id, _row(db, ws_id, run_id))
    tr.setdefault("instrument_reads", {})[st["uid"]] = {"value": r["value"], "method": r["method"], "t": now()}
    guidance.save(db, ws_id, run_id, tr)
    if cam:
        vision.hub.publish(cam, {"type": "instrument", "instrument_id": inst["id"], "name": inst["name"], **r})
    if r["value"] is None:
        return {"result": "unsure", "say": f"I can't read the {inst['name']}. Point the camera at it.", "reading": None,
                "method": r["method"], "instrument": r}
    reading = {"value": r["value"], "unit": r["unit"] or (st.get("reading") or {}).get("unit", ""),
               "confidence": r["confidence"], "method": r["method"]}
    if r["needs_confirm"]:
        return {"result": "unsure", "say": f"I read {r['value']:g} {reading['unit']}. Please confirm it.".strip(),
                "reading": reading, "method": r["method"], "instrument": r, "needs_confirm": True}
    ok, why = vision.judge_reading(st, reading)
    if ok is False:
        return {"result": "fail", "say": f"Out of spec: {why}.", "reading": reading, "method": r["method"], "instrument": r}
    return {"result": "pass", "say": f"{inst['name']}: {r['value']:g} {reading['unit']}. In range.".strip(),
            "reading": reading, "method": r["method"], "instrument": r, "confidence": r["confidence"]}


# ================================================================ recovery plans
RECOVERY_PROMPT = """You help a technician recover from a mistake during a standard operating procedure.
You get the procedure as a numbered list with each step's status, and what went wrong. Reply as JSON:
{"what": "what happened, one short sentence", "impact": [step numbers that this breaks or puts at risk],
 "why": "why it matters, one sentence", "fix": ["short actions to recover"],
 "insert": [{"instruction": str, "check": "what correct looks like on camera"}]}
Rules: only steps from the list in "impact"; "insert" holds the extra steps needed before redoing the failed step
(empty when simply redoing it is enough); never invent limits; keep every sentence under 15 words."""


def _wants_recovery(db: Database, ws_id: str, run_id: str) -> bool:
    tr = guidance.load(db, ws_id, run_id) or {}
    return bool(tr.get("recovery_needed")) and os.environ.get("IRRIS_AUTO_RECOVER", "1") == "1"


def _schedule_recovery(db: Database, workspace: dict, run_id: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_auto_recover(db, workspace, run_id))


async def _auto_recover(db: Database, workspace: dict, run_id: str) -> None:
    try:
        await propose_recovery(db, workspace, run_id)
    except Exception:  # noqa: BLE001 - a background proposal never breaks the run
        pass


def _dependents(tr: dict, idx: int) -> list[int]:
    """Steps that (transitively) depend on step idx."""
    uid = tr["plan"][idx]["uid"]
    hit, changed = {uid}, True
    while changed:
        changed = False
        for p in tr["plan"]:
            if p["uid"] not in hit and any(a in hit for a in p.get("after", [])):
                hit.add(p["uid"])
                changed = True
    return [i for i, p in enumerate(tr["plan"]) if p["uid"] in hit and p["uid"] != uid]


async def propose_recovery(db: Database, workspace: dict, run_id: str, *, step: int | None = None,
                           error: str = "") -> dict:
    """What happened, what it breaks, why it matters and how to fix it; stored on the run as a proposal."""
    ws_id = workspace["id"]
    row = _row(db, ws_id, run_id)
    tr = _tracker(db, ws_id, row)
    need = tr.get("recovery_needed") or {}
    idx = int(step if step is not None else need.get("step", guidance.focus(tr)))
    if not 0 <= idx < len(tr["plan"]):
        raise RunError(f"there's no step {idx + 1}")
    if not error and not need.get("error"):           # asked again later: use what was logged for that step
        last = next((e for e in reversed(loads(row["log"], [])) if e.get("step") == idx
                     and e.get("result") in ("fail", "danger") and (e.get("say") or e.get("note"))), None)
        error = (last or {}).get("say") or (last or {}).get("note") or ""
    error = error or need.get("error") or "the step went wrong"
    p = tr["plan"][idx]
    deps = _dependents(tr, idx)
    plan = {"what": f"Step {idx + 1} went wrong: {error}"[:200], "impact": deps,
            "why": (f"Step {deps[0] + 1} depends on it." if len(deps) == 1 else
                    f"Steps {', '.join(str(d + 1) for d in deps[:6])} depend on it.") if deps else "",
            "fix": [f"Redo step {idx + 1}: {p['instruction']}"], "insert": [], "step": idx, "status": "proposed",
            "source": "rules", "t": now()}
    models = llm.chain(workspace, "llm")
    if models:
        sop = "\n".join(f"{i + 1}. [{tr['steps'][q['uid']]['status']}] {q['instruction']}"
                        + (f" (check: {q['check']})" if q.get("check") else "") for i, q in enumerate(tr["plan"]))
        user = f"Procedure:\n{sop}\n\nWhat went wrong at step {idx + 1}: {error}"
        try:
            msg = await llm.chat_chain(models, [{"role": "system", "content": RECOVERY_PROMPT},
                                                {"role": "user", "content": user}], json_mode=True, max_tokens=600)
            out = llm.parse_json(msg.get("content") or "")
            impact = []
            for n in out.get("impact") or []:
                try:
                    k = int(n) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= k < len(tr["plan"]) and k != idx and k not in impact:
                    impact.append(k)
            ins = []
            for s in (out.get("insert") or [])[:4]:
                if isinstance(s, dict) and str(s.get("instruction") or "").strip():
                    ins.append({"instruction": str(s["instruction"]).strip()[:300],
                                **({"check": str(s["check"]).strip()[:300]} if s.get("check") else {})})
            plan.update(what=str(out.get("what") or plan["what"])[:200], impact=sorted(impact or deps),
                        why=str(out.get("why") or plan["why"])[:240],
                        fix=[str(f)[:200] for f in (out.get("fix") or plan["fix"])][:5], insert=ins, source="model")
        except llm.ModelError:
            pass
    tr = _tracker(db, ws_id, _row(db, ws_id, run_id))
    tr["recovery"] = plan
    tr.pop("recovery_needed", None)
    guidance.add_say(tr, f"{plan['what']} {plan['fix'][0] if plan['fix'] else ''}", severity=p.get("severity", "major"),
                     topic=f"{p['uid']}:recovery", kind="warning", step=idx + 1, words=20)
    guidance.save(db, ws_id, run_id, tr)
    s = state(db, ws_id, run_id)
    if row["camera_id"]:
        vision.hub.publish(row["camera_id"], {"type": "run", "run": push_view(s), "say": s["say"]})
    return s


def accept_recovery(db: Database, workspace_id: str, run_id: str, accept: bool = True, *, actor: str = "") -> dict:
    row = _row(db, workspace_id, run_id)
    if row["status"] != "active":
        raise RunError(f"this run is {row['status']}")
    tr = _tracker(db, workspace_id, row)
    plan = tr.get("recovery")
    if not plan or plan.get("status") != "proposed":
        raise RunError("there's no recovery plan to accept")
    if accept:
        inserted = guidance.insert_steps(tr, plan["step"], plan.get("insert") or []) if plan.get("insert") else []
        if not inserted:
            guidance.reopen(tr, plan["step"])               # redoing the step is the fix
        plan.update(status="accepted", inserted=inserted)
    else:
        plan["status"] = "dismissed"
    ops.audit(db, workspace_id, actor or "unknown", f"recovery_plan.{plan['status']}", "recovery_plan", run_id,
              after={k: plan.get(k) for k in ("step", "what", "why", "fix", "insert")}, detail=str(plan.get("what") or ""))
    guidance.save(db, workspace_id, run_id, tr)
    db.execute("UPDATE runs SET step = ? WHERE id = ?", [guidance.focus(tr), run_id])
    s = state(db, workspace_id, run_id)
    if row["camera_id"]:
        vision.hub.publish(row["camera_id"], {"type": "run", "run": push_view(s), "say": s["say"]})
    return s
