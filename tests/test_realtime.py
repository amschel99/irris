"""Real-time behaviour: people before background work, background work never queues, interactive looks time out,
one model call per look, the on-device scene reaches the model, live OCR readings are pushed to watchers."""
import asyncio
import io
import json

import pytest

from irris import llm, vision

CPU = llm.Model("http://127.0.0.1:11434/v1", "", "qwen3.5:2b")


@pytest.fixture(autouse=True)
def cpu_fallback(monkeypatch):
    monkeypatch.setattr(llm, "fallback", lambda kind: CPU if kind == "vlm" else llm.Model("", "", ""))
    vision._cpu_gate = vision.PriorityGate()
    llm._down.clear()
    yield
    llm._down.clear()


def jpeg(color=(200, 200, 200)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG")
    return buf.getvalue()


def fake_chat(delay: float, log: list):
    async def chat(m, messages, tools=None, **kw):
        log.append(("start", kw.get("max_tokens"), messages))
        await asyncio.sleep(delay)
        log.append(("end",))
        return {"content": json.dumps({"say": "A gauge reading 42.", "status": "ok", "match": False, "speak": False,
                                       "result": "pass", "confidence": 0.9, "reading": None})}
    return chat


def test_background_is_skipped_while_the_model_is_busy(monkeypatch):
    log = []
    monkeypatch.setattr(llm, "chat", fake_chat(0.3, log))

    async def go():
        person = asyncio.create_task(vision.look([CPU], jpeg(), "what is this?", workspace_id="w"))
        await asyncio.sleep(0.05)
        with pytest.raises(vision.VisionBusy):
            await vision.watch_check([CPU], jpeg(), "a person without gloves", workspace_id="w")
        with pytest.raises(vision.VisionBusy):
            await vision.coach([CPU], jpeg(), "solder the board", workspace_id="w")
        return await person
    out = asyncio.run(go())
    assert out["say"].startswith("A gauge") and sum(1 for e in log if e[0] == "start") == 1


def test_people_go_before_background_work_that_waits(monkeypatch):
    order = []

    async def chat(m, messages, tools=None, **kw):
        order.append(messages[0]["content"].splitlines()[0])
        await asyncio.sleep(0.1)
        return {"content": "{}"}
    monkeypatch.setattr(llm, "chat", chat)

    async def go():
        first = asyncio.create_task(vision.call([CPU], [{"role": "system", "content": "first person"}]))
        await asyncio.sleep(0.02)
        review = asyncio.create_task(vision.call([CPU], [{"role": "system", "content": "review frames"}], background="wait"))
        await asyncio.sleep(0.02)
        second = asyncio.create_task(vision.call([CPU], [{"role": "system", "content": "second person"}]))
        await asyncio.gather(first, review, second)
    asyncio.run(go())
    assert order == ["first person", "second person", "review frames"]


def test_an_interactive_look_times_out_and_frees_the_model(monkeypatch):
    log = []
    monkeypatch.setattr(llm, "chat", fake_chat(1.0, log))

    async def go():
        with pytest.raises(vision.VisionTimeout):
            await vision.call([CPU], [{"role": "system", "content": "x"}], timeout=0.2)
        assert not vision.busy_cpu() and vision.queue_depth() == 0
    asyncio.run(go())


def test_a_cancelled_waiter_doesnt_hold_the_gate(monkeypatch):
    monkeypatch.setattr(llm, "chat", fake_chat(0.2, []))

    async def go():
        a = asyncio.create_task(vision.call([CPU], [{"role": "system", "content": "a"}]))
        await asyncio.sleep(0.02)
        b = asyncio.create_task(vision.call([CPU], [{"role": "system", "content": "b"}]))
        await asyncio.sleep(0.02)
        b.cancel()
        await a
        with pytest.raises(asyncio.CancelledError):
            await b
        await asyncio.sleep(0)
        assert not vision.busy_cpu() and vision.queue_depth() == 0
    asyncio.run(go())


def test_a_look_is_one_short_call_with_the_on_device_scene(monkeypatch):
    log = []
    monkeypatch.setattr(llm, "chat", fake_chat(0.0, log))
    out = asyncio.run(vision.look([CPU], jpeg(), "Where is the screwdriver?", workspace_id="w",
                                  scene="hands: 2 moving · holding: scissors · objects: scissors 0.81"))
    starts = [e for e in log if e[0] == "start"]
    assert len(starts) == 1 and starts[0][1] == 150          # no self-caption call on the CPU model
    text = starts[0][2][1]["content"][0]["text"]
    assert "holding: scissors" in text and out["where"] == "cpu"


def test_live_ocr_pushes_readings_to_watchers(monkeypatch):
    from irris import perception
    monkeypatch.setattr(perception, "ocr", lambda img, *a, **k: [{"text": "182.4", "conf": 0.95, "box": [0, 0, 1, 1]},
                                                                   {"text": "°C", "conf": 0.9, "box": [0, 0, 1, 1]}])
    monkeypatch.setattr(perception, "display_reading",
                        lambda texts, unit="": {"value": 182.4, "unit": "°C", "confidence": 0.95})

    async def go():
        vision.live_ocr = vision.LiveOCR()
        q = vision.hub.subscribe("cam-ocr")
        try:
            vision.hub.put("cam-ocr", jpeg())
            msg = await asyncio.wait_for(q.get(), 3)
            vision.hub.put("cam-ocr", jpeg())                   # same picture, within a second: no second OCR
            await asyncio.sleep(0.2)
            assert q.empty()
            return msg
        finally:
            vision.hub.unsubscribe("cam-ocr", q)
    msg = asyncio.run(go())
    assert msg["type"] == "reading" and msg["value"] == 182.4 and msg["source"] == "ocr"


def test_auto_check_says_busy_instead_of_queueing(signed_in, monkeypatch):
    c = signed_in
    p = c.post("/api/v1/procedures", json={"name": "Check", "steps": ["Look at the bench"]}).json()
    cam = c.post("/api/v1/cameras", json={"kind": "phone"}).json()
    tok = cam["pair_url"].split("t=")[1]
    c.post("/api/v1/cam/frame", params={"t": tok}, content=jpeg())
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"], "camera_id": cam["id"]}).json()
    monkeypatch.setattr(llm, "chain", lambda ws, kind: [CPU])

    async def busy(*a, **k):
        raise vision.VisionBusy("busy")
    from irris import runs
    monkeypatch.setattr(vision, "check_step", busy)
    r = c.post(f"/api/v1/runs/{run['id']}/check/auto", json={"camera_id": cam["id"], "scene": "hands: none"})
    assert r.status_code == 200 and r.json()["check"]["result"] == "busy"
    st = c.get("/api/v1/vision/status").json()
    assert "expected_s" in st and "queue" in st
