"""Draft twin from a website (or three questions).

A team pastes its website. A background job reads a few public pages (home plus about/products/services/quality,
at most 4, private addresses refused), and the reasoning model drafts: what they make, likely processes, 3-5 draft
SOPs, standards and regulations that probably apply (marked "likely, verify"), and the documents worth uploading.
Nothing is applied by itself: the person accepts or rejects each piece, and each decision is audited.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx

from irris import jobs, llm, ops, store
from irris.db import Database, dumps, loads, now

MAX_PAGES = 4
MAX_BYTES = 1_500_000
MAX_TEXT = 15_000
TRANSPORT: httpx.AsyncBaseTransport | None = None        # tests swap in a fake site
_LINK = re.compile(r'<a\s[^>]*href=["\']([^"\'#]+)["\']', re.I)
_WANT = re.compile(r"about|company|product|service|solution|capabilit|quality|manufactur|what-we-do|industr", re.I)


class OnboardingError(Exception):
    pass


def html_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", html)
    text = unescape(re.sub(r"(?s)<[^>]+>", " ", html))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


async def _get(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """GET with redirects followed by hand (each hop re-checked against private addresses)."""
    from irris import mcp_client
    for _ in range(4):
        try:
            mcp_client.check_url(url)
        except mcp_client.MCPError as e:
            raise OnboardingError(str(e).replace("MCP URL", "website"))
        r = await client.get(url, headers={"User-Agent": "Irris onboarding (+https://irris-production.up.railway.app)"})
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            continue
        if r.status_code >= 400:
            raise OnboardingError(f"the site answered {r.status_code}")
        if "html" not in r.headers.get("content-type", "html"):
            return url, ""
        return url, r.text[:MAX_BYTES]
    raise OnboardingError("too many redirects")


async def read_site(url: str) -> list[dict]:
    if not re.match(r"^https?://", url.strip(), re.I):
        url = "https://" + url.strip()
    pages = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, transport=TRANSPORT) as client:
        final, html = await _get(client, url)
        pages.append({"url": final, "text": html_text(html)})
        host = urlparse(final).hostname
        links = []
        for href in _LINK.findall(html):
            u = urljoin(final, href)
            if urlparse(u).hostname == host and _WANT.search(u) and u not in links and u != final:
                links.append(u)
        for u in links[:MAX_PAGES - 1]:
            try:
                got, h = await _get(client, u)
                pages.append({"url": got, "text": html_text(h)})
            except (OnboardingError, httpx.HTTPError):
                continue
    if not any(p["text"] for p in pages):
        raise OnboardingError("the website had no readable text")
    return pages


PROMPT = """You are setting up Irris, an AI twin for a manufacturing / engineering / lab team, from what their
website says (or their own short answers). Draft, as JSON:
{"profile": {"summary": str (2-3 sentences: who they are, what they make), "products": [str], "processes": [str]
  (the physical/lab processes they likely run), "market": str},
 "procedures": [3-5 of {"name": str, "description": str, "steps": [4-10 of {"instruction": str (one physical
  action), "check": str (what correct looks like, visible to a camera)}]}] (their most likely critical SOPs:
  receiving/QC, a core production or test process, cleaning/changeover, release),
 "standards": [{"name": str (e.g. ISO 9001, GMP, HACCP, ISO 17025, ISO 13485, FSSC 22000, KEBS), "why": str,
  "confidence": "likely"|"possible"}],
 "documents": [{"name": str (e.g. "Current SOPs for X", "Equipment manuals", "Batch records from last quarter",
  "Specifications"), "why": str}] (4-8 items they should upload so Irris knows their operation)}
Rules: base everything on the text; where you infer, keep it generic and plausible, never invent certifications
they claim to hold or numbers. Standards are suggestions to verify, not facts."""


def _clean(d: dict) -> dict:
    prof = d.get("profile") if isinstance(d.get("profile"), dict) else {}
    procs = []
    for p in (d.get("procedures") or [])[:5]:
        try:
            steps = store.clean_steps(p.get("steps"))
        except Exception:  # noqa: BLE001 - drop a malformed draft rather than fail the whole twin
            continue
        if str(p.get("name") or "").strip():
            procs.append({"name": str(p["name"]).strip()[:120], "description": str(p.get("description") or "")[:600],
                          "steps": steps[:12]})
    std = [{"name": str(s.get("name") or "")[:80], "why": str(s.get("why") or "")[:300],
            "confidence": s.get("confidence") if s.get("confidence") in ("likely", "possible") else "possible"}
           for s in (d.get("standards") or [])[:8] if isinstance(s, dict) and s.get("name")]
    docs = [{"name": str(x.get("name") or "")[:120], "why": str(x.get("why") or "")[:300]}
            for x in (d.get("documents") or [])[:10] if isinstance(x, dict) and x.get("name")]
    return {"profile": {"summary": str(prof.get("summary") or "")[:1000],
                        "products": [str(x)[:120] for x in (prof.get("products") or [])[:12]],
                        "processes": [str(x)[:120] for x in (prof.get("processes") or [])[:12]],
                        "market": str(prof.get("market") or "")[:300]},
            "procedures": procs, "standards": std, "documents": docs}


def api(db: Database, workspace_id: str) -> dict:
    row = db.one("SELECT * FROM onboarding WHERE workspace_id = ?", [workspace_id])
    if not row:
        return {"status": "none", "status_info": ops.info("none", "No draft twin yet.",
                                                         "Paste your company's website, or answer three questions.",
                                                         ["start"])}
    out = {"status": row["status"], "input": loads(row["input"], {}), "draft": loads(row["draft"], {}),
           "decisions": loads(row["decisions"], {}), "error": row["error"], "job_id": row["job_id"],
           "updated": row["updated"]}
    st = row["status"]
    if st == "working":
        job = jobs.get(db, workspace_id, row["job_id"]) if row["job_id"] else None
        out["status_info"] = ops.info("working", (job or {}).get("message") or "Reading your website and drafting.",
                                      "", ["cancel"])
    elif st == "failed":
        out["status_info"] = ops.info("failed", f"Couldn't draft the twin: {row['error']}",
                                      "Check the address, or answer the three questions instead.", ["start"])
    else:
        pending = len(items(out["draft"])) - len(out["decisions"])
        out["status_info"] = ops.info("review" if pending else "done",
                                      f"{pending} suggestions are waiting for your review." if pending else "All reviewed.",
                                      "Accept what fits, reject the rest." if pending else "", ["decide", "start"])
    return out


def items(draft: dict) -> list[str]:
    keys = ["profile"] if draft.get("profile", {}).get("summary") else []
    keys += [f"procedure:{i}" for i in range(len(draft.get("procedures") or []))]
    keys += [f"standard:{i}" for i in range(len(draft.get("standards") or []))]
    keys += [f"document:{i}" for i in range(len(draft.get("documents") or []))]
    return keys


def start(db: Database, workspace: dict, *, url: str = "", answers: dict | None = None, actor: str = "") -> dict:
    url, answers = (url or "").strip(), answers or {}
    if not url and not any(str(v or "").strip() for v in answers.values()):
        raise OnboardingError("paste your website, or answer the three questions")
    if not llm.available(workspace, "llm"):
        raise OnboardingError("drafting needs a reasoning model (Settings -> Models)")
    if url:
        from irris import mcp_client
        try:
            mcp_client.check_url(url if re.match(r"^https?://", url, re.I) else "https://" + url)
        except mcp_client.MCPError as e:
            raise OnboardingError(str(e).replace("MCP URL", "website"))
    ws_id = workspace["id"]
    job = jobs.enqueue(db, ws_id, "onboarding", {"url": url, "answers": answers}, title="Drafting your twin")
    inp = dumps({"url": url, "answers": answers, "by": actor})
    if db.one("SELECT workspace_id FROM onboarding WHERE workspace_id = ?", [ws_id]):
        db.execute("UPDATE onboarding SET status = 'working', input = ?, draft = '{}', decisions = '{}', job_id = ?, "
                   "error = '', updated = ? WHERE workspace_id = ?", [inp, job["id"], now(), ws_id])
    else:
        db.execute("INSERT INTO onboarding (workspace_id, status, input, job_id, created, updated) VALUES (?,?,?,?,?,?)",
                   [ws_id, "working", inp, job["id"], now(), now()])
    return api(db, ws_id)


@jobs.handler("onboarding")
async def _job(ctx: jobs.JobCtx) -> dict:
    db, ws_id = ctx.db, ctx.workspace_id
    url, answers = ctx.payload.get("url") or "", ctx.payload.get("answers") or {}
    try:
        if url:
            await ctx.progress(0.1, "Reading your website")
            pages = await read_site(url)
            source = "\n\n".join(f"[{p['url']}]\n{p['text']}" for p in pages)[:MAX_TEXT]
        else:
            source = (f"What we make: {answers.get('makes', '')}\nOur process: {answers.get('process', '')}\n"
                      f"Our market: {answers.get('market', '')}")
        await ctx.progress(0.5, "Drafting procedures and standards")
        models = llm.chain(ctx.workspace(), "llm")
        msg = await llm.chat_chain(models, [{"role": "system", "content": PROMPT},
                                            {"role": "user", "content": source}], json_mode=True, max_tokens=3500)
        draft = _clean(llm.parse_json(msg.get("content") or ""))
        if not (draft["profile"]["summary"] or draft["procedures"]):
            raise OnboardingError("the model didn't return a usable draft")
        db.execute("UPDATE onboarding SET status = 'ready', draft = ?, updated = ? WHERE workspace_id = ?",
                   [dumps(draft), now(), ws_id])
        return {"procedures": len(draft["procedures"]), "standards": len(draft["standards"])}
    except (OnboardingError, httpx.HTTPError, llm.ModelError) as e:
        db.execute("UPDATE onboarding SET status = 'failed', error = ?, updated = ? WHERE workspace_id = ?",
                   [str(e)[:300] or type(e).__name__, now(), ws_id])
        return {"error": str(e)}


def decide(db: Database, workspace: dict, item: str, decision: str, *, actor: str) -> dict:
    """Accept or reject one suggestion. Accepting applies it: the profile to the team description, a procedure as a
    new procedure, a standard as a lesson to verify, a document as a checklist item."""
    ws_id = workspace["id"]
    row = db.one("SELECT * FROM onboarding WHERE workspace_id = ?", [ws_id])
    if not row or row["status"] not in ("ready",):
        raise OnboardingError("there's no draft to review")
    draft, decisions = loads(row["draft"], {}), loads(row["decisions"], {})
    if item not in items(draft):
        raise OnboardingError("unknown suggestion")
    if decision not in ("accept", "reject"):
        raise OnboardingError("decision must be accept or reject")
    if item in decisions:
        raise OnboardingError("already decided")
    result: dict = {}
    if decision == "accept":
        kind, _, idx = item.partition(":")
        if kind == "profile":
            p = draft["profile"]
            about = p["summary"] + (f"\nProducts: {', '.join(p['products'])}." if p["products"] else "") + \
                (f"\nProcesses: {', '.join(p['processes'])}." if p["processes"] else "") + \
                (f"\nMarket: {p['market']}" if p["market"] else "")
            old = workspace.get("about") or ""
            db.execute("UPDATE workspaces SET about = ? WHERE id = ?", [(old + "\n\n" + about).strip()[:8000], ws_id])
        elif kind == "procedure":
            p = draft["procedures"][int(idx)]
            result = store.save_procedure(db, ws_id, {"name": p["name"], "description": p["description"] +
                                                      "\n(Drafted from your website; review every step.)",
                                                      "steps": p["steps"]})
        elif kind == "standard":
            s = draft["standards"][int(idx)]
            result = store.add_lesson(db, ws_id, f"Likely applicable standard (verify): {s['name']}. {s['why']}",
                                      subject="standard", source="onboarding")
        # documents are a checklist: the decision itself is the record
    decisions[item] = {"decision": decision, "by": actor, "at": now()}
    db.execute("UPDATE onboarding SET decisions = ?, updated = ? WHERE workspace_id = ?", [dumps(decisions), now(), ws_id])
    ops.audit(db, ws_id, actor, f"onboarding.{decision}", "onboarding", item, after=draft_item(draft, item))
    return {"item": item, "decision": decision, "applied": result.get("id") if isinstance(result, dict) else None}


def draft_item(draft: dict, item: str):
    kind, _, idx = item.partition(":")
    if kind == "profile":
        return draft.get("profile")
    return (draft.get({"procedure": "procedures", "standard": "standards", "document": "documents"}[kind]) or [])[int(idx)]
