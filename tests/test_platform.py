"""Status contract, approvals and sign-off, audit chain, change feed, idempotency, error shapes, webhooks, onboarding,
plain-words queries and the MCP upgrades."""
import asyncio
import json

import httpx
import pytest


def _run_to_end(c, name="Mould change"):
    p = c.post("/api/v1/procedures", json={"name": name, "steps": ["Lock out the press", "Fit the mould"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"], "label": "B-7"}).json()
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"})
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "pass"})
    return c.get(f"/api/v1/runs/{run['id']}").json()


def test_errors_carry_code_and_next_action(signed_in):
    c = signed_in
    r = c.get("/api/v1/records/nope")
    assert r.status_code == 404 and r.json()["code"] == "not_found" and r.json()["next_action"]
    r = c.get("/api/v1/records", params={"limit": "lots"})
    assert r.status_code == 422 and r.json()["code"] == "invalid" and "limit" in r.json()["detail"]
    c.cookies.clear()
    r = c.get("/api/v1/records")
    assert r.status_code == 401 and r.json()["code"] == "unauthorized" and "API key" in r.json()["next_action"]


def test_status_contract_everywhere(signed_in):
    c = signed_in
    run = _run_to_end(c)
    assert run["status_info"]["state"] == "awaiting_signoff" and "signoff" in run["status_info"]["allowed_actions"]
    assert c.get(f"/api/v1/status/run/{run['id']}").json()["state"] == "awaiting_signoff"
    p = c.post("/api/v1/procedures", json={"name": "X", "steps": ["a", "b"]}).json()
    active = c.post("/api/v1/runs", json={"procedure_id": p["id"]}).json()
    assert active["status_info"]["state"] == "in_progress" and "check" in active["status_info"]["allowed_actions"]
    cam = c.post("/api/v1/cameras", json={"kind": "phone", "name": "Bench"}).json()
    info = c.get(f"/api/v1/status/camera/{cam['id']}").json()
    assert info["state"] == "never_connected" and "pairing link" in info["next_action"]
    listed = c.get("/api/v1/runs").json()["runs"]
    assert all("status_info" in r for r in listed)
    hook = c.post("/api/v1/sources", json={"kind": "webhook", "name": "SCADA"}).json()
    assert c.get(f"/api/v1/status/source/{hook['id']}").json()["state"] == "waiting"
    assert c.get("/api/v1/status/onboarding/x").json()["state"] == "none"
    assert c.get("/api/v1/status/wat/1").status_code == 400
    assert c.get("/api/v1/status/run/nope").status_code == 404


def test_batch_signoff_releases_and_is_audited(signed_in):
    c = signed_in
    run = _run_to_end(c)
    rid = run["id"]
    assert c.post(f"/api/v1/runs/{rid}/signoff", json={"meaning": "approved", "password": "wrong"}).status_code == 401
    assert c.post(f"/api/v1/runs/{rid}/signoff", json={"meaning": "stamped", "password": "correct horse"}).status_code == 400
    r = c.post(f"/api/v1/runs/{rid}/signoff", json={"meaning": "reviewed", "password": "correct horse"}).json()
    assert r["status_info"]["state"] == "reviewed" and not r["released"]
    r = c.post(f"/api/v1/runs/{rid}/signoff", json={"meaning": "approved", "password": "correct horse",
                                                   "comment": "all good"}).json()
    assert r["released"] and r["status_info"]["state"] == "released"
    assert [s["meaning"] for s in r["signatures"]] == ["reviewed", "approved"]
    assert c.post(f"/api/v1/runs/{rid}/signoff", json={"meaning": "approved", "password": "correct horse"}).status_code == 409
    batch = c.get("/api/v1/records", params={"type": "batch_record"}).json()["records"][0]
    full = c.get(f"/api/v1/records/{batch['id']}").json()
    assert full["data"]["released"] and full["data"]["signatures"][-1]["by"] == "ada@plant.co"
    feed = c.get("/api/v1/changes").json()["items"]
    assert any(i["kind"] == "batch.released" and i["ref"] == rid for i in feed)
    audit = c.get("/api/v1/audit").json()
    assert audit["chain"]["ok"] and any(e["action"] == "batch_signoff.approved" for e in audit["entries"])
    key = c.post("/api/v1/keys", json={"name": "bot"}).json()["key"]
    run2 = _run_to_end(c, "Other")
    c.cookies.clear()
    r = c.post(f"/api/v1/runs/{run2['id']}/signoff", json={"meaning": "approved", "password": "correct horse"},
               headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403                                           # keys can't sign


def test_audit_chain_detects_tampering(signed_in, db):
    from irris import ops
    c = signed_in
    c.post("/api/v1/procedures", json={"name": "A", "steps": ["x"]})
    c.post("/api/v1/procedures", json={"name": "B", "steps": ["y"]})
    ws = c.get("/api/v1/workspace").json()["id"]
    assert ops.verify_audit(db, ws)["ok"]
    first = db.one("SELECT id FROM audit_log WHERE workspace_id = ? ORDER BY id LIMIT 1", [ws])["id"]
    db.execute("UPDATE audit_log SET detail = 'edited' WHERE id = ?", [first])
    assert not ops.verify_audit(db, ws)["ok"]


def test_change_feed_and_failures(signed_in):
    c = signed_in
    c.post("/api/v1/ingest", json={"type": "note", "text": "hello"})
    p = c.post("/api/v1/procedures", json={"name": "P", "steps": ["a", "b"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"]}).json()
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "fail", "note": "wrong part"})
    page = c.get("/api/v1/changes", params={"limit": 1}).json()
    assert page["more"] and len(page["items"]) == 1
    rest = c.get("/api/v1/changes", params={"cursor": page["cursor"], "limit": 100}).json()
    kinds = [i["kind"] for i in page["items"] + rest["items"]]
    assert "records.written" in kinds and "step.failed" in kinds and "deviation.opened" in kinds
    only = c.get("/api/v1/changes", params={"kinds": "step.failed"}).json()["items"]
    assert [i["kind"] for i in only] == ["step.failed"] and only[0]["data"]["say"] == "wrong part"


def test_idempotency_key(signed_in):
    c = signed_in
    h = {"Idempotency-Key": "abc-1"}
    a = c.post("/api/v1/ingest", json={"type": "note", "text": "once"}, headers=h)
    b = c.post("/api/v1/ingest", json={"type": "note", "text": "once"}, headers=h)
    assert a.json() == b.json() and b.headers.get("idempotent-replayed") == "true"
    assert c.get("/api/v1/records", params={"type": "note"}).json()["total"] == 1
    clash = c.post("/api/v1/ingest", json={"type": "note", "text": "different"}, headers=h)
    assert clash.status_code == 409 and clash.json()["code"] == "conflict"
    assert c.post("/api/v1/ingest", json={"type": "note", "text": "once"}).json()["created"] == 1   # no key: not deduped


def test_feedback(signed_in):
    c = signed_in
    assert c.post("/api/v1/feedback", json={"text": "The dial reader missed 0.5 steps"}).json()["received"]
    assert c.post("/api/v1/feedback", json={"text": " "}).status_code == 400
    assert c.get("/api/v1/feedback").json()["feedback"][0]["text"].startswith("The dial")


# ---------------------------------------------------------------- webhooks
@pytest.fixture()
def receiver(monkeypatch):
    from irris import webhooks
    got = []

    def handle(request: httpx.Request) -> httpx.Response:
        got.append(request)
        return httpx.Response(500 if b"fail-me" in request.url.path.encode() else 200)

    monkeypatch.setattr(webhooks, "TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setenv("IRRIS_ALLOW_PRIVATE_MCP", "1")
    return got


def test_webhooks_draft_test_approve_deliver(signed_in, receiver, db, monkeypatch):
    from irris import webhooks
    c = signed_in
    w = c.post("/api/v1/webhooks", json={"url": "http://hooks.test/in", "events": ["step.failed", "batch.released"]}).json()
    assert w["status"] == "draft" and w["secret"].startswith("whsec_") and w["status_info"]["state"] == "awaiting_approval"
    assert "secret" not in c.get("/api/v1/webhooks").json()["webhooks"][0]
    t = c.post(f"/api/v1/webhooks/{w['id']}/test").json()
    assert t["delivered"] and receiver[-1].headers["x-irris-event"] == "ping"
    assert webhooks.verify(w["secret"], receiver[-1].content, receiver[-1].headers["x-irris-signature"])
    p = c.post("/api/v1/procedures", json={"name": "P", "steps": ["a"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"]}).json()
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "fail"})
    asyncio.run(webhooks.run_due(db))
    assert len(receiver) == 1                                            # a draft sends nothing but tests
    assert c.post(f"/api/v1/webhooks/{w['id']}/approve").json()["status"] == "active"
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "fail"})
    asyncio.run(webhooks.run_due(db))
    sent = receiver[-1]
    body = json.loads(sent.content)
    assert sent.headers["x-irris-event"] == "step.failed" and body["data"]["run_id"] == run["id"]
    assert webhooks.verify(w["secret"], sent.content, sent.headers["x-irris-signature"])
    assert not webhooks.verify("whsec_other", sent.content, sent.headers["x-irris-signature"])
    log = c.get(f"/api/v1/webhooks/{w['id']}/deliveries").json()["deliveries"]
    assert log[0]["status"] == "delivered"
    audit = [e["action"] for e in c.get("/api/v1/audit").json()["entries"]]
    assert "webhook.drafted" in audit and "webhook.approved" in audit


def test_webhook_retries_and_refuses_private(signed_in, receiver, db, monkeypatch):
    from irris import webhooks
    c = signed_in
    w = c.post("/api/v1/webhooks", json={"url": "http://hooks.test/fail-me", "events": ["step.failed"]}).json()
    c.post(f"/api/v1/webhooks/{w['id']}/approve")
    p = c.post("/api/v1/procedures", json={"name": "P", "steps": ["a"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"]}).json()
    c.post(f"/api/v1/runs/{run['id']}/check", json={"result": "fail"})
    asyncio.run(webhooks.run_due(db))
    d = c.get(f"/api/v1/webhooks/{w['id']}/deliveries").json()["deliveries"][0]
    assert d["status"] == "retrying" and d["code"] == 500
    assert c.get("/api/v1/webhooks").json()["webhooks"][0]["status_info"]["state"] == "failing"
    monkeypatch.delenv("IRRIS_ALLOW_PRIVATE_MCP")
    assert c.post("/api/v1/webhooks", json={"url": "http://127.0.0.1:11434/x"}).status_code == 400


# ---------------------------------------------------------------- onboarding
def test_draft_twin_from_a_website(signed_in, monkeypatch, db):
    from irris import jobs, llm, onboarding
    c = signed_in
    monkeypatch.setenv("IRRIS_ALLOW_PRIVATE_MCP", "1")
    pages = {"/": '<html><body><h1>Acme Foods</h1><p>We make chilled yoghurt and juices in Nairobi.</p>'
                  '<a href="/about">About</a><a href="/products">Products</a><a href="/careers">Jobs</a></body></html>',
             "/about": "<p>FSSC 22000 journey; 40 staff; cold chain to supermarkets.</p>",
             "/products": "<ul><li>Greek yoghurt</li><li>Mango juice</li></ul>"}
    seen = []

    def site(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, text=pages.get(request.url.path, "nope"), headers={"content-type": "text/html"})

    monkeypatch.setattr(onboarding, "TRANSPORT", httpx.MockTransport(site))
    draft = {"profile": {"summary": "Acme Foods makes chilled dairy and juice.", "products": ["Greek yoghurt"],
                         "processes": ["pasteurisation", "filling"], "market": "Kenyan supermarkets"},
             "procedures": [{"name": "Pasteuriser start-up", "description": "HTST start",
                             "steps": [{"instruction": "Check the divert valve", "check": "valve in divert"},
                                       {"instruction": "Record holding temperature", "check": "display >= 72"}]}],
             "standards": [{"name": "FSSC 22000", "why": "food safety certification", "confidence": "likely"}],
             "documents": [{"name": "HACCP plan", "why": "hazards and CCPs"}]}

    async def fake(models, messages, tools=None, **kw):
        assert "Greek yoghurt" in messages[-1]["content"] and "careers" not in messages[-1]["content"].lower()
        return {"content": json.dumps(draft)}

    monkeypatch.setattr(llm, "chat_chain", fake)
    monkeypatch.setattr(llm, "chain", lambda ws, kind: [llm.Model("http://m", "k", "m")])
    monkeypatch.setattr(llm, "available", lambda ws, kind: True)
    r = c.post("/api/v1/onboarding", json={"url": "http://acme.test/"}).json()
    assert r["status"] == "working"
    asyncio.run(jobs.run_pending(db))
    ob = c.get("/api/v1/onboarding").json()
    assert ob["status"] == "ready" and ob["status_info"]["state"] == "review" and set(seen) == {"/", "/about", "/products"}
    assert ob["draft"]["procedures"][0]["name"] == "Pasteuriser start-up"
    acc = c.post("/api/v1/onboarding/decide", json={"item": "procedure:0", "decision": "accept"}).json()
    assert acc["applied"] and c.get(f"/api/v1/procedures/{acc['applied']}").json()["name"] == "Pasteuriser start-up"
    c.post("/api/v1/onboarding/decide", json={"item": "standard:0", "decision": "accept"})
    assert "FSSC 22000" in c.get("/api/v1/lessons").json()["lessons"][0]["text"]
    c.post("/api/v1/onboarding/decide", json={"item": "profile", "decision": "accept"})
    assert "chilled dairy" in c.get("/api/v1/workspace").json()["about"]
    last = c.post("/api/v1/onboarding/decide", json={"item": "document:0", "decision": "reject"}).json()
    assert last["onboarding"]["status_info"]["state"] == "done"
    assert c.post("/api/v1/onboarding/decide", json={"item": "profile", "decision": "accept"}).status_code == 400
    assert any(e["action"] == "onboarding.accept" for e in c.get("/api/v1/audit").json()["entries"])


def test_onboarding_needs_input_and_a_model(signed_in):
    c = signed_in
    assert c.post("/api/v1/onboarding", json={}).status_code == 400
    r = c.post("/api/v1/onboarding", json={"answers": {"makes": "yoghurt"}})
    assert r.status_code == 400 and "reasoning model" in r.json()["detail"]


# ---------------------------------------------------------------- plain-words queries
def test_query_parse_and_run(signed_in):
    import time
    c = signed_in
    now = time.time()
    c.post("/api/v1/ingest", json=[
        {"type": "batch_record", "id": "B1", "source": "runs", "time": now - 5 * 86400, "data": {"temperature": 190}},
        {"type": "batch_record", "id": "B2", "source": "runs", "time": now - 6 * 86400, "data": {"temperature": 180}},
        {"type": "batch_record", "id": "B3", "source": "runs", "time": now - 60 * 86400, "data": {"temperature": 199}},
        {"type": "reading", "subject": "asset:press-7", "data": {"value": 182.4, "unit": "C"}}])
    q = c.post("/api/v1/query/parse", json={"text": "batches last month where temperature > 185"}).json()
    assert q["type"] == "batch_record" and q["since"] and q["conditions"] == [
        {"field": "temperature", "op": ">", "value": 185, "said": "temperature > 185"}]
    run = c.post("/api/v1/query/run", json={"text": "batches last month where temperature > 185"}).json()
    assert [r["external_id"] for r in run["records"]] == ["B1"] and "temperature > 185" in run["explain"]
    p = c.post("/api/v1/query/parse", json={"text": "readings for press 7 above 180 today"}).json()
    assert p["type"] == "reading" and p["subject"] == "asset:press-7" and p["conditions"][0]["field"] == "value"
    edited = {**q, "conditions": [{"field": "temperature", "op": "<", "value": 185}]}
    assert [r["external_id"] for r in c.post("/api/v1/query/run", json={"query": edited}).json()["records"]] == ["B2"]


def test_assistant_shows_the_query_it_ran(signed_in, monkeypatch):
    from irris import llm
    c = signed_in
    c.post("/api/v1/ingest", json=[{"type": "batch_record", "id": "B1", "source": "runs", "data": {"temperature": 190}}])
    calls = []

    async def fake(models, messages, tools=None, **kw):
        calls.append(1)
        if len(calls) == 1:
            return {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
                "name": "records", "arguments": json.dumps({"where": "batches where temperature > 185"})}}]}
        return {"content": "One batch ran hot [1]."}

    monkeypatch.setattr(llm, "chat_chain", fake)
    monkeypatch.setattr(llm, "chain", lambda ws, kind: [llm.Model("http://m", "k", "m")])
    r = c.post("/api/v1/ask", json={"message": "which batches ran hot?"}).json()
    assert r["queries"][0]["explain"].startswith("batch records where temperature > 185")
    assert r["queries"][0]["matched"] == 1


# ---------------------------------------------------------------- MCP
def test_mcp_anonymous_tier_and_api_tools(signed_in):
    c = signed_in
    key = c.post("/api/v1/keys", json={"name": "agent"}).json()["key"]
    c.cookies.clear()
    rpc = lambda m, p=None, h=None: c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p or {}},
                                           headers=h or {}).json()
    v = rpc("tools/call", {"name": "irf_validate", "arguments": {"records": [{"type": "reading", "data": {"value": 3}}]}})
    assert json.loads(v["result"]["content"][0]["text"])["valid"] is True
    denied = rpc("tools/call", {"name": "search", "arguments": {"query": "x"}})
    assert denied["result"]["isError"] and "API key" in denied["result"]["content"][0]["text"]
    assert [r["uri"] for r in rpc("resources/list")["result"]["resources"]] == ["irris://irf"]
    h = {"Authorization": f"Bearer {key}"}
    listed = json.loads(rpc("tools/call", {"name": "api_list", "arguments": {"query": "runs"}}, h)["result"]["content"][0]["text"])
    assert any(e["path"] == "/api/v1/runs" and e["method"] == "GET" for e in listed["endpoints"])
    desc = json.loads(rpc("tools/call", {"name": "api_describe", "arguments": {"method": "POST", "path": "/api/v1/lessons"}},
                          h)["result"]["content"][0]["text"])
    assert desc["path"] == "/api/v1/lessons"
    made = json.loads(rpc("tools/call", {"name": "api_change", "arguments": {
        "method": "POST", "path": "/api/v1/lessons", "body": {"text": "Press 7 runs hot"}}}, h)["result"]["content"][0]["text"])
    assert made["status"] == 200
    read = json.loads(rpc("tools/call", {"name": "api_read", "arguments": {"path": "/api/v1/lessons"}}, h)["result"]["content"][0]["text"])
    assert read["body"]["lessons"][0]["text"] == "Press 7 runs hot"
    bad = rpc("tools/call", {"name": "api_read", "arguments": {"path": "/etc/passwd"}}, h)
    assert bad["result"]["isError"]
    p = json.loads(rpc("tools/call", {"name": "api_change", "arguments": {
        "method": "POST", "path": "/api/v1/procedures", "body": {"name": "P", "steps": ["a"]}}}, h)["result"]["content"][0]["text"])
    run = json.loads(rpc("tools/call", {"name": "api_change", "arguments": {
        "method": "POST", "path": "/api/v1/runs", "body": {"procedure_id": p["body"]["id"]}}}, h)["result"]["content"][0]["text"])
    st = json.loads(rpc("tools/call", {"name": "status", "arguments": {"kind": "run", "id": run["body"]["id"]}}, h)["result"]["content"][0]["text"])
    assert st["state"] == "in_progress"


def test_recovery_decision_is_audited(signed_in, db):
    from irris import guidance, runs
    c = signed_in
    p = c.post("/api/v1/procedures", json={"name": "P", "steps": ["a", "b"]}).json()
    run = c.post("/api/v1/runs", json={"procedure_id": p["id"]}).json()
    ws = c.get("/api/v1/workspace").json()["id"]
    row = db.one("SELECT * FROM runs WHERE id = ?", [run["id"]])
    tr = runs._tracker(db, ws, row)
    tr["recovery"] = {"status": "proposed", "step": 0, "what": "wrong part", "insert": [], "fix": ["redo it"]}
    guidance.save(db, ws, run["id"], tr)
    assert c.get(f"/api/v1/runs/{run['id']}").json()["status_info"]["state"] == "awaiting_recovery"
    c.post(f"/api/v1/runs/{run['id']}/recover", json={"accept": True})
    entries = c.get("/api/v1/audit").json()["entries"]
    assert any(e["action"] == "recovery_plan.accepted" and e["actor"] == "ada@plant.co" for e in entries)
