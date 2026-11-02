"""The assistant: understands what the team wants, then searches their data, queries their databases, looks through
their cameras and starts or checks procedures, and answers with citations.

Without a reasoning model configured it still answers: it returns the best passages from search, cited.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable

from irris import connectors, llm, runs, store, vision
from irris.db import Database, dumps, loads, new_id, now
from irris.security import decrypt

MAX_STEPS = 8

SYSTEM = """You are Irris, the AI twin of {workspace}, a {industry} team. You know their plant, products, procedures and
data because they connected it to you. You help engineers, technicians and managers do their work: answer
questions, find things, analyse data, check work through cameras, and guide procedures step by step.

About the team: {about}

Team instructions (always follow): {instructions}

Lessons the team taught you (always apply):
{lessons}

How you work:
- Work out what the person actually wants to do, then use your tools. Search before you answer anything about
  their plant, products, data or procedures. Don't guess their numbers.
- Cite every fact from their data with its marker, like [1] or [2], matching the sources your tools returned.
- For numbers, trends and joins in a connected database, query it (read-only SQL). Look at the schema first.
- When they ask about something physical in front of them, look through the camera.
- When they want to do a procedure, start a guided run on it.
- If the data doesn't have the answer, say so plainly and say what data would answer it.
- Be brief and concrete, like a senior engineer on their team. Use short paragraphs and lists when they help.
- When they correct you, save it as a lesson.
Now: {now} (UTC)."""

TOOLS = [
    {"name": "search", "description": "Full-text search over everything the team connected: documents (with pages), "
     "records, rows, procedures, lessons, batch records. Returns numbered sources to cite.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "types": {"type": "array", "items": {"type": "string"},
                                                 "description": "only these record types, e.g. ['document']"},
         "limit": {"type": "integer", "default": 6}}, "required": ["query"]}},
    {"name": "records", "description": "Recent records filtered by type, subject or source, newest first. Use for "
     "'latest readings of asset:press-7', 'events from line-3 today', 'last batch records'.",
     "parameters": {"type": "object", "properties": {
         "type": {"type": "string"}, "subject": {"type": "string"}, "source": {"type": "string"},
         "hours": {"type": "number", "description": "only the last N hours"}, "limit": {"type": "integer", "default": 20}}}},
    {"name": "databases", "description": "The team's connected databases and their tables and columns.",
     "parameters": {"type": "object", "properties": {"source_id": {"type": "string"}}}},
    {"name": "sql", "description": "Run one read-only SQL query (Postgres dialect) on a connected database. "
     "At most 200 rows come back.",
     "parameters": {"type": "object", "properties": {"source_id": {"type": "string"}, "query": {"type": "string"}},
                    "required": ["source_id", "query"]}},
    {"name": "look", "description": "Look through one of the team's cameras and answer a question about what it sees "
     "(read an instrument, check an assembly, spot a problem).",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"},
                                                     "camera_id": {"type": "string"}}, "required": ["question"]}},
    {"name": "procedures", "description": "The team's procedures (SOPs) with their steps.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "filter by name"}}}},
    {"name": "start_run", "description": "Start a guided run of a procedure, so Irris checks each step through a camera.",
     "parameters": {"type": "object", "properties": {"procedure_id": {"type": "string"},
                                                     "camera_id": {"type": "string"},
                                                     "label": {"type": "string", "description": "batch or job label"}},
                    "required": ["procedure_id"]}},
    {"name": "remember", "description": "Save a lesson the team taught you (a correction, a rule, a preference).",
     "parameters": {"type": "object", "properties": {"lesson": {"type": "string"}, "subject": {"type": "string"}},
                    "required": ["lesson"]}},
    {"name": "log", "description": "Write a record into the team's data: a reading, an event, a note, an observation.",
     "parameters": {"type": "object", "properties": {
         "type": {"type": "string"}, "title": {"type": "string"}, "text": {"type": "string"},
         "subject": {"type": "string"}, "data": {"type": "object"}}, "required": ["type"]}},
]

Emit = Callable[[str, dict], Awaitable[None]]


async def _noop(event: str, data: dict) -> None:
    return None


class Turn:
    """One assistant turn's tool context: the workspace, the sources found so far, the current camera."""

    def __init__(self, db: Database, workspace: dict, user_id: str = "", camera_id: str = "",
                 frame: bytes | None = None):
        self.db, self.ws, self.user_id = db, workspace, user_id
        self.camera_id, self.frame = camera_id, frame
        self.sources: list[dict] = []
        self.files: list[dict] = []
        self.actions: list[dict] = []

    def _cite(self, hit: dict) -> int:
        for s in self.sources:
            if s["record_id"] == hit["record_id"] and s.get("page") == hit.get("page"):
                return s["n"]
        n = len(self.sources) + 1
        self.sources.append({"n": n, "record_id": hit["record_id"], "title": hit.get("title") or hit.get("type"),
                             "type": hit.get("type"), "source": hit.get("source"), "page": hit.get("page"),
                             "excerpt": (hit.get("text") or "")[:300]})
        return n

    async def call(self, name: str, args: dict) -> Any:
        ws_id = self.ws["id"]
        if name == "search":
            hits = store.search(self.db, ws_id, str(args.get("query", "")), limit=min(int(args.get("limit") or 6), 12),
                                types=args.get("types") or None)
            if not hits:
                return {"results": [], "note": "nothing matched; try other words or check records/databases"}
            return {"results": [{"cite": f"[{self._cite(h)}]", "title": h["title"], "type": h["type"],
                                 "source": h["source"], "page": h["page"], "text": h["text"][:1100]} for h in hits]}
        if name == "records":
            since = now() - float(args["hours"]) * 3600 if args.get("hours") else None
            res = store.query(self.db, ws_id, type=args.get("type") or "", subject=args.get("subject") or "",
                              source=args.get("source") or "", since=since, limit=min(int(args.get("limit") or 20), 100),
                              full=True)
            out = []
            for r in res["records"]:
                n = self._cite({"record_id": r["id"], "title": r["title"] or f"{r['type']} {r['subject']}",
                                "type": r["type"], "source": r["source"], "text": r["text"] or dumps(r["data"])})
                out.append({"cite": f"[{n}]", "type": r["type"], "subject": r["subject"], "title": r["title"],
                            "time": time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["time"])), "text": r["text"][:400],
                            "data": r["data"]})
            return {"total": res["total"], "records": out}
        if name == "databases":
            rows = self.db.execute("SELECT * FROM sources WHERE workspace_id = ? AND kind = 'postgres'", [ws_id])
            if args.get("source_id"):
                rows = [r for r in rows if r["id"] == args["source_id"]]
            if not rows:
                return {"databases": [], "note": "no database is connected (Sources → Postgres)"}
            out = []
            for r in rows:
                cfg = loads(r["config"], {})
                tables = cfg.get("schema") or []
                out.append({"source_id": r["id"], "name": r["name"],
                            "tables": [{"table": t["table"], "rows": t.get("rows_estimate"),
                                        "columns": ", ".join(f"{c['name']} {c['type']}" for c in t["columns"][:60])}
                                       for t in tables[:80]]})
            return {"databases": out}
        if name == "sql":
            src = self.db.one("SELECT * FROM sources WHERE id = ? AND workspace_id = ? AND kind = 'postgres'",
                              [args.get("source_id"), ws_id])
            if not src:
                return {"error": "unknown source_id; call databases first"}
            import asyncio
            try:
                res = await asyncio.to_thread(connectors.run, decrypt(src["secret"]), str(args.get("query", "")))
            except connectors.ConnectorError as e:
                return {"error": str(e)}
            n = self._cite({"record_id": f"sql:{src['id']}", "title": f"{src['name']} (SQL)", "type": "sql",
                            "source": src["name"], "text": str(args.get("query", ""))})
            return {"cite": f"[{n}]", **res}
        if name == "look":
            cam = args.get("camera_id") or self.camera_id
            frame = self.frame if (self.frame and (not args.get("camera_id") or cam == self.camera_id)) else None
            if frame is None:
                frame = await camera_frame(self.db, ws_id, cam)
            if frame is None:
                return {"error": "no live camera frame. Open Live on a phone or connect a camera."}
            model = llm.resolve(self.ws, "vlm")
            if not model.ready:
                return {"error": "no vision model is set up (Settings → Models)"}
            out = await vision.look(model, frame, str(args.get("question", "")))
            n = self._cite({"record_id": f"camera:{cam or 'live'}", "title": "Camera", "type": "camera",
                            "source": "camera", "text": out["say"]})
            return {"cite": f"[{n}]", **out}
        if name == "procedures":
            procs = store.procedures(self.db, ws_id)
            if args.get("name"):
                procs = [p for p in procs if str(args["name"]).lower() in p["name"].lower()] or procs
            return {"procedures": [{"id": p["id"], "name": p["name"], "description": p["description"][:300],
                                    "steps": [s["instruction"] for s in p["steps"]]} for p in procs[:30]]}
        if name == "start_run":
            try:
                run = runs.start(self.db, ws_id, str(args.get("procedure_id")),
                                 camera_id=str(args.get("camera_id") or self.camera_id or ""),
                                 user_id=self.user_id, label=str(args.get("label") or ""))
            except (store.NotFound, runs.RunError) as e:
                return {"error": str(e)}
            self.actions.append({"type": "run_started", "run_id": run["id"], "procedure": run["procedure"]["name"]})
            return {"run_id": run["id"], "procedure": run["procedure"]["name"],
                    "first_step": run["steps"][0]["instruction"], "open": f"/app/live?run={run['id']}"}
        if name == "remember":
            lesson = store.add_lesson(self.db, ws_id, str(args.get("lesson", "")), str(args.get("subject") or ""),
                                      source="assistant")
            return {"saved": lesson["id"]}
        if name == "log":
            from irris import irf
            rec = irf.normalize({"type": args.get("type") or "note", "source": "assistant",
                                 "title": args.get("title") or "", "text": args.get("text") or "",
                                 "subject": args.get("subject") or "", "data": args.get("data") or {}}, now=now())
            res = store.write(self.db, ws_id, [rec])
            return {"saved": res["ids"][0]}
        return {"error": f"unknown tool {name}"}


async def camera_frame(db: Database, workspace_id: str, camera_id: str) -> bytes | None:
    if not camera_id:
        cams = db.execute("SELECT id FROM cameras WHERE workspace_id = ?", [workspace_id])
        for c in cams:                       # any camera that is live right now
            f = vision.hub.get(c["id"])
            if f:
                return f
        return None
    cam = db.one("SELECT * FROM cameras WHERE id = ? AND workspace_id = ?", [camera_id, workspace_id])
    if not cam:
        return None
    if cam["kind"] == "url":
        try:
            frame = await vision.fetch_external(decrypt(cam["url"]))
        except Exception:  # noqa: BLE001 - a dead camera is reported as no frame
            return None
        vision.hub.put(cam["id"], frame)
        return frame
    return vision.hub.get(cam["id"])


def _system(db: Database, ws: dict) -> str:
    lessons = store.lessons(db, ws["id"], limit=40)
    return SYSTEM.format(workspace=ws["name"], industry=ws.get("industry") or "manufacturing and engineering",
                         about=ws.get("about") or "(not described yet)",
                         instructions=ws.get("instructions") or "(none)",
                         lessons="\n".join(f"- {l['text']}" for l in lessons) or "(none yet)",
                         now=time.strftime("%Y-%m-%d %H:%M", time.gmtime()))


def history(db: Database, conversation_id: str, limit: int = 20) -> list[dict]:
    rows = db.execute("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                      [conversation_id, limit])
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def _fallback(turn: Turn, message: str) -> str:
    """No reasoning model: answer from search, cited."""
    res = await turn.call("search", {"query": message, "limit": 5})
    if not res.get("results"):
        return ("I couldn't find anything about that in your data yet. Connect more sources, or set up a reasoning "
                "model in Settings → Models so I can reason over it.")
    lines = ["Here's what your data says (no reasoning model is configured, so these are the closest passages):", ""]
    for r in res["results"]:
        where = r["title"] or r["type"]
        if r.get("page"):
            where += f", p. {r['page']}"
        lines.append(f"{r['cite']} **{where}**: {r['text'][:400].strip()}")
    return "\n".join(lines)


async def answer(db: Database, workspace: dict, message: str, *, conversation_id: str | None = None,
                 user_id: str = "", camera_id: str = "", frame: bytes | None = None, context: str = "",
                 emit: Emit = _noop) -> dict:
    """One full turn: plan, use tools, answer. Saves the exchange to the conversation."""
    ws_id = workspace["id"]
    if conversation_id:
        conv = db.one("SELECT id FROM conversations WHERE id = ? AND workspace_id = ?", [conversation_id, ws_id])
        if not conv:
            raise store.NotFound("conversation not found")
    else:
        conversation_id = new_id()
        db.execute("INSERT INTO conversations (id, workspace_id, user_id, title, created, updated) VALUES (?,?,?,?,?,?)",
                   [conversation_id, ws_id, user_id, message.strip()[:80], now(), now()])
    past = history(db, conversation_id)
    db.execute("INSERT INTO messages (conversation_id, role, content, meta, created) VALUES (?,?,?,?,?)",
               [conversation_id, "user", message, "{}", now()])
    turn = Turn(db, workspace, user_id=user_id, camera_id=camera_id, frame=frame)
    started = time.time()
    tools_used: list[str] = []
    model = llm.resolve(workspace, "llm")

    if not model.ready:
        text = await _fallback(turn, message)
    else:
        msgs: list[dict] = [{"role": "system", "content": _system(db, workspace)}, *past,
                            {"role": "user", "content": (context + "\n\n" if context else "") + message}]
        tools = [{"type": "function", "function": t} for t in TOOLS]
        text = ""
        for _ in range(MAX_STEPS):
            try:
                reply = await llm.chat(model, msgs, tools, max_tokens=1500)
            except llm.ModelError as e:
                text = f"I couldn't reach the reasoning model ({e}). " + await _fallback(turn, message)
                break
            calls = reply.get("tool_calls") or []
            if not calls:
                text = (reply.get("content") or "").strip()
                break
            msgs.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": calls})
            for c in calls:
                name = c.get("function", {}).get("name", "")
                try:
                    args = json.loads(c.get("function", {}).get("arguments") or "{}")
                except ValueError:
                    args = {}
                tools_used.append(name)
                await emit("tool", {"name": name, "args": args})
                try:
                    result = await turn.call(name, args)
                except Exception as e:  # noqa: BLE001 - a failing tool is reported to the model, not the user
                    result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                msgs.append({"role": "tool", "tool_call_id": c.get("id", name), "content": dumps(result)[:14000]})
        if not text:
            text = "I ran out of steps before finishing. Ask me again more narrowly and I'll pick it up."

    used = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
    sources = [s for s in turn.sources if s["n"] in used] if used else []
    meta = {"sources": sources, "tools": tools_used, "actions": turn.actions, "seconds": round(time.time() - started, 1)}
    msg_id = db.insert_id("INSERT INTO messages (conversation_id, role, content, meta, created) VALUES (?,?,?,?,?)",
                          [conversation_id, "assistant", text, dumps(meta), now()])
    db.execute("UPDATE conversations SET updated = ? WHERE id = ?", [now(), conversation_id])
    return {"conversation_id": conversation_id, "message_id": msg_id, "reply": text, **meta}
