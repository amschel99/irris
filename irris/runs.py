"""Guided runs: a person works through a procedure while Irris checks each step through a camera.

Every check is logged. Readings become `reading` records. When the last step passes, the run writes a
`batch_record` record: every step, who or what checked it, the result, the readings and the times.
"""
from __future__ import annotations

from irris import irf, llm, observe, perception, store, vision
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
                      "snapshot": next((e["snapshot"] for e in reversed(checks) if e.get("snapshot")), None),
                      "checks": len(checks)})
    clips = db.execute("SELECT id, data, created FROM records WHERE workspace_id = ? AND type = 'recording' AND source = ? "
                       "ORDER BY created", [workspace_id, f"runs/{run_id}/recordings"])
    recordings = [{"record_id": r["id"], "created": r["created"],
                   **{k: v for k, v in loads(r["data"], {}).items() if k in ("file_id", "mime", "size", "step")}}
                  for r in clips]
    return {"id": row["id"], "status": row["status"], "step": row["step"], "label": row["label"],
            "recordings": recordings,
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
                    "deviations": sum(1 for e in log if e["result"] in ("fail", "danger", "skip"))})
    return out


def record(db: Database, workspace_id: str, run_id: str, result: str, *, say: str = "", reading: dict | None = None,
           by: str = "vision", note: str = "", snapshot: str | None = None, meta: dict | None = None) -> dict:
    """Log one check of the current step. A pass (or a skip) moves to the next step.

    Every check keeps evidence: the frame that was judged (vision checks), or, for a check marked by hand while a
    camera is live, the frame at that moment."""
    if result not in ("pass", "fail", "unsure", "danger", "skip"):
        raise RunError("result must be pass, fail, unsure, danger or skip")
    row = _row(db, workspace_id, run_id)
    if row["status"] != "active":
        raise RunError(f"this run is {row['status']}")
    proc = store.procedure(db, workspace_id, row["procedure_id"])
    total = len(proc["steps"])
    step = row["step"]
    if snapshot is None and row["camera_id"]:
        frame = vision.hub.get(row["camera_id"])
        if frame:
            snapshot = vision.save_snapshot(db, workspace_id, frame, f"run {run_id} step {step + 1}.jpg")
    entry = {"step": step, "result": result, "say": say, "reading": reading, "by": by, "note": note, "time": now(),
             "snapshot": snapshot, **(meta or {})}
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
    out = state(db, workspace_id, run_id)
    if row["camera_id"]:
        vision.hub.publish(row["camera_id"], {"type": "verdict", "run_id": run_id, "step": step + 1,
                                              "result": result, "say": say, "reading": reading,
                                              "next": out["steps"][out["step"]]["instruction"]
                                              if out["status"] == "active" and out["step"] != step else ""})
    return out


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


def deviations_of(s: dict) -> list[dict]:
    """Failures and dangers, steps skipped, and, once a run has ended, steps that were never done."""
    out = [e for e in s["log"] if e["result"] in ("fail", "danger", "skip")]
    if s["status"] != "active":
        done = {e["step"] for e in s["log"] if e["result"] in ("pass", "skip")}
        out += [{"step": st["index"], "result": "not done", "say": "This step was never completed.", "time": s["ended"]}
                for st in s["steps"] if st["index"] not in done]
    return out


def _batch_record(db: Database, workspace_id: str, run_id: str) -> None:
    s = state(db, workspace_id, run_id)
    deviations = deviations_of(s)
    steps = [{"step": st["index"] + 1, "instruction": st["instruction"], "result": st["result"] or "not done",
              "reading": st["measured"], "checks": st["checks"], "snapshot": st["snapshot"]} for st in s["steps"]]
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
                                  "deviations": deviations, "log": s["log"], "recordings": s["recordings"]},
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


async def vision_check(db: Database, workspace: dict, run_id: str, jpeg: bytes, *, gated: bool = False) -> dict:
    """Ask the vision model whether the current step is done, then log it.

    Verify before advancing: with `gated` (auto-check), a cheap change score on the step's region proposes "something
    changed, the step may be done"; only then is the model asked, and only its confirmation (or the person) moves the
    run on. An unchanged scene costs nothing."""
    ws_id = workspace["id"]
    s = state(db, ws_id, run_id)
    if s["status"] != "active":
        raise RunError(f"this run is {s['status']}")
    step = s["steps"][s["step"]]
    roi = roi_for(db, ws_id, s["camera_id"], s["procedure"]["id"], s["step"])
    key = (run_id, s["step"])
    th = perception.roi_thumb(jpeg, roi)
    if gated and key in _last_seen and not vision.changed(_last_seen[key], th):
        vision.gate_count(ws_id, False)
        return {"check": {"result": "unchanged", "say": "", "reading": None, "skipped": True}, "run": s}
    hits = store.search(db, ws_id, step["instruction"] + " " + step.get("check", ""), limit=3,
                        types=["document", "lesson", "row", "asset"])
    knowledge = "\n".join(f"- ({h['title'] or h['type']}{', p. ' + str(h['page']) if h['page'] else ''}) "
                          f"{h['text'][:400]}" for h in hits)
    models = llm.chain(workspace, "vlm")
    if not models:
        raise RunError("no vision model is set up. Add one in Settings → Models, or mark the step by hand.")
    out = await vision.check_step(models, jpeg, step, s["step"], len(s["steps"]), knowledge, workspace_id=ws_id, roi=roi)
    _last_seen[key] = th
    snap = vision.save_snapshot(db, ws_id, jpeg, f"run {run_id} step {s['step'] + 1}.jpg")
    out["snapshot"] = snap
    cam = s["camera_id"]
    observe.record(db, ws_id, camera_id=cam, subject=f"{s['procedure']['name']} step {s['step'] + 1}",
                   relation="check", value=out["result"], conf=0.8 if out["result"] in ("pass", "fail") else 0.4,
                   evidence=f"file:{snap}", source="run")
    if out.get("reading"):
        spec = step.get("reading") or {}
        observe.record(db, ws_id, camera_id=cam, subject=spec.get("label") or f"{s['procedure']['name']} step {s['step'] + 1}",
                       relation="reading", value=out["reading"]["value"], unit=out["reading"].get("unit", ""),
                       conf=out["reading"].get("confidence", 0.5), evidence=f"file:{snap}", source="run")
    return {"check": out, "run": record(db, ws_id, run_id, out["result"], say=out["say"], reading=out["reading"],
                                        snapshot=snap, meta={"model": out.get("model", ""), "where": out.get("where", ""),
                                                             "ms": out.get("ms")})}
