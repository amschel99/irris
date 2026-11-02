import io
import json


def test_signup_me_and_login(signed_in):
    me = signed_in.get("/api/v1/me").json()
    assert me["user"]["email"] == "ada@plant.co" and me["workspaces"][0]["name"] == "Line 3"
    signed_in.post("/api/v1/auth/logout")
    assert signed_in.get("/api/v1/me").status_code == 401
    assert signed_in.post("/api/v1/auth/login", json={"email": "ada@plant.co", "password": "nope"}).status_code == 401
    r = signed_in.post("/api/v1/auth/login", json={"email": "ADA@plant.co", "password": "correct horse"})
    assert r.status_code == 200


def test_duplicate_email_and_short_password(client):
    assert client.post("/api/v1/auth/signup", json={"email": "a@b.co", "password": "short"}).status_code == 400
    assert client.post("/api/v1/auth/signup", json={"email": "a@b.co", "password": "longenough"}).status_code == 200
    assert client.post("/api/v1/auth/signup", json={"email": "a@b.co", "password": "longenough"}).status_code == 409


def test_workspaces_are_isolated(client):
    a = client.post("/api/v1/auth/signup", json={"email": "a@x.co", "password": "password1"}).json()
    client.post("/api/v1/ingest", json={"type": "note", "text": "secret alloy recipe"})
    client.cookies.clear()
    client.post("/api/v1/auth/signup", json={"email": "b@x.co", "password": "password1"})
    assert client.get("/api/v1/search", params={"q": "alloy"}).json()["hits"] == []
    assert client.get("/api/v1/records", headers={"X-Workspace": a["workspaces"][0]["id"]}).status_code == 403


def test_ingest_irf_any_json_ndjson_and_upsert(signed_in):
    c = signed_in
    r = c.post("/api/v1/ingest", json={"irf": "1.0", "type": "reading", "source": "line-3/plc", "id": "t1",
                                       "subject": "asset:press-7", "data": {"value": 182.4, "unit": "C"}})
    assert r.json()["created"] == 1
    r = c.post("/api/v1/ingest", json={"irf": "1.0", "type": "reading", "source": "line-3/plc", "id": "t1",
                                       "subject": "asset:press-7", "data": {"value": 190.1, "unit": "C"}})
    assert r.json()["updated"] == 1
    r = c.post("/api/v1/ingest?type=work_order&source=erp", json=[{"wo": 1, "part": "PN-2231"}, {"wo": 2}])
    assert r.json()["accepted"] == 2
    nd = "\n".join(json.dumps({"type": "event", "text": f"stop {i}"}) for i in range(3))
    r = c.post("/api/v1/ingest", content=nd, headers={"Content-Type": "application/x-ndjson"})
    assert r.json()["accepted"] == 3
    readings = c.get("/api/v1/records", params={"type": "reading"}).json()
    assert readings["total"] == 1
    full = c.get(f"/api/v1/records/{readings['records'][0]['id']}").json()
    assert full["data"]["value"] == 190.1
    hits = c.get("/api/v1/search", params={"q": "PN-2231"}).json()["hits"]
    assert hits and hits[0]["type"] == "work_order"
    assert c.post("/api/v1/ingest", json={"type": "note"}).status_code == 422


def test_file_upload_csv_and_text(signed_in):
    c = signed_in
    csv = "batch,temp,result\nB-1,181,ok\nB-2,199,scrap\n"
    r = c.post("/api/v1/ingest/file", files={"file": ("batches.csv", csv, "text/csv")})
    assert r.status_code == 200 and r.json()["accepted"] == 2
    row = c.get("/api/v1/records", params={"source": "upload/batches"}).json()
    assert row["total"] == 2
    manual = "Press 7 manual\n\nPlaten temperature must stay between 175 and 185 C during the hold phase."
    r = c.post("/api/v1/ingest/file", files={"file": ("press7.md", manual, "text/markdown")})
    assert r.json()["kind"] == "document"
    hit = c.get("/api/v1/search", params={"q": "platen hold temperature"}).json()["hits"][0]
    assert hit["title"] == "press7.md"
    assert c.get(f"/api/v1/files/{r.json()['file_id']}").content.startswith(b"Press 7")
    bad = c.post("/api/v1/ingest/file", files={"file": ("x.bin", b"\x00\x01", "application/octet-stream")})
    assert bad.status_code == 422


def test_pdf_pages_are_cited(signed_in):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    for text in ("Introduction to the line", "Torque the M8 bolts to 25 Nm in a star pattern"):
        page = w.add_blank_page(612, 792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    r = signed_in.post("/api/v1/ingest/file", files={"file": ("sop.pdf", buf.getvalue(), "application/pdf")})
    assert r.status_code == 200, r.text
    hit = signed_in.get("/api/v1/search", params={"q": "torque bolts"}).json()["hits"][0]
    assert hit["page"] == 2


def test_api_key_and_webhook(signed_in):
    c = signed_in
    key = c.post("/api/v1/keys", json={"name": "plc bridge"}).json()["key"]
    assert key.startswith("irr_")
    hook = c.post("/api/v1/sources", json={"kind": "webhook", "name": "SCADA", "type": "alarm"}).json()
    c.cookies.clear()
    assert c.get("/api/v1/records").status_code == 401
    h = {"Authorization": f"Bearer {key}"}
    assert c.post("/api/v1/ingest", json={"type": "note", "text": "from the key"}, headers=h).json()["created"] == 1
    r = c.post(hook["url"], json={"alarm": "E41 hydraulic pressure low", "machine": "press-7"})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    alarms = c.get("/api/v1/records", params={"type": "alarm"}, headers=h).json()
    assert alarms["total"] == 1 and alarms["records"][0]["source"] == "webhook/SCADA"
    assert c.post("/api/v1/hooks/whk_wrong", json={}).status_code == 404
    assert c.get("/api/v1/records", params={"key": key}).status_code == 200


def test_procedures_and_a_guided_run(signed_in):
    c = signed_in
    p = c.post("/api/v1/procedures", json={"name": "Mould change", "steps": [
        "Lock out the press",
        {"instruction": "Check the platen temperature", "check": "display visible",
         "reading": {"label": "platen", "min": 175, "max": 185, "unit": "C"}},
        {"instruction": "Fit the new mould", "check": "four clamps closed"}]}).json()
    assert len(p["steps"]) == 3 and p["steps"][1]["reading"]["max"] == 185
    p2 = c.put(f"/api/v1/procedures/{p['id']}", json={**p, "description": "for tool T-12"}).json()
    assert p2["id"] == p["id"] and p2["description"] == "for tool T-12"
    assert len(c.get("/api/v1/procedures").json()["procedures"]) == 1

    run = c.post("/api/v1/runs", json={"procedure_id": p["id"], "label": "B-1042"}).json()
    assert run["status"] == "active" and run["step"] == 0
    run = c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"}).json()["run"]
    out = c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass", "reading": {"value": 191, "unit": "C"}}).json()
    assert out["check"]["result"] == "fail" and out["run"]["step"] == 1     # out of spec: stays on the step
    run = c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass", "reading": {"value": 180, "unit": "C"}}).json()["run"]
    run = c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"}).json()["run"]
    assert run["status"] == "done"
    batch = c.get("/api/v1/records", params={"type": "batch_record"}).json()
    assert batch["total"] == 1
    rec = c.get(f"/api/v1/records/{batch['records'][0]['id']}").json()
    assert len(rec["data"]["deviations"]) == 1 and "3/3 steps passed" in rec["text"]
    assert c.get("/api/v1/records", params={"type": "reading"}).json()["total"] == 2
    ov = c.get("/api/v1/overview").json()
    assert ov["week"]["deviations"] == 1 and ov["procedures"] == 1


def test_ask_without_a_model_answers_from_search(signed_in):
    c = signed_in
    c.post("/api/v1/ingest", json={"type": "document", "title": "Dryer SOP",
                                   "text": "Dry the PA66 resin at 80 C for 4 hours before moulding."})
    r = c.post("/api/v1/ask", json={"message": "how long do we dry PA66?"}).json()
    assert "4 hours" in r["reply"] and r["conversation_id"]
    conv = c.get(f"/api/v1/conversations/{r['conversation_id']}").json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    lines = c.get("/api/v1/training/export").text.strip().splitlines()
    assert json.loads(lines[0])["messages"][0]["content"] == "how long do we dry PA66?"


def test_ask_with_a_model_uses_tools_and_cites(signed_in, monkeypatch):
    from irris import llm
    c = signed_in
    c.post("/api/v1/ingest", json={"type": "document", "title": "Dryer SOP",
                                   "text": "Dry the PA66 resin at 80 C for 4 hours before moulding."})
    calls = []

    async def fake_chat(model, messages, tools=None, **kw):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
                "name": "search", "arguments": json.dumps({"query": "PA66 drying"})}}]}
        tool_msg = json.loads(messages[-1]["content"])
        assert tool_msg["results"][0]["cite"] == "[1]"
        return {"content": "Dry it at 80 C for 4 hours [1]."}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "resolve", lambda ws, kind: llm.Model("http://model", "k", "m"))
    r = c.post("/api/v1/ask", json={"message": "PA66 drying?"}).json()
    assert r["reply"].endswith("[1].") and r["tools"] == ["search"] and r["sources"][0]["title"] == "Dryer SOP"
    calls.clear()
    stream = c.post("/api/v1/ask", json={"message": "again", "stream": True})
    assert "event: tool" in stream.text and "event: done" in stream.text


def test_lessons_and_models_settings(signed_in):
    c = signed_in
    c.post("/api/v1/lessons", json={"text": "Press 7 runs 5 C hot; subtract 5 from its display."})
    assert len(c.get("/api/v1/lessons").json()["lessons"]) == 1
    ws = c.patch("/api/v1/workspace", json={"about": "We mould PA66 housings.",
                                           "models": {"vlm": {"base_url": "http://gpu:11434/v1", "model": "qwen3.5:2b",
                                                              "api_key": "sk-123"}}}).json()
    assert ws["models"]["vlm"] == {"base_url": "http://gpu:11434/v1", "model": "qwen3.5:2b", "has_key": True}
    assert "sk-123" not in json.dumps(ws)
    from irris import llm
    from irris.db import get_db
    row = get_db().one("SELECT * FROM workspaces WHERE id = ?", [ws["id"]])
    assert llm.resolve(row, "vlm").api_key == "sk-123"


def test_members_and_invites(signed_in):
    c = signed_in
    inv = c.post("/api/v1/members", json={"email": "bo@plant.co"}).json()
    c.cookies.clear()
    r = c.post("/api/v1/auth/signup", json={"email": "bo@plant.co", "password": "password9",
                                            "invite": inv["link"].split("invite=")[1]}).json()
    assert [w["name"] for w in r["workspaces"]] == ["Line 3"] and r["workspaces"][0]["role"] == "member"
    assert c.post("/api/v1/keys", json={}).status_code == 403


def test_cameras_pairing_and_frames(signed_in):
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Bench phone"}).json()
    tok = cam["pair_url"].split("t=")[1]
    assert c.get("/api/v1/cam/info", params={"t": tok}).json()["name"] == "Bench phone"
    assert c.get(f"/api/v1/cameras/{cam['id']}/frame.jpg").status_code == 404
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100 + b"\xff\xd9"
    assert c.post("/api/v1/cam/frame", params={"t": tok}, content=jpeg).status_code == 200
    assert c.get(f"/api/v1/cameras/{cam['id']}/frame.jpg").content == jpeg
    assert c.get("/api/v1/cameras").json()["cameras"][0]["live"] is True
    assert c.post("/api/v1/cam/frame", params={"t": "cam_bad"}, content=jpeg).status_code == 401


def test_mcp(signed_in):
    c = signed_in
    key = c.post("/api/v1/keys", json={"name": "claude"}).json()["key"]
    c.cookies.clear()
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    h = {"Authorization": f"Bearer {key}"}
    init = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-06-18"}}, headers=h).json()
    assert init["result"]["serverInfo"]["name"] == "irris"
    names = {t["name"] for t in c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                       headers=h).json()["result"]["tools"]}
    assert {"ask", "ingest", "search", "sql", "look", "start_run", "check_step"} <= names
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "ingest", "arguments": {"records": [{"type": "asset", "title": "Press 7", "subject": "asset:press-7",
                                                      "data": {"tonnage": 250}}]}}}, headers=h).json()
    assert json.loads(r["result"]["content"][0]["text"])["created"] == 1
    r = c.post(f"/mcp?key={key}", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "search", "arguments": {"query": "tonnage"}}}).json()
    assert "Press 7" in r["result"]["content"][0]["text"]
    assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h).status_code == 202


def test_site_and_dashboard_routes(client):
    assert client.get("/").status_code == 200
    assert client.get("/standard").status_code == 200
    assert client.get("/nope-page").status_code == 404
    assert client.get("/api/v1/health").json()["ok"] is True
    assert client.get("/api/v1/irf").text.startswith("# IRF")
    assert client.get("/app/../irris/db.py").status_code in (200, 404, 503)
    assert b"SCHEMA" not in client.get("/app/../irris/db.py").content


def test_cameras_are_private_to_their_workspace(signed_in):
    c = signed_in
    cam = c.post("/api/v1/cameras", json={"kind": "phone"}).json()
    c.cookies.clear()
    c.post("/api/v1/auth/signup", json={"email": "eve@other.co", "password": "password1"})
    assert c.post("/api/v1/ask", json={"message": "what do you see", "camera_id": cam["id"]}).status_code == 404
    assert c.get(f"/api/v1/cameras/{cam['id']}/frame.jpg").status_code == 404
    assert c.post(f"/api/v1/cameras/{cam['id']}/frame", content=b"\xff\xd8x\xff\xd9").status_code == 404
