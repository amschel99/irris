"""The senses: voice (listen, speak), vision status, cameras that stream, watch, coach and review. /api/v1/...

Loaded by app.py as a feature module: `router` is mounted, `startup()` starts the watch loop and warms the voices.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, WebSocket
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect

from irris import agent, auth, llm, observe, perception, runs, store, vision, voice, watch
from irris.cancel import unless_gone
from irris.auth import Ctx, ctx
from irris.db import dumps, get_db, loads, new_id, now
from irris.security import sha256

router = APIRouter(prefix="/api/v1")
MAX_AUDIO = 25 * 1024 * 1024
MAX_VIDEO = 300 * 1024 * 1024


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except ValueError:
        raise HTTPException(400, "the body isn't valid JSON")
    if not isinstance(b, dict):
        raise HTTPException(400, "send a JSON object")
    return b


def _camera(c: Ctx, camera_id: str) -> dict:
    cam = c.db.one("SELECT * FROM cameras WHERE id = ? AND workspace_id = ?", [camera_id, c.ws_id])
    if not cam:
        raise HTTPException(404, "camera not found")
    return cam


# ================================================================ vision status
@router.get("/vision/status")
def vision_status(c: Ctx = Depends(ctx)):
    chain = []
    for m in llm.chain(c.workspace, "vlm"):
        down = llm._down.get(m.base_url, 0) > time.time()
        own = bool((json.loads(c.workspace.get("models") or "{}").get("vlm") or {}).get("base_url"))
        chain.append({"model": m.model, "where": vision.where(m), "down": down,
                      "source": "cpu fallback" if vision.where(m) == "cpu" else ("workspace" if own else "server")})
    active = next((x for x in chain if not x["down"]), None)
    return {"ready": bool(chain), "chain": chain, "active": active, "last": vision.stats(c.ws_id),
            "cpu_busy": vision.busy_cpu(), "queue": vision.queue_depth(),
            "expected_s": vision.expected_seconds(llm.chain(c.workspace, "vlm")),
            "perception": perception.status(), "gate": vision.gate_stats(c.ws_id)}


# ================================================================ voice
@router.get("/voice/settings")
def voice_settings(c: Ctx = Depends(ctx)):
    cfg = voice.get_settings(c.db, c.ws_id)
    return {**voice.public_settings(cfg), "status": voice.status(), "voices": voice.voices(cfg)}


@router.put("/voice/settings")
async def put_voice_settings(request: Request, c: Ctx = Depends(ctx)):
    b = await _body(request)
    c.require_admin()
    try:
        cfg = voice.save_settings(c.db, c.ws_id, b)
    except voice.VoiceError as e:
        raise HTTPException(400, str(e))
    return {**voice.public_settings(cfg), "status": voice.status(), "voices": voice.voices(cfg)}


@router.get("/voice/voices")
def voice_voices(c: Ctx = Depends(ctx)):
    return {"voices": voice.voices(voice.get_settings(c.db, c.ws_id))}


@router.post("/voice/stt")
async def stt(file: UploadFile = File(...), c: Ctx = Depends(ctx)):
    audio = await file.read()
    if len(audio) > MAX_AUDIO:
        raise HTTPException(413, "the recording is over 25 MB")
    try:
        return await voice.transcribe(voice.get_settings(c.db, c.ws_id), audio, file.content_type or "audio/webm",
                                      file.filename or "speech.webm")
    except voice.VoiceError as e:
        raise HTTPException(503, str(e))


@router.post("/voice/tts")
async def tts(request: Request, c: Ctx = Depends(ctx)):
    b = await _body(request)
    try:
        out = await voice.synthesize(voice.get_settings(c.db, c.ws_id), str(b.get("text") or ""), str(b.get("voice") or ""))
    except voice.VoiceError as e:
        raise HTTPException(503, str(e))
    headers = {"Cache-Control": "no-store", "X-Voice-Engine": out["engine"], "X-Voice-Ms": str(out["ms"])}
    if out.get("note"):
        headers["X-Voice-Note"] = out["note"][:200]
    return Response(out["audio"], media_type=out["mime"], headers=headers)


@router.post("/voice/test")
async def voice_test(request: Request, c: Ctx = Depends(ctx)):
    """Try the configured speech engines end to end: speak a sentence, then transcribe it back."""
    b = await _body(request)
    cfg = voice.get_settings(c.db, c.ws_id)
    out: dict[str, Any] = {}
    try:
        spoken = await voice.synthesize(cfg, str(b.get("text") or "Irris is listening. Step three passed."))
        out["tts"] = {"ok": True, "engine": spoken["engine"], "ms": spoken["ms"], "note": spoken.get("note", "")}
    except voice.VoiceError as e:
        return {"tts": {"ok": False, "error": str(e)}, "stt": {"ok": False, "error": "nothing to transcribe"}}
    try:
        heard = await voice.transcribe(cfg, spoken["audio"], spoken["mime"],
                                       "test.mp3" if spoken["mime"] == "audio/mpeg" else "test.wav")
        out["stt"] = {"ok": True, "engine": heard["engine"], "ms": heard["ms"], "text": heard["text"],
                      "note": heard.get("note", "")}
    except voice.VoiceError as e:
        out["stt"] = {"ok": False, "error": str(e)}
    return out


# ================================================================ camera state: goal, coach, notice
@router.get("/cameras/{camera_id}/state")
def get_camera_state(camera_id: str, c: Ctx = Depends(ctx)):
    _camera(c, camera_id)
    return {**watch.camera_state(c.db, c.ws_id, camera_id), "rules": watch.list_rules(c.db, c.ws_id, camera_id)}


@router.put("/cameras/{camera_id}/state")
async def put_camera_state(camera_id: str, request: Request, c: Ctx = Depends(ctx)):
    b = await _body(request)
    try:
        return watch.set_camera_state(c.db, c.ws_id, camera_id, b)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (TypeError, ValueError):
        raise HTTPException(400, "coach_s must be a number of seconds")


@router.post("/cameras/{camera_id}/snapshot")
async def snapshot(camera_id: str, request: Request, c: Ctx = Depends(ctx)):
    """Keep the current frame: a file plus an `image` record (searchable, citable)."""
    cam = _camera(c, camera_id)
    b = await request.json() if (await request.body()) else {}
    frame = await watch.frame_for(c.db, cam)
    if frame is None:
        raise HTTPException(409, "no live frame from that camera")
    fid = vision.save_snapshot(c.db, c.ws_id, frame, f"{cam['name']} snapshot.jpg")
    from irris import irf
    note = str((b or {}).get("note") or "")
    rec = irf.normalize({"type": "image", "source": f"cameras/{cam['name']}", "id": fid, "title": f"{cam['name']} snapshot",
                         "subject": f"camera:{cam['name']}", "text": note,
                         "data": {"file_id": fid, "camera_id": cam["id"], "mime": "image/jpeg"}}, now=time.time())
    rid = store.write(c.db, c.ws_id, [rec])["ids"][0]
    return {"file_id": fid, "record_id": rid}


@router.post("/cameras/{camera_id}/read")
async def read_instrument(camera_id: str, request: Request, c: Ctx = Depends(ctx)):
    """Read the instrument in view (vision model, cross-checked by the gauge reader / OCR) and log the reading."""
    cam = _camera(c, camera_id)
    b = await _body(request)
    frame = await watch.frame_for(c.db, cam)
    if frame is None:
        raise HTTPException(409, "no live frame from that camera")
    models = llm.chain(c.workspace, "vlm")
    spec = b.get("spec") if isinstance(b.get("spec"), dict) else None
    what = str(b.get("what") or "the instrument in view")
    if models:
        roi = runs.roi_for(c.db, c.ws_id, cam["id"], "", -1)
        try:
            v = await vision.read_verified(models, frame, spec, roi, workspace_id=c.ws_id, hint=f"Read {what}.")
        except llm.ModelError as e:
            raise HTTPException(502, str(e))
        r = v["reading"]
        say = (f"{r['value']:g} {r.get('unit', '')}".strip() + ("" if v["verified"] else f". Not sure: {v['note']}")) \
            if r else "I can't read it. Move closer or draw the reading area."
        out = {"say": say, "status": "ok" if v["verified"] else "unsure", "reading": r, "verified": v["verified"],
               "reads": v["reads"], "model": v["model"], "where": v["where"], "ms": v["ms"]}
    else:
        fast = await vision.fast_reading(frame, spec)
        if not fast:
            raise HTTPException(400, "no vision model is set up and the display couldn't be read by OCR")
        out = {"say": f"{fast['value']:g} {fast['unit']}".strip(), "status": "ok", "reading": fast}
    reading = out.get("reading")
    if reading:
        observe.record(c.db, c.ws_id, camera_id=cam["id"], subject=str(b.get("subject") or what), relation="reading",
                       value=reading["value"], unit=reading.get("unit", ""), conf=reading.get("confidence", 0.5),
                       source="read")
    if reading and b.get("log", True) and out.get("verified", True):
        from irris import irf
        rec = irf.normalize({"type": "reading", "source": f"cameras/{cam['name']}",
                             "subject": str(b.get("subject") or f"camera:{cam['name']}"), "title": what,
                             "data": {**reading, "camera_id": cam["id"]}, "units": {"value": reading.get("unit", "")}},
                            now=time.time())
        out["record_id"] = store.write(c.db, c.ws_id, [rec])["ids"][0]
    return out


# ================================================================ watch rules
@router.get("/watch")
def rules(camera_id: str = "", c: Ctx = Depends(ctx)):
    return {"rules": watch.list_rules(c.db, c.ws_id, camera_id)}


@router.post("/watch")
async def add_rule(request: Request, c: Ctx = Depends(ctx)):
    b = await _body(request)
    try:
        return watch.add_rule(c.db, c.ws_id, str(b.get("camera_id") or ""), b)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@router.patch("/watch/{rule_id}")
async def patch_rule(rule_id: str, request: Request, c: Ctx = Depends(ctx)):
    b = await _body(request)
    try:
        return watch.update_rule(c.db, c.ws_id, rule_id, b)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@router.delete("/watch/{rule_id}")
def delete_rule(rule_id: str, c: Ctx = Depends(ctx)):
    watch.delete_rule(c.db, c.ws_id, rule_id)
    return {"deleted": rule_id}


@router.post("/watch/{rule_id}/test")
async def test_rule(rule_id: str, c: Ctx = Depends(ctx)):
    row = c.db.one("SELECT * FROM watch_rules WHERE id = ? AND workspace_id = ?", [rule_id, c.ws_id])
    if not row:
        raise HTTPException(404, "rule not found")
    try:
        out = await watch.check_rule(c.db, row, force=True)
    except llm.ModelError as e:
        raise HTTPException(502, str(e))
    if out is None:
        raise HTTPException(409, "no live frame from that camera, or no vision model")
    return out


# ================================================================ alerts
@router.get("/alerts")
def recent_alerts(since: float = 0, c: Ctx = Depends(ctx)):
    return {"alerts": watch.alerts.since(c.ws_id, since), "now": time.time()}


@router.get("/alerts/stream")
async def alert_stream(request: Request, c: Ctx = Depends(ctx)):
    q = watch.alerts.subscribe(c.ws_id)
    ws_id = c.ws_id

    async def events():
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    a = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"event: alert\ndata: {dumps(a)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            watch.alerts.unsubscribe(ws_id, q)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ================================================================ reviews (what happened, videos)
@router.post("/cameras/{camera_id}/review")
async def review_recent(camera_id: str, request: Request, c: Ctx = Depends(ctx)):
    cam = _camera(c, camera_id)
    b = await _body(request)
    seconds = max(10, min(float(b.get("seconds") or 90), vision.HISTORY_SECONDS))
    frames = vision.hub.history(camera_id, seconds)
    if len(frames) < 2:
        raise HTTPException(409, "not enough recent video from that camera yet")
    if not llm.available(c.workspace, "vlm"):
        raise HTTPException(400, "no vision model is set up")
    picked = frames
    question = str(b.get("question") or "")
    ws = c.workspace
    db = c.db
    return watch.start_job(db, c.ws_id, "recent", {"camera_id": camera_id, "seconds": seconds, "question": question},
                           lambda progress: watch.review_job(db, ws, picked, title=f"{cam['name']}: last {int(seconds)} s",
                                                             question=question, procedure_id=str(b.get("procedure_id") or ""),
                                                             camera=cam["name"], camera_id=cam["id"], progress=progress))


@router.post("/video/review")
async def review_video(file: UploadFile = File(...), question: str = Form(""), procedure_id: str = Form(""),
                       c: Ctx = Depends(ctx)):
    data = await file.read()
    if len(data) > MAX_VIDEO:
        raise HTTPException(413, "the video is over 300 MB")
    if not llm.available(c.workspace, "vlm"):
        raise HTTPException(400, "no vision model is set up")
    try:
        frames, duration = await asyncio.to_thread(vision.sample_video, data, 72)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ws, db, name = c.workspace, c.db, file.filename or "video"
    job = watch.start_job(db, c.ws_id, "video", {"name": name, "duration": round(duration, 1), "question": question},
                          lambda progress: watch.review_job(db, ws, frames, title=f"Video review: {name}",
                                                            question=question, procedure_id=procedure_id,
                                                            relative=False, progress=progress))
    return {**job, "frames": len(frames), "duration": round(duration, 1)}


@router.get("/media/jobs")
def jobs(c: Ctx = Depends(ctx)):
    return {"jobs": watch.list_jobs(c.db, c.ws_id)}


@router.get("/media/jobs/{job_id}")
def job(job_id: str, c: Ctx = Depends(ctx)):
    j = watch.get_job(c.db, c.ws_id, job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


# ================================================================ gated auto-check, regions of interest, observations
@router.post("/runs/{run_id}/check/auto")
async def autocheck(run_id: str, request: Request, c: Ctx = Depends(ctx)):
    """Auto-check: the model is asked only when the step's region changed since the last check. Unchanged ->
    {"check": {"result": "unchanged", "skipped": true}} at no model cost."""
    b = await request.json() if (await request.body()) else {}
    try:
        s = runs.state(c.db, c.ws_id, run_id)
    except store.NotFound as e:
        raise HTTPException(404, str(e))
    frame = await agent.camera_frame(c.db, c.ws_id, str((b or {}).get("camera_id") or s["camera_id"] or ""))
    if frame is None:
        raise HTTPException(409, "no live camera frame to check")
    try:
        return await unless_gone(request, runs.vision_check(c.db, c.workspace, run_id, frame, gated=True,
                                                            scene=str((b or {}).get("scene") or ""),
                                                            step=int(b["step"]) if (b or {}).get("step") is not None
                                                            else None, hands=(b or {}).get("hands")))
    except vision.VisionBusy:
        return {"check": {"result": "busy", "say": "", "reading": None, "skipped": True}, "run": s}
    except runs.RunError as e:
        raise HTTPException(400, str(e))
    except llm.ModelError as e:
        raise HTTPException(502, str(e))


# ================================================================ calibrated instruments (docs/GUIDANCE_SPEC.md)
@router.get("/instruments")
def list_instruments(camera_id: str = "", c: Ctx = Depends(ctx)):
    from irris import instruments
    found = instruments.list_all(c.db, c.ws_id)
    return {"instruments": [i for i in found if not camera_id or i["camera_id"] == camera_id]}


@router.post("/instruments")
async def create_instrument(request: Request, c: Ctx = Depends(ctx)):
    from irris import instruments
    b = await request.json()
    if b.get("camera_id"):
        _camera(c, str(b["camera_id"]))
    try:
        return instruments.create(c.db, c.ws_id, b)
    except instruments.InstrumentError as e:
        raise HTTPException(400, str(e))


@router.put("/instruments/{iid}")
async def update_instrument(iid: str, request: Request, c: Ctx = Depends(ctx)):
    from irris import instruments
    b = await request.json()
    if b.get("camera_id"):
        _camera(c, str(b["camera_id"]))
    try:
        return instruments.update(c.db, c.ws_id, iid, b)
    except instruments.InstrumentError as e:
        raise HTTPException(400, str(e))
    except store.NotFound as e:
        raise HTTPException(404, str(e))


@router.delete("/instruments/{iid}")
def delete_instrument(iid: str, c: Ctx = Depends(ctx)):
    from irris import instruments
    try:
        instruments.delete(c.db, c.ws_id, iid)
    except store.NotFound as e:
        raise HTTPException(404, str(e))
    return {"deleted": iid}


@router.post("/instruments/{iid}/read")
async def read_instrument(iid: str, request: Request, c: Ctx = Depends(ctx)):
    """Read it from the camera's latest frames (body {} uses the instrument's camera; "camera_id" overrides;
    "limits": {"min", "max"} flags values near a limit). The vision model is used only when geometry/OCR can't read
    it, and its value always needs a person to confirm."""
    from irris import instruments
    b = await request.json() if (await request.body()) else {}
    try:
        inst = instruments.get(c.db, c.ws_id, iid)
    except store.NotFound as e:
        raise HTTPException(404, str(e))
    cam = str(b.get("camera_id") or inst.get("camera_id") or "")
    frame = await agent.camera_frame(c.db, c.ws_id, cam) if cam else None
    if frame is None:
        raise HTTPException(409, "no live camera frame for this instrument")
    frames = vision.hub.recent(cam, 2, 6.0) + [frame]
    limits = b.get("limits") if isinstance(b.get("limits"), dict) else None
    out = await asyncio.to_thread(instruments.read, inst, frames, limits=limits)
    if out["value"] is None:
        models = llm.chain(c.workspace, "vlm")
        if models:
            crop = perception.crop(frame, dict(zip("xywh", inst["roi"]))) if inst.get("roi") else frame
            try:
                r = await vision.look(models, crop, f"What value does the {inst['name']} show? Unit {inst.get('unit', '')}.",
                                      workspace_id=c.ws_id)
            except llm.ModelError:
                r = None
            if r and r.get("reading"):
                out = {"value": r["reading"]["value"], "unit": r["reading"].get("unit") or inst.get("unit", ""),
                       "method": "vlm", "confidence": min(0.5, r["reading"].get("confidence", 0.5)),
                       "needs_confirm": True, "reason": "read by the vision model; please confirm"}
    if cam:
        vision.hub.publish(cam, {"type": "instrument", "instrument_id": iid, "name": inst["name"], **out})
    lim = limits or inst.get("limits") or {}
    v = out.get("value")
    if v is not None and not out.get("needs_confirm") and (
            (lim.get("min") is not None and v < float(lim["min"])) or (lim.get("max") is not None and v > float(lim["max"]))):
        from irris import ops
        ops.emit(c.db, c.ws_id, "instrument.out_of_limits", iid,
                 {"instrument": inst["name"], "value": v, "unit": out.get("unit") or inst.get("unit", ""),
                  "limits": lim, "method": out.get("method")})
    return out


def _roi_box(b: dict) -> dict:
    box = b.get("box") or {}
    try:
        out = {k: max(0.0, min(1.0, float(box[k]))) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "box needs x, y, w and h between 0 and 1")
    if out["w"] < 0.02 or out["h"] < 0.02:
        raise HTTPException(400, "that area is too small")
    return out


@router.get("/cameras/{camera_id}/rois")
def rois(camera_id: str, c: Ctx = Depends(ctx)):
    _camera(c, camera_id)
    rows = c.db.execute("SELECT key, box, label, updated FROM camera_rois WHERE camera_id = ? AND workspace_id = ?",
                        [camera_id, c.ws_id])
    return {"rois": [{"key": r["key"], "box": loads(r["box"], {}), "label": r["label"], "updated": r["updated"]} for r in rows]}


@router.put("/cameras/{camera_id}/roi")
async def put_roi(camera_id: str, request: Request, c: Ctx = Depends(ctx)):
    """The area to read on this camera: for one step ("<procedure_id>:<step index>") or the camera's default."""
    _camera(c, camera_id)
    b = await _body(request)
    key = str(b.get("key") or "default")[:120]
    box = _roi_box(b)
    c.db.execute("DELETE FROM camera_rois WHERE camera_id = ? AND key = ?", [camera_id, key])
    c.db.execute("INSERT INTO camera_rois (id, workspace_id, camera_id, key, box, label, updated) VALUES (?,?,?,?,?,?,?)",
                 [new_id(), c.ws_id, camera_id, key, dumps(box), str(b.get("label") or "")[:80], now()])
    return {"key": key, "box": box}


@router.delete("/cameras/{camera_id}/roi")
def delete_roi(camera_id: str, key: str = "default", c: Ctx = Depends(ctx)):
    _camera(c, camera_id)
    c.db.execute("DELETE FROM camera_rois WHERE camera_id = ? AND workspace_id = ? AND key = ?", [camera_id, c.ws_id, key])
    return {"deleted": key}


@router.get("/observations")
def observations(camera_id: str = "", since: float = 0, until: float = 0, q: str = "", tz_offset_min: int = 0,
                 c: Ctx = Depends(ctx)):
    """What the cameras saw, as facts. With q: answered from facts alone ("when did the guard open?",
    "what happened at 10:15?", "last reading of the platen"), strict first, then relaxed. No model call."""
    if q:
        return observe.answer(c.db, c.ws_id, q, camera_id=camera_id, tz_offset_min=tz_offset_min)
    t1 = until or now()
    return {"facts": observe.between(c.db, c.ws_id, since or t1 - 3600, t1, camera_id)}


@router.get("/cameras/{camera_id}/summaries")
def shift_summaries(camera_id: str, c: Ctx = Depends(ctx)):
    _camera(c, camera_id)
    return {"summaries": observe.summaries(c.db, c.ws_id, camera_id)}


# ================================================================ recordings ("record what you see")
async def _save_recording(db, ws: dict, file: UploadFile, *, camera: dict | None, run_id: str, review: bool,
                          question: str, by: str) -> dict:
    from irris import files, irf, runs
    name = file.filename or "clip.webm"
    mime = (file.content_type or "video/webm").split(";")[0]
    if not mime.startswith(("video/", "audio/")):
        raise HTTPException(415, "send a video or audio clip")
    try:
        stored = await files.save_upload(db, ws["id"], file, name, mime)
    except files.TooBig as e:
        raise HTTPException(413, str(e))
    run = None
    if not run_id and camera:
        row = db.one("SELECT id FROM runs WHERE workspace_id = ? AND camera_id = ? AND status = 'active' "
                     "ORDER BY started DESC LIMIT 1", [ws["id"], camera["id"]])
        run_id = row["id"] if row else ""
    if run_id:
        try:
            run = runs.state(db, ws["id"], run_id)
        except store.NotFound:
            run_id = ""
    title = f"Recording: {run['procedure']['name']}" if run else f"Recording: {camera['name']}" if camera else "Recording"
    active = bool(run and run["status"] == "active")
    rec = irf.normalize({"type": "recording", "source": f"runs/{run_id}/recordings" if run_id else "recordings",
                         "id": stored["id"], "title": title,
                         "subject": f"run:{run_id}" if run_id else (f"camera:{camera['name']}" if camera else ""),
                         "text": f"Clip recorded by {by}" + (f" during step {run['step'] + 1} of {run['procedure']['name']}"
                                                              if active else ""),
                         "data": {"file_id": stored["id"], "mime": mime, "size": stored["size"], "run_id": run_id,
                                  "camera_id": camera["id"] if camera else "",
                                  "step": run["step"] + 1 if active else None}},
                        now=time.time())
    rid = store.write(db, ws["id"], [rec])["ids"][0]
    if run and not active:
        runs.refresh_batch_record(db, ws["id"], run_id)
    out: dict[str, Any] = {"file_id": stored["id"], "record_id": rid, "run_id": run_id, "size": stored["size"],
                           "url": f"/api/v1/media/files/{stored['id']}"}
    if review and mime.startswith("video/") and llm.available(ws, "vlm"):
        data = files.read_bytes(db, ws["id"], stored["id"]) or b""
        try:
            frames, duration = await asyncio.to_thread(vision.sample_video, data, 72)
        except ValueError as e:
            out["review_error"] = str(e)
        else:
            pid = run["procedure"]["id"] if run else ""
            out["job"] = watch.start_job(db, ws["id"], "video", {"name": name, "duration": round(duration, 1),
                                                                 "question": question, "recording": rid},
                                         lambda progress: watch.review_job(db, ws, frames, title=f"Review: {title}",
                                                                           question=question, procedure_id=pid,
                                                                           relative=False, progress=progress))
    return out


@router.post("/recordings")
async def upload_recording(file: UploadFile = File(...), run_id: str = Form(""), camera_id: str = Form(""),
                           review: bool = Form(True), question: str = Form(""), c: Ctx = Depends(ctx)):
    """A clip from Live: kept as a file and a `recording` record, attached to the run (evidence), then reviewed."""
    camera = _camera(c, camera_id) if camera_id else None
    return await _save_recording(c.db, c.workspace, file, camera=camera, run_id=run_id, review=review,
                                 question=question, by=c.user["email"] if c.user else "api")


@router.post("/cam/recording")
async def paired_recording(t: str, file: UploadFile = File(...), review: bool = Form(True)):
    db = get_db()
    cam = db.one("SELECT * FROM cameras WHERE token_hash = ? AND kind = 'phone'", [sha256(t)])
    if not cam:
        raise HTTPException(401, "this pairing link is no longer valid. Pair the phone again.")
    ws = db.one("SELECT * FROM workspaces WHERE id = ?", [cam["workspace_id"]])
    return await _save_recording(db, ws, file, camera=cam, run_id="", review=review, question="",
                                 by=f"the {cam['name']} phone")


@router.post("/cam/tts")
async def paired_tts(t: str, request: Request):
    """The paired phone speaks verdicts and alerts in the workspace's voice."""
    db = get_db()
    cam = db.one("SELECT workspace_id FROM cameras WHERE token_hash = ? AND kind = 'phone'", [sha256(t)])
    if not cam:
        raise HTTPException(401, "this pairing link is no longer valid")
    b = await _body(request)
    try:
        out = await voice.synthesize(voice.get_settings(db, cam["workspace_id"]), str(b.get("text") or "")[:600])
    except voice.VoiceError as e:
        raise HTTPException(503, str(e))
    return Response(out["audio"], media_type=out["mime"],
                    headers={"Cache-Control": "no-store", "X-Voice-Engine": out["engine"]})


@router.get("/media/files/{file_id}")
def media_file(file_id: str, request: Request, c: Ctx = Depends(ctx)):
    """A stored file with HTTP Range support, so recorded clips play and seek in the browser."""
    from irris import files
    f = files.find(c.db, c.ws_id, file_id)
    if not f:
        raise HTTPException(404, "file not found")
    size = f["size"]
    headers = {"Accept-Ranges": "bytes", "Content-Disposition": f'inline; filename="{f["name"]}"',
               "Cache-Control": "private, max-age=3600"}
    rng = request.headers.get("range", "")
    start, end = 0, size - 1
    if rng.startswith("bytes="):
        a, _, b = rng[6:].split(",")[0].partition("-")
        try:
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            else:
                start = max(0, size - int(b))
        except ValueError:
            raise HTTPException(416, "bad range")
        end = min(end, size - 1)
        if start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    length = end - start + 1
    headers["Content-Length"] = str(length)

    def body():
        if "content" in f:
            yield f["content"][start:end + 1]
            return
        with open(f["path"], "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=206 if rng else 200, media_type=f["mime"], headers=headers)


# ================================================================ WebSocket camera stream
@router.websocket("/cam/ws")
async def cam_ws(ws: WebSocket):
    """Binary JPEG frames up; verdicts, alerts and coach messages down.

    A paired phone connects with ?t=<pairing token>. A signed-in browser connects with its session cookie (or
    ?key=irr_...) plus ?camera=<id>&ws=<workspace id>."""
    db = get_db()
    q = ws.query_params
    cam = None
    if q.get("t"):
        cam = db.one("SELECT * FROM cameras WHERE token_hash = ? AND kind = 'phone'", [sha256(q["t"])])
    elif q.get("camera"):
        workspace_ids: list[str] = []
        raw = q.get("key", "")
        if raw.startswith(auth.KEY_PREFIX):
            w = auth.key_workspace(db, raw)
            workspace_ids = [w["id"]] if w else []
        else:
            user = auth.session_user(db, ws.cookies.get(auth.COOKIE, ""))
            if user:
                workspace_ids = [w["id"] for w in auth.workspaces_of(db, user["id"])]
        row = db.one("SELECT * FROM cameras WHERE id = ?", [q["camera"]])
        if row and row["workspace_id"] in workspace_ids:
            cam = row
    if not cam:
        await ws.close(code=4401)
        return
    await ws.accept()
    sub = vision.hub.subscribe(cam["id"])
    await ws.send_json({"type": "hello", "camera": cam["name"], "camera_id": cam["id"]})
    active = db.one("SELECT id FROM runs WHERE workspace_id = ? AND camera_id = ? AND status = 'active' "
                    "ORDER BY started DESC LIMIT 1", [cam["workspace_id"], cam["id"]])
    if active:                                   # the phone gets the current run (zones, step) as soon as it connects
        try:
            st = runs.state(db, cam["workspace_id"], active["id"])
            await ws.send_json({"type": "run", "run": runs.push_view(st), "say": st["say"]})
        except store.NotFound:
            pass

    async def pump():
        while True:
            msg = await sub.get()
            await ws.send_json(msg)

    sender = asyncio.create_task(pump())
    try:
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                break
            data = m.get("bytes")
            if data:
                if data[:2] == b"\xff\xd8" and len(data) <= 4_000_000:
                    vision.hub.put(cam["id"], data)
                continue
            if m.get("text"):
                try:
                    j = json.loads(m["text"])
                except ValueError:
                    continue
                if j.get("type") == "ping":
                    await ws.send_json({"type": "pong", "t": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        vision.hub.unsubscribe(cam["id"], sub)


# ================================================================ MCP tools (added to /mcp by irris/mcp.py)
MCP_TOOLS: list[dict] = [
    {"name": "snapshot", "description": "The current frame from a camera, as an image (and kept as evidence).",
     "inputSchema": {"type": "object", "properties": {"camera_id": {"type": "string"}}, "required": ["camera_id"]}},
    {"name": "read_instrument", "description": "Read the gauge or display a camera sees (vision model cross-checked by "
     "a needle reader / OCR) and log the reading.",
     "inputSchema": {"type": "object", "properties": {"camera_id": {"type": "string"},
                                                       "what": {"type": "string"}, "subject": {"type": "string"}},
                     "required": ["camera_id"]}},
    {"name": "watch", "description": "List a camera's watch rules, or add one: a condition in plain words checked every "
     "interval_s seconds; when it becomes true Irris logs an event with a snapshot and alerts the team.",
     "inputSchema": {"type": "object", "properties": {
         "camera_id": {"type": "string"}, "condition": {"type": "string"}, "interval_s": {"type": "integer"},
         "severity": {"type": "string", "enum": ["info", "warning", "danger"]}}}},
    {"name": "what_happened", "description": "Review the last N seconds of a camera in the background (timeline, "
     "readings, problems). Returns a job id for job().",
     "inputSchema": {"type": "object", "properties": {"camera_id": {"type": "string"}, "seconds": {"type": "integer"},
                                                       "question": {"type": "string"}}, "required": ["camera_id"]}},
    {"name": "job", "description": "Progress or result of a review job.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}},
    {"name": "coach", "description": "Set a camera's goal and coach mode: look every N seconds and speak up only for "
     "mistakes, safety, readings or finished steps (seconds 0 = off).",
     "inputSchema": {"type": "object", "properties": {"camera_id": {"type": "string"}, "goal": {"type": "string"},
                                                       "seconds": {"type": "integer"}}, "required": ["camera_id"]}},
    {"name": "observations", "description": "What the cameras saw, as time-stamped facts. Ask 'when did X happen?', "
     "'what happened at 10:15?' or 'last reading of Y'; answered from stored facts, no model call.",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}, "camera_id": {"type": "string"}},
                     "required": ["question"]}},
    {"name": "speak", "description": "Irris's voice: text to speech (WAV or MP3 audio).",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]
MCP_NAMES = {t["name"] for t in MCP_TOOLS}


def _mcp_text(value: Any, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": value if isinstance(value, str) else dumps(value)}], "isError": error}


async def mcp_call(c: Ctx, name: str, args: dict) -> dict:
    import base64
    cam = None
    if args.get("camera_id"):
        cam = c.db.one("SELECT * FROM cameras WHERE id = ? AND workspace_id = ?", [args["camera_id"], c.ws_id])
        if not cam:
            return _mcp_text("Unknown camera_id; call cameras first.", True)
    if name == "snapshot":
        frame = await watch.frame_for(c.db, cam) if cam else None
        if frame is None:
            return _mcp_text("No live frame from that camera.", True)
        fid = vision.save_snapshot(c.db, c.ws_id, frame, f"{cam['name']} snapshot.jpg")
        return {"content": [{"type": "image", "data": base64.b64encode(vision.shrink(frame, 1024)).decode(),
                             "mimeType": "image/jpeg"}, {"type": "text", "text": f"file_id: {fid}"}], "isError": False}
    if name == "read_instrument":
        frame = await watch.frame_for(c.db, cam) if cam else None
        models = llm.chain(c.workspace, "vlm")
        if frame is None or not models:
            return _mcp_text("No live frame, or no vision model.", True)
        out = await vision.look(models, frame, f"Read {args.get('what') or 'the instrument in view'}.", workspace_id=c.ws_id)
        if out.get("reading"):
            from irris import irf
            rec = irf.normalize({"type": "reading", "source": f"cameras/{cam['name']}",
                                 "subject": str(args.get("subject") or f"camera:{cam['name']}"),
                                 "title": str(args.get("what") or "reading"), "data": out["reading"],
                                 "units": {"value": out["reading"].get("unit", "")}}, now=time.time())
            out["record_id"] = store.write(c.db, c.ws_id, [rec])["ids"][0]
        return _mcp_text(out)
    if name == "watch":
        if args.get("condition"):
            if not cam:
                return _mcp_text("camera_id is required to add a rule.", True)
            try:
                return _mcp_text(watch.add_rule(c.db, c.ws_id, cam["id"], args))
            except (ValueError, LookupError) as e:
                return _mcp_text(str(e), True)
        return _mcp_text({"rules": watch.list_rules(c.db, c.ws_id, cam["id"] if cam else "")})
    if name == "what_happened":
        if not cam:
            return _mcp_text("camera_id is required.", True)
        seconds = max(10, min(float(args.get("seconds") or 90), vision.HISTORY_SECONDS))
        frames = vision.hub.history(cam["id"], seconds)
        if len(frames) < 2 or not llm.available(c.workspace, "vlm"):
            return _mcp_text("Not enough recent video from that camera, or no vision model.", True)
        ws, db, q = c.workspace, c.db, str(args.get("question") or "")
        j = watch.start_job(db, c.ws_id, "recent", {"camera_id": cam["id"], "seconds": seconds, "question": q},
                            lambda progress: watch.review_job(db, ws, frames, title=f"{cam['name']}: last {int(seconds)} s",
                                                              question=q, camera=cam["name"], camera_id=cam["id"],
                                                              progress=progress))
        return _mcp_text({"job_id": j["id"], "note": "Call job with this id in a minute."})
    if name == "job":
        j = watch.get_job(c.db, c.ws_id, str(args.get("job_id")))
        return _mcp_text(j) if j else _mcp_text("No such job.", True)
    if name == "coach":
        if not cam:
            return _mcp_text("camera_id is required.", True)
        body = {"coach_s": int(args.get("seconds") or 0)}
        if "goal" in args:
            body["goal"] = args["goal"]
        return _mcp_text(watch.set_camera_state(c.db, c.ws_id, cam["id"], body))
    if name == "observations":
        return _mcp_text(observe.answer(c.db, c.ws_id, str(args.get("question") or ""), camera_id=cam["id"] if cam else ""))
    if name == "speak":
        try:
            out = await voice.synthesize(voice.get_settings(c.db, c.ws_id), str(args.get("text") or ""))
        except voice.VoiceError as e:
            return _mcp_text(str(e), True)
        return {"content": [{"type": "audio", "data": base64.b64encode(out["audio"]).decode(), "mimeType": out["mime"]}],
                "isError": False}
    return _mcp_text(f"Unknown tool {name}", True)


# ================================================================ lifecycle
async def startup() -> None:
    await watch.start()
    import os
    if os.environ.get("VOICE_WARM", "1") == "1" and not os.environ.get("PYTEST_CURRENT_TEST"):
        asyncio.get_running_loop().run_in_executor(None, voice.warm)


async def shutdown() -> None:
    await watch.stop()
