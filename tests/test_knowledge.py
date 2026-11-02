"""Jobs, huge documents, reading, hybrid search, study, memory, context building, stats and insights."""
import asyncio
import io
import json
import re

import numpy as np
import pytest


# ---------------------------------------------------------------- helpers
def make_pdf(pages: list[str], outline: list[tuple[str, int]] | None = None) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = w._add_object(font)
    for text in pages:
        page = w.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
        lines = text.split("\n")
        ops = "BT /F1 11 Tf 14 TL 72 720 Td " + " ".join(f"({l.replace('(', '').replace(')', '')}) Tj T*" for l in lines) + " ET"
        stream = DecodedStreamObject()
        stream.set_data(ops.encode())
        page[NameObject("/Contents")] = w._add_object(stream)
    for title, page in outline or []:
        w.add_outline_item(title, page - 1)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def run_jobs(n: int = 200) -> int:
    from irris import jobs
    from irris.db import get_db
    return asyncio.run(jobs.run_pending(get_db(), limit=n))


class FakeLLM:
    """Answers each prompt kind the way a cooperative model would."""

    def __init__(self):
        self.calls = []
        self.fail_after = None

    async def __call__(self, models, messages, tools=None, **kw):
        from irris import llm
        prompt = messages[-1]["content"] if isinstance(messages[-1]["content"], str) else ""
        self.calls.append(prompt[:80])
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise llm.ModelError("model unreachable: ConnectError")
        if "studying a document" in prompt:
            m = re.search(r"--- (?:page|part) (\d+) ---", prompt)
            page = int(m.group(1)) if m else 1
            body = prompt.split("---", 1)[-1]
            facts = []
            limit = re.search(r"between (\d+) and (\d+) C", body)
            if limit:
                before = re.findall(r"--- (?:page|part) (\d+) ---", prompt[:prompt.index(limit.group(0))])
                page = int(before[-1]) if before else page
                facts.append({"text": f"Platen temperature must stay between {limit.group(1)} and {limit.group(2)} C.",
                              "page": page, "kind": "limit", "subject": "asset:press-7"})
            return {"content": json.dumps({"title": f"Section from p. {page}",
                                           "notes": f"Notes on pages starting at {page} (p. {page}).",
                                           "facts": facts})}
        if "condensing your notes" in prompt:
            return {"content": json.dumps({"summary": "merged notes (p. 1)", "chapters": []})}
        if "Write the digest" in prompt:
            return {"content": json.dumps({"digest": "This manual covers the press. Keep the platen at 175-185 C.",
                                           "key_points": ["Platen 175-185 C (p. 3)"],
                                           "chapters": [{"title": "Setup", "from": 1, "to": 2}]})}
        if "working summary" in prompt:
            return {"content": "They are tuning press 7 and discussed platen limits."}
        if "memory of Irris" in prompt:
            q = prompt.split("User (", 1)[-1]
            if "Bo" in q or "metric" in q:
                return {"content": json.dumps({"memories": [
                    {"text": "Bo runs the night shift on line 3 and wants answers in metric units.",
                     "kind": "semantic", "subject": "person:bo", "predicate": "role", "source": "user_stated",
                     "scope": "team", "importance": 0.8, "confidence": 0.9}]})}
            return {"content": json.dumps({"memories": []})}
        return {"content": "ok"}


@pytest.fixture()
def fake_llm(monkeypatch):
    from irris import llm
    fake = FakeLLM()
    monkeypatch.setattr(llm, "chat_chain", fake)
    monkeypatch.setattr(llm, "chain", lambda ws, kind: [llm.Model("http://model", "k", "m")] if kind == "llm" else [])
    return fake


class FakeEmbedder:
    """Words map to concepts, so synonyms land close together without sharing any letters."""
    model = "fake-embed"
    CONCEPTS = [("coolant", "fluid", "liquid"), ("replace", "change", "swap"), ("hours", "often", "interval", "every"),
                ("torque", "tighten"), ("bolt", "fastener", "screw"), ("temperature", "heat", "hot")]

    def _vec(self, text):
        v = np.zeros(len(self.CONCEPTS) + 1, dtype=np.float32)
        for w in re.findall(r"[a-z]+", text.lower()):
            for i, group in enumerate(self.CONCEPTS):
                if any(w.startswith(g) for g in group):
                    v[i] += 1
        v[-1] = 0.05
        return v / np.linalg.norm(v)

    def passages(self, texts):
        return np.vstack([self._vec(t) for t in texts])

    def query(self, text):
        return self._vec(text)


@pytest.fixture()
def fake_embed():
    from irris import embed
    e = FakeEmbedder()
    embed.set_embedder(e)
    yield e
    embed.set_embedder(None)


def ws_of(client):
    from irris.db import get_db
    wid = client.get("/api/v1/workspace").json()["id"]
    return get_db(), get_db().one("SELECT * FROM workspaces WHERE id = ?", [wid])


# ---------------------------------------------------------------- jobs
def test_jobs_claim_progress_retry_resume_and_cancel(db):
    from irris import jobs
    from irris.db import new_id, now
    db.execute("INSERT INTO workspaces (id, name, created) VALUES (?,?,?)", ["w1", "W", now()])
    seen = []

    @jobs.handler("test_count")
    async def count(ctx):
        start = ctx.state.get("i", 0)
        for i in range(start, 6):
            seen.append(i)
            ctx.state["i"] = i + 1
            await ctx.progress((i + 1) / 6, f"{i + 1}")
            if i == 2 and ctx.attempts == 1:
                raise RuntimeError("crash mid-way")
        return {"n": 6}

    j = jobs.enqueue(db, "w1", "test_count", {}, title="count", dedupe="c1")
    assert jobs.enqueue(db, "w1", "test_count", {}, dedupe="c1")["id"] == j["id"]      # deduped while queued
    asyncio.run(jobs.run_pending(db))
    first = jobs.get(db, "w1", j["id"])
    assert first["status"] == "queued" and "crash mid-way" in first["error"]
    db.execute("UPDATE jobs SET run_after = 0 WHERE id = ?", [j["id"]])
    asyncio.run(jobs.run_pending(db))
    done = jobs.get(db, "w1", j["id"])
    assert done["status"] == "done" and done["result"] == {"n": 6} and done["progress"] == 1
    assert seen == [0, 1, 2, 3, 4, 5]               # resumed after the checkpoint, nothing redone

    @jobs.handler("test_slow")
    async def slow(ctx):
        jobs.cancel(db, "w1", ctx.id)
        await ctx.progress(0.5)
        return {"never": True}

    k = jobs.enqueue(db, "w1", "test_slow", {})
    asyncio.run(jobs.run_pending(db))
    assert jobs.get(db, "w1", k["id"])["status"] == "cancelled"
    assert jobs.retry(db, "w1", k["id"])["status"] == "queued"

    jobs.cancel(db, "w1", k["id"])
    stale = jobs.enqueue(db, "w1", "test_count", {})
    db.execute("UPDATE jobs SET status = 'running', locked_until = 1 WHERE id = ?", [stale["id"]])
    assert jobs.claim(db)["id"] == stale["id"]        # a dead worker's job is reclaimed


# ---------------------------------------------------------------- huge documents
def test_big_pdf_reads_in_the_background_and_reads_back(signed_in):
    c = signed_in
    pages = []
    for i in range(1, 131):
        head = f"Chapter {i // 40 + 1} Maintenance" if i % 40 == 1 else ""
        body = f"Page {i} of the manual. Torque the M{8 + i % 4} bolts to {20 + i} Nm."
        if i == 97:
            body = "Platen temperature must stay between 175 and 185 C during the hold phase."
        pages.append(f"{head}\n{body}" if head else body)
    pdf = make_pdf(pages)
    r = c.post("/api/v1/ingest/file", files={"file": ("manual.pdf", pdf, "application/pdf")}).json()
    assert r["status"] == "queued" and r["job"]["kind"] == "read" and r["pages"] == 130
    rid = r["record_id"]
    assert c.get(f"/api/v1/documents/{rid}").json()["status"] == "queued"
    run_jobs()
    doc = c.get(f"/api/v1/documents/{rid}").json()
    assert doc["status"] == "indexed" and doc["pages"] == 130 and doc["read"] == 130
    assert [o["page"] for o in doc["outline"]][:3] == [1, 41, 81]          # headings detected without bookmarks
    hit = c.get(f"/api/v1/documents/{rid}/search", params={"q": "platen hold temperature"}).json()["hits"][0]
    assert hit["page"] == 97
    first = c.get(f"/api/v1/documents/{rid}/pages", params={"start": 1, "end": 5}).json()
    assert [p["page"] for p in first["content"]] == [1, 2, 3, 4, 5] and first["next"] == 6
    from irris import docs
    from irris.db import get_db
    wid = c.get("/api/v1/workspace").json()["id"]
    page, seen = 1, 0
    while page:                                   # the whole book, end to end, in reads
        out = docs.read(get_db(), wid, rid, page)
        seen += len(out["content"])
        page = out["next"]
    assert seen == 130
    file_bytes = c.get(f"/api/v1/files/{r['file_id']}").content
    assert file_bytes == pdf


def test_pdf_bookmarks_markdown_parts_and_text(signed_in):
    c = signed_in
    pdf = make_pdf(["Intro", "Setup steps", "Safety rules"], outline=[("Setup", 2), ("Safety", 3)])
    r = c.post("/api/v1/ingest/file", files={"file": ("small.pdf", pdf, "application/pdf")}).json()
    assert r["status"] == "indexed"
    doc = c.get(f"/api/v1/documents/{r['record_id']}").json()
    assert [(o["title"], o["page"]) for o in doc["outline"]] == [("Setup", 2), ("Safety", 3)]
    md = "# Line 3 handbook\n\n" + "\n\n".join(
        f"## Section {i}\n\n" + ("Coolant and setup notes. " * 60) for i in range(1, 9))
    r = c.post("/api/v1/ingest/file", files={"file": ("handbook.md", md, "text/markdown")}).json()
    doc = c.get(f"/api/v1/documents/{r['record_id']}").json()
    assert doc["unit"] == "part" and doc["pages"] > 3
    assert doc["outline"][0]["title"] == "Line 3 handbook" and any(o["title"] == "Section 8" for o in doc["outline"])


def test_upload_limit(signed_in, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    r = signed_in.post("/api/v1/ingest/file", files={"file": ("big.txt", b"x" * (1024 * 1024 + 10), "text/plain")})
    assert r.status_code == 413


def test_big_csv_imports_in_a_job(signed_in, monkeypatch):
    from irris import api_knowledge
    monkeypatch.setattr(api_knowledge, "INLINE_ROWS_BYTES", 100)
    csv = "batch,temp\n" + "\n".join(f"B-{i},{170 + i % 20}" for i in range(2500))
    r = signed_in.post("/api/v1/ingest/file", files={"file": ("batches.csv", csv, "text/csv")}).json()
    assert r["status"] == "queued"
    run_jobs()
    assert signed_in.get("/api/v1/records", params={"source": "upload/batches"}).json()["total"] == 2500
    job = signed_in.get(f"/api/v1/jobs/{r['job']['id']}").json()
    assert job["status"] == "done" and job["result"]["rows"] == 2500


# ---------------------------------------------------------------- hybrid search
def test_semantic_search_finds_meaning_without_shared_words(signed_in, fake_embed):
    c = signed_in
    c.post("/api/v1/ingest", json=[
        {"type": "note", "title": "Service plan", "text": "Swap the coolant every 500 hours of running."},
        {"type": "note", "title": "Assembly", "text": "Tighten each fastener in a star pattern."}])
    from irris import embed, store
    db, ws = ws_of(c)
    assert embed.embed_missing(db, ws["id"]) > 0
    assert store.search(db, ws["id"], "how often do we change the liquid") != []   # no keyword in common
    hits = store.search(db, ws["id"], "how often do we change the liquid")
    assert hits[0]["title"] == "Service plan" and hits[0]["similarity"] > 0.5
    run_jobs()                                       # the queued embed job finds nothing left
    assert embed.pending(db, ws["id"]) == 0
    rid = hits[0]["record_id"]
    c.delete(f"/api/v1/records/{rid}")
    assert db.one("SELECT COUNT(*) AS n FROM embeddings WHERE record_id = ?", [rid])["n"] == 0
    assert all(h["record_id"] != rid for h in store.search(db, ws["id"], "change the liquid"))


# ---------------------------------------------------------------- study
def test_study_reads_every_page_writes_facts_and_a_digest_and_resumes(signed_in, fake_llm, monkeypatch):
    c = signed_in
    from irris import study
    monkeypatch.setattr(study, "SECTION_CHARS", 300)
    pages = [f"Page {i}. General operating notes for press 7. " * 3 for i in range(1, 13)]
    pages[2] = "Platen temperature must stay between 175 and 185 C during the hold phase."
    r = c.post("/api/v1/ingest/file", params={"teach": "true"},
               files={"file": ("press7.pdf", make_pdf(pages), "application/pdf")}).json()
    assert r["job"]["kind"] == "study"
    fake_llm.fail_after = 3                          # the model drops out part-way through
    run_jobs()
    job = c.get(f"/api/v1/jobs/{r['job']['id']}").json()
    assert job["status"] == "queued" and "unavailable" in job["message"]
    partial = len(c.get("/api/v1/knowledge", params={"document_id": r["record_id"]}).json()["knowledge"])
    fake_llm.fail_after = None
    from irris.db import get_db
    get_db().execute("UPDATE jobs SET run_after = 0")
    run_jobs()
    job = c.get(f"/api/v1/jobs/{r['job']['id']}").json()
    assert job["status"] == "done", job
    know = c.get("/api/v1/knowledge", params={"document_id": r["record_id"]}).json()["knowledge"]
    notes = [k for k in know if k["kind"] == "notes"]
    covered = sorted(p for n in notes for p in range(n["from"], n["to"] + 1))
    assert covered == list(range(1, 13))             # every page studied exactly once
    assert len(know) >= partial
    limit = [k for k in know if k["kind"] == "limit"]
    assert limit and limit[0]["page"] == 3
    doc = c.get(f"/api/v1/documents/{r['record_id']}").json()
    assert doc["study"] == "done" and "175-185" in doc["digest"]["text"]
    hits = c.get("/api/v1/search", params={"q": "platen temperature limit", "type": "knowledge"}).json()["hits"]
    assert hits


def test_without_a_model_documents_are_indexed_but_not_studied(signed_in):
    r = signed_in.post("/api/v1/ingest/file", params={"teach": "true"},
                       files={"file": ("a.md", "# A\n\nSome text about presses.", "text/markdown")}).json()
    assert r["job"] is None
    doc = signed_in.get(f"/api/v1/documents/{r['record_id']}").json()
    assert doc["status"] == "indexed" and doc["study"] == "needs a reasoning model"
    assert signed_in.post(f"/api/v1/documents/{r['record_id']}/study").status_code == 400


# ---------------------------------------------------------------- memory
def test_memories_are_distilled_deduped_and_recalled(signed_in, fake_llm):
    c = signed_in
    r = c.post("/api/v1/ask", json={"message": "I'm Bo, I run nights on line 3. Give me metric units please."}).json()
    run_jobs()
    mems = c.get("/api/v1/memories").json()["memories"]
    assert len(mems) == 1 and "metric" in mems[0]["text"] and mems[0]["scope"] == "team"
    c.post("/api/v1/ask", json={"message": "Bo again: remember I want metric", "conversation_id": r["conversation_id"]})
    run_jobs()
    assert len(c.get("/api/v1/memories").json()["memories"]) == 1          # merged, not duplicated
    m = c.post("/api/v1/memories", json={"text": "Press 7 was rebuilt in March 2026", "kind": "asset",
                                         "importance": 0.9}).json()
    c.post("/api/v1/memories", json={"text": "The canteen closes at 3", "importance": 0.1})
    from irris import memory
    db, ws = ws_of(c)
    top = memory.relevant(db, ws["id"], "", "when was press 7 rebuilt", k=1)
    assert top[0]["id"] == m["id"]
    assert c.patch(f"/api/v1/memories/{m['id']}", json={"text": "Press 7 was rebuilt in April 2026"}).json()["text"].endswith("April 2026")
    assert c.delete(f"/api/v1/memories/{m['id']}").status_code == 200
    assert c.delete(f"/api/v1/memories/{m['id']}").status_code == 404


def test_long_conversations_get_a_running_summary(signed_in, fake_llm):
    from irris import agent, memory
    c = signed_in
    conv = None
    for i in range(18):
        res = c.post("/api/v1/ask", json={"message": f"Question {i} about press 7 " + "detail " * 300,
                                         **({"conversation_id": conv} if conv else {})}).json()
        conv = res["conversation_id"]
    run_jobs()
    db, ws = ws_of(c)
    s = memory.summary(db, conv)
    assert s and "press 7" in s["summary"]
    turns = agent.recent_turns(db, conv)
    assert turns[0]["role"] == "user" and sum(len(t["content"]) for t in turns) <= agent.HISTORY_CHARS + 4000
    assert len(turns) <= 12
    assert c.get(f"/api/v1/conversations/{conv}/summary").json()["summary"]


# ---------------------------------------------------------------- context engineering
def test_context_has_the_workspace_map_relevant_lessons_and_memories(signed_in):
    from irris import agent, memory, store
    c = signed_in
    c.patch("/api/v1/workspace", json={"about": "We mould PA66 housings on line 3.", "instructions": "Answer in metric."})
    r = c.post("/api/v1/ingest/file", files={"file": ("press7.md", "# Press 7\n\nPlaten 175-185 C.", "text/markdown")}).json()
    c.post("/api/v1/procedures", json={"name": "Mould change", "steps": ["Lock out"]})
    db, ws = ws_of(c)
    for i in range(300):
        store.add_lesson(db, ws["id"], f"Rule {i}: the canteen schedule changes on day {i}.")
    store.add_lesson(db, ws["id"], "Press 7's display reads 5 C hot; subtract 5 from it.")
    memory.add(db, ws["id"], "Press 7 platen heater was replaced on 3 October.", importance=0.8)
    agent._maps.clear()
    system = agent.build_system(db, ws, "", None, "what's the real platen temperature on press 7?")
    assert "We mould PA66" in system and "Answer in metric." in system
    assert "subtract 5" in system and "more lessons not shown" in system
    assert "heater was replaced" in system
    assert f"press7.md [{r['record_id']}]" in system and "Mould change" in system
    assert len(system) <= agent.CONTEXT_CHARS + 8000


def test_tool_results_are_trimmed_to_fit():
    from irris import agent
    big = {"results": [{"text": "x" * 900, "n": i} for i in range(200)], "total": 200}
    out = agent.fit(big, 6000)
    assert len(out) <= 6000 and "left out to fit" in json.loads(out)["truncated"]
    assert agent.trim("a" * 100, 50).count("cut") == 1


def test_agent_reads_a_document_and_cites_its_page(signed_in, monkeypatch):
    from irris import llm
    c = signed_in
    pages = [f"Page {i}: general notes." for i in range(1, 9)]
    pages[5] = "Torque the M8 clamp bolts to 60 Nm."
    rid = c.post("/api/v1/ingest/file", files={"file": ("t.pdf", make_pdf(pages), "application/pdf")}).json()["record_id"]
    step = {"n": 0}

    async def fake(models, messages, tools=None, **kw):
        step["n"] += 1
        if step["n"] == 1:
            assert rid in messages[1]["content"]          # the workspace map gave it the id
            return {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
                "name": "read", "arguments": json.dumps({"record_id": rid, "from_page": 5, "to_page": 7})}}]}
        result = json.loads(messages[-1]["content"])
        cite = next(p["cite"] for p in result["content"] if "60 Nm" in p["text"])
        return {"content": f"Torque them to 60 Nm {cite}."}

    monkeypatch.setattr(llm, "chat_chain", fake)
    monkeypatch.setattr(llm, "chain", lambda ws, kind: [llm.Model("http://m", "", "m")] if kind == "llm" else [])
    res = c.post("/api/v1/ask", json={"message": "what torque for the clamp bolts?"}).json()
    assert res["tools"] == ["read"] and res["sources"][0]["page"] == 6 and res["sources"][0]["record_id"] == rid


# ---------------------------------------------------------------- numbers
def test_stats_and_insights(signed_in):
    import time
    c = signed_in
    t0 = time.time() - 20 * 3600
    readings = [{"type": "reading", "source": "plc", "subject": "asset:press-7", "time": t0 + i * 3600,
                 "data": {"value": 180 + (i % 3) * 0.5, "unit": "C", "min": 175, "max": 185}} for i in range(19)]
    readings.append({"type": "reading", "source": "plc", "subject": "asset:press-7", "time": t0 + 19 * 3600,
                     "data": {"value": 196.0, "unit": "C", "min": 175, "max": 185}})
    readings += [{"type": "reading", "source": "plc", "subject": "asset:dryer", "time": t0 + i * 3600,
                  "data": {"value": 80.0 + (0.05 if i % 2 else 0), "unit": "C"}} for i in range(12)]
    c.post("/api/v1/ingest", json=readings)
    s = c.get("/api/v1/stats/series").json()
    press = next(g for g in s["groups"] if g["group"] == "asset:press-7")
    assert press["count"] == 20 and press["max"] == 196.0 and press["out_of_limits"] == 1 and press["unit"] == "C"
    out = c.post("/api/v1/insights/run").json()
    assert out["insights"] >= 2
    found = c.get("/api/v1/insights").json()["insights"]
    kinds = {(i["subject"], i["kind"]) for i in found}
    assert ("asset:press-7", "out_of_limits") in kinds and ("asset:press-7", "anomaly") in kinds
    assert not any(i["subject"] == "asset:dryer" for i in found)
    assert found[0]["severity"] == "high"
    c.post("/api/v1/ingest", json=[{"type": "reading", "source": "plc", "subject": "asset:press-7",
                                    "time": t0 + 20 * 3600 + i, "data": {"value": 180.0, "unit": "C"}}
                                   for i in range(1)])
    from irris import agent
    db, ws = ws_of(c)
    res = asyncio.run(agent.Turn(db, ws).call("stats", {"subject": "asset:press-7"}))
    assert res["groups"][0]["count"] == 21 and res["cite"] == "[1]"


def test_knowledge_status_and_jobs_listing(signed_in):
    c = signed_in
    c.post("/api/v1/ingest", json={"type": "note", "text": "hello"})
    st = c.get("/api/v1/knowledge/status").json()
    assert st["passages"] >= 1 and isinstance(st["active_jobs"], list)
    jobs_list = c.get("/api/v1/jobs", params={"status": "active"}).json()["jobs"]
    assert any(j["kind"] == "embed" for j in jobs_list)
    j = jobs_list[0]
    assert c.post(f"/api/v1/jobs/{j['id']}/cancel").json()["status"] == "cancelled"
    assert c.get("/api/v1/jobs/nope").status_code == 404


def test_mcp_exposes_the_document_tools(signed_in):
    c = signed_in
    key = c.post("/api/v1/keys", json={"name": "agent"}).json()["key"]
    rid = c.post("/api/v1/ingest/file", files={"file": ("m.md", "# Manual\n\nTorque 60 Nm.", "text/markdown")}).json()["record_id"]
    h = {"Authorization": f"Bearer {key}"}
    names = {t["name"] for t in c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                       headers=h).json()["result"]["tools"]}
    assert {"documents", "outline", "read", "search_in", "stats", "recall"} <= names
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "read", "arguments": {"record_id": rid}}}, headers=h).json()
    assert "Torque 60 Nm" in r["result"]["content"][0]["text"]
