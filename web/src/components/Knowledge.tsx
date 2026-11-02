import { BookOpen, Download, GraduationCap, MessageSquare, Search, X } from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { get, getWorkspaceId, post } from "../lib/api";
import { isActive, size, studyLabel, useActiveJobs, useJob, type DocumentDetail, type DocumentInfo, type Job, type PageRead } from "../lib/knowledge";
import { useSession } from "../lib/session";
import type { Hit } from "../lib/types";
import { ago, errText, useInterval, useLoad } from "../lib/util";
import { Empty, ErrorBox, Loading, Spinner } from "./ui";
import "../knowledge.css";

// ---------------------------------------------------------------- jobs
export function JobLine({ job, label, onCancel }: { job: Job; label?: ReactNode; onCancel?: () => void }) {
  const pct = Math.round((job.progress || 0) * 100);
  const running = job.status === "running";
  const indeterminate = running && job.progress === 0;
  return (
    <div className={`jobline ${job.status}`}>
      <span className="what">{label ?? job.title ?? job.kind}</span>
      <span className="row" style={{ gap: 8 }}>
        <span className="pct">{job.status === "queued" ? "QUEUED" : job.status === "error" ? "FAILED" : job.status === "done" ? "DONE" : `${pct}%`}</span>
        {onCancel && isActive(job) && (
          <button className="btn sm icon ghost" aria-label="Cancel" onClick={onCancel}><X /></button>
        )}
      </span>
      {isActive(job) && <div className={`bar${indeterminate ? " indeterminate" : ""}`}><i style={{ width: `${Math.max(2, pct)}%` }} /></div>}
      <span className="msg">{job.status === "error" ? job.error : job.message}</span>
    </div>
  );
}

/** What Irris is working on in the background (reading, studying, importing). Hidden when idle. */
export function WorkingOn({ deps = [] }: { deps?: unknown[] }) {
  const jobs = useActiveJobs(deps).filter((j) => j.kind !== "embed" || j.status === "running");
  if (!jobs.length) return null;
  return (
    <section className="working" aria-live="polite">
      <div className="working-head"><span className="dot live" /> Irris is working on {jobs.length === 1 ? "one thing" : `${jobs.length} things`}</div>
      {jobs.slice(0, 5).map((j) => <JobLine key={j.id} job={j} />)}
    </section>
  );
}

// ---------------------------------------------------------------- documents
export function DocList({ docs, onOpen }: { docs: DocumentInfo[]; onOpen: (d: DocumentInfo) => void }) {
  return (
    <ul className="docs">
      {docs.map((d) => {
        const s = studyLabel(d);
        const ext = (d.title.split(".").pop() || "doc").slice(0, 4).toUpperCase();
        const reading = d.status !== "indexed";
        return (
          <li key={d.id} tabIndex={0} onClick={() => onOpen(d)} onKeyDown={(e) => e.key === "Enter" && onOpen(d)}>
            <span className="doc-glyph" aria-hidden>{ext}</span>
            <div style={{ minWidth: 0 }}>
              <div className="doc-title">{d.title}</div>
              <div className="doc-meta">
                <span>{d.pages ? `${d.pages.toLocaleString()} ${d.unit}s` : "…"}</span>
                {d.size ? <span>{size(d.size)}</span> : null}
                <span>{ago(d.updated)}</span>
                {d.empty_pages > 0 && <span title="Pages with no text layer (scanned images)">{d.empty_pages} without text</span>}
              </div>
              {reading && d.pages > 0 && <div className="doc-read"><i style={{ width: `${Math.round((d.read / d.pages) * 100)}%` }} /></div>}
            </div>
            <span className={`tag ${s.tone}`}>{reading && d.pages ? `${s.text} ${Math.round((d.read / d.pages) * 100)}%` : s.text}</span>
          </li>
        );
      })}
    </ul>
  );
}

function highlight(text: string, terms: string[]): ReactNode {
  if (!terms.length) return text;
  const re = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return text.split(re).map((part, i) => (i % 2 ? <mark key={i}>{part}</mark> : <Fragment key={i}>{part}</Fragment>));
}

/** Read any document, however long: outline, search inside, page by page, and its digest once studied. */
export function DocumentViewer({ id, onClose, page: startPage, query: startQuery }: { id: string; onClose: () => void; page?: number | null; query?: string }) {
  const { toast } = useSession();
  const nav = useNavigate();
  const detail = useLoad<DocumentDetail>(() => get(`/documents/${id}`), [id]);
  const doc = detail.data;
  const [view, setView] = useState<"read" | "digest">("read");
  const [pages, setPages] = useState<{ page: number; text: string }[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [loadingPages, setLoadingPages] = useState(false);
  const [pageError, setPageError] = useState("");
  const [focus, setFocus] = useState<number | null>(startPage || null);
  const [q, setQ] = useState(startQuery || "");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [ask, setAsk] = useState("");
  const [jump, setJump] = useState("");
  const main = useRef<HTMLDivElement>(null);
  const terms = useMemo(() => (hits ? q.toLowerCase().split(/\W+/).filter((t) => t.length > 2) : []), [hits, q]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  const open = useCallback(async (from: number, append = false) => {
    setLoadingPages(true);
    setPageError("");
    try {
      const r = await get<PageRead>(`/documents/${id}/pages?start=${from}&end=${from + 9}`);
      setPages((cur) => (append ? [...cur, ...r.content] : r.content));
      setNext(r.next);
      if (!append) main.current?.scrollTo({ top: 0 });
    } catch (e) {
      setPageError(errText(e));
    } finally {
      setLoadingPages(false);
    }
  }, [id]);

  useEffect(() => {
    open(Math.max(1, (startPage || 1) - 1));
  }, [open, startPage]);

  useEffect(() => {
    if (startQuery) search(startQuery);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startQuery]);

  useInterval(() => detail.reload(), doc && (doc.status !== "indexed" || doc.study === "studying" || doc.study === "queued") ? 2500 : null);

  const goto = (p: number) => {
    setFocus(p);
    setView("read");
    if (pages.some((x) => x.page === p)) {
      document.getElementById(`dp-${id}-${p}`)?.scrollIntoView({ block: "start" });
    } else {
      open(Math.max(1, p - 1)).then(() => setTimeout(() => document.getElementById(`dp-${id}-${p}`)?.scrollIntoView({ block: "start" }), 50));
    }
  };

  async function search(text: string) {
    if (!text.trim()) {
      setHits(null);
      return;
    }
    setSearching(true);
    try {
      const r = await get<{ hits: Hit[] }>(`/documents/${id}/search?q=${encodeURIComponent(text)}&limit=30`);
      setHits(r.hits.filter((h) => h.page).sort((a, b) => (a.page || 0) - (b.page || 0)));
    } catch (e) {
      toast(errText(e), true);
    } finally {
      setSearching(false);
    }
  }

  const study = async () => {
    try {
      await post(`/documents/${id}/study`);
      toast("Irris is studying every page. You can close this; it keeps going.");
      detail.reload();
    } catch (e) {
      toast(errText(e), true);
    }
  };

  const studyJob = doc?.jobs.find((j) => j.kind === "study" && isActive(j));
  const readJob = doc?.jobs.find((j) => j.kind === "read" && isActive(j));
  const canStudy = doc && doc.status === "indexed" && !["done", "studying", "queued"].includes(doc.study);

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="docview" role="dialog" aria-modal="true" aria-label={doc?.title || "Document"}>
        <div className="docview-head">
          <BookOpen size={18} className="faint" style={{ flex: "none" }} />
          <div className="grow" style={{ minWidth: 0 }}>
            <h2>{doc?.title || "Document"}</h2>
            {doc && <div className="doc-meta">{doc.pages.toLocaleString()} {doc.unit}s · {studyLabel(doc).text}{doc.knowledge ? ` · ${doc.knowledge} notes and facts` : ""}</div>}
          </div>
          {doc?.digest && (
            <div className="tabs" role="tablist" style={{ marginBottom: 0 }}>
              <button role="tab" aria-selected={view === "read"} className={view === "read" ? "on" : ""} onClick={() => setView("read")}>Read</button>
              <button role="tab" aria-selected={view === "digest"} className={view === "digest" ? "on" : ""} onClick={() => setView("digest")}>Digest</button>
            </div>
          )}
          {canStudy && <button className="btn sm" onClick={study} aria-label="Study every page"><GraduationCap /> <span className="hide-sm">Study every page</span></button>}
          {doc?.file_id && <a className="btn sm icon ghost" aria-label="Download the original" href={`/api/v1/files/${doc.file_id}?ws=${encodeURIComponent(getWorkspaceId())}`} target="_blank" rel="noreferrer"><Download /></a>}
          <button className="btn sm icon ghost" aria-label="Close" onClick={onClose}><X /></button>
        </div>
        <div className="docview-body">
          <aside className="docview-side">
            {(readJob || studyJob) && (
              <div className="stack sm" style={{ padding: "0 8px" }}>
                {readJob && <JobLine job={readJob} label="Reading" />}
                {studyJob && <JobLine job={studyJob} label="Studying" />}
              </div>
            )}
            <form className="stack sm" onSubmit={(e) => { e.preventDefault(); search(q); }}>
              <label className="ask-bar" style={{ height: 36, borderRadius: 8, padding: "0 6px 0 10px" }}>
                <Search />
                <span className="sr">Search this document</span>
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search this document" style={{ fontSize: 13 }} />
                {searching && <Spinner />}
                {hits && !searching && <button type="button" className="btn sm icon ghost" aria-label="Clear search" onClick={() => { setQ(""); setHits(null); }}><X /></button>}
              </label>
            </form>
            {hits ? (
              <div className="stack sm">
                <div className="side-label">{hits.length ? `${hits.length} matches` : "No matches"}</div>
                <ul className="doc-hits">
                  {hits.map((h, i) => (
                    <li key={i}><button onClick={() => h.page && goto(h.page)}><span className="pg">p.{h.page}</span>{h.text.replace(/^.*?\n/, "").slice(0, 140)}…</button></li>
                  ))}
                </ul>
              </div>
            ) : (
              <div className="stack sm">
                <div className="side-label">Contents</div>
                {doc?.outline.length ? (
                  <ul className="outline">
                    {doc.outline.map((o, i) => (
                      <li key={i}>
                        <button className={`l${Math.min(o.level, 3)}${focus === o.page ? " on" : ""}`} onClick={() => goto(o.page)}>
                          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{o.title}</span>
                          <span className="pg">{o.page}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : <p className="faint" style={{ fontSize: 12, margin: "0 8px" }}>{doc?.status === "indexed" ? "This document has no chapters Irris could find. Search it, or jump to a page." : "Contents appear as Irris reads."}</p>}
              </div>
            )}
            {doc && doc.pages > 1 && (
              <form className="row" style={{ padding: "0 8px" }} onSubmit={(e) => { e.preventDefault(); const p = parseInt(jump); if (p >= 1 && p <= doc.pages) goto(p); }}>
                <input className="input mono" inputMode="numeric" value={jump} onChange={(e) => setJump(e.target.value.replace(/\D/g, ""))} placeholder={`Go to ${doc.unit} (1–${doc.pages})`} aria-label="Go to page" style={{ height: 32, fontSize: 12 }} />
                <button className="btn sm" disabled={!jump}>Go</button>
              </form>
            )}
            <form className="stack sm" style={{ padding: "0 8px", marginTop: "auto" }} onSubmit={(e) => { e.preventDefault(); if (ask.trim()) nav(`/ask?q=${encodeURIComponent(`${ask.trim()} (in "${doc?.title}")`)}`); }}>
              <div className="side-label" style={{ padding: 0 }}>Ask about this document</div>
              <div className="row">
                <input className="input" value={ask} onChange={(e) => setAsk(e.target.value)} placeholder="What's the torque for the clamps?" style={{ height: 34, fontSize: 13 }} aria-label="Ask about this document" />
                <button className="btn sm primary icon" aria-label="Ask" disabled={!ask.trim()}><MessageSquare /></button>
              </div>
            </form>
          </aside>
          <div className="docview-main" ref={main}>
            {detail.loading && !doc && <Loading />}
            <ErrorBox error={detail.error} retry={detail.reload} />
            {view === "digest" && doc?.digest ? (
              <article className="digest">
                <div className="eyebrow" style={{ marginBottom: 12 }}>What Irris learned from it</div>
                <div className="md" style={{ whiteSpace: "pre-wrap", fontSize: 15, lineHeight: 1.7 }}>{doc.digest.text.split("\n\nKey points:")[0]}</div>
                {!!doc.digest.key_points?.length && (
                  <ul className="points">{doc.digest.key_points.map((p, i) => <li key={i}>{p}</li>)}</ul>
                )}
                {!!doc.digest.chapters?.length && (
                  <div className="stack sm" style={{ marginTop: 28 }}>
                    <div className="eyebrow">Chapters</div>
                    <ul className="outline">{doc.digest.chapters.map((c, i) => (
                      <li key={i}><button onClick={() => goto(c.from)}><span>{c.title}</span><span className="pg">{c.from}–{c.to}</span></button></li>
                    ))}</ul>
                  </div>
                )}
              </article>
            ) : (
              <>
                {pageError && <div style={{ maxWidth: 760, margin: "24px auto" }}><ErrorBox error={pageError} /></div>}
                {!pages.length && !loadingPages && doc && (
                  <div style={{ maxWidth: 760, margin: "48px auto" }}>
                    <Empty title={doc.status === "indexed" ? "No text on these pages" : "Irris is reading this document"}>
                      {doc.status === "indexed" ? "They may be scanned images without a text layer." : "Pages appear here as soon as they're read."}
                    </Empty>
                  </div>
                )}
                {pages.map((p) => (
                  <section key={p.page} id={`dp-${id}-${p.page}`} className={`doc-page${focus === p.page ? " hit" : ""}`}>
                    <div className="pno">{doc?.unit === "part" ? "PART" : "PAGE"} {p.page}</div>
                    <pre>{highlight(p.text, terms)}</pre>
                  </section>
                ))}
                {(next || loadingPages) && (
                  <div className="row" style={{ justifyContent: "center", padding: "28px 0" }}>
                    <button className="btn" onClick={() => next && open(next, true)} disabled={loadingPages}>
                      {loadingPages ? <><Spinner /> Loading</> : `Continue reading from ${doc?.unit || "page"} ${next}`}
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

/** One upload, followed from the first byte to "studied". */
export interface UploadItem {
  key: number;
  name: string;
  size: number;
  progress: number;
  status: "uploading" | "done" | "error";
  message: string;
  jobId?: string;
  recordId?: string;
  studyJobId?: string;
}

export function UploadRow({ item, onOpen }: { item: UploadItem; onOpen?: (recordId: string) => void }) {
  const [jobId, setJobId] = useState(item.jobId);
  useEffect(() => setJobId(item.jobId), [item.jobId]);
  const job = useJob(jobId, (j) => {
    if (j.kind === "read" && j.status === "done") {
      // reading finished: follow the study that may have been queued after it
      get<{ jobs: Job[] }>("/jobs?status=active&kind=study&limit=20").then((r) => {
        const s = r.jobs.find((x) => x.payload.record_id === item.recordId);
        if (s) setJobId(s.id);
      }).catch(() => {});
    }
  });
  const label = job ? (job.kind === "study" ? "Studying every page" : job.kind === "rows" ? "Importing rows" : "Reading") : "";
  return (
    <li>
      <div className="grow stack sm" style={{ minWidth: 0 }}>
        <div className="row between">
          <span style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.name}</span>
          <span className="faint mono" style={{ fontSize: 12, flex: "none" }}>{size(item.size)}</span>
        </div>
        {item.status === "uploading" ? (
          <div className="bar"><i style={{ width: `${Math.round(item.progress * 100)}%` }} /></div>
        ) : item.status === "error" ? (
          <span className="red" style={{ fontSize: 13 }}>{item.message}</span>
        ) : job && isActive(job) ? (
          <JobLine job={job} label={label} />
        ) : (
          <span className="muted" style={{ fontSize: 13 }}>
            ✓ {job?.kind === "study" && job.status === "done" ? `studied: ${job.result.facts ?? 0} key facts and a digest` : job?.status === "error" ? <span className="red">{job.error}</span> : item.message}
          </span>
        )}
      </div>
      {item.recordId && onOpen && item.status === "done" && <button className="btn sm" onClick={() => onOpen(item.recordId!)}>Open</button>}
    </li>
  );
}
