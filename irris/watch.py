"""Cameras that keep watching: watch rules, coach mode, notice mode, alerts, and review jobs.

- Watch rules: "alert me if anyone reaches into the press without gloves", checked every N seconds per camera. A
  rule only spends a vision-model call when the scene changed (a cheap thumbnail diff), and never queues behind a
  person while vision runs on the CPU fallback. A rule fires when its condition becomes true (then after a
  cooldown), writes an `event` record with a snapshot, and alerts every Live screen and the camera itself.
- Coach mode (per camera, with a goal): look every N seconds, speak only for mistakes, safety, finished steps.
- Notice mode (per camera): a question written on paper or a screen held up to the camera gets answered (needs OCR).
- Review jobs: "what happened in the last 2 minutes?" and uploaded video reviews run in the background; the result
  is a `video_review` record.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

from irris import irf, llm, observe, perception, store, vision
from irris.db import Database, dumps, get_db, loads, new_id, now

log = logging.getLogger("irris.watch")

TICK = 2.0
CHANGE_MIN = 0.025             # thumbnail difference that counts as "the scene changed"
STALE_RECHECK = 6              # re-check an unchanged scene after this many intervals anyway
SEVERITIES = ("info", "warning", "danger")
MIN_INTERVAL = 5


# ================================================================ alerts
class Alerts:
    def __init__(self):
        self._recent: dict[str, deque] = {}
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def push(self, workspace_id: str, alert: dict) -> dict:
        alert = {"id": new_id(), "time": time.time(), **alert}
        self._recent.setdefault(workspace_id, deque(maxlen=100)).append(alert)
        for q in list(self._subs.get(workspace_id, ())):
            try:
                q.put_nowait(alert)
            except asyncio.QueueFull:
                pass
        if alert.get("camera_id"):
            vision.hub.publish(alert["camera_id"], {"type": "alert", **alert})
        return alert

    def since(self, workspace_id: str, t: float) -> list[dict]:
        return [a for a in self._recent.get(workspace_id, ()) if a["time"] > t]

    def subscribe(self, workspace_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subs.setdefault(workspace_id, set()).add(q)
        return q

    def unsubscribe(self, workspace_id: str, q: asyncio.Queue) -> None:
        self._subs.get(workspace_id, set()).discard(q)


alerts = Alerts()


# ================================================================ watch rules
def rule_api(r: dict) -> dict:
    return {"id": r["id"], "camera_id": r["camera_id"], "condition": r["condition"], "interval_s": r["interval_s"],
            "severity": r["severity"], "speak": bool(r["speak"]), "enabled": bool(r["enabled"]),
            "cooldown_s": r["cooldown_s"], "last_check": r["last_check"], "last_match": r["last_match"],
            "active": bool(r["last_state"]), "last_say": r["last_say"], "checks": r["checks"], "matches": r["matches"],
            "created": r["created"]}


def list_rules(db: Database, workspace_id: str, camera_id: str = "") -> list[dict]:
    sql, params = "SELECT * FROM watch_rules WHERE workspace_id = ?", [workspace_id]
    if camera_id:
        sql += " AND camera_id = ?"
        params.append(camera_id)
    return [rule_api(r) for r in db.execute(sql + " ORDER BY created", params)]


def _clean(body: dict, partial: bool = False) -> dict:
    out: dict[str, Any] = {}
    if "condition" in body or not partial:
        cond = str(body.get("condition") or "").strip()
        if len(cond) < 4:
            raise ValueError("describe what to watch for, in plain words")
        out["condition"] = cond[:400]
    if "interval_s" in body or not partial:
        out["interval_s"] = max(MIN_INTERVAL, min(int(body.get("interval_s") or 30), 3600))
    if "severity" in body or not partial:
        sev = body.get("severity") or "warning"
        if sev not in SEVERITIES:
            raise ValueError("severity must be info, warning or danger")
        out["severity"] = sev
    if "cooldown_s" in body or not partial:
        out["cooldown_s"] = max(0, min(int(body.get("cooldown_s") if body.get("cooldown_s") is not None else 300), 86400))
    for k in ("speak", "enabled"):
        if k in body or not partial:
            out[k] = 1 if body.get(k, k == "enabled") else 0
    return out


def add_rule(db: Database, workspace_id: str, camera_id: str, body: dict) -> dict:
    if not db.one("SELECT id FROM cameras WHERE id = ? AND workspace_id = ?", [camera_id, workspace_id]):
        raise LookupError("camera not found")
    v = _clean(body)
    rid = new_id()
    db.execute("INSERT INTO watch_rules (id, workspace_id, camera_id, condition, interval_s, severity, speak, enabled, "
               "cooldown_s, created) VALUES (?,?,?,?,?,?,?,?,?,?)",
               [rid, workspace_id, camera_id, v["condition"], v["interval_s"], v["severity"], v["speak"], v["enabled"],
                v["cooldown_s"], now()])
    return rule_api(db.one("SELECT * FROM watch_rules WHERE id = ?", [rid]))


def update_rule(db: Database, workspace_id: str, rule_id: str, body: dict) -> dict:
    if not db.one("SELECT id FROM watch_rules WHERE id = ? AND workspace_id = ?", [rule_id, workspace_id]):
        raise LookupError("rule not found")
    v = _clean(body, partial=True)
    for k, val in v.items():
        db.execute(f"UPDATE watch_rules SET {k} = ? WHERE id = ?", [val, rule_id])
    return rule_api(db.one("SELECT * FROM watch_rules WHERE id = ?", [rule_id]))


def delete_rule(db: Database, workspace_id: str, rule_id: str) -> None:
    db.execute("DELETE FROM watch_rules WHERE id = ? AND workspace_id = ?", [rule_id, workspace_id])


_rule_thumbs: dict[str, bytes] = {}


async def frame_for(db: Database, cam: dict) -> bytes | None:
    if cam["kind"] == "url":
        from irris.security import decrypt
        try:
            frame = await vision.fetch_external(decrypt(cam["url"]))
        except Exception:  # noqa: BLE001
            return None
        vision.hub.put(cam["id"], frame)
        return frame
    return vision.hub.get(cam["id"])


async def check_rule(db: Database, rule: dict, *, force: bool = False) -> dict | None:
    """Check one rule now. Returns the check, or None when skipped (no frame, unchanged scene, CPU busy)."""
    cam = db.one("SELECT * FROM cameras WHERE id = ? AND workspace_id = ?", [rule["camera_id"], rule["workspace_id"]])
    ws = db.one("SELECT * FROM workspaces WHERE id = ?", [rule["workspace_id"]])
    if not cam or not ws:
        return None
    models = llm.chain(ws, "vlm")
    if not models:
        return None
    if not force and vision.busy_cpu() and all(vision.where(m) == "cpu" for m in models):
        return None
    frame = await frame_for(db, cam)
    t = now()
    if frame is None:
        return None
    th = perception.thumb(frame)
    prev = _rule_thumbs.get(rule["id"])
    stale = rule["last_check"] is None or t - rule["last_check"] >= rule["interval_s"] * STALE_RECHECK
    if not force and prev and th and perception.change(prev, th) < CHANGE_MIN and not stale:
        db.execute("UPDATE watch_rules SET last_check = ? WHERE id = ?", [t, rule["id"]])
        vision.gate_count(ws["id"], False)
        return None
    try:
        out = await vision.watch_check(models, frame, rule["condition"], workspace_id=ws["id"], background=not force)
    except vision.VisionBusy:
        return None
    if th:
        _rule_thumbs[rule["id"]] = th
    matched = out["match"] and out["confidence"] >= 0.5
    if matched and out["confidence"] < 0.75:
        # never alert on one uncertain look: confirm on a sharper image first
        try:
            again = await vision.watch_check(models, vision.shrink(frame, 1024), rule["condition"], workspace_id=ws["id"],
                                             background=not force)
        except vision.VisionBusy:
            return None
        matched = again["match"] and again["confidence"] >= 0.5
        out = {**again, "confirmed": True} if matched else {**out, "say": again["say"] or out["say"], "unconfirmed": True}
    observe.record(db, ws["id"], camera_id=cam["id"], subject=rule["condition"], relation="is true",
                   value="yes" if matched else "no", conf=out["confidence"], source="watch", t=t)
    fire = matched and (not rule["last_state"] or rule["last_match"] is None or
                        t - rule["last_match"] >= rule["cooldown_s"])
    db.execute("UPDATE watch_rules SET last_check = ?, last_state = ?, last_say = ?, checks = checks + 1, "
               "matches = matches + ? WHERE id = ?", [t, 1 if matched else 0, out["say"], 1 if matched else 0, rule["id"]])
    if fire:
        snap = vision.save_snapshot(db, ws["id"], frame, f"{cam['name']} alert.jpg")
        db.execute("UPDATE watch_rules SET last_match = ? WHERE id = ?", [t, rule["id"]])
        rec = irf.normalize({"type": "event", "source": f"watch/{cam['name']}", "subject": f"camera:{cam['name']}",
                             "title": rule["condition"], "text": out["say"],
                             "data": {"rule_id": rule["id"], "severity": rule["severity"], "camera_id": cam["id"],
                                      "snapshot": snap, "confidence": out["confidence"], "model": out["model"]}},
                            now=t)
        store.write(db, ws["id"], [rec])
        alerts.push(ws["id"], {"kind": "watch", "camera_id": cam["id"], "camera": cam["name"], "rule_id": rule["id"],
                               "severity": rule["severity"], "title": rule["condition"], "say": out["say"],
                               "speak": bool(rule["speak"]), "snapshot": snap})
    return {**out, "fired": fire, "matched": matched}


# ================================================================ coach and notice (per camera)
def camera_state(db: Database, workspace_id: str, camera_id: str) -> dict:
    row = db.one("SELECT * FROM camera_state WHERE camera_id = ? AND workspace_id = ?", [camera_id, workspace_id])
    return {"goal": row["goal"] if row else "", "coach_s": row["coach_s"] if row else 0,
            "notice": bool(row["notice"]) if row else False}


def set_camera_state(db: Database, workspace_id: str, camera_id: str, body: dict) -> dict:
    if not db.one("SELECT id FROM cameras WHERE id = ? AND workspace_id = ?", [camera_id, workspace_id]):
        raise LookupError("camera not found")
    cur = camera_state(db, workspace_id, camera_id)
    if "goal" in body:
        cur["goal"] = str(body["goal"] or "").strip()[:500]
    if "coach_s" in body:
        cur["coach_s"] = 0 if not body["coach_s"] else max(10, min(int(body["coach_s"]), 600))
    if "notice" in body:
        cur["notice"] = bool(body["notice"])
    if db.one("SELECT camera_id FROM camera_state WHERE camera_id = ?", [camera_id]):
        db.execute("UPDATE camera_state SET goal = ?, coach_s = ?, notice = ?, updated = ? WHERE camera_id = ?",
                   [cur["goal"], cur["coach_s"], 1 if cur["notice"] else 0, now(), camera_id])
    else:
        db.execute("INSERT INTO camera_state (camera_id, workspace_id, goal, coach_s, notice, updated) VALUES (?,?,?,?,?,?)",
                   [camera_id, workspace_id, cur["goal"], cur["coach_s"], 1 if cur["notice"] else 0, now()])
    return cur


_coach_last: dict[str, tuple[float, bytes | None, str]] = {}      # camera -> (time, thumb, last thing said)
_notice_last: dict[str, tuple[float, str]] = {}


async def _coach_tick(db: Database, row: dict) -> None:
    cid = row["camera_id"]
    frame = vision.hub.get(cid)
    if frame is None:
        return
    t = time.time()
    last_t, last_th, last_say = _coach_last.get(cid, (0.0, None, ""))
    if t - last_t < row["coach_s"]:
        return
    th = perception.thumb(frame)
    if last_th and th and perception.change(last_th, th) < CHANGE_MIN:
        _coach_last[cid] = (t, last_th, last_say)
        vision.gate_count(row["workspace_id"], False)
        return
    ws = db.one("SELECT * FROM workspaces WHERE id = ?", [row["workspace_id"]])
    models = llm.chain(ws, "vlm") if ws else []
    if not models or (vision.busy_cpu() and all(vision.where(m) == "cpu" for m in models)):
        return
    _coach_last[cid] = (t, th, last_say)
    try:
        out = await vision.coach(models, frame, row["goal"], workspace_id=ws["id"])
    except vision.VisionBusy:
        _coach_last[cid] = (t - 1e9, last_th, last_say)      # try again on the next tick
        return
    observe.record(db, ws["id"], camera_id=cid, subject="work", relation="coach", value=out["say"] if out["speak"] else "ok",
                   conf=0.6, source="coach", t=t)
    if out["speak"] and out["say"] != last_say:
        _coach_last[cid] = (t, th, out["say"])
        cam = db.one("SELECT name FROM cameras WHERE id = ?", [cid])
        alerts.push(ws["id"], {"kind": "coach", "camera_id": cid, "camera": cam["name"] if cam else "",
                               "severity": {"ok": "info"}.get(out["status"], out["status"]), "title": "Coach",
                               "say": out["say"], "speak": True})


async def _notice_tick(db: Database, row: dict) -> None:
    cid = row["camera_id"]
    frame = vision.hub.get(cid)
    t = time.time()
    last_t, last_q = _notice_last.get(cid, (0.0, ""))
    if frame is None or t - last_t < 6:
        return
    _notice_last[cid] = (t, last_q)
    texts = await asyncio.to_thread(perception.ocr, frame)
    q = perception.written_question(texts)
    if not q or q == last_q:
        return
    ws = db.one("SELECT * FROM workspaces WHERE id = ?", [row["workspace_id"]])
    models = llm.chain(ws, "vlm") if ws else []
    if not models:
        return
    _notice_last[cid] = (t, q)
    try:
        out = await vision.look(models, frame, q, workspace_id=ws["id"], background=True)
    except vision.VisionBusy:
        _notice_last[cid] = (t, last_q)
        return
    cam = db.one("SELECT name FROM cameras WHERE id = ?", [cid])
    alerts.push(ws["id"], {"kind": "notice", "camera_id": cid, "camera": cam["name"] if cam else "", "severity": "info",
                           "title": q, "say": out["say"], "speak": True})


# ================================================================ jobs (reviews)
Progress = Callable[[float, str], Awaitable[None]]
_tasks: dict[str, asyncio.Task] = {}


def job_api(r: dict) -> dict:
    return {"id": r["id"], "kind": r["kind"], "status": r["status"], "progress": r["progress"], "note": r["note"],
            "params": loads(r["params"], {}), "result": loads(r["result"], {}), "error": r["error"],
            "created": r["created"], "updated": r["updated"]}


def start_job(db: Database, workspace_id: str, kind: str, params: dict, work: Callable[[Progress], Awaitable[dict]]) -> dict:
    jid = new_id()
    t = now()
    db.execute("INSERT INTO media_jobs (id, workspace_id, kind, status, progress, note, params, result, error, created, "
               "updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)", [jid, workspace_id, kind, "running", 0, "starting", dumps(params),
                                                         "{}", "", t, t])

    async def progress(f: float, note: str) -> None:
        db.execute("UPDATE media_jobs SET progress = ?, note = ?, updated = ? WHERE id = ?",
                   [round(min(1.0, f), 3), note[:200], now(), jid])

    async def runner():
        try:
            result = await work(progress)
            db.execute("UPDATE media_jobs SET status = 'done', progress = 1, note = 'done', result = ?, updated = ? "
                       "WHERE id = ?", [dumps(result), now(), jid])
            alerts.push(workspace_id, {"kind": "job", "severity": "info", "title": "Review ready",
                                       "say": (result.get("summary") or "")[:300], "job_id": jid,
                                       "record_id": result.get("record_id")})
        except Exception as e:  # noqa: BLE001 - a failed job reports why
            log.warning("job %s failed: %s", jid, e)
            db.execute("UPDATE media_jobs SET status = 'failed', error = ?, updated = ? WHERE id = ?",
                       [f"{type(e).__name__}: {str(e)[:300]}", now(), jid])
        finally:
            _tasks.pop(jid, None)

    _tasks[jid] = asyncio.create_task(runner())
    return job_api(db.one("SELECT * FROM media_jobs WHERE id = ?", [jid]))


def get_job(db: Database, workspace_id: str, job_id: str) -> dict | None:
    row = db.one("SELECT * FROM media_jobs WHERE id = ? AND workspace_id = ?", [job_id, workspace_id])
    return job_api(row) if row else None


def list_jobs(db: Database, workspace_id: str, limit: int = 20) -> list[dict]:
    return [job_api(r) for r in db.execute("SELECT * FROM media_jobs WHERE workspace_id = ? ORDER BY created DESC LIMIT ?",
                                           [workspace_id, limit])]


async def review_job(db: Database, ws: dict, frames: list[tuple[float, bytes]], *, title: str, question: str = "",
                     procedure_id: str = "", camera: str = "", camera_id: str = "", relative: bool = True,
                     progress: Progress) -> dict:
    """Review frames with the vision chain, compare against a procedure if given, save a `video_review` record."""
    models = llm.chain(ws, "vlm")
    if not models:
        raise ValueError("no vision model is set up")
    context = ""
    if procedure_id:
        try:
            p = store.procedure(db, ws["id"], procedure_id)
            context = f"They should be following '{p['name']}':\n" + "\n".join(
                f"{i + 1}. {s['instruction']}" for i, s in enumerate(p["steps"]))
        except store.NotFound:
            pass
    await progress(0.02, f"looking at {len(frames)} frames")
    facts = ""
    if relative and camera_id:          # what the cameras already noted in this window: free context, no model call
        facts = observe.timeline_text(observe.between(db, ws["id"], frames[0][0], frames[-1][0], camera_id, limit=40),
                                      frames[0][0])
    out = await vision.review(models, frames, question, context, workspace_id=ws["id"], relative=relative,
                              progress=lambda f, n: progress(0.05 + 0.85 * f, n), facts=facts)
    if camera_id and out.get("summary"):
        observe.add_summary(db, ws["id"], camera_id, out["summary"], frames[0][0] if relative else now(), frames[-1][0]
                            if relative else now())
        asyncio.create_task(_consolidate(db, ws, camera_id))
    keys = [vision.save_snapshot(db, ws["id"], f, f"{title} {vision.clock(t, frames[0][0] if relative else 0)}.jpg")
            for t, f in frames[:: max(1, len(frames) // 4)][:4]]
    text = out["summary"] + ("\n\nProblems:\n" + "\n".join(f"- {p.get('t', '')} {p.get('what', '')}" for p in out["problems"])
                             if out["problems"] else "")
    rec = irf.normalize({"type": "video_review", "source": f"cameras/{camera}" if camera else "videos",
                         "title": title, "subject": f"camera:{camera}" if camera else "", "text": text,
                         "data": {**out, "question": question, "procedure_id": procedure_id, "snapshots": keys}},
                        now=now())
    rid = store.write(db, ws["id"], [rec])["ids"][0]
    return {**out, "record_id": rid, "snapshots": keys}


async def _merge_texts(ws: dict, texts: list[str]) -> str:
    models = llm.chain(ws, "llm") or llm.chain(ws, "vlm")
    if not models:
        return " ".join(texts)[:4000]
    msg = await llm.chat_chain(models, [{"role": "user", "content": "Merge these consecutive summaries of one camera's "
                               "shift into one factual summary of 3-5 sentences. Keep times, readings and problems:\n\n" +
                               "\n".join(f"- {x}" for x in texts)}], max_tokens=400)
    return (msg.get("content") or " ".join(texts)).strip()


async def _consolidate(db: Database, ws: dict, camera_id: str) -> None:
    try:
        await observe.consolidate(db, ws["id"], camera_id, lambda texts: _merge_texts(ws, texts))
    except Exception as e:  # noqa: BLE001 - consolidation is background housekeeping
        log.warning("consolidate %s: %s", camera_id, e)


async def recover_jobs() -> None:
    db = get_db()
    db.execute("UPDATE media_jobs SET status = 'failed', error = 'the server restarted' WHERE status = 'running'")


# ================================================================ the loop
_loop_task: asyncio.Task | None = None


async def tick(db: Database) -> None:
    t = now()
    rules = db.execute("SELECT * FROM watch_rules WHERE enabled = 1")
    for r in rules:
        if r["last_check"] is not None and t - r["last_check"] < r["interval_s"]:
            continue
        try:
            await check_rule(db, r)
        except Exception as e:  # noqa: BLE001 - one bad rule never stops the others
            log.warning("watch rule %s: %s", r["id"], e)
            db.execute("UPDATE watch_rules SET last_check = ?, last_say = ? WHERE id = ?",
                       [t, f"couldn't check: {str(e)[:150]}", r["id"]])
    for row in db.execute("SELECT * FROM camera_state WHERE (coach_s > 0 AND goal != '') OR notice = 1"):
        try:
            if row["coach_s"] and row["goal"]:
                await _coach_tick(db, row)
            if row["notice"] and perception.status()["ocr"]:
                await _notice_tick(db, row)
        except Exception as e:  # noqa: BLE001
            log.warning("camera %s: %s", row["camera_id"], e)


async def loop() -> None:
    while True:
        try:
            await tick(get_db())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("watch loop: %s", e)
        await asyncio.sleep(TICK)


async def start() -> None:
    global _loop_task
    await recover_jobs()
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(loop())


async def stop() -> None:
    global _loop_task
    if _loop_task:
        _loop_task.cancel()
        try:
            await _loop_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _loop_task = None
    for t in list(_tasks.values()):
        t.cancel()
