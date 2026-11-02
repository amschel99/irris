"""Voice, vision chain, watch rules, coach, reviews and the camera WebSocket, with fake engines and models."""
import asyncio
import io
import json

import pytest

JPEG_A = None


def jpeg(color=(40, 40, 40), size=(64, 48)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture()
def fake_vision(monkeypatch):
    """Every vision call answers from a script keyed by the prompt kind."""
    from irris import llm, vision
    answers = {"check": {"result": "pass", "say": "Clamps closed.", "reading": None},
               "look": {"say": "A press with the guard open.", "status": "warning", "reading": None},
               "watch": {"match": True, "say": "Someone's hand is inside the guard.", "confidence": 0.9},
               "coach": {"speak": True, "say": "Tighten the left clamp first.", "status": "warning"},
               "review": {"summary": "They opened the guard and fitted the mould.",
                          "timeline": [{"t": "0:00", "what": "guard opened"}], "readings": [],
                          "problems": [{"t": "0:02", "what": "no gloves", "severity": "warning"}]},
               "scan": {"slices": [{"t": "0:05", "flag": True, "why": "hands near the guard"}]},
               "review-gate": {"keep_problems": [0], "keep_timeline": [0], "summary": "They opened the guard and fitted the mould."},
               "read": {"value": 182, "unit": "C", "confidence": 0.9, "box": {"x": 0.2, "y": 0.2, "w": 0.4, "h": 0.3}},
               "caption": {"objects": [{"label": "press", "location": "left", "state": "guard open", "text_seen": ""}],
                           "scene": "a press"}}
    calls = []
    models = [llm.Model("http://gpu:11434/v1", "", "qwen-gpu"), llm.Model("http://127.0.0.1:11434/v1", "", "qwen-cpu")]

    async def fake_call(ms, messages, *, workspace_id="", kind="look", max_tokens=400, **_kw):
        calls.append(kind)
        vision.gate_count(workspace_id, True)
        info = {"model": "qwen-gpu", "where": "gpu", "ms": 12, "kind": kind, "at": 0, "fallback": False}
        vision._stats[workspace_id] = info
        return {"content": json.dumps(answers[kind])}, info

    monkeypatch.setattr(vision, "call", fake_call)
    monkeypatch.setattr(llm, "chain", lambda ws, kind: models)
    return answers, calls


def test_voice_settings_are_encrypted_and_fall_back(signed_in, monkeypatch):
    from irris import voice
    c = signed_in
    r = c.put("/api/v1/voice/settings", json={"tts": {"provider": "elevenlabs", "api_key": "xi-secret", "voice": "abc"},
                                              "stt": {"provider": "openai", "base_url": "https://api.groq.com/openai/v1",
                                                      "model": "whisper-large-v3", "api_key": "gsk-1"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tts"] == {"provider": "elevenlabs", "base_url": "", "model": "", "voice": "abc", "has_key": True}
    assert "xi-secret" not in r.text and "gsk-1" not in r.text
    assert c.put("/api/v1/voice/settings", json={"tts": {"provider": "nope"}}).status_code == 400

    async def broken(*a, **k):
        raise RuntimeError("401 from provider")

    monkeypatch.setattr(voice, "_premium_tts", broken)
    monkeypatch.setattr(voice, "_premium_stt", broken)
    monkeypatch.setattr(voice, "_kokoro_say", lambda text, v: voice.wav_bytes([0.0] * 2400, 24000))
    monkeypatch.setattr(voice, "_local_stt", lambda audio: "check the clamp")
    r = c.post("/api/v1/voice/tts", json={"text": "Step **two** passed [1]."})
    assert r.status_code == 200 and r.headers["x-voice-engine"] == "kokoro" and r.content[:4] == b"RIFF"
    assert "elevenlabs failed" in r.headers["x-voice-note"]
    r = c.post("/api/v1/voice/stt", files={"file": ("a.webm", b"\x1aE\xdf\xa3fake", "audio/webm")})
    assert r.json()["text"] == "check the clamp" and r.json()["engine"] == "whisper" and "openai failed" in r.json()["note"]
    test = c.post("/api/v1/voice/test", json={}).json()
    assert test["tts"]["ok"] and test["stt"]["text"] == "check the clamp"


def test_premium_voice_used_when_it_works(signed_in, monkeypatch):
    from irris import voice
    c = signed_in
    c.put("/api/v1/voice/settings", json={"tts": {"provider": "openai", "api_key": "sk", "voice": "nova"}})
    seen = {}

    async def premium(cfg, text, v):
        seen["key"] = voice.decrypt(cfg["api_key"])
        seen["text"] = text
        return b"ID3fake", "audio/mpeg"

    monkeypatch.setattr(voice, "_premium_tts", premium)
    r = c.post("/api/v1/voice/tts", json={"text": "Hello `there`"})
    assert r.headers["x-voice-engine"] == "openai" and r.headers["content-type"] == "audio/mpeg"
    assert seen == {"key": "sk", "text": "Hello there"}


def test_no_voice_engine_is_a_clear_503(signed_in, monkeypatch):
    from irris import voice
    monkeypatch.setattr(voice, "_kokoro_say", lambda t, v: (_ for _ in ()).throw(voice.VoiceError("kokoro unavailable")))
    monkeypatch.setattr(voice, "_piper_say", lambda t: (_ for _ in ()).throw(voice.VoiceError("piper unavailable")))
    r = signed_in.post("/api/v1/voice/tts", json={"text": "hi"})
    assert r.status_code == 503 and "kokoro unavailable" in r.json()["detail"]


def test_vision_chain_falls_back_and_reports_where(monkeypatch):
    from irris import llm, vision
    gpu, cpu = llm.Model("http://gpu:11434/v1", "", "qwen"), llm.Model("http://127.0.0.1:11434/v1", "", "qwen")
    monkeypatch.setattr(llm, "fallback", lambda kind: cpu)

    async def chat(m, messages, tools=None, **kw):
        if m is gpu:
            raise llm.ModelError("model unreachable: ConnectError")
        return {"content": '{"say": "ok", "status": "ok", "reading": {"value": 182, "unit": "C", "confidence": 0.8}}'}

    monkeypatch.setattr(llm, "chat", chat)
    llm._down.clear()
    out = asyncio.run(vision.look([gpu, cpu], jpeg(), "read it", workspace_id="w1"))
    assert out["where"] == "cpu" and out["reading"]["value"] == 182
    assert vision.stats("w1")["fallback"] is True
    llm._down.clear()


def test_reading_cross_check_flags_disagreement():
    from irris import perception, vision
    vlm = {"value": 182.0, "unit": "C", "confidence": 0.8}
    agree = vision.combine(vlm, {"value": 183.0, "unit": "C", "confidence": 0.7, "method": "ocr"}, {"min": 0, "max": 300})
    assert agree["method"] == "gauge-cv+vlm" and agree["value"] == 182.5
    disagree = vision.combine(vlm, {"value": 120.0, "unit": "C", "confidence": 0.7, "method": "ocr"}, {"min": 0, "max": 300})
    assert disagree["confidence"] == 0.3 and "Check the dial" in disagree["note"]
    assert perception.display_reading([{"text": "TEMP 182.4°C", "conf": 0.9}], "C")["value"] == 182.4
    assert perception.written_question([{"text": "what torque for M8?", "conf": 0.9}]) == "what torque for M8?"
    assert perception.change(perception.thumb(jpeg((0, 0, 0))), perception.thumb(jpeg((255, 255, 255)))) > 0.9


def test_run_checks_keep_evidence_and_push_verdicts(signed_in, fake_vision):
    from irris import vision
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Bench"}).json()
    p = c.post("/api/v1/procedures", json={"name": "Mould change", "steps": [
        {"instruction": "Close the clamps", "check": "four clamps closed"}, "Lock out"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"], "camera_id": cam["id"]}).json()
    q = vision.hub.subscribe(cam["id"])
    vision.hub.put(cam["id"], jpeg())
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={}).json()
    assert out["check"]["result"] == "pass" and out["check"]["where"] == "gpu" and out["check"]["snapshot"]
    assert out["run"]["steps"][0]["snapshot"] == out["check"]["snapshot"]
    assert c.get(f"/api/v1/files/{out['check']['snapshot']}").headers["content-type"] == "image/jpeg"
    pushed = q.get_nowait()
    assert pushed["type"] == "verdict" and pushed["next"] == "Lock out"
    run2 = c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"}).json()["run"]
    assert run2["status"] == "done" and run2["steps"][1]["snapshot"]          # manual check, camera live: evidence too
    batch = c.get("/api/v1/records", params={"type": "batch_record"}).json()["records"][0]
    steps = c.get(f"/api/v1/records/{batch['id']}").json()["data"]["steps"]
    assert all(s["snapshot"] for s in steps)
    st = c.get("/api/v1/vision/status").json()
    assert st["ready"] and st["last"]["model"] == "qwen-gpu"
    vision.hub.unsubscribe(cam["id"], q)


def test_watch_rules_fire_once_then_cool_down(signed_in, fake_vision):
    from irris import vision, watch
    from irris.db import get_db
    answers, calls = fake_vision
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Press 7"}).json()
    assert c.post("/api/v1/watch", json={"camera_id": cam["id"], "condition": "x"}).status_code == 400
    rule = c.post("/api/v1/watch", json={"camera_id": cam["id"], "condition": "a hand inside the guard",
                                         "interval_s": 5, "severity": "danger", "speak": True}).json()
    assert rule["interval_s"] == 5 and rule["enabled"]
    db = get_db()
    vision.hub.put(cam["id"], jpeg((10, 10, 10)))
    out = asyncio.run(watch.check_rule(db, db.one("SELECT * FROM watch_rules WHERE id = ?", [rule["id"]])))
    assert out["fired"] is True
    events = c.get("/api/v1/records", params={"type": "event"}).json()
    assert events["total"] == 1 and events["records"][0]["source"] == "watch/Press 7"
    al = c.get("/api/v1/alerts").json()["alerts"]
    assert al[-1]["severity"] == "danger" and al[-1]["snapshot"] and al[-1]["speak"]
    # same scene: skipped without a model call; changed scene but still true: no second alert (cooldown)
    n = len(calls)
    assert asyncio.run(watch.check_rule(db, db.one("SELECT * FROM watch_rules WHERE id = ?", [rule["id"]]))) is None
    assert len(calls) == n
    vision.hub.put(cam["id"], jpeg((240, 240, 240)))
    again = asyncio.run(watch.check_rule(db, db.one("SELECT * FROM watch_rules WHERE id = ?", [rule["id"]])))
    assert again["matched"] and not again["fired"]
    r = c.patch(f"/api/v1/watch/{rule['id']}", json={"enabled": False}).json()
    assert r["enabled"] is False and r["checks"] == 2 and r["matches"] == 2
    assert c.post(f"/api/v1/watch/{rule['id']}/test").json()["matched"] is True
    c.delete(f"/api/v1/watch/{rule['id']}")
    assert c.get("/api/v1/watch").json()["rules"] == []


def test_coach_and_camera_state(signed_in, fake_vision):
    from irris import vision, watch
    from irris.db import get_db
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Bench"}).json()
    st = c.put(f"/api/v1/cameras/{cam['id']}/state", json={"goal": "Fit the mould", "coach_s": 3}).json()
    assert st == {"goal": "Fit the mould", "coach_s": 10, "notice": False}
    vision.hub.put(cam["id"], jpeg())
    db = get_db()
    asyncio.run(watch._coach_tick(db, db.one("SELECT * FROM camera_state WHERE camera_id = ?", [cam["id"]])))
    al = c.get("/api/v1/alerts").json()["alerts"]
    assert al[-1]["kind"] == "coach" and al[-1]["say"] == "Tighten the left clamp first."
    assert c.get(f"/api/v1/cameras/{cam['id']}/state").json()["goal"] == "Fit the mould"


def test_what_happened_review_job(signed_in, fake_vision):
    from irris import vision, watch
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Bench"}).json()
    assert c.post(f"/api/v1/cameras/{cam['id']}/review", json={}).status_code == 409
    import time
    hist = vision.hub._history.setdefault(cam["id"], __import__("collections").deque())
    for i in range(6):
        hist.append((time.time() - 30 + i * 5, jpeg((i * 30, 0, 0))))

    async def go():
        from irris.db import get_db
        from irris import store
        db = get_db()
        ws = db.one("SELECT * FROM workspaces")
        frames = vision.hub.history(cam["id"], 90)
        return await watch.review_job(db, ws, frames, title="Bench: last 90 s", camera="Bench",
                                      progress=lambda f, n: asyncio.sleep(0))

    out = asyncio.run(go())
    assert out["summary"].startswith("They opened") and out["problems"][0]["what"] == "no gloves"
    rec = c.get(f"/api/v1/records/{out['record_id']}").json()
    assert rec["type"] == "video_review" and len(rec["data"]["snapshots"]) >= 1
    job = c.post(f"/api/v1/cameras/{cam['id']}/review", json={"seconds": 60, "question": "did they wear gloves?"})
    assert job.status_code == 200 and job.json()["status"] == "running"


def test_snapshot_and_read_instrument(signed_in, fake_vision):
    from irris import vision
    answers, _ = fake_vision
    answers["look"] = {"say": "182 C", "status": "ok", "reading": {"value": 182, "unit": "C", "confidence": 0.9}}
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Oven"}).json()
    assert c.post(f"/api/v1/cameras/{cam['id']}/snapshot").status_code == 409
    vision.hub.put(cam["id"], jpeg())
    snap = c.post(f"/api/v1/cameras/{cam['id']}/snapshot").json()
    assert c.get(f"/api/v1/records/{snap['record_id']}").json()["type"] == "image"
    out = c.post(f"/api/v1/cameras/{cam['id']}/read", json={"what": "the oven display", "subject": "asset:oven-2"}).json()
    assert out["reading"]["value"] == 182 and out["record_id"]
    assert c.get("/api/v1/records", params={"type": "reading", "subject": "asset:oven-2"}).json()["total"] == 1


def test_camera_websocket(signed_in):
    from irris import vision
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Phone"}).json()
    tok = cam["pair_url"].split("t=")[1]
    frame = jpeg()
    with c.websocket_connect(f"/api/v1/cam/ws?t={tok}") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_bytes(frame)
        ws.send_text(json.dumps({"type": "ping"}))
        assert ws.receive_json()["type"] == "pong"
        assert vision.hub.get(cam["id"]) == frame
        vision.hub.publish(cam["id"], {"type": "alert", "say": "stop"})
        assert ws.receive_json()["say"] == "stop"
    ws_id = c.get("/api/v1/me").json()["workspaces"][0]["id"]
    with c.websocket_connect(f"/api/v1/cam/ws?camera={cam['id']}&ws={ws_id}") as ws:      # signed-in browser
        assert ws.receive_json()["camera"] == "Phone"
    with pytest.raises(Exception):
        with c.websocket_connect("/api/v1/cam/ws?t=cam_wrong") as ws:
            ws.receive_json()


def test_media_mcp_tools(signed_in, fake_vision, monkeypatch):
    from irris import vision, voice
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Bench"}).json()
    key = c.post("/api/v1/keys", json={"name": "agent"}).json()["key"]
    vision.hub.put(cam["id"], jpeg())
    h = {"Authorization": f"Bearer {key}"}

    def call(name, args):
        return c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                               "params": {"name": name, "arguments": args}}).json()["result"]

    names = {t["name"] for t in c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]}
    assert {"snapshot", "watch", "what_happened", "coach", "speak", "read_instrument"} <= names
    snap = call("snapshot", {"camera_id": cam["id"]})
    assert snap["content"][0]["type"] == "image"
    rule = call("watch", {"camera_id": cam["id"], "condition": "smoke above the oven"})
    assert "smoke above the oven" in rule["content"][0]["text"]
    monkeypatch.setattr(voice, "_kokoro_say", lambda text, v: voice.wav_bytes([0.0] * 100, 24000))
    assert call("speak", {"text": "hello"})["content"][0]["type"] == "audio"
    assert call("snapshot", {"camera_id": "nope"})["isError"] is True


def make_clip() -> bytes:
    import av
    import numpy as np
    buf = io.BytesIO()
    out = av.open(buf, "w", format="mp4")
    st = out.add_stream("mpeg4", rate=10)
    st.width, st.height, st.pix_fmt = 64, 48, "yuv420p"
    for i in range(20):
        frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), i * 12, dtype=np.uint8), format="rgb24")
        for p in st.encode(frame):
            out.mux(p)
    for p in st.encode():
        out.mux(p)
    out.close()
    return buf.getvalue()


def test_recording_attaches_to_run_and_is_reviewed(signed_in, fake_vision, tmp_path, monkeypatch):
    import time
    monkeypatch.setenv("FILES_DIR", str(tmp_path / "files"))
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Bench"}).json()
    p = c.post("/api/v1/procedures", json={"name": "Mould change", "steps": ["Open", "Close"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"], "camera_id": cam["id"]}).json()
    clip = make_clip()
    r = c.post("/api/v1/recordings", data={"run_id": run["id"], "camera_id": cam["id"], "question": "gloves?"},
               files={"file": ("clip.mp4", clip, "video/mp4")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["run_id"] == run["id"] and out["job"]["status"] == "running"
    state = c.get(f"/api/v1/runs/{run['id']}").json()
    assert state["recordings"][0]["file_id"] == out["file_id"] and state["recordings"][0]["step"] == 1
    whole = c.get(out["url"])
    assert whole.status_code == 200 and whole.content == clip
    part = c.get(out["url"], headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100 and part.headers["content-range"].startswith("bytes 0-99/")
    for _ in range(50):
        job = c.get(f"/api/v1/media/jobs/{out['job']['id']}").json()
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "done", job
    assert c.get(f"/api/v1/records/{job['result']['record_id']}").json()["type"] == "video_review"
    # a paired phone's clip lands on the camera's active run
    tok = cam["pair_url"].split("t=")[1]
    r2 = c.post(f"/api/v1/cam/recording?t={tok}", data={"review": "false"}, files={"file": ("c.mp4", clip, "video/mp4")})
    assert r2.json()["run_id"] == run["id"] and "job" not in r2.json()
    assert c.post("/api/v1/recordings", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 415
