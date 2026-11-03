import { Check, FileSignature, Globe, Play, Plus, Search, Send, Trash2, X } from "lucide-react";
import { useState } from "react";
import { del, get, post } from "../lib/api";
import { useSession } from "../lib/session";
import { ago, errText, stamp, useInterval, useLoad } from "../lib/util";
import "../platform.css";
import { StatusLine, type StatusInfo } from "./Status";
import { CopyButton, Empty, ErrorBox, Field, Loading, Modal, Spinner, TypeTag } from "./ui";

// ---------------------------------------------------------------- batch-record sign-off
type Signature = { by: string; meaning: string; status: string; comment: string; time: number };
type Signable = { id: string; status: string; released?: boolean; signatures?: Signature[]; status_info?: StatusInfo };

export function SignOff({ run, onDone }: { run: Signable; onDone: () => void }) {
  const { me, toast } = useSession();
  const [open, setOpen] = useState<"" | "reviewed" | "approved">("");
  const [pw, setPw] = useState("");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  if (run.status === "active") return null;
  const sigs = run.signatures || [];

  const sign = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await post(`/runs/${run.id}/signoff`, { meaning: open, password: pw, comment });
      toast(open === "approved" ? "Approved and released" : "Signed as reviewed");
      setOpen("");
      setPw("");
      setComment("");
      onDone();
    } catch (x) {
      setErr(errText(x));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="signoff" aria-label="Sign-off">
      <div className="row between wrap">
        <h3 style={{ margin: 0 }}>Sign-off</h3>
        {run.released && <span className="released">Released</span>}
      </div>
      <StatusLine info={run.status_info} />
      {sigs.map((s, i) => (
        <div className="sig" key={i}>
          <FileSignature size={14} />
          <span><b>{s.by}</b> signed as <b>{s.meaning}</b> · {stamp(s.time)}{s.comment ? ` · “${s.comment}”` : ""}</span>
        </div>
      ))}
      {!run.released && (
        <div className="row wrap">
          <button className="btn sm" onClick={() => setOpen("reviewed")}>Sign as reviewed</button>
          <button className="btn sm primary" onClick={() => setOpen("approved")}><Check /> Approve and release</button>
        </div>
      )}
      {open && (
        <Modal title={open === "approved" ? "Approve and release this batch" : "Sign as reviewed"} onClose={() => setOpen("")}>
          <form className="stack" onSubmit={sign}>
            <p className="muted" style={{ margin: 0 }}>
              You're signing as <b>{me?.user.email}</b>. Your signature means you have {open === "approved" ? "approved this batch record for release" : "reviewed this batch record"}.
              It is recorded with the time and can't be changed.
            </p>
            <Field label="Comment" hint="Optional.">
              <input className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
            </Field>
            <Field label="Your password" hint="Re-entering your password is your signature.">
              <input className="input" type="password" required autoFocus value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="current-password" />
            </Field>
            {err && <div className="error-box">{err}</div>}
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button type="button" className="btn" onClick={() => setOpen("")}>Cancel</button>
              <button className="btn primary" disabled={busy}>{busy ? <Spinner /> : <FileSignature />} Sign</button>
            </div>
          </form>
        </Modal>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- outgoing webhooks
type Hook = { id: string; name: string; url: string; events: string[]; status: string; status_info: StatusInfo; secret?: string };
type Delivery = { id: number; event: string; status: string; attempts: number; code: number | null; error: string; created: number };

function HookRow({ hook, onChange }: { hook: Hook; onChange: () => void }) {
  const { toast, isAdmin } = useSession();
  const [log, setLog] = useState<Delivery[] | null>(null);
  const [busy, setBusy] = useState("");
  const act = async (a: string) => {
    setBusy(a);
    try {
      const r = await post<{ delivered?: boolean; code?: number; error?: string }>(`/webhooks/${hook.id}/${a}`);
      if (a === "test") toast(r.delivered ? `Test delivered (${r.code})` : `Test failed: ${r.error || r.code}`, !r.delivered);
      onChange();
    } catch (e) { toast(errText(e), true); } finally { setBusy(""); }
  };
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="stack sm" style={{ minWidth: 0 }}>
          <div className="mono" style={{ fontSize: 13, overflowWrap: "anywhere" }}>{hook.url}</div>
          <div className="row wrap" style={{ gap: 4 }}>{hook.events.map((e) => <span key={e} className="tag mono">{e}</span>)}</div>
          <StatusLine info={hook.status_info} />
        </div>
        <div className="row">
          <button className="btn sm" onClick={() => act("test")} disabled={!!busy}>{busy === "test" ? <Spinner /> : <Send />} Test</button>
          {isAdmin && hook.status !== "active" && <button className="btn sm primary" onClick={() => act("approve")} disabled={!!busy}><Check /> Approve</button>}
          {isAdmin && hook.status === "active" && <button className="btn sm" onClick={() => act("pause")} disabled={!!busy}>Pause</button>}
          <button className="btn sm" onClick={async () => setLog((await get<{ deliveries: Delivery[] }>(`/webhooks/${hook.id}/deliveries`)).deliveries)}>Log</button>
          {isAdmin && <button className="btn sm icon danger" aria-label="Delete webhook" onClick={async () => { if (confirm("Delete this webhook?")) { await del(`/webhooks/${hook.id}`); onChange(); } }}><Trash2 /></button>}
        </div>
      </div>
      {log && (
        <div className="panel-body">
          {!log.length ? <div className="faint">Nothing sent yet.</div> : (
            <div className="table-wrap"><table className="t">
              <thead><tr><th>Event</th><th>Result</th><th>Tries</th><th>When</th></tr></thead>
              <tbody>{log.map((d) => (
                <tr key={d.id}><td className="mono">{d.event}</td>
                  <td className="mono" style={{ color: d.status === "delivered" ? undefined : "var(--red)" }}>{d.status}{d.code ? ` · ${d.code}` : ""}{d.error ? ` · ${d.error}` : ""}</td>
                  <td className="mono">{d.attempts}</td><td className="mono nowrap">{ago(d.created)}</td></tr>))}
              </tbody></table></div>
          )}
        </div>
      )}
    </div>
  );
}

export function WebhooksPanel() {
  const { workspace, isAdmin } = useSession();
  const { data, error, loading, reload } = useLoad<{ webhooks: Hook[]; events: string[] }>(() => get("/webhooks"), [workspace?.id]);
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>([]);
  const [made, setMade] = useState<Hook | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      setMade(await post<Hook>("/webhooks", { url, events }));
      setUrl("");
      reload();
    } catch (x) { setErr(errText(x)); } finally { setBusy(false); }
  };
  return (
    <section className="stack">
      <div className="row between wrap">
        <div>
          <h3 style={{ margin: 0 }}>Webhooks</h3>
          <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>Irris POSTs signed JSON to your systems when a step fails, a deviation opens, a batch is released, a run completes, an instrument reads out of limits or a sync fails. A new webhook sends nothing until an admin approves it.</p>
        </div>
        {isAdmin && <button className="btn sm" onClick={() => { setOpen(true); setMade(null); setEvents(data?.events || []); }}><Plus /> Add webhook</button>}
      </div>
      {loading && !data && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && !data.webhooks.length && <div className="faint" style={{ fontSize: 13 }}>No webhooks yet.</div>}
      {data?.webhooks.map((h) => <HookRow key={h.id} hook={h} onChange={reload} />)}
      {open && (
        <Modal title="Add a webhook" onClose={() => setOpen(false)}>
          {made ? (
            <div className="stack">
              <div className="secret-once">
                <div style={{ marginBottom: 6 }}>Signing secret — shown once. Verify <span className="mono">X-Irris-Signature</span> (HMAC-SHA256 of <span className="mono">t.body</span>) with it.</div>
                <div className="row"><code className="mono" style={{ overflowWrap: "anywhere" }}>{made.secret}</code><CopyButton text={made.secret || ""} /></div>
              </div>
              <p className="muted" style={{ margin: 0 }}>It's a draft: send a test, then approve it to start sending.</p>
              <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn primary" onClick={() => setOpen(false)}>Done</button></div>
            </div>
          ) : (
            <form className="stack" onSubmit={create}>
              <Field label="URL" hint="Public https address that accepts POST."><input className="input mono" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" /></Field>
              <Field label="Events">
                <div className="stack sm">{(data?.events || []).map((ev) => (
                  <label key={ev} className="row" style={{ gap: 8, fontSize: 13 }}>
                    <input type="checkbox" className="check" checked={events.includes(ev)} onChange={(e) => setEvents(e.target.checked ? [...events, ev] : events.filter((x) => x !== ev))} />
                    <span className="mono">{ev}</span>
                  </label>))}
                </div>
              </Field>
              {err && <div className="error-box">{err}</div>}
              <div className="row" style={{ justifyContent: "flex-end" }}>
                <button type="button" className="btn" onClick={() => setOpen(false)}>Cancel</button>
                <button className="btn primary" disabled={busy || !events.length}>{busy ? <Spinner /> : null} Create draft</button>
              </div>
            </form>
          )}
        </Modal>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- feedback
export function FeedbackPanel() {
  const { workspace, isAdmin, toast } = useSession();
  const { data, reload } = useLoad<{ feedback: { id: number; actor: string; text: string; created: number }[] }>(
    () => (isAdmin ? get("/feedback") : Promise.resolve({ feedback: [] })), [workspace?.id, isAdmin]);
  const [text, setText] = useState("");
  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    try { await post("/feedback", { text }); setText(""); toast("Thanks, sent"); reload(); } catch (x) { toast(errText(x), true); }
  };
  return (
    <section className="stack">
      <div>
        <h3 style={{ margin: 0 }}>Feedback</h3>
        <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>Anyone with a key (people or agents) can <span className="mono">POST /api/v1/feedback</span>. It's free.</p>
      </div>
      <form className="row" onSubmit={send}>
        <input className="input" value={text} onChange={(e) => setText(e.target.value)} placeholder="Something missing, wrong or great?" />
        <button className="btn sm" disabled={!text.trim()}><Send /> Send</button>
      </form>
      {!!data?.feedback.length && (
        <div className="stack sm">{data.feedback.slice(0, 20).map((f) => (
          <div key={f.id} style={{ fontSize: 13 }}><span className="faint mono">{ago(f.created)} · {f.actor}</span><div>{f.text}</div></div>
        ))}</div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- draft twin (onboarding)
type Draft = {
  profile: { summary: string; products: string[]; processes: string[]; market: string };
  procedures: { name: string; description: string; steps: { instruction: string; check?: string }[] }[];
  standards: { name: string; why: string; confidence: string }[];
  documents: { name: string; why: string }[];
};
type Onboarding = { status: string; draft?: Draft; decisions?: Record<string, { decision: string; by: string }>; status_info: StatusInfo; error?: string };

export function DraftTwin({ compact }: { compact?: boolean }) {
  const { workspace, toast, isAdmin } = useSession();
  const { data, reload } = useLoad<Onboarding>(() => get("/onboarding"), [workspace?.id]);
  const [url, setUrl] = useState("");
  const [mode, setMode] = useState<"url" | "questions">("url");
  const [ans, setAns] = useState({ makes: "", process: "", market: "" });
  const [busy, setBusy] = useState("");
  useInterval(() => { if (data?.status === "working") reload(); }, data?.status === "working" ? 2500 : null);

  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy("start");
    try { await post("/onboarding", mode === "url" ? { url } : { answers: ans }); reload(); } catch (x) { toast(errText(x), true); } finally { setBusy(""); }
  };
  const decide = async (item: string, decision: "accept" | "reject") => {
    setBusy(item);
    try { await post("/onboarding/decide", { item, decision }); reload(); } catch (x) { toast(errText(x), true); } finally { setBusy(""); }
  };

  if (!data) return null;
  const dec = data.decisions || {};
  const d = data.draft;
  const Item = ({ id, kind, title, children }: { id: string; kind: string; title: string; children?: React.ReactNode }) => (
    <div className="twin-item">
      <div><div className="twin-kind">{kind}</div><h4>{title}</h4>{children}</div>
      {dec[id] ? <span className="decided">{dec[id].decision === "accept" ? "Accepted" : "Rejected"}</span> : isAdmin && (
        <div className="row">
          <button className="btn sm" onClick={() => decide(id, "reject")} disabled={!!busy} aria-label={`Reject ${title}`}><X /></button>
          <button className="btn sm primary" onClick={() => decide(id, "accept")} disabled={!!busy}>{busy === id ? <Spinner /> : <Check />} Accept</button>
        </div>
      )}
    </div>
  );

  if (data.status === "none" || data.status === "failed") {
    if (compact && data.status === "none" && !isAdmin) return null;
    return (
      <section className="panel">
        <div className="panel-head"><div className="row"><Globe size={16} /><b>Draft your twin</b></div></div>
        <div className="panel-body stack">
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>Paste your company's website. Irris reads a few public pages and drafts what you make, your likely processes, 3–5 SOPs, the standards that probably apply and the documents to upload. You accept each piece.</p>
          {data.status === "failed" && <StatusLine info={data.status_info} />}
          <form className="stack sm" onSubmit={start}>
            {mode === "url" ? (
              <div className="row"><input className="input" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="yourcompany.com" required /><button className="btn primary" disabled={busy === "start"}>{busy === "start" ? <Spinner /> : "Draft"}</button></div>
            ) : (
              <>
                <input className="input" value={ans.makes} onChange={(e) => setAns({ ...ans, makes: e.target.value })} placeholder="What do you make?" required />
                <input className="input" value={ans.process} onChange={(e) => setAns({ ...ans, process: e.target.value })} placeholder="What's your main process?" />
                <input className="input" value={ans.market} onChange={(e) => setAns({ ...ans, market: e.target.value })} placeholder="Who do you sell to?" />
                <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn primary" disabled={busy === "start"}>{busy === "start" ? <Spinner /> : "Draft"}</button></div>
              </>
            )}
          </form>
          <button className="btn sm ghost" style={{ alignSelf: "flex-start" }} onClick={() => setMode(mode === "url" ? "questions" : "url")}>{mode === "url" ? "No website? Answer three questions" : "Use a website instead"}</button>
        </div>
      </section>
    );
  }
  if (data.status === "working") {
    return <section className="panel"><div className="panel-body row"><Spinner /><StatusLine info={data.status_info} /></div></section>;
  }
  if (!d || (compact && data.status_info.state === "done")) return null;
  return (
    <section className="panel">
      <div className="panel-head"><div className="row"><Globe size={16} /><b>Your draft twin</b></div><StatusLine info={data.status_info} compact /></div>
      <div className="panel-body">
        {d.profile.summary && (
          <Item id="profile" kind="About you" title={d.profile.summary}>
            {!!d.profile.processes.length && <p>Processes: {d.profile.processes.join(", ")}</p>}
          </Item>
        )}
        {d.procedures.map((p, i) => (
          <Item key={`p${i}`} id={`procedure:${i}`} kind="Draft procedure" title={p.name}>
            <ol>{p.steps.slice(0, 6).map((s, j) => <li key={j}>{s.instruction}</li>)}{p.steps.length > 6 && <li className="faint">+{p.steps.length - 6} more</li>}</ol>
          </Item>
        ))}
        {d.standards.map((s, i) => (
          <Item key={`s${i}`} id={`standard:${i}`} kind={`Standard · ${s.confidence}, verify`} title={s.name}><p>{s.why}</p></Item>
        ))}
        {d.documents.map((x, i) => (
          <Item key={`d${i}`} id={`document:${i}`} kind="Upload" title={x.name}><p>{x.why}</p></Item>
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------- plain-words queries
type Query = { type: string; subject: string; source: string; since: number | null; until: number | null;
  conditions: { field: string; op: string; value: number; said: string }[]; terms: string[]; unparsed: string[]; explain: string };
type QueryResult = { explain: string; total: number | null; complete: boolean; records: { id: string; type: string; title: string; subject: string; time: number; data: Record<string, unknown> }[] };

export function QueryBox({ onOpen }: { onOpen: (id: string) => void }) {
  const [text, setText] = useState("");
  const [q, setQ] = useState<Query | null>(null);
  const [res, setRes] = useState<QueryResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const parse = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(""); setRes(null);
    try { setQ(await post<Query>("/query/parse", { text })); } catch (x) { setErr(errText(x)); }
  };
  const run = async () => {
    if (!q) return;
    setBusy(true);
    try { setRes(await post<QueryResult>("/query/run", { query: q })); } catch (x) { setErr(errText(x)); } finally { setBusy(false); }
  };
  const drop = (i: number) => q && setQ({ ...q, conditions: q.conditions.filter((_, j) => j !== i) });
  return (
    <section className="stack sm">
      <form className="row" onSubmit={parse}>
        <input className="input" value={text} onChange={(e) => { setText(e.target.value); setQ(null); setRes(null); }} placeholder="Ask in plain words: batches last month where temperature > 185" aria-label="Query in plain words" />
        <button className="btn sm" disabled={!text.trim()}><Search /> Preview</button>
      </form>
      {q && (
        <div className="stack sm">
          <div className="query-chips" aria-label="What Irris understood">
            <span className="query-chip"><i>type</i>{q.type || "any"}</span>
            {q.subject && <span className="query-chip"><i>subject</i>{q.subject}</span>}
            {q.since && <span className="query-chip"><i>since</i>{stamp(q.since)}</span>}
            {q.until && <span className="query-chip"><i>until</i>{stamp(q.until)}</span>}
            {q.conditions.map((c, i) => (
              <span key={i} className="query-chip">{c.field} {c.op} {c.value}<button type="button" className="btn sm icon ghost" style={{ width: 18, height: 18 }} aria-label="Remove condition" onClick={() => drop(i)}><X size={12} /></button></span>
            ))}
            {q.terms.map((t) => <span key={t} className="query-chip"><i>mentions</i>{t}</span>)}
            {q.unparsed.map((u) => <span key={u} className="query-chip warn" title="Not understood; left out">? {u}</span>)}
          </div>
          <div className="row"><button className="btn sm primary" onClick={run} disabled={busy}>{busy ? <Spinner /> : <Play />} Run</button><span className="faint" style={{ fontSize: 12 }}>{q.explain}</span></div>
        </div>
      )}
      {err && <div className="error-box">{err}</div>}
      {res && (
        res.records.length ? (
          <div className="table-wrap"><table className="t">
            <thead><tr><th>Type</th><th>Title</th><th>When</th><th>Values</th></tr></thead>
            <tbody>{res.records.map((r) => (
              <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => onOpen(r.id)}>
                <td><TypeTag type={r.type} /></td><td>{r.title || r.subject || <span className="faint">—</span>}</td>
                <td className="mono nowrap">{ago(r.time)}</td>
                <td className="mono" style={{ fontSize: 12 }}>{(q?.conditions || []).map((c) => `${c.field}=${String((r.data as Record<string, unknown>)[c.field] ?? "—")}`).join(" ")}</td>
              </tr>))}</tbody>
          </table></div>
        ) : <Empty title="Nothing matched">{res.explain}</Empty>
      )}
      {res && <div className="faint" style={{ fontSize: 12 }}>{res.total != null ? `${res.total} matched` : "Showing the first matches of a large set"}</div>}
    </section>
  );
}
