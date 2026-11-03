"""Guidance v2 (docs/GUIDANCE_SPEC.md): tracker rules, voted checklists, evidence/abstention, instruments,
learning from runs, drafted checks, recovery plans and speech suggestions."""
import asyncio
import io
import json
import math

import pytest


def jpeg(color=(40, 40, 40), size=(64, 48)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def textured(size=(640, 480), blur=0) -> bytes:
    """A frame with real detail (a checkerboard), optionally blurred."""
    from PIL import Image, ImageDraw, ImageFilter
    img = Image.new("RGB", size, (200, 200, 200))
    d = ImageDraw.Draw(img)
    for x in range(0, size[0], 32):
        for y in range(0, size[1], 32):
            if (x // 32 + y // 32) % 2:
                d.rectangle([x, y, x + 31, y + 31], fill=(30, 30, 30))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def gauge(value, vmin=0.0, vmax=300.0, size=480) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (size, size), (90, 90, 95))
    d = ImageDraw.Draw(img)
    c, R = size / 2, size * 0.42
    d.ellipse([c - R, c - R, c + R, c + R], fill=(235, 235, 230), outline=(30, 30, 30), width=4)
    for k in range(31):
        a = math.radians(135 + 270 * k / 30)
        r0 = R * 0.85 if k % 5 else R * 0.78
        d.line([c + r0 * math.cos(a), c + r0 * math.sin(a), c + R * .95 * math.cos(a), c + R * .95 * math.sin(a)],
               fill=(40, 40, 40), width=2)
    a = math.radians(135 + 270 * (value - vmin) / (vmax - vmin))
    d.line([c - .12 * R * math.cos(a), c - .12 * R * math.sin(a), c + .8 * R * math.cos(a), c + .8 * R * math.sin(a)],
           fill=(20, 20, 20), width=6)
    d.ellipse([c - 10, c - 10, c + 10, c + 10], fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


CALIB = {"center": [0.5, 0.5],
         "min": {"point": [0.5 + 0.42 * math.cos(math.radians(135)), 0.5 + 0.42 * math.sin(math.radians(135))], "value": 0},
         "max": {"point": [0.5 + 0.42 * math.cos(math.radians(45)), 0.5 + 0.42 * math.sin(math.radians(45))], "value": 300}}


@pytest.fixture()
def scripted(monkeypatch):
    """vision.call answers from a queue per kind (the last answer repeats); records every call."""
    from irris import llm, vision
    script: dict[str, list] = {"check": [{"answers": [{"i": 1, "a": "yes"}], "say": "Looks done."}]}
    calls: list[dict] = []
    models = [llm.Model("http://gpu:11434/v1", "", "qwen-gpu")]

    async def fake_call(ms, messages, *, workspace_id="", kind="look", temperature=0.2, **_kw):
        q = script.get(kind) or [{}]
        ans = q.pop(0) if len(q) > 1 else q[0]
        calls.append({"kind": kind, "temperature": temperature, "messages": messages})
        info = {"model": "qwen-gpu", "where": "gpu", "ms": 7, "kind": kind, "at": 0, "fallback": False}
        return {"content": json.dumps(ans)}, info

    monkeypatch.setattr(vision, "call", fake_call)
    monkeypatch.setattr(llm, "chain", lambda ws, kind: models)
    return script, calls


def _run(c, steps, camera=True):
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Bench"}).json() if camera else None
    p = c.post("/api/v1/procedures", json={"name": "Assembly", "steps": steps}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"], **({"camera_id": cam["id"]} if cam else {})}).json()
    return p, run, cam


# ================================================================ 1. the tracker validates what the model proposes
def test_tracker_rules():
    from irris import guidance
    st = {"plan": [], "steps": {}}
    st = {"plan": [dict(guidance.normalize_step({"instruction": t}, i), uid=f"s{i}",
                        after=[f"s{i - 1}"] if i else []) for i, t in enumerate(["A", "B", "C"])],
          "steps": {f"s{i}": guidance._new_step_state() for i in range(3)}, "say": [], "phase": "working",
          "metrics": {"vision_checks": 0, "abstained": 0, "overrides": 0, "readings": []}, "last_activity": 0}
    ev = guidance.apply_result(st, 1, "pass", by="vision", explicit=True, t=100)          # B before A: rejected
    assert ev["rejected"] and ev["deviation"] == "order" and st["steps"]["s1"]["status"] == "in_progress"
    ev = guidance.apply_result(st, 0, "pass", by="vision", explicit=False, t=100)         # auto: needs a 2nd pass
    assert ev["confirming"] and st["steps"]["s0"]["status"] == "in_progress"
    ev = guidance.apply_result(st, 0, "pass", by="vision", explicit=False, t=101.5)       # too soon
    assert not ev["completed"]
    ev = guidance.apply_result(st, 0, "pass", by="vision", explicit=False, t=104)         # >= 3 s after the first
    assert ev["completed"] and guidance.focus(st) == 1
    ev = guidance.apply_result(st, 0, "fail", by="vision", explicit=True, t=105)          # never reopened by the model
    assert ev["ignored"] and st["steps"]["s0"]["status"] == "complete"
    ev = guidance.apply_result(st, 1, "fail", by="vision", explicit=False, t=106)         # one auto fail: not confirmed
    assert ev["deviation"] == "error" and not ev["confirmed_error"]
    ev = guidance.apply_result(st, 1, "fail", by="vision", explicit=False, t=110)
    assert ev["confirmed_error"]
    guidance.apply_result(st, 1, "pass", by="ada@plant.co", explicit=True, t=120)         # a person overrides the fail
    assert st["metrics"]["overrides"] == 1 and guidance.metrics(st)["false_alarms"] == 1
    guidance.update_phase(st, camera_changed=False, t=120 + guidance.IDLE_S + 1)
    assert st["phase"] == "idle"                                                          # idle, never a deviation
    guidance.update_phase(st, camera_changed=True, t=120 + guidance.IDLE_S + 2)
    assert st["phase"] == "working"


def test_settles_after_three_agreeing_looks():
    from irris import guidance
    st = {"plan": [dict(guidance.normalize_step({"instruction": "A"}, 0), uid="s0", after=[])],
          "steps": {"s0": guidance._new_step_state()}, "say": [], "phase": "working", "last_activity": 0,
          "metrics": {"vision_checks": 0, "abstained": 0, "overrides": 0, "readings": []}}
    for k in range(3):
        guidance.apply_result(st, 0, "unsure", by="vision", explicit=False, t=10 + k, confidence=0.9)
    assert st["steps"]["s0"]["settled"]


def test_progress_from_votes_and_time():
    from irris import guidance
    p = guidance.normalize_step({"instruction": "Fit", "questions": [{"q": "a?"}, {"q": "b?"}], "median_s": 100}, 0)
    st = {"plan": [dict(p, uid="s0", after=[])], "steps": {"s0": guidance._new_step_state()}}
    st["steps"]["s0"].update(status="in_progress", started_at=0,
                             votes={"a?": {"answer": "yes"}, "b?": {"answer": "unsure"}})
    assert guidance.progress(st, 0, t=10) == 0.45                      # 1 of 2 musts: 0.9 * 0.5
    assert guidance.progress(st, 0, t=90) == 0.81                      # 90% of the median time
    st["steps"]["s0"]["status"] = "complete"
    assert guidance.progress(st, 0) == 1.0


def test_default_questions_and_old_procedures(signed_in):
    from irris import guidance
    qs = guidance.default_questions({"instruction": "Fit", "check": "four clamps closed and the guard down"})
    assert [q["q"] for q in qs] == ["Can you see this: four clamps closed?", "Can you see this: the guard down?"]
    p, run, _ = _run(signed_in, ["Lock out", {"instruction": "Check temp", "reading": {"min": 1, "max": 2}}], camera=False)
    assert "questions" not in p["steps"][0]                            # stored as saved
    st = run["steps"]
    assert st[0]["questions"] and st[0]["kind"] == "action" and st[1]["kind"] == "measurement"
    assert st[1]["prereqs"] == [0] and st[0]["status"] == "not_started" and run["phase"] == "working"


# ================================================================ 2. checklists: yes/no questions, voted
def test_vote_rules():
    from irris import guidance
    qs = [{"q": "a?", "must": True}, {"q": "b?", "must": True}, {"q": "c?", "must": False}]
    assert guidance.vote([["yes", "yes", "no"]] * 3, qs)["result"] == "pass"            # a non-must "no" is fine
    assert guidance.vote([["yes", "no", "yes"], ["yes", "no", "yes"], ["yes", "yes", "yes"]], qs)["result"] == "fail"
    out = guidance.vote([["yes", "unsure", "yes"], ["yes", "yes", "yes"], ["no", "unsure", "yes"]], qs)
    assert out["result"] == "unsure" and out["votes"]["a?"]["yes"] == 2


def test_explicit_check_votes_three_samples_and_auto_previews_one(signed_in, scripted):
    from irris import vision
    script, calls = scripted
    c = signed_in
    _, run, cam = _run(c, [{"instruction": "Close the clamps", "questions": [{"q": "Are all four clamps closed?"},
                                                                             {"q": "Is the guard down?"}]}, "Lock out"])
    script["check"] = [{"answers": [{"i": 1, "a": "yes"}, {"i": 2, "a": "yes"}]},
                       {"answers": [{"i": 1, "a": "yes"}, {"i": 2, "a": "no"}]},
                       {"answers": [{"i": 1, "a": "yes"}, {"i": 2, "a": "yes"}]}]
    vision.hub.put(cam["id"], jpeg())
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={}).json()
    assert len(calls) == 3 and all(x["temperature"] == 0.7 for x in calls)
    assert out["check"]["result"] == "pass" and out["check"]["votes"]["Is the guard down?"]["no"] == 1
    assert out["run"]["steps"][0]["status"] == "complete" and out["run"]["step"] == 1
    assert out["run"]["steps"][0]["votes"]["Are all four clamps closed?"]["answer"] == "yes"
    calls.clear()
    script["check"] = [{"answers": [{"i": 1, "a": "yes"}]}]
    vision.hub.put(cam["id"], jpeg((200, 200, 200)))
    out = c.post(f"/api/v1/runs/{run['id']}/check/auto", json={}).json()
    assert len(calls) == 1 and calls[0]["temperature"] == 0.2              # a preview: one sample
    assert out["check"]["result"] == "pass" and out["run"]["steps"][1]["status"] == "in_progress"   # needs a 2nd pass


def test_two_identical_samples_stop_early_and_danger_wins(signed_in, scripted):
    from irris import guidance, llm
    script, calls = scripted
    step = guidance.normalize_step({"instruction": "Weld", "questions": [{"q": "Is the mask down?"}]}, 0)
    script["check"] = [{"answers": [{"i": 1, "a": "no"}], "danger": True, "say": "Mask up while welding."}]
    out = asyncio.run(guidance.checklist([llm.Model("http://g/v1", "", "q")], [jpeg()], step, 0, 1, decision=True))
    assert len(calls) == 2 and out["result"] == "danger" and out["say"] == "Mask up while welding."


def test_checklist_sends_several_frames(signed_in, scripted):
    from irris import guidance, llm
    script, calls = scripted
    step = guidance.normalize_step({"instruction": "Fit"}, 0)
    asyncio.run(guidance.checklist([llm.Model("http://g/v1", "", "q")], [jpeg(), jpeg((90, 90, 90)), jpeg((150,) * 3)],
                                   step, 0, 1, decision=False))
    images = [p for p in calls[0]["messages"][1]["content"] if p.get("type") == "image_url"]
    assert len(images) == 3 and "1. Is this done: Fit?" in calls[0]["messages"][1]["content"][0]["text"]


# ================================================================ evidence: abstain before the model
def test_evidence_scores():
    from irris import guidance
    assert guidance.evidence(textured())["abstain"] is False
    blurry = guidance.evidence(textured(blur=9))
    assert blurry["abstain"] and "blurry" in blurry["reasons"] and "still" in blurry["say"]
    assert "too dark" in guidance.evidence(jpeg((5, 5, 5), (640, 480)))["reasons"]
    assert guidance.evidence(jpeg())["score"] is None                       # too small to judge: never abstains
    roi = {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2}
    covered = guidance.evidence(textured(), roi=roi, hands=[{"x": 0.35, "y": 0.35, "w": 0.3, "h": 0.3}])
    assert covered["abstain"] and "covered by hands" in covered["reasons"]
    old = guidance.evidence(textured(), age_s=12)
    assert "old frame" in old["reasons"]
    soft = guidance.evidence(textured(), expected=["screwdriver"], scene="person, cup")
    assert not soft["abstain"] and "no screwdriver in view" in soft["reasons"]


def test_check_abstains_without_calling_the_model(signed_in, scripted):
    from irris import vision
    _, calls = scripted
    c = signed_in
    _, run, cam = _run(c, ["Close the clamps", "Lock out"])
    vision.hub.put(cam["id"], textured(blur=9))
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={}).json()
    assert calls == [] and out["check"]["abstained"] and out["check"]["result"] == "unsure"
    assert out["run"]["metrics"]["abstained"] == 1 and out["run"]["metrics"]["abstention_rate"] == 1.0
    say = out["run"]["say"][-1]
    assert say["kind"] == "question" and say["severity"] == "minor" and "still" in say["text"]
    hands = c.post(f"/api/v1/runs/{run['id']}/check", json={"hands": [{"x": 0, "y": 0, "w": 1, "h": 1}]}).json()
    assert hands["check"]["abstained"] is True and calls == []


def test_settled_steps_skip_auto_checks_unless_critical(signed_in, scripted):
    from irris import guidance, runs, vision
    from irris.db import get_db
    script, calls = scripted
    c = signed_in
    _, run, cam = _run(c, ["Hold the part", {"instruction": "Weld", "severity": "critical"}])
    ws = c.get("/api/v1/me").json()["workspaces"][0]["id"]
    db = get_db()
    tr = guidance.load(db, ws, run["id"])
    for uid in ("s0", "s1"):
        tr["steps"][uid].update(settled=True)
    guidance.save(db, ws, run["id"], tr)
    vision.hub.put(cam["id"], jpeg())
    out = c.post(f"/api/v1/runs/{run['id']}/check/auto", json={}).json()
    assert out["check"].get("settled") and calls == []
    out = c.post(f"/api/v1/runs/{run['id']}/check/auto", json={"step": 1}).json()        # critical: still checked
    assert len(calls) == 1


# ================================================================ 3. instruments: geometry and OCR, not the model
@pytest.mark.parametrize("value", [0, 17, 63.5, 150, 182.4, 241, 300])
def test_needle_reading_within_two_percent(value):
    from irris import instruments
    r = instruments.needle_value(gauge(value), {**CALIB, "direction": "cw", "needle": "dark"})
    assert r is not None and abs(r["value"] - value) / 300 < 0.02


def test_instrument_read_agreement_and_limits():
    from irris import instruments
    inst = {"type": "analog", "unit": "C", "calib": {**CALIB, "direction": "cw", "needle": "dark"}}
    ok = instruments.read(inst, [gauge(150), gauge(150.5), gauge(149.8)])
    assert ok["method"] == "needle" and not ok["needs_confirm"] and abs(ok["value"] - 150) < 3 and ok["agreeing"] >= 2
    split = instruments.read(inst, [gauge(50), gauge(150), gauge(250)])
    assert split["needs_confirm"] and "disagree" in split["reason"]
    near = instruments.read(inst, [gauge(195)] * 3, limits={"min": 100, "max": 200})
    assert near["needs_confirm"] and "close to a limit" in near["reason"]
    digital = {"type": "digital", "unit": "C", "format": r"^\d{1,3}\.\d$"}
    fake = lambda texts: (lambda j: texts)                                        # noqa: E731
    out = instruments.read(digital, [jpeg()] * 3, ocr=fake([{"text": "TEMP 182.4 C", "conf": 0.93},
                                                            {"text": "18", "conf": 0.99}]))
    assert out["value"] == 182.4 and out["method"] == "ocr" and not out["needs_confirm"]
    none = instruments.read(digital, [jpeg()] * 3, ocr=fake([{"text": "1824", "conf": 0.9}]))
    assert none["value"] is None and none["needs_confirm"]


def test_digital_display_with_real_ocr():
    from irris import instruments, perception
    if perception.status().get("ocr") is False or perception._engine() is None:
        pytest.skip("OCR engine not installed")
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (640, 480), (220, 220, 215))
    d = ImageDraw.Draw(img)
    d.rectangle([160, 150, 480, 300], fill=(15, 25, 15))
    try:
        font = ImageFont.truetype("arial.ttf", 90)
    except OSError:
        font = ImageFont.load_default()
    d.text((185, 175), "182.4", fill=(140, 255, 140), font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    inst = {"type": "digital", "unit": "C", "format": r"^\d{1,3}\.\d$", "roi": [0.22, 0.28, 0.56, 0.38]}
    out = instruments.read(inst, [buf.getvalue()] * 3)
    assert out["value"] == 182.4, out


def test_instrument_api_and_runs_use_it_without_the_model(signed_in, scripted):
    from irris import vision
    _, calls = scripted
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Oven cam"}).json()
    bad = c.post("/api/v1/instruments", json={"name": "Dial", "type": "analog", "camera_id": cam["id"]})
    assert bad.status_code == 400
    inst = c.post("/api/v1/instruments", json={"name": "Oven dial", "type": "analog", "camera_id": cam["id"],
                                               "unit": "C", "calib": CALIB}).json()
    assert inst["calib"]["direction"] == "cw" and c.get("/api/v1/instruments").json()["instruments"][0]["id"] == inst["id"]
    vision.hub.put(cam["id"], gauge(182))
    r = c.post(f"/api/v1/instruments/{inst['id']}/read", json={}).json()
    assert r["method"] == "needle" and abs(r["value"] - 182) < 6
    p = c.post("/api/v1/procedures", json={"name": "Bake", "steps": [
        {"instruction": "Check the oven", "reading": {"label": "oven", "min": 100, "max": 185, "unit": "C"},
         "instrument_id": inst["id"]}, "Unload"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"], "camera_id": cam["id"]}).json()
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={}).json()
    assert calls == [] and out["check"]["method"] == "needle"
    assert out["check"]["result"] == "unsure" and out["check"]["needs_confirm"]           # 182 is near the 185 limit
    assert out["check"]["reading"]["value"] == pytest.approx(182, abs=6) and out["run"]["step"] == 0
    # a person confirms the value: the run logs instrument vs person
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass", "reading": {"value": 181, "unit": "C"}}).json()
    m = out["run"]["metrics"]
    assert m["readings"] and m["readings"][-1]["confirmed"] == 181 and m["reading_error_mean"] is not None
    assert c.put(f"/api/v1/instruments/{inst['id']}", json={"name": "Oven dial 2"}).json()["name"] == "Oven dial 2"
    assert c.delete(f"/api/v1/instruments/{inst['id']}").status_code == 200


# ================================================================ 6. learning from runs, drafting checks
def test_precedence_and_durations():
    from irris import learning
    orders = [{0: 10, 1: 20, 2: 30}, {0: 12, 2: 25, 1: 40}, {0: 5, 1: 9, 2: 18}]
    pre = learning.precedence(orders, 3)
    assert pre == [[], [0], [0]]                          # 1 and 2 swap order in one run: no edge between them
    dur = learning.durations([{"started": 0}, {"started": 0}, {"started": 0}], orders, 3)
    assert dur[0][0] == 10 and dur[1][0] is not None


def test_learn_endpoint(signed_in):
    c = signed_in
    p = c.post("/api/v1/procedures", json={"name": "Three", "steps": ["A", "B", "C"]}).json()
    assert c.post(f"/api/v1/procedures/{p['id']}/learn").json()["changes"] == []
    for _ in range(2):
        run = c.post("/api/v1/runs", json={"procedure_id": p["id"]}).json()
        for _s in range(3):
            c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"})
    out = c.post(f"/api/v1/procedures/{p['id']}/learn").json()
    assert out["runs"] == 2 and [s.get("prereqs") for s in out["steps"]] == [[], [0], [1]]
    assert all(s.get("median_s") is not None for s in out["steps"])
    assert any("Prototypes skipped" in n for n in out["notes"])
    saved = c.put(f"/api/v1/procedures/{p['id']}", json={**p, "steps": out["steps"]}).json()
    assert saved["steps"][2]["prereqs"] == [1] and "median_s" in saved["steps"][0]


def test_prototypes_from_captions(signed_in, scripted, monkeypatch):
    from irris import embed, learning, vision
    import numpy as np
    script, _ = scripted
    script["caption"] = [{"objects": [{"label": "clamp", "location": "left", "state": "closed"}], "scene": "a press"}]

    class E:
        model = "fake"

        def query(self, text):
            return np.asarray([1.0, 0.0, 0.0], dtype=np.float32)

        def passages(self, texts):
            return np.asarray([[1.0, 0.0, 0.1 * i] for i, _t in enumerate(texts)], dtype=np.float32)

    monkeypatch.setattr(embed, "get", lambda: E())
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone"}).json()
    p = c.post("/api/v1/procedures", json={"name": "Two", "steps": ["Close clamps", "Lock out"]}).json()
    for k in range(2):
        run = c.post("/api/v1/runs", json={"procedure_id": p["id"], "camera_id": cam["id"]}).json()
        vision.hub.put(cam["id"], jpeg((40 + k, 40, 40)))
        c.post(f"/api/v1/runs/{run['id']}/check", json={})
        c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"})
    out = c.post(f"/api/v1/procedures/{p['id']}/learn").json()
    proto = out["steps"][0]["prototype"]
    assert proto["n"] == 2 and len(proto["vector"]) == 3 and proto["p85"] >= 0
    assert learning._p85([0.0, 0.1, 0.2, 0.3]) == pytest.approx(0.255)


def test_draft_checks(signed_in, monkeypatch):
    from irris import llm
    c = signed_in
    p = c.post("/api/v1/procedures", json={"name": "Seal", "steps": ["Fill the jar", "Screw the cap"]}).json()
    assert c.post(f"/api/v1/procedures/{p['id']}/draft-checks").status_code == 400      # no reasoning model

    async def chat_chain(models, messages, tools=None, **kw):
        assert "concrete, visible property" in messages[0]["content"]
        return {"content": json.dumps({"steps": [
            {"index": 2, "questions": [{"q": "Is the cap screwed down flush?", "must": True}, "Is the label facing out?"],
             "typical_errors": ["cross-threaded cap"], "kind": "action", "severity": "critical", "reversible": False}]})}

    monkeypatch.setattr(llm, "chain", lambda ws, kind: [llm.Model("http://m/v1", "k", "deepseek")])
    monkeypatch.setattr(llm, "chat_chain", chat_chain)
    out = c.post(f"/api/v1/procedures/{p['id']}/draft-checks").json()
    s2 = out["steps"][1]
    assert s2["questions"][0] == {"q": "Is the cap screwed down flush?", "must": True}
    assert s2["severity"] == "critical" and s2["reversible"] is False and s2["intervention"] == "remind_before"
    assert out["steps"][0] == p["steps"][0] and out["changes"][0]["index"] == 1
    saved = c.put(f"/api/v1/procedures/{p['id']}", json={**p, "steps": out["steps"]}).json()
    assert saved["steps"][1]["typical_errors"] == ["cross-threaded cap"]
    patched = c.patch(f"/api/v1/procedures/{p['id']}/steps/0",
                      json={"zone": {"camera_id": "", "polygon": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]},
                            "expected_objects": ["jar"]}).json()
    assert patched["steps"][0]["zone"]["polygon"][1] == [0.5, 0.1] and patched["steps"][0]["expected_objects"] == ["jar"]
    assert c.patch(f"/api/v1/procedures/{p['id']}/steps/9", json={}).status_code == 404


# ================================================================ 7. recovery plans, speech suggestions
def test_recovery_plan_insert_and_finish(signed_in, monkeypatch):
    from irris import llm
    c = signed_in
    _, run, _ = _run(c, ["Fit the gasket", "Place the cover", "Torque the bolts"], camera=False)
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"})
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "fail", "note": "cover upside down"}).json()
    assert out["run"]["steps"][1]["status"] == "error"
    plan = c.post(f"/api/v1/runs/{run['id']}/recover", json={}).json()["recovery"]     # no model: rule-based plan
    assert plan["source"] == "rules" and plan["impact"] == [2] and plan["status"] == "proposed"

    async def chat_chain(models, messages, tools=None, **kw):
        assert "[error] Place the cover" in messages[1]["content"]
        return {"content": json.dumps({"what": "The cover went on upside down.", "impact": [3, 99],
                                       "why": "The bolts won't seat.", "fix": ["Lift the cover off", "Flip it"],
                                       "insert": [{"instruction": "Lift the cover off", "check": "cover off the dowels"}]})}

    monkeypatch.setattr(llm, "chain", lambda ws, kind: [llm.Model("http://m/v1", "k", "deepseek")])
    monkeypatch.setattr(llm, "chat_chain", chat_chain)
    s = c.post(f"/api/v1/runs/{run['id']}/recover", json={"step": 1}).json()
    assert s["recovery"]["impact"] == [2] and s["recovery"]["source"] == "model"
    assert any(x["topic"].endswith(":recovery") for x in s["say"])
    s = c.post(f"/api/v1/runs/{run['id']}/recover", json={"accept": True}).json()
    names = [st["instruction"] for st in s["steps"]]
    assert names == ["Fit the gasket", "Lift the cover off", "Place the cover", "Torque the bolts"]
    assert s["steps"][1]["recovery"] is True and s["step"] == 1 and s["plan_version"] == 2
    assert s["steps"][2]["status"] == "not_started" and s["steps"][2]["prereqs"] == [1]
    for _ in range(3):
        s = c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"}).json()["run"]
    assert s["status"] == "done"
    batch = c.get("/api/v1/records", params={"type": "batch_record"}).json()["records"][0]
    data = c.get(f"/api/v1/records/{batch['id']}").json()["data"]
    assert [x["recovery"] for x in data["steps"]] == [False, True, False, False]
    assert data["recovery"]["status"] == "accepted" and "metrics" in data


def test_confirmed_vision_fail_and_danger_suggest_speech(signed_in, scripted):
    from irris import vision
    script, _ = scripted
    c = signed_in
    _, run, cam = _run(c, [{"instruction": "Weld the seam", "severity": "critical", "questions": [{"q": "Mask down?"}]}])
    script["check"] = [{"answers": [{"i": 1, "a": "no"}], "say": "Mask is up."}]
    vision.hub.put(cam["id"], jpeg())
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={}).json()
    say = out["run"]["say"][-1]
    assert out["check"]["result"] == "fail" and say["severity"] == "critical" and say["topic"] == "s0:error"
    script["check"] = [{"answers": [{"i": 1, "a": "no"}], "danger": True, "say": "Stop, sparks near the solvent."}]
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={}).json()
    assert out["run"]["say"][-1]["kind"] == "warning" and out["run"]["say"][-1]["severity"] == "critical"
    assert len(out["run"]["say"][-1]["text"].split()) <= 13


def test_reminders_before_irreversible_steps_and_when_overdue():
    from irris import guidance
    st = {"plan": [dict(guidance.normalize_step({"instruction": "Cut the cable", "reversible": False}, 0), uid="s0",
                        after=[]),
                   dict(guidance.normalize_step({"instruction": "Purge the line now please", "median_s": 30}, 1),
                        uid="s1", after=["s0"])],
          "steps": {"s0": guidance._new_step_state(), "s1": guidance._new_step_state()}, "say": [],
          "metrics": {"vision_checks": 0, "abstained": 0, "overrides": 0, "readings": []}, "last_activity": 0}
    guidance.reminders(st, t=1)
    assert st["say"][-1]["kind"] == "reminder" and "can't be undone" in st["say"][-1]["text"]
    guidance.apply_result(st, 0, "pass", by="person", explicit=True, t=2)
    guidance.reminders(st, t=10)
    assert len(st["say"]) == 1                                         # not overdue yet
    guidance.reminders(st, t=2 + 30 + guidance.OVERDUE_GRACE_S + 1)
    assert st["say"][-1]["kind"] == "question" and st["say"][-1]["text"].startswith("Have you done this")
    assert not guidance.add_say(st, "again", topic=st["say"][-1]["topic"])   # one line per topic per 25 s


def test_ws_pushes_run_state(signed_in):
    from irris import vision
    c = signed_in
    _, run, cam = _run(c, ["A", "B"])
    q = vision.hub.subscribe(cam["id"])
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"})
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    kinds = [m["type"] for m in msgs]
    assert kinds == ["verdict", "run"]
    assert msgs[1]["run"]["steps"][0]["status"] == "complete" and msgs[1]["run"]["step"] == 1
    assert "say" in msgs[1]["run"] and "phase" in msgs[1]["run"]


def test_back_reopens_and_order_deviations_are_recorded(signed_in):
    c = signed_in
    _, run, _ = _run(c, ["A", "B", "C"], camera=False)
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"})
    s = c.post(f"/api/v1/runs/{run['id']}/back").json()
    assert s["step"] == 0 and s["steps"][0]["status"] == "in_progress"
    s = c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass", "step": 2}).json()["run"]   # out of order
    assert s["steps"][2]["status"] == "complete" and s["log"][-1]["deviation"] == "order"
    runs_list = c.get("/api/v1/runs").json()["runs"]
    assert runs_list[0]["deviations"] == 1


def test_recovery_dismiss_and_instrument_camera_filter(signed_in):
    c = signed_in
    _, run, cam = _run(c, ["A", "B"])
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "fail", "note": "wrong part"})
    assert c.post(f"/api/v1/runs/{run['id']}/recover", json={}).json()["recovery"]["status"] == "proposed"
    s = c.post(f"/api/v1/runs/{run['id']}/recover", json={"dismiss": True}).json()
    assert s["recovery"]["status"] == "dismissed" and [x["instruction"] for x in s["steps"]] == ["A", "B"]
    assert c.post(f"/api/v1/runs/{run['id']}/recover", json={"accept": True}).status_code == 400
    other = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Other"}).json()
    c.post("/api/v1/instruments", json={"name": "D1", "type": "digital", "camera_id": cam["id"], "format": r"^\d+$"})
    c.post("/api/v1/instruments", json={"name": "D2", "type": "digital", "camera_id": other["id"]})
    got = c.get("/api/v1/instruments", params={"camera_id": cam["id"]}).json()["instruments"]
    assert [i["name"] for i in got] == ["D1"] and len(c.get("/api/v1/instruments").json()["instruments"]) == 2
    assert c.post("/api/v1/instruments", json={"name": "Bad", "type": "digital", "format": "(["}).status_code == 400


def test_recovery_reuses_the_logged_error(signed_in):
    c = signed_in
    _, run, _ = _run(c, ["A", "B", "C"], camera=False)
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"})
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "fail", "note": "cover upside down"})
    c.post(f"/api/v1/runs/{run['id']}/recover", json={})
    plan = c.post(f"/api/v1/runs/{run['id']}/recover", json={}).json()["recovery"]       # asked twice
    assert "cover upside down" in plan["what"] and plan["why"] == "Step 3 depends on it."


def test_phone_gets_the_run_on_connect_and_on_changes(signed_in):
    from irris import vision
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Bench"}).json()
    tok = cam["pair_url"].split("t=")[1]
    p = c.post("/api/v1/procedures", json={"name": "Zones", "steps": [
        {"instruction": "Fit", "zone": {"camera_id": cam["id"], "polygon": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4]]},
         "expected_objects": ["scissors"]}, "Close"]}).json()
    q = vision.hub.subscribe(cam["id"])
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"], "camera_id": cam["id"]}).json()
    started = q.get_nowait()
    assert started["type"] == "run" and started["run"]["steps"][0]["zone"]["polygon"][0] == [0.1, 0.1]
    assert started["run"]["steps"][0]["started"] and "say" in started
    with c.websocket_connect(f"/api/v1/cam/ws?t={tok}") as ws:
        assert ws.receive_json()["type"] == "hello"
        first = ws.receive_json()
        assert first["type"] == "run" and first["run"]["id"] == run["id"]
        assert first["run"]["steps"][0]["expected_objects"] == ["scissors"] and first["run"]["step"] == 0
    c.post(f"/api/v1/runs/{run['id']}/back")
    assert q.get_nowait()["type"] == "run"
