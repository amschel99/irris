import { Database, Play, Plus, RefreshCw, Search, Trash2, Upload as UploadIcon, Webhook } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { RecordDrawer } from "../components/RecordDrawer";
import { DocList, DocumentViewer, UploadRow, type UploadItem } from "../components/Knowledge";
import { Code, CopyButton, Empty, ErrorBox, Field, Head, Loading, Modal, Spinner, Tabs, Toggle, TypeTag } from "../components/ui";
import { del, get, patch, post } from "../lib/api";
import { uploadFile, type DocumentInfo } from "../lib/knowledge";
import { useSession } from "../lib/session";
import type { DataSource, Hit, RecordSummary } from "../lib/types";
import { ago, clock, errText, useInterval, useLoad } from "../lib/util";

type Tab = "upload" | "documents" | "databases" | "api" | "records";

function UploadTab({ onOpen }: { onOpen: (id: string) => void }) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [over, setOver] = useState(false);
  const [teach, setTeach] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const { workspace } = useSession();
  const llmReady = !!(workspace?.defaults.llm || workspace?.models.llm?.base_url);

  const send = async (files: FileList | File[]) => {
    for (const file of Array.from(files)) {
      const key = Date.now() + Math.random();
      setItems((x) => [{ key, name: file.name, size: file.size, progress: 0, status: "uploading", message: "" }, ...x]);
      const up = (p: Partial<UploadItem>) => setItems((x) => x.map((i) => (i.key === key ? { ...i, ...p } : i)));
      try {
        const res = await uploadFile(file, (f) => up({ progress: f }), { teach });
        const what = res.kind === "row" ? (res.status === "queued" ? "importing rows" : `${res.accepted.toLocaleString()} rows`)
          : res.kind === "document" ? `${(res.pages || 0).toLocaleString()} pages, searchable` : res.kind === "image" ? "image" : `${res.accepted} records`;
        up({ status: "done", progress: 1, message: what + (res.updated ? ` (${res.updated} updated)` : ""), jobId: res.job?.id,
             recordId: res.kind === "document" ? res.record_id : undefined });
      } catch (e) {
        up({ status: "error", message: errText(e) });
      }
    }
  };

  return (
    <div className="stack lg">
      <div
        className={`drop${over ? " over" : ""}`}
        role="button"
        tabIndex={0}
        onClick={() => input.current?.click()}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); send(e.dataTransfer.files); }}
      >
        <UploadIcon />
        <strong>Drop files here, or click to choose</strong>
        <span>Manuals and books of any length (PDF, Word, text, Markdown) · logs and exports (CSV, Excel, JSON, NDJSON) · photos</span>
        <input ref={input} type="file" multiple hidden onChange={(e) => e.target.files && send(e.target.files)} />
      </div>
      <div className="row between wrap">
        <div className="row" style={{ gap: 10 }}>
          <Toggle on={teach} onChange={setTeach} label="Teach Irris: study every page" />
          <span style={{ fontSize: 13, cursor: "pointer" }} onClick={() => setTeach(!teach)}>Teach Irris: study every page</span>
        </div>
        <span className="faint" style={{ fontSize: 12, maxWidth: 520 }}>
          {llmReady
            ? "Every document is read in full and searchable. Studying also turns each section into notes, key facts and limits, and a digest Irris checks first. Documents up to 300 pages are studied anyway."
            : "Every document is read in full and searchable. Studying needs a reasoning model (Settings → Models)."}
        </span>
      </div>
      {items.length > 0 && (
        <ul className="list">
          {items.map((i) => <UploadRow key={i.key} item={i} onOpen={onOpen} />)}
        </ul>
      )}
    </div>
  );
}

function DocumentsTab({ onOpen }: { onOpen: (id: string) => void }) {
  const { workspace } = useSession();
  const [q, setQ] = useState("");
  const { data, error, loading, reload } = useLoad<{ documents: DocumentInfo[] }>(() => get("/documents"), [workspace?.id]);
  const busy = data?.documents.some((d) => d.status !== "indexed" || d.study === "studying" || d.study === "queued");
  useInterval(reload, busy ? 3000 : null);
  const docs = (data?.documents || []).filter((d) => !q || d.title.toLowerCase().includes(q.toLowerCase()));
  return (
    <div className="stack lg">
      <div className="section-head" style={{ marginBottom: 0 }}>
        <div><h2>Documents</h2><p>Every page of every document, readable and searchable. Open one to read it, search inside it, or see what Irris learned from it.</p></div>
        {!!data?.documents.length && <input className="input" style={{ maxWidth: 240 }} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by title" aria-label="Filter documents" />}
      </div>
      {loading && !data && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && !data.documents.length && <Empty title="No documents yet">Upload manuals, SOPs, datasheets or whole books. Irris reads every page, even a 4,000-page PDF.</Empty>}
      {!!docs.length && <DocList docs={docs} onOpen={(d) => onOpen(d.id)} />}
    </div>
  );
}

function SqlBox({ source }: { source: DataSource }) {
  const [sql, setSql] = useState(source.schema?.[0] ? `select * from ${source.schema[0].table} limit 20` : "select now()");
  const [res, setRes] = useState<{ columns: string[]; rows: Record<string, unknown>[]; truncated: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const run = async () => {
    setBusy(true);
    setError("");
    try {
      setRes(await post(`/sources/${source.id}/query`, { sql }));
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack">
      <textarea className="textarea mono" rows={3} value={sql} onChange={(e) => setSql(e.target.value)} aria-label="SQL query"
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run(); }} />
      <div className="row between">
        <span className="faint" style={{ fontSize: 12 }}>Read-only. One SELECT at a time, 200 rows max. <span className="kbd">Ctrl</span> <span className="kbd">Enter</span> runs it.</span>
        <button className="btn sm primary" onClick={run} disabled={busy}>{busy ? <Spinner /> : <Play />} Run</button>
      </div>
      <ErrorBox error={error} />
      {res && (
        res.rows.length ? (
          <div className="table-wrap" style={{ maxHeight: 360 }}>
            <table className="t">
              <thead><tr>{res.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>
                {res.rows.map((r, i) => (
                  <tr key={i}>{res.columns.map((c) => <td key={c} className="mono">{r[c] === null ? <span className="faint">null</span> : typeof r[c] === "object" ? JSON.stringify(r[c]) : String(r[c])}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="note">The query returned no rows.</div>
      )}
      {res?.truncated && <span className="faint" style={{ fontSize: 12 }}>Showing the first 200 rows.</span>}
    </div>
  );
}

function DatabaseCard({ source, onChange }: { source: DataSource; onChange: () => void }) {
  const { toast, isAdmin } = useSession();
  const [picked, setPicked] = useState<string[]>(source.tables || []);
  const [busy, setBusy] = useState(false);
  const [showSql, setShowSql] = useState(false);
  const dirty = JSON.stringify(picked.slice().sort()) !== JSON.stringify((source.tables || []).slice().sort());

  const sync = async () => {
    setBusy(true);
    try {
      if (dirty) await patch(`/sources/${source.id}`, { tables: picked });
      await post(`/sources/${source.id}/sync`);
      toast("Synced");
      onChange();
    } catch (e) {
      toast(errText(e), true);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!confirm(`Disconnect ${source.name}? Rows already synced stay until you delete them.`)) return;
    await del(`/sources/${source.id}`).catch((e) => toast(errText(e), true));
    onChange();
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <div className="row">
          <Database size={16} />
          <div>
            <div style={{ fontWeight: 500 }}>{source.name}</div>
            <div className="faint" style={{ fontSize: 12 }}>{source.status}{source.last_sync ? ` · ${ago(source.last_sync)}` : ""}</div>
          </div>
        </div>
        <div className="row">
          <button className="btn sm" onClick={() => setShowSql((s) => !s)}>{showSql ? "Hide query" : "Query"}</button>
          {isAdmin && <button className="btn sm icon danger" aria-label="Disconnect" onClick={remove}><Trash2 /></button>}
        </div>
      </div>
      <div className="panel-body stack">
        {showSql && <SqlBox source={source} />}
        <div className="stack sm">
          <h3>Tables to sync into Irris</h3>
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>Irris can query every table live. Synced tables also become searchable records (up to 5,000 rows each), re-synced on demand.</p>
        </div>
        {source.schema?.length ? (
          <div className="table-wrap" style={{ maxHeight: 260 }}>
            <table className="t">
              <thead><tr><th style={{ width: 36 }} /><th>Table</th><th>Columns</th><th>Rows (est.)</th></tr></thead>
              <tbody>
                {source.schema.map((t) => (
                  <tr key={t.table}>
                    <td><input type="checkbox" className="check" aria-label={`Sync ${t.table}`} checked={picked.includes(t.table)} onChange={(e) => setPicked(e.target.checked ? [...picked, t.table] : picked.filter((x) => x !== t.table))} /></td>
                    <td className="mono">{t.table}</td>
                    <td className="mono">{t.columns}</td>
                    <td className="mono">{t.rows_estimate}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="note">No tables found. Check the user can read the schemas you need.</div>}
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn sm primary" onClick={sync} disabled={busy}>{busy ? <Spinner /> : <RefreshCw />} {dirty ? "Save and sync" : "Sync now"}</button>
        </div>
      </div>
    </div>
  );
}

function DatabasesTab() {
  const { workspace, isAdmin } = useSession();
  const { data, error, loading, reload } = useLoad<{ sources: DataSource[] }>(() => get("/sources"), [workspace?.id]);
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ name: "", url: "" });
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const dbs = data?.sources.filter((s) => s.kind === "postgres") || [];

  const connect = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setFormError("");
    try {
      await post("/sources", { kind: "postgres", ...f });
      setOpen(false);
      setF({ name: "", url: "" });
      reload();
    } catch (err) {
      setFormError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack lg">
      <div className="section-head" style={{ marginBottom: 0 }}>
        <div>
          <h2>Databases</h2>
          <p>Connect Postgres (MES, ERP, quality, historian exports). Irris reads it with a read-only transaction and never writes.</p>
        </div>
        {isAdmin && <button className="btn primary" onClick={() => setOpen(true)}><Plus /> Connect Postgres</button>}
      </div>
      {loading && !data && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && !dbs.length && (
        <Empty title="No database connected" action={isAdmin ? <button className="btn sm" onClick={() => setOpen(true)}>Connect Postgres</button> : undefined}>
          Use a read-only database user. Irris tests the connection, reads the schema, and can then answer questions with live SQL.
        </Empty>
      )}
      {dbs.map((s) => <DatabaseCard key={s.id} source={s} onChange={reload} />)}
      {open && (
        <Modal title="Connect Postgres" onClose={() => setOpen(false)}>
          <form className="stack" onSubmit={connect}>
            <Field label="Name"><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="MES" /></Field>
            <Field label="Connection string" hint="Stored encrypted and never shown again. Create a user with SELECT only.">
              <input className="input mono" required value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} placeholder="postgresql://irris_ro:…@db.internal.local:5432/mes" autoComplete="off" spellCheck={false} />
            </Field>
            {formError && <div className="error-box">{formError}</div>}
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button type="button" className="btn" onClick={() => setOpen(false)}>Cancel</button>
              <button className="btn primary" disabled={busy}>{busy ? <><Spinner /> Testing…</> : "Test and connect"}</button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}

function ApiTab() {
  const { workspace, isAdmin, toast } = useSession();
  const { data, reload } = useLoad<{ sources: DataSource[] }>(() => get("/sources"), [workspace?.id]);
  const [created, setCreated] = useState<string>("");
  const [name, setName] = useState("");
  const [type, setType] = useState("event");
  const hooks = data?.sources.filter((s) => s.kind === "webhook") || [];
  const origin = location.origin;

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const h = await post("/sources", { kind: "webhook", name: name || "Webhook", type });
      setCreated(origin + h.url);
      setName("");
      reload();
    } catch (err) {
      toast(errText(err), true);
    }
  };

  const irf = `curl -X POST ${origin}/api/v1/ingest \\
  -H "Authorization: Bearer $IRRIS_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "irf": "1.0",
    "type": "reading",
    "source": "line-3/plc",
    "id": "press-7-temp-0815",
    "subject": "asset:press-7",
    "data": { "value": 182.4, "unit": "C" }
  }'`;
  const anyJson = `# Any JSON: each object becomes a record of ?type=
curl -X POST "${origin}/api/v1/ingest?type=work_order&source=erp" \\
  -H "Authorization: Bearer $IRRIS_KEY" \\
  -H "Content-Type: application/json" \\
  -d '[{"wo": 1042, "part": "PN-2231", "qty": 500}]'

# A stream: one record per line
curl -X POST ${origin}/api/v1/ingest \\
  -H "Authorization: Bearer $IRRIS_KEY" \\
  -H "Content-Type: application/x-ndjson" \\
  --data-binary @events.ndjson`;

  return (
    <div className="stack lg">
      <section className="stack">
        <div className="section-head" style={{ marginBottom: 0 }}>
          <div>
            <h2>Webhooks</h2>
            <p>A private URL any system can POST JSON to (SCADA alarms, MES events, Zapier, n8n). No key needed: the URL is the secret.</p>
          </div>
        </div>
        {isAdmin && (
          <form className="row wrap" onSubmit={create}>
            <input className="input" style={{ maxWidth: 240 }} value={name} onChange={(e) => setName(e.target.value)} placeholder="SCADA alarms" aria-label="Webhook name" />
            <input className="input mono" style={{ maxWidth: 160 }} value={type} onChange={(e) => setType(e.target.value)} placeholder="event" aria-label="Record type" title="Record type for incoming JSON" />
            <button className="btn primary"><Webhook /> Create webhook</button>
          </form>
        )}
        {created && (
          <div className="stack sm">
            <div className="note" style={{ color: "var(--fg)" }}>Copy this URL now. It's shown only once.</div>
            <div className="row"><input className="input mono" readOnly value={created} onFocus={(e) => e.target.select()} /><CopyButton text={created} /></div>
          </div>
        )}
        {hooks.length > 0 && (
          <ul className="list">
            {hooks.map((h) => (
              <li key={h.id}>
                <Webhook size={16} className="faint" />
                <div className="grow">
                  <div style={{ fontWeight: 500 }}>{h.name}</div>
                  <div className="faint mono" style={{ fontSize: 12 }}>…/hooks/{h.hint}… · type {h.type} · {h.status}{h.last_sync ? ` · ${ago(h.last_sync)}` : ""}</div>
                </div>
                {isAdmin && <button className="btn sm icon danger" aria-label="Delete webhook" onClick={async () => { await del(`/sources/${h.id}`); reload(); }}><Trash2 /></button>}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="stack">
        <div className="section-head" style={{ marginBottom: 0 }}>
          <div>
            <h2>REST API</h2>
            <p>Send records in the Irris Record Format, or any JSON. Get a key in Settings → Developers. Same source and id updates the record instead of duplicating it.</p>
          </div>
          <a className="btn sm" href="/standard" target="_blank" rel="noreferrer">The IRF standard</a>
        </div>
        <Code>{irf}</Code>
        <Code>{anyJson}</Code>
      </section>
    </div>
  );
}

function RecordsTab({ onOpenDoc }: { onOpenDoc: (id: string, page?: number | null, q?: string) => void }) {
  const { workspace } = useSession();
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [type, setType] = useState("");
  const [source, setSource] = useState("");
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<{ id: string; page?: number | null; excerpt?: string } | null>(null);
  const stats = useLoad<{ by_type: { type: string; n: number }[]; sources: { source: string; n: number }[] }>(() => get("/stats"), [workspace?.id]);
  const list = useLoad<{ total: number; records?: RecordSummary[]; hits?: Hit[] }>(() => {
    const p = new URLSearchParams({ limit: "50", offset: String(offset) });
    if (query) p.set("q", query);
    if (type) p.set("type", type);
    if (source && !query) p.set("source", source);
    return get(`/records?${p}`);
  }, [workspace?.id, query, type, source, offset]);

  useEffect(() => setOffset(0), [query, type, source]);

  return (
    <div className="stack lg">
      <div className="row wrap">
        <form className="ask-bar grow" style={{ height: 40, borderRadius: 10, minWidth: 240 }} onSubmit={(e) => { e.preventDefault(); setQuery(q.trim()); }}>
          <Search />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search everything Irris knows" style={{ fontSize: 14 }} aria-label="Search records" />
          {query && <button type="button" className="btn sm ghost" onClick={() => { setQ(""); setQuery(""); }}>Clear</button>}
        </form>
        <select className="select input" style={{ width: 170 }} value={type} onChange={(e) => setType(e.target.value)} aria-label="Type">
          <option value="">All types</option>
          {stats.data?.by_type.map((t) => <option key={t.type} value={t.type}>{t.type} · {t.n}</option>)}
        </select>
        {!query && (
          <select className="select input" style={{ width: 200 }} value={source} onChange={(e) => setSource(e.target.value)} aria-label="Source">
            <option value="">All sources</option>
            {stats.data?.sources.map((s) => <option key={s.source} value={s.source}>{s.source}</option>)}
          </select>
        )}
      </div>
      {list.loading && !list.data && <Loading />}
      <ErrorBox error={list.error} retry={list.reload} />
      {list.data?.hits && (
        list.data.hits.length ? (
          <ul className="list">
            {list.data.hits.map((h, i) => (
              <li key={i} className="clickable" style={{ alignItems: "flex-start", padding: "12px 8px" }} onClick={() => (h.type === "document" ? onOpenDoc(h.record_id, h.page, query) : setOpen({ id: h.record_id, page: h.page, excerpt: h.text }))}>
                <div className="grow stack sm">
                  <div className="row"><span style={{ fontWeight: 500 }}>{h.title || h.subject || h.type}</span>{h.page && <span className="tag">p. {h.page}</span>}</div>
                  <div className="muted" style={{ fontSize: 13, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{h.text}</div>
                  <div className="faint mono" style={{ fontSize: 11 }}>{h.source}</div>
                </div>
                <TypeTag type={h.type} />
              </li>
            ))}
          </ul>
        ) : <Empty title={`Nothing matches "${query}"`}>Try other words, a part number, or a machine name.</Empty>
      )}
      {list.data?.records && (
        list.data.records.length ? (
          <>
            <div className="table-wrap">
              <table className="t">
                <thead><tr><th>Time</th><th>Type</th><th>Title or content</th><th>Subject</th><th>Source</th></tr></thead>
                <tbody>
                  {list.data.records.map((r) => (
                    <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => (r.type === "document" ? onOpenDoc(r.id) : setOpen({ id: r.id }))}>
                      <td className="mono nowrap faint">{clock(r.time)}</td>
                      <td><TypeTag type={r.type} /></td>
                      <td style={{ maxWidth: 420, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.title || r.preview}</td>
                      <td className="mono">{r.subject}</td>
                      <td className="mono faint">{r.source}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="row between">
              <span className="faint" style={{ fontSize: 13 }}>{offset + 1}–{offset + list.data.records.length} of {list.data.total.toLocaleString()}</span>
              <div className="row">
                <button className="btn sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button>
                <button className="btn sm" disabled={offset + 50 >= list.data.total} onClick={() => setOffset(offset + 50)}>Next</button>
              </div>
            </div>
          </>
        ) : <Empty title="No records yet">Upload a file, connect a database or send data to the API, and it shows up here.</Empty>
      )}
      {open && <RecordDrawer id={open.id} page={open.page} excerpt={open.excerpt} onClose={() => setOpen(null)} onDeleted={() => { list.reload(); stats.reload(); }} />}
    </div>
  );
}

export function Data() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "upload";
  const [doc, setDoc] = useState<{ id: string; page?: number | null; q?: string } | null>(null);
  return (
    <div className="page">
      <Head title="Data" sub="Everything Irris knows about your operation. Any format and any size goes in; the Irris Record Format keeps it consistent." />
      <Tabs<Tab>
        tabs={[{ id: "upload", label: "Upload" }, { id: "documents", label: "Documents" }, { id: "databases", label: "Databases" }, { id: "api", label: "Webhooks & API" }, { id: "records", label: "Records" }]}
        value={tab}
        onChange={(t) => setParams({ tab: t })}
      />
      {tab === "upload" && <UploadTab onOpen={(id) => setDoc({ id })} />}
      {tab === "documents" && <DocumentsTab onOpen={(id) => setDoc({ id })} />}
      {tab === "databases" && <DatabasesTab />}
      {tab === "api" && <ApiTab />}
      {tab === "records" && <RecordsTab onOpenDoc={(id, page, q) => setDoc({ id, page, q })} />}
      {doc && <DocumentViewer id={doc.id} page={doc.page} query={doc.q} onClose={() => setDoc(null)} />}
    </div>
  );
}
