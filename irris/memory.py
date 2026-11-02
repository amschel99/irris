"""Memory: what Irris carries between turns and between conversations.

Long-term memories (`memory_items`) have a kind (episodic, semantic, procedural, lesson, reflection, summary, goal),
a source type with a trust level, importance and confidence, and an optional (subject, predicate) key.

- Write gate: only importance >= 0.6 and confidence >= 0.7 from a trusted source is stored (external content has
  trust 0 until verified). Pinned items always pass.
- Dedupe: the same text (normalised, or cosine >= 0.92 with embeddings) in the same workspace, scope and kind
  strengthens the existing item instead of adding one.
- Supersede, don't overwrite: a new value for the same (subject, predicate) closes the old one (valid_to,
  superseded_by) when its source ranks at least as high (user_stated > doc > tool > agent_inferred; newest wins
  within a source). Superseded items stay as history and are never retrieved by default.
- Retrieval: score = 0.5 * RRF(keyword, vector) + 0.2 * recency + 0.2 * importance + 0.1 * trust, with recency
  halving every 14 days (episodic), 180 (lesson), 365 (semantic, procedural); goals and pinned items don't decay.
  Trivial turns ("thanks") skip retrieval.
- Nightly consolidation: three or more agreeing episodic items from the last 30 days become one semantic item with
  their evidence ids (the episodes stay, with lower importance); reflections must cite three items and expire when
  one of them is superseded or deleted; unpinned, never-used, faded items are archived after 90 days.
- Every write, read, update, supersede, archive and delete is logged in `memory_events`.

Conversations get a running summary (compaction) that keeps the goal, what was inspected, decisions, failed
approaches, open questions and pending approvals; pinned items are re-injected every turn, never summarised away.
When the user corrects Irris, the correction is paired with the answer it corrects and becomes a scoped lesson.
"""
from __future__ import annotations

import asyncio
import math
import re

import numpy as np

from irris import jobs, llm, store, usage
from irris.db import Database, dumps, loads, new_id, now

KINDS = ("episodic", "semantic", "procedural", "lesson", "reflection", "summary", "goal")
TRUST = {"user_stated": 1.0, "doc": 0.8, "tool": 0.7, "agent_inferred": 0.5, "external": 0.0}
RANK = {"user_stated": 4, "doc": 3, "tool": 2, "agent_inferred": 1, "external": 0}
HALF_LIFE = {"episodic": 14, "lesson": 180, "semantic": 365, "procedural": 365, "reflection": 90, "summary": 30,
             "goal": None}
GATE_IMPORTANCE = 0.6
GATE_CONFIDENCE = 0.7
DUP_COSINE = 0.92
RRF_K = 60
KEEP_TURNS = 6                 # turns kept verbatim; older ones are compacted
HISTORY_CHARS = 16_000         # the verbatim history budget (agent.py uses the same)
COMPACT_AT = 0.75              # compact when the unsummarised history reaches 75% of the budget
_STOP = set("""a an the and or but if of to in on at for with by from as is are was were be been it this that these those
i you he she we they me my our your their them do does did not no yes can could should would will what which who how
when where why there here about into than then so just also only very more most some any all""".split())
_TRIVIAL = re.compile(r"^\s*(hi|hey|hello|yo|thanks?( you)?|thank you|thx|ok(ay)?|cool|great|nice|good( morning| night)?|"
                      r"bye|cheers|sure|yes|no|yep|nope|got it)[\s!.]*$", re.I)
_CORRECTION = re.compile(r"^\s*(no\b|nope|wrong|that'?s (not|wrong|incorrect)|incorrect|actually\b|not quite|you'?re wrong|"
                         r"it should be|it'?s actually|that is wrong)|\b(should be|is wrong|was wrong|isn'?t right)\b", re.I)


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9\-\.]+", text.lower()) if w not in _STOP and len(w) > 1}


def overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def normalise(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def is_trivial(message: str) -> bool:
    return bool(_TRIVIAL.match(message or "")) or len((message or "").strip()) < 3


def looks_like_correction(message: str) -> bool:
    return bool(_CORRECTION.search(message or ""))


def recency(row: dict, t: float | None = None) -> float:
    if row.get("pinned"):
        return 1.0
    hl = HALF_LIFE.get(row["kind"], 90)
    if hl is None:
        return 1.0
    age = ((t or now()) - float(row.get("last_used") or row["updated"])) / 86400
    return 0.5 ** (max(0.0, age) / hl)


# ---------------------------------------------------------------- vectors (cached by text)
_vec_cache: dict[str, np.ndarray] = {}


def _vectors(texts: list[str]) -> list[np.ndarray] | None:
    from irris import embed
    e = embed.get()
    if e is None or not texts:
        return None
    try:
        if len(_vec_cache) > 50_000:
            _vec_cache.clear()
        todo = [t for t in dict.fromkeys(texts) if t not in _vec_cache]
        for i in range(0, len(todo), 256):
            for t, v in zip(todo[i:i + 256], e.passages(todo[i:i + 256])):
                _vec_cache[t] = v
        return [_vec_cache[t] for t in texts]
    except Exception:  # noqa: BLE001
        return None


def _query_vec(text: str) -> np.ndarray | None:
    from irris import embed
    e = embed.get()
    if e is None:
        return None
    try:
        return e.query(text)
    except Exception:  # noqa: BLE001
        return None


def rank_texts(query: str, texts: list[str], k: int) -> list[int]:
    """Indexes of the k texts most relevant to the query (RRF of keyword and vector rankings)."""
    return [i for i, _ in _rrf_rank(query, texts)[:k]]


def _rrf_rank(query: str, texts: list[str]) -> list[tuple[int, float]]:
    """[(index, normalised RRF score 0..1)], best first. Only texts relevant by either signal are ranked."""
    qt = tokens(query)
    kw = sorted(((i, overlap(qt, tokens(t))) for i, t in enumerate(texts)), key=lambda x: -x[1])
    rankings = [[i for i, s in kw if s > 0]]
    q = _query_vec(query) if texts else None
    vecs = _vectors(texts) if q is not None else None
    if vecs is not None:
        sims = [float(v @ q) for v in vecs]
        rankings.append([i for i in sorted(range(len(texts)), key=lambda i: -sims[i]) if sims[i] > 0.3])
    score: dict[int, float] = {}
    for r in rankings:
        for pos, i in enumerate(r):
            score[i] = score.get(i, 0.0) + 1.0 / (RRF_K + pos + 1)
    best = len(rankings) / (RRF_K + 1)
    return sorted(((i, s / best) for i, s in score.items()), key=lambda x: -x[1])


# ---------------------------------------------------------------- events
def event(db: Database, workspace_id: str, memory_id: str, kind: str, actor: str = "", detail: dict | None = None) -> None:
    db.execute("INSERT INTO memory_events (workspace_id, memory_id, event, actor, detail, created) VALUES (?,?,?,?,?,?)",
               [workspace_id, memory_id, kind, actor[:120], dumps(detail or {}), now()])


def events(db: Database, workspace_id: str, memory_id: str = "", limit: int = 200) -> list[dict]:
    where, params = "workspace_id = ?", [workspace_id]
    if memory_id:
        where += " AND memory_id = ?"
        params.append(memory_id)
    rows = db.execute(f"SELECT * FROM memory_events WHERE {where} ORDER BY id DESC LIMIT ?", [*params, limit])
    return [{**r, "detail": loads(r["detail"], {})} for r in rows]


# ---------------------------------------------------------------- items
def memory_api(r: dict) -> dict:
    return {"id": r["id"], "text": r["text"], "kind": r["kind"], "scope": "personal" if r["user_id"] else "team",
            "subject": r["subject"], "predicate": r["predicate"], "importance": round(float(r["importance"]), 2),
            "confidence": round(float(r["confidence"]), 2), "source_type": r["source_type"],
            "trust": round(float(r["trust"]), 2), "source": r["source"], "pinned": bool(r["pinned"]),
            "status": r["status"], "superseded_by": r["superseded_by"] or None,
            "evidence_ids": loads(r["evidence_ids"], []), "uses": r["access_count"], "valid_from": r["valid_from"],
            "valid_to": r["valid_to"], "created": r["created"], "updated": r["updated"], "last_used": r["last_used"]}


def _get(db: Database, workspace_id: str, memory_id: str) -> dict | None:
    return db.one("SELECT * FROM memory_items WHERE id = ? AND workspace_id = ?", [memory_id, workspace_id])


def _unit(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def write(db: Database, workspace_id: str, text: str, *, kind: str = "semantic", user_id: str = "", subject: str = "",
          predicate: str = "", importance: float = 0.6, confidence: float = 0.8, source_type: str = "agent_inferred",
          source: str = "", pinned: bool = False, evidence_ids: list[str] | None = None, actor: str = "") -> dict:
    """Store a memory through the gate. Returns {"status": added|merged|superseded|kept_old|rejected, "memory", "reason"}."""
    text = " ".join(str(text).split())[:800]
    if not text:
        raise ValueError("a memory needs text")
    kind = kind if kind in KINDS else "semantic"
    source_type = source_type if source_type in TRUST else "agent_inferred"
    trust = TRUST[source_type]
    importance, confidence = _unit(importance), _unit(confidence)
    subject, predicate = subject.strip().lower()[:160], predicate.strip().lower()[:120]
    if not pinned and (importance < GATE_IMPORTANCE or confidence < GATE_CONFIDENCE or trust <= 0):
        why = "untrusted source" if trust <= 0 else f"importance {importance:.2f} / confidence {confidence:.2f} below the gate"
        event(db, workspace_id, "", "reject", actor, {"text": text[:200], "reason": why, "kind": kind})
        return {"status": "rejected", "memory": None, "reason": why}

    t = now()
    norm = normalise(text)
    same = db.execute("SELECT * FROM memory_items WHERE workspace_id = ? AND user_id = ? AND kind = ? AND status = 'active'",
                      [workspace_id, user_id, kind])
    dup = next((r for r in same if r["norm"] == norm), None)
    if dup is None and same:
        vecs = _vectors([text] + [r["text"] for r in same])
        if vecs is not None:
            sims = [float(vecs[0] @ v) for v in vecs[1:]]
            best = max(range(len(sims)), key=lambda i: sims[i])
            if sims[best] >= DUP_COSINE:
                dup = same[best]
    if dup is not None:
        db.execute("UPDATE memory_items SET importance = ?, access_count = access_count + 1, pinned = ?, updated = ? "
                   "WHERE id = ?", [min(1.0, max(importance, float(dup["importance"])) + 0.05),
                                    1 if (pinned or dup["pinned"]) else 0, t, dup["id"]])
        event(db, workspace_id, dup["id"], "merge", actor, {"text": text[:200]})
        return {"status": "merged", "memory": memory_api(_get(db, workspace_id, dup["id"])), "reason": "duplicate"}

    mid = new_id()
    old = None
    if subject and predicate:
        old = db.one("SELECT * FROM memory_items WHERE workspace_id = ? AND user_id = ? AND subject = ? AND predicate = ? "
                     "AND status = 'active' ORDER BY updated DESC", [workspace_id, user_id, subject, predicate])
    status, superseded_by, valid_to = "active", "", None
    if old is not None and RANK[source_type] < RANK.get(old["source_type"], 1):
        status, superseded_by, valid_to = "superseded", old["id"], t        # kept as history; the better source stands
    db.execute("INSERT INTO memory_items (id, workspace_id, user_id, kind, text, norm, subject, predicate, importance, "
               "confidence, source_type, trust, source, pinned, status, evidence_ids, superseded_by, valid_from, "
               "valid_to, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               [mid, workspace_id, user_id, kind, text, norm, subject, predicate, importance, confidence, source_type,
                trust, source[:200], 1 if pinned else 0, status, dumps(evidence_ids or []), superseded_by, t, valid_to,
                t, t])
    if status == "superseded":
        event(db, workspace_id, mid, "write", actor, {"text": text[:200], "superseded_on_arrival_by": old["id"]})
        return {"status": "kept_old", "memory": memory_api(_get(db, workspace_id, mid)),
                "reason": f"{old['source_type']} outranks {source_type}"}
    event(db, workspace_id, mid, "write", actor, {"text": text[:200], "kind": kind, "source_type": source_type})
    if old is not None:
        _supersede(db, workspace_id, old["id"], mid, actor)
        return {"status": "superseded", "memory": memory_api(_get(db, workspace_id, mid)), "reason": f"replaces {old['id']}"}
    return {"status": "added", "memory": memory_api(_get(db, workspace_id, mid)), "reason": ""}


def _supersede(db: Database, workspace_id: str, old_id: str, new_id_: str, actor: str) -> None:
    db.execute("UPDATE memory_items SET status = 'superseded', superseded_by = ?, valid_to = ?, updated = ? WHERE id = ?",
               [new_id_, now(), now(), old_id])
    event(db, workspace_id, old_id, "supersede", actor, {"by": new_id_})
    _expire_reflections(db, workspace_id, old_id, actor)


def _expire_reflections(db: Database, workspace_id: str, gone_id: str, actor: str) -> None:
    for r in db.execute("SELECT id, evidence_ids FROM memory_items WHERE workspace_id = ? AND kind = 'reflection' AND "
                        "status = 'active'", [workspace_id]):
        if gone_id in loads(r["evidence_ids"], []):
            db.execute("UPDATE memory_items SET status = 'expired', valid_to = ?, updated = ? WHERE id = ?",
                       [now(), now(), r["id"]])
            event(db, workspace_id, r["id"], "expire", actor, {"because": gone_id})


def add(db: Database, workspace_id: str, text: str, *, user_id: str = "", kind: str = "semantic",
        importance: float = 0.7, source: str = "", pinned: bool = False, subject: str = "", predicate: str = "") -> dict:
    """A memory the team states directly (the API and the dashboard): trusted, full confidence."""
    res = write(db, workspace_id, text, kind=kind, user_id=user_id, importance=max(_unit(importance), GATE_IMPORTANCE),
                confidence=1.0, source_type="user_stated", source=source, pinned=pinned, subject=subject,
                predicate=predicate, actor=source)
    return res["memory"]


def update(db: Database, workspace_id: str, memory_id: str, fields: dict, actor: str = "") -> dict | None:
    row = _get(db, workspace_id, memory_id)
    if not row:
        return None
    text = " ".join(str(fields.get("text", row["text"])).split())[:800] or row["text"]
    kind = fields.get("kind") if fields.get("kind") in KINDS else row["kind"]
    imp = _unit(fields.get("importance", row["importance"]))
    pin = 1 if fields.get("pinned", bool(row["pinned"])) else 0
    db.execute("UPDATE memory_items SET text = ?, norm = ?, kind = ?, importance = ?, pinned = ?, updated = ? WHERE id = ?",
               [text, normalise(text), kind, imp, pin, now(), memory_id])
    event(db, workspace_id, memory_id, "update", actor,
          {k: fields[k] for k in fields if k in ("text", "kind", "importance", "pinned")})
    return memory_api(_get(db, workspace_id, memory_id))


def forget(db: Database, workspace_id: str, memory_id: str, actor: str = "") -> bool:
    """Hard delete everywhere: the item, its text in the event log, its place in other items' evidence."""
    row = _get(db, workspace_id, memory_id)
    if not row:
        return False
    db.execute("DELETE FROM memory_items WHERE id = ?", [memory_id])
    db.execute("UPDATE memory_events SET detail = '{}' WHERE workspace_id = ? AND memory_id = ?", [workspace_id, memory_id])
    _expire_reflections(db, workspace_id, memory_id, actor)
    for r in db.execute("SELECT id, evidence_ids FROM memory_items WHERE workspace_id = ? AND evidence_ids LIKE ?",
                        [workspace_id, f"%{memory_id}%"]):
        ev = [e for e in loads(r["evidence_ids"], []) if e != memory_id]
        db.execute("UPDATE memory_items SET evidence_ids = ? WHERE id = ?", [dumps(ev), r["id"]])
    db.execute("UPDATE memory_items SET superseded_by = '' WHERE workspace_id = ? AND superseded_by = ?",
               [workspace_id, memory_id])
    _vec_cache.pop(row["text"], None)
    event(db, workspace_id, memory_id, "delete", actor, {})
    return True


def listing(db: Database, workspace_id: str, user_id: str = "", q: str = "", history: bool = False,
            limit: int = 300) -> list[dict]:
    status = "" if history else "AND status = 'active'"
    rows = db.execute(f"SELECT * FROM memory_items WHERE workspace_id = ? AND (user_id = '' OR user_id = ?) {status} "
                      f"ORDER BY pinned DESC, importance DESC, updated DESC LIMIT 3000", [workspace_id, user_id])
    if q:
        ranked = [rows[i] for i, _ in _rrf_rank(q, [r["text"] for r in rows])]
        seen = {r["id"] for r in ranked}
        rows = ranked + [r for r in rows if r["id"] not in seen and q.lower() in r["text"].lower()]
    return [memory_api(r) for r in rows[:limit]]


def chain(db: Database, workspace_id: str, memory_id: str) -> list[dict]:
    """A fact's history: every value its (subject, predicate) has had, newest first."""
    row = _get(db, workspace_id, memory_id)
    if not row:
        return []
    if not (row["subject"] and row["predicate"]):
        return [memory_api(row)]
    rows = db.execute("SELECT * FROM memory_items WHERE workspace_id = ? AND user_id = ? AND subject = ? AND predicate = ? "
                      "ORDER BY created DESC", [workspace_id, row["user_id"], row["subject"], row["predicate"]])
    return [memory_api(r) for r in rows]


def pinned(db: Database, workspace_id: str, user_id: str = "") -> list[dict]:
    rows = db.execute("SELECT * FROM memory_items WHERE workspace_id = ? AND (user_id = '' OR user_id = ?) AND pinned = 1 "
                      "AND status = 'active' ORDER BY updated", [workspace_id, user_id])
    return [memory_api(r) for r in rows]


def retrieve(db: Database, workspace_id: str, user_id: str, query: str, k: int = 8, *, touch: bool = True,
             kinds: tuple[str, ...] | None = None, include_pinned: bool = False, actor: str = "") -> list[dict]:
    """The k memories worth knowing for this query. Superseded, expired and archived items never come back."""
    if is_trivial(query):
        return []
    rows = db.execute("SELECT * FROM memory_items WHERE workspace_id = ? AND (user_id = '' OR user_id = ?) AND "
                      "status = 'active' LIMIT 5000", [workspace_id, user_id])
    rows = [r for r in rows if (include_pinned or not r["pinned"]) and (not kinds or r["kind"] in kinds)]
    if not rows:
        return []
    t = now()
    scored = []
    for i, rel in _rrf_rank(query, [r["text"] for r in rows]):
        r = rows[i]
        scored.append((0.5 * rel + 0.2 * recency(r, t) + 0.2 * float(r["importance"]) + 0.1 * float(r["trust"]), r))
    scored.sort(key=lambda x: -x[0])
    top = scored[:k]
    if touch and top:
        ids = [r["id"] for _, r in top]
        marks = ",".join("?" * len(ids))
        db.execute(f"UPDATE memory_items SET access_count = access_count + 1, last_used = ? WHERE id IN ({marks})",
                   [t, *ids])
        for s, r in top:
            event(db, workspace_id, r["id"], "read", actor, {"score": round(s, 3)})
    return [{**memory_api(r), "score": round(s, 3)} for s, r in top]


def relevant(db: Database, workspace_id: str, user_id: str, query: str, k: int = 8, touch: bool = True) -> list[dict]:
    return retrieve(db, workspace_id, user_id, query, k, touch=touch)


# ---------------------------------------------------------------- task types and scoped lessons
TASK_WORDS = {
    "documents": r"\b(manual|document|doc|page|chapter|book|handbook|spec(ification)?|datasheet|standard|pdf|says?)\b",
    "data": r"\b(sql|database|table|query|rows?|average|mean|trend|count|how many|total|max(imum)?|min(imum)?|"
            r"stats?|chart|readings?|kpi|oee|yield|scrap rate)\b",
    "camera": r"\b(camera|see|look|picture|photo|gauge|display|dial|screen|in front of me|bench)\b",
    "procedure": r"\b(procedure|sop|step|steps|run|checklist|batch record|guide me)\b",
}


def task_type(message: str) -> str:
    m = (message or "").lower()
    best, hits = "general", 0
    for name, pat in TASK_WORDS.items():
        n = len(re.findall(pat, m))
        if n > hits:
            best, hits = name, n
    return best


def scoped_lessons(db: Database, workspace_id: str, message: str, k: int = 12) -> list[dict]:
    """Lessons for this turn: relevance, plus those scoped to this task type, time-weighted (180-day half-life)."""
    lessons = store.lessons(db, workspace_id, limit=2000)
    if not lessons:
        return []
    task = task_type(message)
    rel = dict(_rrf_rank(message, [l["text"] for l in lessons])) if not is_trivial(message) else {}
    t = now()
    scored = []
    for i, l in enumerate(lessons):
        scope = (l.get("subject") or "").lower()
        scope_match = 1.0 if scope == f"task:{task}" else 0.4 if scope.startswith("tool:") else 0.0
        rec = 0.5 ** (max(0.0, (t - l["created"]) / 86400) / 180)
        scored.append((0.55 * rel.get(i, 0.0) + 0.25 * scope_match + 0.2 * rec, l))
    scored.sort(key=lambda x: -x[0])
    return [l for _, l in scored[:k]]


# ---------------------------------------------------------------- conversation summaries (compaction)
def summary(db: Database, conversation_id: str) -> dict | None:
    return db.one("SELECT summary, upto FROM conv_summaries WHERE conversation_id = ?", [conversation_id])


COMPACT_PROMPT = """You maintain the working summary of a conversation between Irris (the AI twin of {team}) and the team,
so the conversation can continue without the old turns. Fold the new turns into the summary.

PRESERVE, precisely:
- the task or goal they're working on, and its current state
- the records, documents (with pages), tables and cameras already inspected, and what each one held
- decisions made, and their reasons
- approaches that failed, and why
- open questions, and anything awaiting approval or a reply
- numbers, names, ids and units that may be needed again
Everything else may be dropped. At most 350 words, plain text, no preamble.

Summary so far:
{summary}

New turns:
{turns}"""


def needs_compaction(msgs: list[dict], upto: int) -> list[dict]:
    """The messages to fold into the summary: everything older than the last KEEP_TURNS turns, once the
    unsummarised history passes COMPACT_AT of its budget (or more than KEEP_TURNS + 2 turns are waiting)."""
    pending = [m for m in msgs if m["id"] > upto]
    keep = KEEP_TURNS * 2
    if len(pending) <= keep:
        return []
    size = sum(len(m["content"]) for m in pending)
    if size < COMPACT_AT * HISTORY_CHARS and len(pending) <= keep + 4:
        return []
    return pending[:-keep]


async def compact(db: Database, workspace_id: str, models, team: str, conv_id: str) -> bool:
    msgs = db.execute("SELECT id, role, content FROM messages WHERE conversation_id = ? ORDER BY id", [conv_id])
    cur = summary(db, conv_id)
    fold = needs_compaction(msgs, cur["upto"] if cur else 0)
    if not fold:
        return False
    turns = "\n".join(f"{m['role']}: {m['content'][:2500]}" for m in fold)[-40_000:]
    msg = await usage.chat(db, workspace_id, "compaction", models, [{"role": "user", "content": COMPACT_PROMPT.format(
        team=team, summary=(cur or {}).get("summary") or "(none yet)", turns=turns)}], max_tokens=800, ref=conv_id)
    text = (msg.get("content") or "").strip()
    if not text:
        return False
    last = fold[-1]["id"]
    if cur:
        db.execute("UPDATE conv_summaries SET summary = ?, upto = ?, updated = ? WHERE conversation_id = ?",
                   [text, last, now(), conv_id])
    else:
        db.execute("INSERT INTO conv_summaries (conversation_id, summary, upto, updated) VALUES (?,?,?,?)",
                   [conv_id, text, last, now()])
    return True


# ---------------------------------------------------------------- after each turn
MEMORY_PROMPT = """You are the memory of Irris, the AI twin of {team}. From the latest exchange, pick out durable things
worth remembering in future conversations: facts about their operation, assets, products, people and roles,
preferences, goals, open issues, decisions, and how they do things. Not: one-off questions, things already in their
documents, small talk, or anything Irris said that the user didn't confirm.

Known memories that may overlap (reuse the same subject and predicate to update a fact):
{known}

Latest exchange:
User ({user}): {question}
Irris: {answer}

Return JSON: {{"memories": [{{"text": "one self-contained sentence",
  "kind": "semantic|procedural|episodic|goal", "subject": "what it's about, e.g. asset:press-7 or person:bo",
  "predicate": "which property, e.g. platen_limit or role (empty if not a keyed fact)",
  "source": "user_stated|agent_inferred", "scope": "team|personal",
  "importance": 0.0-1.0, "confidence": 0.0-1.0}}]}}
Return {{"memories": []}} when nothing is worth keeping. At most 4."""

LESSON_PROMPT = """Irris answered a question and the user corrected it. Write the lesson Irris should apply next time.
Question: {question}
Irris's answer: {answer}
Tools Irris used: {tools}
The user's correction: {correction}

Return JSON: {{"lesson": "one rule, specific and reusable (not a restatement of this one answer)",
"scope": "task:documents|task:data|task:camera|task:procedure|task:general|tool:<tool name>",
"worth_keeping": true|false}}"""


def after_turn(db: Database, workspace_id: str, conversation_id: str, user_id: str) -> None:
    jobs.enqueue(db, workspace_id, "memory", {"conversation_id": conversation_id, "user_id": user_id},
                 title="Remembering the conversation", dedupe=f"memory:{conversation_id}")


@jobs.handler("memory")
async def memory_job(ctx: jobs.JobCtx) -> dict:
    db, ws_id = ctx.db, ctx.workspace_id
    ws = ctx.workspace()
    models = llm.chain(ws, "llm")
    if not models or not ws:
        return {"skipped": "no reasoning model"}
    conv_id, user_id = ctx.payload["conversation_id"], ctx.payload.get("user_id", "")
    team = f"{ws['name']} ({ws.get('industry') or 'manufacturing and engineering'})"
    msgs = await asyncio.to_thread(db.execute, "SELECT id, role, content, meta FROM messages WHERE conversation_id = ? "
                                               "ORDER BY id", [conv_id])
    if not msgs:
        return {"skipped": "empty conversation"}
    result: dict = {"compacted": False, "memories": {}, "lesson": None}
    try:
        result["compacted"] = await compact(db, ws_id, models, team, conv_id)
        result["memories"] = await _distil(db, models, team, ws_id, user_id, conv_id, msgs)
        result["lesson"] = await _learn_from_correction(db, models, ws_id, conv_id, msgs)
    except usage.BudgetExhausted:
        raise jobs.Retry("the daily model budget is used up; continuing tomorrow", after=usage.seconds_to_reset())
    except llm.ModelError as e:
        raise jobs.Retry(f"the reasoning model is unavailable ({str(e)[:120]})", after=120)
    return result


async def _distil(db: Database, models, team: str, ws_id: str, user_id: str, conv_id: str, msgs: list[dict]) -> dict:
    last_user = next((m for m in reversed(msgs) if m["role"] == "user"), None)
    last_bot = next((m for m in reversed(msgs) if m["role"] == "assistant"), None)
    if not last_user or not last_bot or is_trivial(last_user["content"]):
        return {}
    user = db.one("SELECT name, email FROM users WHERE id = ?", [user_id]) if user_id else None
    who = (user or {}).get("name") or (user or {}).get("email") or "a team member"
    known = retrieve(db, ws_id, user_id, last_user["content"], k=12, touch=False)
    prompt = MEMORY_PROMPT.format(team=team, user=who, question=last_user["content"][:3000],
                                  answer=last_bot["content"][:3000],
                                  known="\n".join(f"- {m['text']} (subject={m['subject']}, predicate={m['predicate']})"
                                                  for m in known) or "(none)")
    msg = await usage.chat(db, ws_id, "memory", models, [{"role": "user", "content": prompt}], json_mode=True,
                           max_tokens=800, ref=conv_id)
    items = llm.parse_json(msg.get("content") or "").get("memories") or []
    counts: dict[str, int] = {}
    for it in items[:4]:
        if not isinstance(it, dict) or not str(it.get("text") or "").strip():
            continue
        source_type = "user_stated" if it.get("source") == "user_stated" else "agent_inferred"
        res = write(db, ws_id, str(it["text"]), kind=str(it.get("kind") or "semantic"),
                    user_id=user_id if it.get("scope") == "personal" else "", subject=str(it.get("subject") or ""),
                    predicate=str(it.get("predicate") or ""), importance=it.get("importance", 0.5),
                    confidence=it.get("confidence", 0.5), source_type=source_type, source=f"conversation:{conv_id}",
                    actor="memory-job")
        counts[res["status"]] = counts.get(res["status"], 0) + 1
    return counts


async def _learn_from_correction(db: Database, models, ws_id: str, conv_id: str, msgs: list[dict]) -> str | None:
    """When the latest user message corrects the previous answer, pair them and keep a scoped lesson."""
    if len(msgs) < 3:
        return None
    last_user = msgs[-2] if msgs[-1]["role"] == "assistant" else msgs[-1]
    if last_user["role"] != "user" or not looks_like_correction(last_user["content"]):
        return None
    idx = msgs.index(last_user)
    prev_bot = next((m for m in reversed(msgs[:idx]) if m["role"] == "assistant"), None)
    prev_user = next((m for m in reversed(msgs[:idx]) if m["role"] == "user"), None)
    if not prev_bot or not prev_user:
        return None
    tools = ", ".join(loads(prev_bot.get("meta"), {}).get("tools", [])) or "none"
    msg = await usage.chat(db, ws_id, "lesson", models, [{"role": "user", "content": LESSON_PROMPT.format(
        question=prev_user["content"][:2000], answer=prev_bot["content"][:2000], tools=tools,
        correction=last_user["content"][:2000])}], json_mode=True, max_tokens=400, ref=conv_id)
    out = llm.parse_json(msg.get("content") or "")
    lesson = str(out.get("lesson") or "").strip()
    if not lesson or out.get("worth_keeping") is False:
        return None
    scope = str(out.get("scope") or "")[:60]
    if not re.match(r"^(task|tool):[a-z_]+$", scope):
        scope = f"task:{task_type(prev_user['content'])}"
    saved = store.add_lesson(db, ws_id, lesson, subject=scope, source="correction")
    event(db, ws_id, saved["id"], "lesson", "memory-job", {"scope": scope, "from_message": prev_bot["id"]})
    return saved["id"]


# ---------------------------------------------------------------- nightly consolidation
REFLECT_PROMPT = """You are Irris, reflecting on what you know about {team}. From the memories below, write at most 3 insights
that connect several of them (patterns, recurring problems, what the team cares about). Each must cite at least 3
memory ids it's based on. Return JSON: {{"reflections": [{{"text": str, "evidence_ids": [str, ...],
"importance": 0.0-1.0, "confidence": 0.0-1.0}}]}} (empty if nothing is well supported).

{items}"""

CONSOLIDATE_PROMPT = """These notes from different conversations say the same thing. Write it once, as one durable fact.
Return JSON: {{"text": str, "subject": str, "predicate": str}}

{items}"""


def _clusters(rows: list[dict]) -> list[list[dict]]:
    vecs = _vectors([r["text"] for r in rows])
    toks = [tokens(r["text"]) for r in rows]
    used, out = set(), []
    for i in range(len(rows)):
        if i in used:
            continue
        group = [i]
        for j in range(i + 1, len(rows)):
            if j in used:
                continue
            sim = float(vecs[i] @ vecs[j]) if vecs is not None else overlap(toks[i], toks[j])
            if sim >= (0.8 if vecs is not None else 0.5):
                group.append(j)
        if len(group) >= 3:
            used.update(group)
            out.append([rows[k] for k in group])
    return out


async def consolidate(db: Database, workspace_id: str, models) -> dict:
    t = now()
    ws = db.one("SELECT name, industry FROM workspaces WHERE id = ?", [workspace_id]) or {"name": "the team", "industry": ""}
    team = f"{ws['name']} ({ws.get('industry') or 'manufacturing and engineering'})"
    out = {"semantic": 0, "reflections": 0, "archived": 0, "expired": 0}
    episodic = db.execute("SELECT * FROM memory_items WHERE workspace_id = ? AND kind = 'episodic' AND status = 'active' "
                          "AND created >= ? ORDER BY created", [workspace_id, t - 30 * 86400])
    by_scope: dict[str, list[dict]] = {}
    for r in episodic:
        by_scope.setdefault(r["user_id"], []).append(r)
    for scope_user, rows in by_scope.items():
        for group in _clusters(rows):
            text, subject, predicate = max((r["text"] for r in group), key=len), "", ""
            if models:
                msg = await usage.chat(db, workspace_id, "consolidate", models, [{"role": "user", "content":
                    CONSOLIDATE_PROMPT.format(items="\n".join(f"- {r['text']}" for r in group))}], json_mode=True,
                    max_tokens=300)
                o = llm.parse_json(msg.get("content") or "")
                text = str(o.get("text") or text)
                subject, predicate = str(o.get("subject") or ""), str(o.get("predicate") or "")
            source_type = min((r["source_type"] for r in group), key=lambda s: RANK.get(s, 0))
            res = write(db, workspace_id, text, kind="semantic", user_id=scope_user, subject=subject,
                        predicate=predicate, importance=max(float(r["importance"]) for r in group), confidence=0.8,
                        source_type=source_type, source="consolidation", evidence_ids=[r["id"] for r in group],
                        actor="consolidation")
            if res["status"] in ("added", "superseded", "merged"):
                out["semantic"] += 1
                for r in group:
                    db.execute("UPDATE memory_items SET importance = ?, updated = ? WHERE id = ?",
                               [max(0.1, float(r["importance"]) * 0.6), t, r["id"]])
                    event(db, workspace_id, r["id"], "consolidate", "consolidation", {"into": res["memory"]["id"]})
    if models:
        recent = db.execute("SELECT * FROM memory_items WHERE workspace_id = ? AND status = 'active' AND kind NOT IN "
                            "('reflection', 'summary') ORDER BY updated DESC LIMIT 40", [workspace_id])
        if len(recent) >= 6:
            ids = {r["id"] for r in recent}
            msg = await usage.chat(db, workspace_id, "reflection", models, [{"role": "user", "content":
                REFLECT_PROMPT.format(team=team, items="\n".join(f"[{r['id']}] {r['text']}" for r in recent))}],
                json_mode=True, max_tokens=700)
            for ref in (llm.parse_json(msg.get("content") or "").get("reflections") or [])[:3]:
                ev = [e for e in (ref.get("evidence_ids") or []) if e in ids] if isinstance(ref, dict) else []
                if len(set(ev)) < 3 or not str(ref.get("text") or "").strip():
                    continue
                res = write(db, workspace_id, str(ref["text"]), kind="reflection",
                            importance=ref.get("importance", 0.6), confidence=ref.get("confidence", 0.7),
                            source_type="agent_inferred", source="reflection", evidence_ids=sorted(set(ev)),
                            actor="consolidation")
                out["reflections"] += res["status"] == "added"
    for r in db.execute("SELECT * FROM memory_items WHERE workspace_id = ? AND status = 'active' AND pinned = 0 AND "
                        "access_count = 0 AND created < ?", [workspace_id, t - 90 * 86400]):
        if recency(r, t) < 0.05:
            db.execute("UPDATE memory_items SET status = 'archived', updated = ? WHERE id = ?", [t, r["id"]])
            event(db, workspace_id, r["id"], "archive", "consolidation", {})
            out["archived"] += 1
    for r in db.execute("SELECT id, evidence_ids FROM memory_items WHERE workspace_id = ? AND kind = 'reflection' AND "
                        "status = 'active'", [workspace_id]):
        ev = loads(r["evidence_ids"], [])
        if ev:
            marks = ",".join("?" * len(ev))
            alive = db.one(f"SELECT COUNT(*) AS n FROM memory_items WHERE id IN ({marks}) AND status = 'active'", ev)["n"]
            if alive < 3:
                db.execute("UPDATE memory_items SET status = 'expired', valid_to = ?, updated = ? WHERE id = ?",
                           [t, t, r["id"]])
                event(db, workspace_id, r["id"], "expire", "consolidation", {"evidence_left": alive})
                out["expired"] += 1
    return out


@jobs.handler("consolidate")
async def consolidate_job(ctx: jobs.JobCtx) -> dict:
    ws = ctx.workspace()
    try:
        return await consolidate(ctx.db, ctx.workspace_id, llm.chain(ws, "llm") if ws else [])
    except usage.BudgetExhausted:
        raise jobs.Retry("the daily model budget is used up; continuing tomorrow", after=usage.seconds_to_reset())
    except llm.ModelError as e:
        raise jobs.Retry(f"the reasoning model is unavailable ({str(e)[:120]})", after=600)


def schedule_consolidation(db: Database) -> int:
    """Queue today's consolidation for workspaces whose memory changed and that haven't had one today."""
    since = usage.day_start()
    n = 0
    for r in db.execute("SELECT DISTINCT workspace_id FROM memory_items WHERE updated >= ?", [now() - 2 * 86400]):
        ws = r["workspace_id"]
        if db.one("SELECT id FROM jobs WHERE workspace_id = ? AND kind = 'consolidate' AND created >= ?", [ws, since]):
            continue
        jobs.enqueue(db, ws, "consolidate", {}, title="Consolidating memory", dedupe=f"consolidate:{ws}")
        n += 1
    return n
