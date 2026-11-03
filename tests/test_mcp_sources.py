import asyncio
import json

import httpx
import pytest


def fake_server(sse: bool = False, token: str = "secret-token"):
    """A tiny streamable-HTTP MCP server: an ERP with work orders."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {token}":
            return httpx.Response(401)
        msg = json.loads(request.content)
        calls.append(msg.get("method"))
        if "id" not in msg:
            return httpx.Response(202)
        mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "resources": {}},
                      "serverInfo": {"name": "erp", "title": "Plant ERP", "version": "2"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "work_orders", "description": "Open work orders",
                                 "inputSchema": {"type": "object", "properties": {"line": {"type": "string"}}}},
                                {"name": "notes", "description": "Shift notes", "inputSchema": {"type": "object"}}]}
        elif method == "resources/list":
            result = {"resources": [{"uri": "erp://stock", "name": "Stock", "mimeType": "application/json"}]}
        elif method == "tools/call" and params["name"] == "work_orders":
            rows = [{"type": "work_order", "id": "WO-1", "source": "erp/wo", "data": {"part": "PN-2231", "qty": 40}},
                    {"type": "work_order", "id": "WO-2", "source": "erp/wo", "data": {"part": "PN-9", "qty": 5}}]
            result = {"content": [{"type": "text", "text": json.dumps(rows)}]}
        elif method == "tools/call" and params["name"] == "notes":
            result = {"content": [{"type": "text", "text": "Line 3 ran hot after lunch."}]}
        elif method == "resources/read":
            result = {"contents": [{"uri": "erp://stock", "text": json.dumps([{"sku": "PA66", "kg": 120}])}]}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
        body = {"jsonrpc": "2.0", "id": mid, "result": result}
        headers = {"mcp-session-id": "s-1"}
        if sse:
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps(body)}\n\n",
                                  headers={**headers, "content-type": "text/event-stream"})
        return httpx.Response(200, json=body, headers=headers)

    return httpx.MockTransport(handle), calls


@pytest.fixture()
def erp(monkeypatch):
    from irris import mcp_client
    monkeypatch.setenv("IRRIS_ALLOW_PRIVATE_MCP", "1")
    transport, calls = fake_server(sse=True)
    monkeypatch.setattr(mcp_client, "TRANSPORT", transport)
    return calls


def test_connect_call_save_and_sync(signed_in, erp):
    c = signed_in
    bad = c.post("/api/v1/mcp-servers", json={"url": "http://erp.test/mcp", "token": "wrong"})
    assert bad.status_code == 400 and "credentials" in bad.json()["detail"]
    s = c.post("/api/v1/mcp-servers", json={"url": "http://erp.test/mcp", "token": "secret-token"}).json()
    assert s["name"] == "Plant ERP" and [t["name"] for t in s["tools"]] == ["work_orders", "notes"]
    assert s["resources"][0]["uri"] == "erp://stock" and s["has_auth"] and "secret-token" not in json.dumps(s)

    r = c.post(f"/api/v1/mcp-servers/{s['id']}/call", json={"tool": "work_orders", "save": True}).json()
    assert r["result"]["content"][1]["id"] == "WO-2" and r["saved"]["created"] == 2
    assert c.post(f"/api/v1/mcp-servers/{s['id']}/call", json={"tool": "work_orders", "save": True}).json()["saved"]["updated"] == 2
    hits = c.get("/api/v1/search", params={"q": "PN-2231"}).json()["hits"]
    assert hits and hits[0]["type"] == "work_order"

    sync = c.post(f"/api/v1/mcp-servers/{s['id']}/syncs", json={"resource": "erp://stock", "type": "stock",
                                                                "every_minutes": 1}).json()
    assert sync["every_minutes"] == 5                                   # at least 5 minutes
    assert sync["status_info"]["state"] == "awaiting_approval"
    blocked = c.post(f"/api/v1/mcp-servers/{s['id']}/syncs/{sync['id']}/run").json()
    assert blocked["status"].startswith("waiting for approval")         # it writes records: an admin approves first
    ok = c.post(f"/api/v1/mcp-servers/{s['id']}/syncs/{sync['id']}/approve").json()
    assert ok["approved"] and ok["status_info"]["state"] == "scheduled"
    out = c.post(f"/api/v1/mcp-servers/{s['id']}/syncs/{sync['id']}/run").json()
    assert out["status"].startswith("ok: 1 new")
    assert c.get("/api/v1/records", params={"type": "stock"}).json()["total"] == 1
    srv = c.get("/api/v1/mcp-servers").json()["servers"][0]
    assert srv["syncs"][0]["last_status"].startswith("ok") and srv["syncs"][0]["next_run"] > srv["syncs"][0]["last_run"]

    text = c.post(f"/api/v1/mcp-servers/{s['id']}/call", json={"tool": "notes", "save": True}).json()
    assert text["saved"]["created"] == 1
    assert c.post(f"/api/v1/mcp-servers/{s['id']}/call", json={"tool": "drop_tables"}).status_code == 400


def test_assistant_uses_connected_servers(signed_in, erp, monkeypatch):
    from irris import llm
    c = signed_in
    s = c.post("/api/v1/mcp-servers", json={"url": "http://erp.test/mcp", "token": "secret-token"}).json()
    steps = []

    async def fake(models, messages, tools=None, **kw):
        steps.append([t["function"]["name"] for t in tools or []])
        if len(steps) == 1:
            assert "mcp_servers" in steps[0]                                  # pre-loaded by "ERP" in the message
            return {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
                "name": "mcp_servers", "arguments": "{}"}}]}
        if len(steps) == 2:
            listed = json.loads(messages[-1]["content"])
            assert listed["servers"][0]["tools"][0]["name"] == "work_orders"
            return {"content": "", "tool_calls": [{"id": "2", "type": "function", "function": {
                "name": "mcp_call", "arguments": json.dumps({"server_id": s["id"], "tool": "work_orders"})}}]}
        got = json.loads(messages[-1]["content"])
        assert got.get("external") is True
        return {"content": "Two open work orders, WO-1 and WO-2 [1]."}

    monkeypatch.setattr(llm, "chat_chain", fake)
    monkeypatch.setattr(llm, "chain", lambda ws, kind: [llm.Model("http://model", "k", "m")])
    r = c.post("/api/v1/ask", json={"message": "What's open in the ERP?"}).json()
    assert "WO-2" in r["reply"] and r["sources"][0]["type"] == "mcp"


def test_private_addresses_are_refused(monkeypatch):
    from irris import mcp_client
    monkeypatch.delenv("IRRIS_ALLOW_PRIVATE_MCP", raising=False)
    for url in ("http://127.0.0.1:11434/mcp", "http://localhost/mcp", "http://169.254.169.254/latest", "ftp://x/mcp"):
        with pytest.raises(mcp_client.MCPError):
            mcp_client.check_url(url)


def test_plain_json_transport_and_session(monkeypatch):
    from irris import mcp_client
    monkeypatch.setenv("IRRIS_ALLOW_PRIVATE_MCP", "1")
    transport, calls = fake_server(sse=False)
    monkeypatch.setattr(mcp_client, "TRANSPORT", transport)
    info = asyncio.run(mcp_client.describe("http://erp.test/mcp", {"Authorization": "Bearer secret-token"}))
    assert info["server"]["name"] == "erp" and len(info["tools"]) == 2
    assert calls[:2] == ["initialize", "notifications/initialized"]
