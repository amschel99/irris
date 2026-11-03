"""The small-VLM upgrades: the change gate, verified reads, verify-before-advance, self-captions, observation facts,
retrieve -> perceive -> review with caps, hierarchical summaries, per-model tweaks."""
import asyncio
import io
import json
import time

from tests.test_media import fake_vision, jpeg  # noqa: F401 - fixture reuse


def _ws(c):
    return c.get("/api/v1/me").json()["workspaces"][0]["id"]


def test_gate_skips_static_scene_auto_checks(signed_in, fake_vision):
    from irris import vision
    answers, calls = fake_vision
    answers["check"] = {"result": "unsure", "say": "Not done yet.", "reading": None}
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Bench"}).json()
    p = c.post("/api/v1/procedures", json={"name": "Fit", "steps": ["Close the clamps", "Lock out"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"], "camera_id": cam["id"]}).json()
    vision.hub.put(cam["id"], jpeg((30, 30, 30)))
    skipped = 0
    for _ in range(30):                                      # 30 auto-checks of a bench where nothing moves
        out = c.post(f"/api/v1/runs/{run['id']}/check/auto", json={}).json()
        skipped += bool(out["check"].get("skipped"))
    assert calls.count("check") == 1 and skipped == 29
    vision.hub.put(cam["id"], jpeg((220, 220, 220)))         # the scene changes: the model looks again
    c.post(f"/api/v1/runs/{run['id']}/check/auto", json={})
    assert calls.count("check") == 2
    g = c.get("/api/v1/vision/status").json()["gate"]
    assert g["skipped"] == 29 and g["called"] >= 2
    print(f"\nstatic scene: 30 auto-checks -> 1 model call, 29 avoided ({29 / 30:.0%})")


def test_verified_reading_never_passes_on_disagreement(signed_in, fake_vision, monkeypatch):
    from irris import vision
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Oven"}).json()
    p = c.post("/api/v1/procedures", json={"name": "Bake", "steps": [
        {"instruction": "Check the oven", "reading": {"label": "oven", "min": 170, "max": 190, "unit": "C"}}, "Unload"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"], "camera_id": cam["id"]}).json()
    vision.hub.put(cam["id"], jpeg())
    seq = iter([182, 120, 150])                               # three reads that never agree

    async def flaky(models, image, *, workspace_id, hint="", think=False, **kw):
        return {"reading": {"value": float(next(seq)), "unit": "C", "confidence": 0.9}, "box": None,
                "info": {"model": "m", "where": "gpu", "ms": 5}}

    monkeypatch.setattr(vision, "_read_once", flaky)
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={}).json()
    assert out["check"]["result"] == "unsure" and out["check"]["verified"] is False and out["run"]["step"] == 0
    seq2 = iter([181, 181.2])                                 # two agreeing, confident reads: verified, the step passes

    async def steady(models, image, *, workspace_id, hint="", think=False, **kw):
        return {"reading": {"value": float(next(seq2)), "unit": "C", "confidence": 0.9},
                "box": {"x": .1, "y": .1, "w": .3, "h": .2}, "info": {"model": "m", "where": "gpu", "ms": 5}}

    monkeypatch.setattr(vision, "_read_once", steady)
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={}).json()
    assert out["check"]["result"] == "pass" and out["check"]["verified"] is True and out["run"]["step"] == 1
    assert out["check"]["reading"]["value"] == 181.1


def test_ocr_conflict_makes_a_reading_unsure(monkeypatch):
    from irris import llm, perception, vision
    m = [llm.Model("http://gpu/v1", "", "qwen")]

    async def read(models, image, *, workspace_id, hint="", think=False, **kw):
        return {"reading": {"value": 182.0, "unit": "C", "confidence": 0.9}, "box": None,
                "info": {"model": "q", "where": "gpu", "ms": 1}}

    monkeypatch.setattr(vision, "_read_once", read)
    monkeypatch.setattr(perception, "text_box", lambda j: None)
    monkeypatch.setattr(perception, "ocr", lambda j: [{"text": "128.0 C", "conf": 0.95}])
    out = asyncio.run(vision.read_verified(m, jpeg(), {"min": 0, "max": 300, "unit": "C"}))
    assert out["verified"] is False and "128" in out["note"]
    monkeypatch.setattr(perception, "ocr", lambda j: [{"text": "182.1 C", "conf": 0.95}])
    out = asyncio.run(vision.read_verified(m, jpeg(), {"min": 0, "max": 300, "unit": "C"}))
    assert out["verified"] and out["reading"]["method"] == "vlm+ocr"


def test_skips_and_unfinished_steps_are_deviations(signed_in):
    c = signed_in
    p = c.post("/api/v1/procedures", json={"name": "Three", "steps": ["A", "B", "C"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"]}).json()
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "skip"})
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"})
    c.post(f"/api/v1/runs/{run['id']}/stop")
    batch = c.get("/api/v1/records", params={"type": "batch_record"}).json()["records"][0]
    devs = c.get(f"/api/v1/records/{batch['id']}").json()["data"]["deviations"]
    assert [d["result"] for d in devs] == ["skip", "not done"]


def test_spatial_questions_use_a_cached_self_caption(fake_vision, monkeypatch):
    from irris import llm, vision
    answers, calls = fake_vision
    seen = []

    async def spy(ms, messages, *, workspace_id="", kind="look", max_tokens=400, **kw):
        calls.append(kind)
        if kind == "look":
            seen.append(messages[1]["content"][0]["text"])
        return {"content": json.dumps(answers[kind])}, {"model": "m", "where": "gpu", "ms": 1}

    monkeypatch.setattr(vision, "call", spy)
    vision._captions.clear()
    m = [llm.Model("http://gpu/v1", "", "m")]
    f = jpeg((5, 6, 7))
    asyncio.run(vision.look(m, f, "Where is the press?"))
    asyncio.run(vision.look(m, f, "Which side is the guard on?"))
    asyncio.run(vision.look(m, f, "Is it safe?"))              # not spatial: no caption
    assert calls.count("caption") == 1 and "press at left (guard open)" in seen[0] and "caption" not in seen[2]


def test_observation_facts_merge_and_answer_without_a_model(signed_in):
    from irris import observe
    from irris.db import get_db
    c = signed_in
    db, ws = get_db(), _ws(c)
    t0 = time.time() - 600
    for i in range(5):                                       # the same fact five times: one row, extended
        observe.record(db, ws, camera_id="cam1", subject="guard", relation="state", value="open", t=t0 + i * 10)
    observe.record(db, ws, camera_id="cam1", subject="guard", relation="state", value="closed", t=t0 + 100)
    observe.record(db, ws, camera_id="cam1", subject="Platen", relation="reading", value=182.4, unit="C", t=t0 + 200)
    observe.record(db, ws, camera_id="cam1", subject="Platen", relation="reading", value=186.0, unit="C", t=t0 + 300)
    rows = db.execute("SELECT * FROM observations WHERE workspace_id = ? AND value = 'open'", [ws])
    assert len(rows) == 1 and rows[0]["n"] == 5 and rows[0]["t_end"] - rows[0]["t_start"] == 40
    last = c.get("/api/v1/observations", params={"q": "last reading of the platen"}).json()
    assert last["kind"] == "last_reading" and last["facts"][0]["value"] == "186.0" and last["relaxed"] == 0
    when = c.get("/api/v1/observations", params={"q": "when was the guard closed?"}).json()
    assert when["kind"] == "when" and when["facts"][0]["value"] == "closed"
    relaxed = c.get("/api/v1/observations", params={"q": "last reading of the platen thermocouple"}).json()
    assert relaxed["facts"] and relaxed["relaxed"] == 1          # strict missed; one word fewer found it
    lt = time.localtime(t0 + 300 + 240)                          # 4 minutes after the last fact: needs a wider window
    tz = lt.tm_gmtoff // 60
    at = c.get("/api/v1/observations", params={"q": f"what happened at {lt.tm_hour:02d}:{lt.tm_min:02d}",
                                               "tz_offset_min": tz}).json()
    assert at["kind"] == "at" and at["facts"] and at["relaxed"] >= 1


def test_frames_are_chosen_by_change_and_stamped():
    from PIL import Image
    from irris import perception
    frames = [(float(i), jpeg((10, 10, 10))) for i in range(30)]
    frames[12] = (12.0, jpeg((250, 250, 250)))
    frames[25] = (25.0, jpeg((120, 0, 0)))
    picked = [t for t, _ in perception.select_by_change(frames, 5)]
    assert 0.0 in picked and 29.0 in picked and 12.0 in picked and 25.0 in picked and picked == sorted(picked)
    stamped = perception.stamp(jpeg(size=(640, 480)), "t=01:15")
    assert max(Image.open(io.BytesIO(stamped)).size) == 448


def test_review_respects_the_call_cap_and_gates_its_findings(fake_vision, monkeypatch):
    from irris import llm, vision
    answers, calls = fake_vision
    answers["review"] = {"summary": "s", "timeline": [{"t": "0:05", "what": "guard opened"}],
                         "problems": [{"t": "0:05", "what": "no gloves"}, {"t": "0:09", "what": "made up"}]}
    answers["review-gate"] = {"keep_problems": [0], "keep_timeline": [0], "summary": "Guard opened without gloves."}
    frames = [(1000.0 + i, jpeg(((i * 37) % 255, 20, 20))) for i in range(120)]
    out = asyncio.run(vision.review([llm.Model("http://gpu/v1", "", "m")], frames, "gloves?"))
    assert out["calls"] <= vision.REVIEW_MAX_CALLS and out["frames"] <= 16 and out["scanned"] <= 24
    assert [p["what"] for p in out["problems"]] == ["no gloves"] and out["gated"] and calls.count("scan") == 1
    monkeypatch.setattr(vision, "REVIEW_MAX_CALLS", 2)
    calls.clear()
    out = asyncio.run(vision.review([llm.Model("http://gpu/v1", "", "m")], frames))
    assert out["capped"] == "call cap" and len(calls) <= 2


def test_hierarchical_shift_summaries(signed_in):
    from irris import observe
    from irris.db import get_db
    db, ws = get_db(), _ws(signed_in)
    for i in range(17):
        observe.add_summary(db, ws, "cam1", f"part {i}", 1000 + i * 60, 1000 + i * 60 + 59)

    async def merge(texts):
        return "merged " + ",".join(t.split()[-1] for t in texts)

    n = asyncio.run(observe.consolidate(db, ws, "cam1", merge))
    left = observe.summaries(db, ws, "cam1")
    assert n == 2 and [s["level"] for s in left].count(1) == 2 and [s["level"] for s in left].count(0) == 1
    assert left[0]["text"] == "merged 0,1,2,3,4,5,6,7"


def test_model_tweaks_live_in_one_place(monkeypatch):
    from irris import llm, vision
    seen = {}

    async def chat(m, messages, tools=None, **kw):
        seen["system"] = messages[0]["content"]
        seen["json"] = kw.get("json_mode")
        return {"content": "{}"}

    monkeypatch.setattr(llm, "chat", chat)
    llm._down.clear()
    asyncio.run(vision.call([llm.Model("http://x/v1", "", "qwen3.5:2b")], [{"role": "system", "content": "S"},
                                                                           {"role": "user", "content": "u"}]))
    assert seen["system"].endswith("Do not think out loud.") and seen["json"] is True
    assert vision.tweaks("qwen3-vl:8b")["box_scale"] == 1000.0
