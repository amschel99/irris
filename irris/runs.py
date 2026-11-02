"""Guided runs: a person works through a procedure while Irris checks each step through a camera.

Every check is logged. Readings become `reading` records. When the last step passes, the run writes a
`batch_record` record: every step, who or what checked it, the result, the readings and the times.
"""
from __future__ import annotations

from irris import irf, llm, store, vision
from irris.db import Database, dumps, loads, new_id, now


class RunError(Exception):
    pass


def _row(db: Database, workspace_id: str, run_id: str) -> dict:
    row = db.one("SELECT * FROM runs WHERE id = ? AND workspace_id = ?", [run_id, workspace_id])
    if not row:
        raise store.NotFound("run not found")
    return row


def state(db: Database, workspace_id: str, run_id: str) -> dict:
    row = _row(db, workspace_id, run_id)
    try:
        proc = store.procedure(db, workspace_id, row["procedure_id"])
    except store.NotFound:
        proc = {"id": row["procedure_id"], "name": "(deleted procedure)", "steps": []}
    log = loads(row["log"], [])
    steps = []
    for i, s in enumerate(proc["steps"]):
        checks = [e for e in log if e["step"] == i]
        last = checks[-1] if checks else None
        steps.append({**s, "index": i, "result": last["result"] if last else None,
                      "say": last["say"] if last else "", "measured": last.get("reading") if last else None,
                      "checks": len(checks)})
    return {"id": row["id"], "status": row["status"], "step": row["step"], "label": row["label"],
            "procedure": {"id": proc["id"], "name": proc["name"]}, "camera_id": row["camera_id"],
            "started": row["started"], "ended": row["ended"], "steps": steps, "log": log[-50:]}


def start(db: Database, workspace_id: str, procedure_id: str, *, camera_id: str = "", user_id: str = "",
          label: str = "") -> dict:
    proc = store.procedure(db, workspace_id, procedure_id)
    if not proc["steps"]:
        raise RunError("that procedure has no steps")
    run_id = new_id()
    db.execute("INSERT INTO runs (id, workspace_id, procedure_id, camera_id, user_id, status, step, log, label, started) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               [run_id, workspace_id, procedure_id, camera_id, user_id, "active", 0, "[]", label[:120], now()])
    return state(db, workspace_id, run_id)


def list_runs(db: Database, workspace_id: str, limit: int = 50) -> list[dict]:
    rows = db.execute("SELECT r.*, p.title AS procedure_name FROM runs r LEFT JOIN records p ON p.id = r.procedure_id "
                      "WHERE r.workspace_id = ? ORDER BY r.started DESC LIMIT ?", [workspace_id, limit])
    out = []
    for r in rows:
        log = loads(r["log"], [])
        out.append({"id": r["id"], "status": r["status"], "step": r["step"], "label": r["label"],
                    "procedure": {"id": r["procedure_id"], "name": r["procedure_name"] or "(deleted procedure)"},
                    "started": r["started"], "ended": r["ended"], "checks": len(log),
                    "deviations": sum(1 for e in log if e["result"] in ("fail", "danger"))})
    return out


def record(db: Database, workspace_id: str, run_id: str, result: str, *, say: str = "", reading: dict | None = None,
           by: str = "vision", note: str = "") -> dict:
    """Log one check of the current step. A pass (or a skip) moves to the next step."""
    if result not in ("pass", "fail", "unsure", "danger", "skip"):
        raise RunError("result must be pass, fail, unsure, danger or skip")
    row = _row(db, workspace_id, run_id)
    if row["status"] != "active":
        raise RunError(f"this run is {row['status']}")
    proc = store.procedure(db, workspace_id, row["procedure_id"])
    total = len(proc["steps"])
    step = row["step"]
    entry = {"step": step, "result": result, "say": say, "reading": reading, "by": by, "note": note, "time": now()}
    log = loads(row["log"], []) + [entry]
    if reading:
        spec = proc["steps"][step].get("reading") or {}
        rec = irf.normalize({"type": "reading", "source": f"runs/{run_id}",
                             "subject": proc["subject"] or f"procedure:{proc['name']}",
                             "title": spec.get("label") or f"{proc['name']}, step {step + 1}",
                             "data": {**reading, "run_id": run_id, "step": step + 1, "result": result,
                                      **({"min": spec["min"]} if "min" in spec else {}),
                                      **({"max": spec["max"]} if "max" in spec else {})},
                             "units": {"value": reading.get("unit", "")}}, now=now())
        store.write(db, workspace_id, [rec])
    next_step, status, ended = step, "active", None
    if result in ("pass", "skip"):
        next_step = step + 1
        if next_step >= total:
            status, ended = "done", now()
    db.execute("UPDATE runs SET log = ?, step = ?, status = ?, ended = ? WHERE id = ?",
               [dumps(log), min(next_step, total - 1) if status == "active" else total, status, ended, run_id])
    if status == "done":
        _batch_record(db, workspace_id, run_id)
    return state(db, workspace_id, run_id)


def move(db: Database, workspace_id: str, run_id: str, action: str) -> dict:
    row = _row(db, workspace_id, run_id)
    if action == "stop":
        if row["status"] == "active":
            db.execute("UPDATE runs SET status = 'stopped', ended = ? WHERE id = ?", [now(), run_id])
            _batch_record(db, workspace_id, run_id)
        return state(db, workspace_id, run_id)
    if row["status"] != "active":
        raise RunError(f"this run is {row['status']}")
    if action == "back":
        db.execute("UPDATE runs SET step = ? WHERE id = ?", [max(0, row["step"] - 1), run_id])
        return state(db, workspace_id, run_id)
    raise RunError("action must be back or stop")


def _batch_record(db: Database, workspace_id: str, run_id: str) -> None:
    s = state(db, workspace_id, run_id)
    deviations = [e for e in s["log"] if e["result"] in ("fail", "danger")]
    steps = [{"step": st["index"] + 1, "instruction": st["instruction"], "result": st["result"] or "not done",
              "reading": st["measured"], "checks": st["checks"]} for st in s["steps"]]
    lines = [f"{s['procedure']['name']}{' · ' + s['label'] if s['label'] else ''}: {s['status']}, "
             f"{sum(1 for x in steps if x['result'] == 'pass')}/{len(steps)} steps passed, {len(deviations)} deviations."]
    lines += [f"Step {x['step']}: {x['result']}" + (f", reading {x['reading']['value']:g} {x['reading']['unit']}"
                                                    if x["reading"] else "") for x in steps]
    rec = irf.normalize({"type": "batch_record", "source": "runs", "id": run_id,
                         "title": f"Batch record: {s['procedure']['name']}" + (f" ({s['label']})" if s["label"] else ""),
                         "subject": f"run:{run_id}", "text": "\n".join(lines),
                         "time": s["ended"] or now(),
                         "data": {"run_id": run_id, "procedure_id": s["procedure"]["id"], "status": s["status"],
                                  "started": s["started"], "ended": s["ended"], "steps": steps,
                                  "deviations": deviations, "log": s["log"]},
                         "links": [{"rel": "follows", "to": f"procedure:{s['procedure']['id']}"}]}, now=now())
    store.write(db, workspace_id, [rec])


async def vision_check(db: Database, workspace: dict, run_id: str, jpeg: bytes) -> dict:
    """Ask the vision model whether the current step is done, then log it."""
    ws_id = workspace["id"]
    s = state(db, ws_id, run_id)
    if s["status"] != "active":
        raise RunError(f"this run is {s['status']}")
    step = s["steps"][s["step"]]
    hits = store.search(db, ws_id, step["instruction"] + " " + step.get("check", ""), limit=3,
                        types=["document", "lesson", "row", "asset"])
    knowledge = "\n".join(f"- ({h['title'] or h['type']}{', p. ' + str(h['page']) if h['page'] else ''}) "
                          f"{h['text'][:400]}" for h in hits)
    models = llm.chain(workspace, "vlm")
    if not models:
        raise RunError("no vision model is set up. Add one in Settings → Models, or mark the step by hand.")
    out = await vision.check_step(models, jpeg, step, s["step"], len(s["steps"]), knowledge)
    return {"check": out, "run": record(db, ws_id, run_id, out["result"], say=out["say"], reading=out["reading"])}
