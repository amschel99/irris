"""Procedures that learn from runs, and checks drafted by the reasoning model (docs/GUIDANCE_SPEC.md).

learn(): from a procedure's finished runs -
- prereqs: an edge i -> j when step i finished before step j in at least 95% of the runs that contain both (and at
  least two such runs), reduced to direct edges;
- median_s / std_s: how long each step takes after the previous step finished;
- prototype: what "done" looks like, as the mean embedding of the vision model's captions of the frames that passed
  the step, with the 85th percentile of the samples' cosine distances to it (a cheap first screen before the model).

draft_checks(): 2-4 atomic yes/no questions per step about visible, concrete properties, plus typical errors, kind,
severity and reversibility.

Both return proposals; the author reviews them and saves through the normal procedure update.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from irris import files, guidance, llm, store, vision
from irris.db import Database, loads

PRECEDENCE = 0.95
MAX_PROTOTYPE_RUNS = 3


def _completions(db: Database, workspace_id: str, run: dict, n: int) -> dict[int, float]:
    """Original step index -> when it finished, for one run."""
    out: dict[int, float] = {}
    tr = guidance.load(db, workspace_id, run["id"])
    if tr:
        for p in tr["plan"]:
            uid = p["uid"]
            if uid.startswith("s") and uid[1:].isdigit():
                i = int(uid[1:])
                s = tr["steps"].get(uid) or {}
                if s.get("status") == "complete" and s.get("completed_at") and i < n:
                    out[i] = float(s["completed_at"])
    if not out:
        for e in loads(run["log"], []):
            i = e.get("step", -1)
            if e.get("result") == "pass" and 0 <= i < n and i not in out:
                out[i] = float(e.get("time") or 0)
    return out


def _pass_snapshots(run: dict, n: int) -> dict[int, str]:
    snaps: dict[int, str] = {}
    for e in loads(run["log"], []):
        i = e.get("step", -1)
        if e.get("result") == "pass" and e.get("snapshot") and 0 <= i < n and e.get("by") == "vision":
            snaps[i] = e["snapshot"]
        elif e.get("result") == "pass" and e.get("snapshot") and 0 <= i < n:
            snaps.setdefault(i, e["snapshot"])
    return snaps


def precedence(orders: list[dict[int, float]], n: int) -> list[list[int]]:
    """prereqs[j]: the steps that finished before j in >= 95% of runs containing both, reduced to direct edges."""
    edge = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            both = [o for o in orders if i in o and j in o]
            if len(both) >= 2 and sum(1 for o in both if o[i] < o[j]) / len(both) >= PRECEDENCE:
                edge[i][j] = True
    pre = []
    for j in range(n):
        direct = [i for i in range(n) if edge[i][j]]
        direct = [i for i in direct if not any(edge[i][k] and edge[k][j] for k in direct if k != i)]
        pre.append(sorted(direct))
    return pre


def durations(runs: list[dict], orders: list[dict[int, float]], n: int) -> list[tuple[float | None, float | None]]:
    per: list[list[float]] = [[] for _ in range(n)]
    for run, o in zip(runs, orders):
        seq = sorted(o.items(), key=lambda kv: kv[1])
        prev = float(run["started"])
        for i, t in seq:
            if t >= prev:
                per[i].append(t - prev)
            prev = t
    out = []
    for d in per:
        if not d:
            out.append((None, None))
        else:
            out.append((round(statistics.median(d), 1), round(statistics.pstdev(d), 1) if len(d) > 1 else 0.0))
    return out


def _cos_dist(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return 1.0 - dot / (na * nb)


def _p85(values: list[float]) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = 0.85 * (len(v) - 1)
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 4)


async def _prototypes(db: Database, workspace: dict, runs: list[dict], n: int) -> tuple[list[dict | None], list[str]]:
    notes: list[str] = []
    out: list[dict | None] = [None] * n
    from irris import embed
    emb = embed.get()
    models = llm.chain(workspace, "vlm")
    if emb is None or not models:
        notes.append("Prototypes skipped: needs " + ("the search embedder" if emb is None else "a vision model") + ".")
        return out, notes
    texts: list[list[str]] = [[] for _ in range(n)]
    for run in runs[:MAX_PROTOTYPE_RUNS]:
        for i, fid in _pass_snapshots(run, n).items():
            data = files.read_bytes(db, workspace["id"], fid)
            if not data:
                continue
            try:
                cap = await vision.caption(models, data, workspace_id=workspace["id"])
            except llm.ModelError:
                notes.append("Some captions failed; prototypes may be thin.")
                continue
            t = vision.caption_text(cap)
            if t.strip():
                texts[i].append(t)
    for i, ts in enumerate(texts):
        if len(ts) < 2:
            continue
        vecs = [list(map(float, v)) for v in emb.passages(ts)]
        dim = len(vecs[0])
        centre = [sum(v[d] for v in vecs) / len(vecs) for d in range(dim)]
        dists = [_cos_dist(v, centre) for v in vecs]
        out[i] = {"vector": [round(x, 5) for x in centre], "p85": _p85(dists), "n": len(vecs)}
    return out, notes


async def learn(db: Database, workspace: dict, procedure_id: str) -> dict:
    proc = store.procedure(db, workspace["id"], procedure_id)
    n = len(proc["steps"])
    runs = db.execute("SELECT id, status, started, ended, log FROM runs WHERE workspace_id = ? AND procedure_id = ? "
                      "AND status IN ('done', 'stopped') ORDER BY started DESC LIMIT 200", [workspace["id"], procedure_id])
    orders = [_completions(db, workspace["id"], r, n) for r in runs]
    keep = [(r, o) for r, o in zip(runs, orders) if len(o) >= 2]
    notes = []
    if len(keep) < 2:
        notes.append("Needs at least two finished runs with two or more completed steps; nothing learned yet.")
        return {"procedure_id": procedure_id, "runs": len(keep), "steps": proc["steps"], "changes": [], "notes": notes}
    runs_k, orders_k = [r for r, _ in keep], [o for _, o in keep]
    pre = precedence(orders_k, n)
    dur = durations(runs_k, orders_k, n)
    protos, pnotes = await _prototypes(db, workspace, runs_k, n)
    notes += pnotes
    steps, changes = [], []
    for i, s in enumerate(proc["steps"]):
        new = dict(s)
        ch: dict[str, Any] = {}
        if pre[i] != s.get("prereqs", [i - 1] if i > 0 else []):
            ch["prereqs"] = {"from": s.get("prereqs"), "to": pre[i]}
        new["prereqs"] = pre[i]                      # the learned order is always explicit in the proposal
        med, sd = dur[i]
        if med is not None:
            ch["median_s"] = {"from": s.get("median_s"), "to": med}
            new["median_s"], new["std_s"] = med, sd
        if protos[i]:
            ch["prototype"] = {"from": bool(s.get("prototype")), "to": f"{protos[i]['n']} samples, p85 {protos[i]['p85']}"}
            new["prototype"] = protos[i]
        steps.append(new)
        if ch:
            changes.append({"index": i, "instruction": s["instruction"], **ch})
    return {"procedure_id": procedure_id, "runs": len(keep), "steps": steps, "changes": changes, "notes": notes}


DRAFT_PROMPT = """You write camera checks for the steps of a standard operating procedure. For each step give:
- "questions": 2-4 yes/no questions a camera can answer, each about ONE concrete, visible property (an object present
  or absent, a position, a colour, a label, a count, a level against a mark, a display showing a value). Never ask about
  invisible or abstract states ("is it sealed properly?"); ask what shows it ("is the cap screwed down flush?"). Don't
  use near-synonym states. Mark "must": true for questions that must be yes for the step to count.
- "typical_errors": 1-3 mistakes people commonly make at this step.
- "kind": "action" | "measurement" | "timing" | "order".
- "severity": "critical" (injury, scrap or an irreversible error) | "major" | "minor".
- "reversible": false when the step can't be undone.
Reply as JSON: {"steps": [{"index": 1, "questions": [{"q": str, "must": bool}], "typical_errors": [str],
"kind": str, "severity": str, "reversible": bool}]}"""


async def draft_checks(db: Database, workspace: dict, procedure_id: str) -> dict:
    proc = store.procedure(db, workspace["id"], procedure_id)
    models = llm.chain(workspace, "llm")
    if not models:
        raise llm.ModelError("drafting checks needs a reasoning model (Settings → Models)")
    lines = []
    for i, s in enumerate(proc["steps"]):
        bits = [f"{i + 1}. {s['instruction']}"]
        if s.get("check"):
            bits.append(f"(correct looks like: {s['check']})")
        if s.get("reading"):
            r = s["reading"]
            bits.append(f"(reading {r.get('label', '')} {r.get('min', '')}-{r.get('max', '')} {r.get('unit', '')})")
        if s.get("caution"):
            bits.append(f"(caution: {s['caution']})")
        lines.append(" ".join(bits))
    user = f"Procedure: {proc['name']}\n" + (proc.get("description") or "") + "\n\n" + "\n".join(lines)
    msg = await llm.chat_chain(models, [{"role": "system", "content": DRAFT_PROMPT}, {"role": "user", "content": user}],
                               json_mode=True, max_tokens=2500)
    out = llm.parse_json(msg.get("content") or "")
    by_index: dict[int, dict] = {}
    for k, d in enumerate(out.get("steps") or []):
        if not isinstance(d, dict):
            continue
        try:
            i = int(d.get("index", k + 1)) - 1
        except (TypeError, ValueError):
            i = k
        if 0 <= i < len(proc["steps"]):
            by_index[i] = d
    steps, changes = [], []
    for i, s in enumerate(proc["steps"]):
        d = by_index.get(i)
        if not d:
            steps.append(s)
            continue
        drafted = store.clean_steps([{**s, **{k: d[k] for k in ("questions", "typical_errors", "kind", "severity",
                                                                  "reversible") if k in d}}])[0]
        merged = {**s, **{k: v for k, v in drafted.items() if k in ("questions", "typical_errors", "kind", "severity",
                                                                    "reversible")}}
        if merged.get("reversible") is False and "intervention" not in s:
            merged["intervention"] = "remind_before"
        steps.append(merged)
        changes.append({"index": i, "instruction": s["instruction"],
                        **{k: {"from": s.get(k), "to": merged.get(k)} for k in
                           ("questions", "typical_errors", "kind", "severity", "reversible") if s.get(k) != merged.get(k)}})
    return {"procedure_id": procedure_id, "steps": steps, "changes": changes,
            "notes": [] if by_index else ["The model returned no usable checks; try again."]}
