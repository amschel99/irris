import { ArrowDown, ArrowUp, BookOpen, Check, Download, Pencil, Pin, Plus, Sparkles, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { DocList, DocumentViewer, UploadRow, type UploadItem } from "../components/Knowledge";
import { Empty, ErrorBox, Field, Head, Loading, Modal, Spinner, Tabs } from "../components/ui";
import { del, get, patch, post, put } from "../lib/api";
import { useSession } from "../lib/session";
import type { Procedure, Step } from "../lib/types";
import { KIND_LABEL, SOURCE_LABEL, uploadFile, type DocumentInfo, type KnowledgeItem, type KnowledgeStatus, type Memory, type UsageToday } from "../lib/knowledge";
import { ago, errText, num, useInterval, useLoad } from "../lib/util";

type Tab = "team" | "knowledge" | "procedures" | "lessons" | "memory" | "export";

function TeamTab() {
  const { workspace, refreshWorkspace, toast, isAdmin } = useSession();
  const [about, setAbout] = useState(workspace?.about || "");
  const [instructions, setInstructions] = useState(workspace?.instructions || "");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setAbout(workspace?.about || "");
    setInstructions(workspace?.instructions || "");
  }, [workspace?.id]);
  const dirty = about !== (workspace?.about || "") || instructions !== (workspace?.instructions || "");

  const save = async () => {
    setBusy(true);
    try {
      await patch("/workspace", { about, instructions });
      await refreshWorkspace();
      toast("Saved. Irris uses this in every answer.");
    } catch (e) {
      toast(errText(e), true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack lg" style={{ maxWidth: 760 }}>
      <Field label="About the team" hint="What you make, your lines and machines, materials, shifts, who does what. Plain words are best.">
        <textarea className="textarea" rows={7} value={about} onChange={(e) => setAbout(e.target.value)} disabled={!isAdmin}
          placeholder={"We mould PA66 and PP housings for electrical meters on two lines in Nairobi.\nLine 3 has four Engel presses (250 t). Press 7 runs hot.\nShifts: 06:00–14:00 and 14:00–22:00. QA signs off every first article."} />
      </Field>
      <Field label="Instructions" hint="Rules Irris always follows: units, tone, what to escalate, what never to do.">
        <textarea className="textarea" rows={6} value={instructions} onChange={(e) => setInstructions(e.target.value)} disabled={!isAdmin}
          placeholder={"Use °C and bar.\nAny reading outside spec is a deviation: say so first, then what to do.\nNever approve a first article; QA does that."} />
      </Field>
      {isAdmin ? (
        <div className="row"><button className="btn primary" onClick={save} disabled={!dirty || busy}>{busy ? <Spinner /> : "Save"}</button>{dirty && <span className="faint" style={{ fontSize: 13 }}>Unsaved changes</span>}</div>
      ) : <div className="note">Only workspace admins can change these.</div>}
    </div>
  );
}

const blankStep = (): Step => ({ instruction: "" });

function ProcedureEditor({ initial, onClose, onSaved }: { initial: Partial<Procedure> | null; onClose: () => void; onSaved: () => void }) {
  const { toast, workspace } = useSession();
  const [name, setName] = useState(initial?.name || "");
  const [description, setDescription] = useState(initial?.description || "");
  const [subject, setSubject] = useState(initial?.subject || "");
  const [steps, setSteps] = useState<Step[]>(initial?.steps?.length ? initial.steps : [blankStep()]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [draftGoal, setDraftGoal] = useState("");
  const [drafting, setDrafting] = useState(false);
  const llmReady = !!(workspace?.defaults.llm || workspace?.models.llm?.base_url);

  const upd = (i: number, p: Partial<Step>) => setSteps((s) => s.map((x, j) => (j === i ? { ...x, ...p } : x)));
  const updReading = (i: number, k: "label" | "min" | "max" | "unit", v: string) =>
    setSteps((s) => s.map((x, j) => {
      if (j !== i) return x;
      const r = { ...(x.reading || {}) } as any;
      if (v === "") delete r[k];
      else r[k] = k === "min" || k === "max" ? Number(v) : v;
      return { ...x, reading: Object.keys(r).length ? r : undefined };
    }));
  const moveStep = (i: number, d: number) => setSteps((s) => {
    const n = [...s];
    const j = i + d;
    if (j < 0 || j >= n.length) return s;
    [n[i], n[j]] = [n[j], n[i]];
    return n;
  });

  const draft = async () => {
    setDrafting(true);
    setError("");
    try {
      const d = await post("/procedures/draft", { goal: draftGoal });
      setName(d.name);
      setDescription(d.description);
      setSteps(d.steps);
      toast(d.sources?.length ? `Drafted from ${d.sources.length} of your documents. Check every step.` : "Drafted. Check every step.");
    } catch (e) {
      setError(errText(e));
    } finally {
      setDrafting(false);
    }
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    const body = { name, description, subject, steps: steps.filter((s) => s.instruction.trim()) };
    try {
      if (initial?.id) await put(`/procedures/${initial.id}`, body);
      else await post("/procedures", body);
      toast("Procedure saved");
      onSaved();
      onClose();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack lg">
      <div className="row between">
        <h2 style={{ fontSize: 18, fontWeight: 500 }}>{initial?.id ? "Edit procedure" : "New procedure"}</h2>
        <button className="btn sm ghost" onClick={onClose}><X /> Close</button>
      </div>
      {!initial?.id && (
        <div className="panel">
          <div className="panel-body stack">
            <div className="row"><Sparkles size={16} /><h3>Draft it with Irris</h3></div>
            <p className="muted" style={{ margin: 0, fontSize: 13 }}>Describe the job. Irris drafts the steps from your documents, with what correct looks like on camera. You review every step before saving.</p>
            <div className="row">
              <input className="input" value={draftGoal} onChange={(e) => setDraftGoal(e.target.value)} placeholder="Mould change on the Engel 250 t presses" disabled={!llmReady} aria-label="Describe the procedure" />
              <button className="btn" onClick={draft} disabled={!draftGoal.trim() || drafting || !llmReady} type="button">{drafting ? <Spinner /> : <Sparkles />} Draft</button>
            </div>
            {!llmReady && <span className="faint" style={{ fontSize: 12 }}>Drafting needs a reasoning model. <Link to="/settings?tab=models">Set one up</Link>.</span>}
          </div>
        </div>
      )}
      <form className="stack lg" onSubmit={save}>
        <div className="grid2">
          <Field label="Name"><input className="input" required value={name} onChange={(e) => setName(e.target.value)} placeholder="Mould change" /></Field>
          <Field label="Subject" hint="Optional. What it's for, as kind:name."><input className="input mono" value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="asset:press-7" /></Field>
        </div>
        <Field label="Description"><textarea className="textarea" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="When to use it, tools needed, who signs off." /></Field>
        <div>
          <div className="row between" style={{ marginBottom: 4 }}><h3>Steps</h3><span className="faint" style={{ fontSize: 12 }}>One physical action per step. Say what correct looks like so the camera can check it.</span></div>
          {steps.map((s, i) => (
            <div className="step-edit" key={i}>
              <span className="n">{String(i + 1).padStart(2, "0")}</span>
              <div className="stack sm">
                <input className="input" value={s.instruction} onChange={(e) => upd(i, { instruction: e.target.value })} placeholder="Close all four clamps on the mould" aria-label={`Step ${i + 1} instruction`} />
                <input className="input" value={s.check || ""} onChange={(e) => upd(i, { check: e.target.value || undefined })} placeholder="What correct looks like: all four clamp levers down, green tags visible" aria-label={`Step ${i + 1} check`} />
                <input className="input" value={s.caution || ""} onChange={(e) => upd(i, { caution: e.target.value || undefined })} placeholder="Caution (optional): platen is hot" aria-label={`Step ${i + 1} caution`} />
                <div className="reading-grid">
                  <input className="input mono" value={s.reading?.label || ""} onChange={(e) => updReading(i, "label", e.target.value)} placeholder="reading (optional)" aria-label="Reading label" />
                  <input className="input mono" inputMode="decimal" value={s.reading?.min ?? ""} onChange={(e) => updReading(i, "min", e.target.value)} placeholder="min" aria-label="Minimum" />
                  <input className="input mono" inputMode="decimal" value={s.reading?.max ?? ""} onChange={(e) => updReading(i, "max", e.target.value)} placeholder="max" aria-label="Maximum" />
                  <input className="input mono" value={s.reading?.unit || ""} onChange={(e) => updReading(i, "unit", e.target.value)} placeholder="unit" aria-label="Unit" />
                </div>
              </div>
              <div className="row" style={{ alignItems: "flex-start", gap: 4 }}>
                <button type="button" className="btn sm icon ghost" aria-label="Move up" onClick={() => moveStep(i, -1)} disabled={i === 0}><ArrowUp /></button>
                <button type="button" className="btn sm icon ghost" aria-label="Move down" onClick={() => moveStep(i, 1)} disabled={i === steps.length - 1}><ArrowDown /></button>
                <button type="button" className="btn sm icon ghost" aria-label="Remove step" onClick={() => setSteps((x) => x.length > 1 ? x.filter((_, j) => j !== i) : x)}><Trash2 /></button>
              </div>
            </div>
          ))}
          <button type="button" className="btn sm" style={{ marginTop: 8 }} onClick={() => setSteps((s) => [...s, blankStep()])}><Plus /> Add step</button>
        </div>
        <ErrorBox error={error} />
        <div className="row"><button className="btn primary" disabled={busy}>{busy ? <Spinner /> : "Save procedure"}</button><button type="button" className="btn" onClick={onClose}>Cancel</button></div>
      </form>
    </div>
  );
}

function ProceduresTab() {
  const { workspace, toast } = useSession();
  const { data, error, loading, reload } = useLoad<{ procedures: Procedure[] }>(() => get("/procedures"), [workspace?.id]);
  const [editing, setEditing] = useState<Partial<Procedure> | null | undefined>(undefined);

  if (editing !== undefined) return <ProcedureEditor initial={editing} onClose={() => setEditing(undefined)} onSaved={reload} />;

  const remove = async (p: Procedure) => {
    if (!confirm(`Delete "${p.name}"? Past runs and batch records keep their copy.`)) return;
    await del(`/procedures/${p.id}`).catch((e) => toast(errText(e), true));
    reload();
  };

  return (
    <div className="stack lg">
      <div className="section-head" style={{ marginBottom: 0 }}>
        <div><h2>Procedures</h2><p>Your SOPs, step by step. Irris guides people through them in Live and checks each step through the camera.</p></div>
        <button className="btn primary" onClick={() => setEditing(null)}><Plus /> New procedure</button>
      </div>
      {loading && !data && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && !data.procedures.length && (
        <Empty title="No procedures yet" action={<button className="btn sm" onClick={() => setEditing(null)}>Write one</button>}>
          Write one here, have Irris draft it from your manuals, or send <span className="mono">procedure</span> records to the API.
        </Empty>
      )}
      {!!data?.procedures.length && (
        <ul className="list">
          {data.procedures.map((p) => (
            <li key={p.id} className="clickable" style={{ padding: "14px 8px" }} onClick={() => setEditing(p)}>
              <div className="grow">
                <div style={{ fontWeight: 500 }}>{p.name}</div>
                <div className="faint" style={{ fontSize: 13 }}>{p.steps.length} steps{p.subject ? ` · ${p.subject}` : ""} · updated {ago(p.updated)}</div>
              </div>
              <Link className="btn sm" to="/live" onClick={(e) => e.stopPropagation()}>Run</Link>
              <button className="btn sm icon ghost" aria-label={`Delete ${p.name}`} onClick={(e) => { e.stopPropagation(); remove(p); }}><Trash2 /></button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function LessonsTab() {
  const { workspace, toast } = useSession();
  const { data, error, loading, reload } = useLoad<{ lessons: { id: string; text: string; subject: string; created: number; source: string }[] }>(() => get("/lessons"), [workspace?.id]);
  const [text, setText] = useState("");
  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await post("/lessons", { text });
      setText("");
      reload();
    } catch (err) {
      toast(errText(err), true);
    }
  };
  return (
    <div className="stack lg" style={{ maxWidth: 820 }}>
      <div className="section-head" style={{ marginBottom: 0 }}>
        <div><h2>Lessons</h2><p>Corrections and rules Irris always applies. It also saves one when you correct it in a conversation.</p></div>
      </div>
      <form className="row" onSubmit={add}>
        <input className="input" value={text} onChange={(e) => setText(e.target.value)} placeholder="Press 7's display reads 5 °C high; subtract 5 from it." aria-label="New lesson" />
        <button className="btn primary" disabled={!text.trim()}><Plus /> Teach</button>
      </form>
      {loading && !data && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && !data.lessons.length && <Empty title="No lessons yet">Teach Irris the things that aren't written down: quirks of machines, house rules, how you name things.</Empty>}
      {!!data?.lessons.length && (
        <ul className="list">
          {data.lessons.map((l) => (
            <li key={l.id}>
              <div className="grow">
                <div>{l.text}</div>
                <div className="faint" style={{ fontSize: 12 }}>{l.source.replace("team/", "")} · {ago(l.created)}</div>
              </div>
              <button className="btn sm icon ghost" aria-label="Forget this lesson" onClick={async () => { await del(`/lessons/${l.id}`); reload(); }}><Trash2 /></button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function KnowledgeTab() {
  const { workspace } = useSession();
  const llmReady = !!(workspace?.defaults.llm || workspace?.models.llm?.base_url);
  const status = useLoad<KnowledgeStatus>(() => get("/knowledge/status"), [workspace?.id]);
  const documents = useLoad<{ documents: DocumentInfo[] }>(() => get("/documents"), [workspace?.id]);
  const [kind, setKind] = useState<"" | "digest" | "limit" | "fact" | "procedure" | "warning">("");
  const items = useLoad<{ knowledge: KnowledgeItem[] }>(() => get(`/knowledge?limit=80${kind ? `&kind=${kind}` : ""}`), [workspace?.id, kind]);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [over, setOver] = useState(false);
  const [doc, setDoc] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const busy = !!status.data?.active_jobs.some((j) => j.kind === "read" || j.kind === "study");
  useInterval(() => { status.reload(); documents.reload(); items.reload(); }, busy ? 4000 : null);

  const teach = async (files: FileList | File[]) => {
    for (const file of Array.from(files)) {
      const key = Date.now() + Math.random();
      setUploads((x) => [{ key, name: file.name, size: file.size, progress: 0, status: "uploading", message: "" }, ...x]);
      const up = (p: Partial<UploadItem>) => setUploads((x) => x.map((i) => (i.key === key ? { ...i, ...p } : i)));
      try {
        const res = await uploadFile(file, (f) => up({ progress: f }), { teach: true });
        up({ status: "done", progress: 1, jobId: res.job?.id, recordId: res.record_id,
             message: res.kind === "document" ? `${(res.pages || 0).toLocaleString()} pages read${llmReady ? "" : " (studying needs a reasoning model)"}` : "imported" });
        status.reload();
        documents.reload();
      } catch (e) {
        up({ status: "error", message: errText(e) });
      }
    }
  };

  const s = status.data;
  return (
    <div className="stack lg">
      <div className="section-head" style={{ marginBottom: 0 }}>
        <div><h2>Knowledge</h2><p>Teach Irris from your manuals, standards and books. It reads every page, then studies them: notes on every section, the key facts and limits with their pages, and a digest of the whole document.</p></div>
      </div>
      <div
        className={`teach-drop${over ? " over" : ""}`}
        role="button"
        tabIndex={0}
        onClick={() => input.current?.click()}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); teach(e.dataTransfer.files); }}
      >
        <BookOpen />
        <div>
          <div style={{ fontWeight: 500 }}>Teach Irris from a document</div>
          <div className="faint" style={{ fontSize: 13 }}>PDF, Word, text or Markdown, any length. A 4,000-page book is read in the background and studied end to end.</div>
        </div>
        <span className="btn sm">Choose a file</span>
        <input ref={input} type="file" multiple hidden accept=".pdf,.docx,.txt,.md,.markdown,.rst,.html,.htm" onChange={(e) => e.target.files && teach(e.target.files)} />
      </div>
      {!llmReady && <div className="note">Studying needs a reasoning model. <Link to="/settings?tab=models">Set one up</Link>; until then documents are read in full and searchable, but not studied.</div>}
      {uploads.length > 0 && <ul className="list">{uploads.map((u) => <UploadRow key={u.key} item={u} onOpen={setDoc} />)}</ul>}

      {s && (
        <div className="kstats">
          <div><div className="num">{num(s.documents)}</div><div className="lbl">Documents</div></div>
          <div><div className="num">{num(s.pages)}</div><div className="lbl">Pages read</div></div>
          <div><div className="num">{num(s.knowledge)}</div><div className="lbl">Notes and key facts</div></div>
          <div><div className="num">{num(s.passages ? Math.round((s.embedded / s.passages) * 100) : 0)}%</div><div className="lbl">Understood by meaning</div></div>
        </div>
      )}

      <section className="stack">
        <h2>Documents</h2>
        {documents.loading && !documents.data && <Loading />}
        <ErrorBox error={documents.error} retry={documents.reload} />
        {documents.data && !documents.data.documents.length && <Empty title="Nothing taught yet">Drop a manual above. Irris reads it, studies it, and cites its pages in every answer.</Empty>}
        {!!documents.data?.documents.length && <DocList docs={documents.data.documents.slice(0, 30)} onOpen={(d) => setDoc(d.id)} />}
      </section>

      <section className="stack">
        <div className="row between wrap">
          <h2>What Irris learned</h2>
          <div className="chips">
            {([["", "All"], ["digest", "Digests"], ["limit", "Limits"], ["procedure", "Procedures"], ["warning", "Warnings"], ["fact", "Facts"]] as const).map(([k, l]) => (
              <button key={k} className={`chip${kind === k ? " on" : ""}`} aria-pressed={kind === k} onClick={() => setKind(k)}>{l}</button>
            ))}
          </div>
        </div>
        {items.loading && !items.data && <Loading />}
        <ErrorBox error={items.error} retry={items.reload} />
        {items.data && !items.data.knowledge.length && <Empty title="Nothing here yet">Once Irris studies a document, its key facts, limits and digest show up here, each with its page.</Empty>}
        {!!items.data?.knowledge.length && (
          <ul className="kitems">
            {items.data.knowledge.filter((k) => kind || k.kind !== "notes").map((k) => (
              <li key={k.id}>
                <span className={`k ${k.kind}`}>{KIND_LABEL[k.kind] || k.kind}</span>
                <div style={{ minWidth: 0 }}>
                  <div className="txt" style={k.kind === "digest" ? { whiteSpace: "pre-wrap", display: "-webkit-box", WebkitLineClamp: 6, WebkitBoxOrient: "vertical", overflow: "hidden" } : undefined}>{k.text}</div>
                  <div className="src">{k.title}</div>
                </div>
                {k.document_id && <button className="btn sm ghost" onClick={() => setDoc(k.document_id!)}>Open</button>}
              </li>
            ))}
          </ul>
        )}
      </section>
      {doc && <DocumentViewer id={doc} onClose={() => setDoc(null)} />}
    </div>
  );
}

function Importance({ value }: { value: number }) {
  const n = Math.round(value * 4);
  return <span className="imp" aria-label={`Importance ${n} of 4`}>{[0, 1, 2, 3].map((i) => <i key={i} className={i < n ? "on" : ""} />)}</span>;
}

const MEM_KINDS = [["", "All"], ["semantic", "Facts"], ["procedural", "How-tos"], ["episodic", "Events"], ["goal", "Goals"],
  ["reflection", "Reflections"]] as const;

function MemoryTab() {
  const { workspace, toast, isAdmin } = useSession();
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<string>("");
  const [showHistory, setShowHistory] = useState(false);
  const { data, error, loading, reload } = useLoad<{ memories: Memory[] }>(
    () => get(`/memories?history=${showHistory}${q ? `&q=${encodeURIComponent(q)}` : ""}`), [workspace?.id, q, showHistory]);
  const budget = useLoad<{ today: UsageToday }>(() => get("/usage?days=1"), [workspace?.id]);
  const [text, setText] = useState("");
  const [personal, setPersonal] = useState(false);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const shown = (data?.memories || []).filter((m) => !kind || m.kind === kind);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await post("/memories", { text, scope: personal ? "personal" : "team", importance: 0.7 });
      setText("");
      reload();
    } catch (err) {
      toast(errText(err), true);
    }
  };
  const save = async () => {
    if (!editing) return;
    try {
      await patch(`/memories/${editing.id}`, { text: editing.text });
      setEditing(null);
      reload();
    } catch (err) {
      toast(errText(err), true);
    }
  };
  const pin = async (m: Memory) => {
    try {
      await patch(`/memories/${m.id}`, { pinned: !m.pinned });
      toast(m.pinned ? "Unpinned" : "Pinned: Irris applies it in every answer");
      reload();
    } catch (err) {
      toast(errText(err), true);
    }
  };
  const forget = async (m: Memory) => {
    if (!confirm("Forget this for good? It's deleted everywhere, including Irris's logs.")) return;
    try {
      await del(`/memories/${m.id}`);
      toast("Forgotten");
      reload();
    } catch (err) {
      toast(errText(err), true);
    }
  };
  const t = budget.data?.today;

  return (
    <div className="stack lg" style={{ maxWidth: 900 }}>
      <div className="section-head" style={{ marginBottom: 0 }}>
        <div><h2>Memory</h2><p>What Irris remembers from your conversations: people and roles, machines, how you do things, decisions, open issues. It keeps only what's important and confident, replaces facts when they change, and picks the few that matter for each question. Pin the ones it must never forget.</p></div>
      </div>
      <form className="row wrap" onSubmit={add}>
        <input className="input grow" style={{ minWidth: 240 }} value={text} onChange={(e) => setText(e.target.value)} placeholder="Line 3 runs a night shift from 22:00; Bo leads it." aria-label="Something to remember" />
        <label className="row faint" style={{ fontSize: 13, gap: 6 }}><input type="checkbox" className="check" checked={personal} onChange={(e) => setPersonal(e.target.checked)} /> Only for me</label>
        <button className="btn primary" disabled={!text.trim()}><Plus /> Remember</button>
      </form>
      <div className="row between wrap">
        <div className="chips">
          {MEM_KINDS.map(([k, l]) => (
            <button key={k} className={`chip${kind === k ? " on" : ""}`} aria-pressed={kind === k} onClick={() => setKind(k)}>{l}</button>
          ))}
        </div>
        <div className="row">
          <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search memories" aria-label="Search memories" style={{ width: 220, height: 34 }} />
          <label className="row faint" style={{ fontSize: 13, gap: 6, whiteSpace: "nowrap" }}><input type="checkbox" className="check" checked={showHistory} onChange={(e) => setShowHistory(e.target.checked)} /> Show history</label>
        </div>
      </div>
      {loading && !data && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && !shown.length && (
        <Empty title={q || kind ? "Nothing matches" : "No memories yet"}>
          {q || kind ? "Try other words, or another kind." : "Talk to Irris in Ask. After each conversation it keeps what's worth remembering, and you can add things here."}
        </Empty>
      )}
      {!!shown.length && (
        <ul className="kitems">
          {shown.map((m) => {
            const old = m.status !== "active";
            return (
              <li key={m.id} style={old ? { opacity: 0.55 } : undefined}>
                <span className="k">{m.pinned ? "pinned · " : ""}{m.kind}{m.scope === "personal" ? " · me" : ""}</span>
                <div style={{ minWidth: 0 }}>
                  {editing?.id === m.id ? (
                    <div className="stack sm">
                      <textarea className="textarea mem-edit" value={editing.text} onChange={(e) => setEditing({ ...editing, text: e.target.value })} autoFocus
                        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) save(); if (e.key === "Escape") setEditing(null); }} aria-label="Edit memory" />
                      <div className="row"><button className="btn sm primary" onClick={save}><Check /> Save</button><button className="btn sm ghost" onClick={() => setEditing(null)}>Cancel</button></div>
                    </div>
                  ) : (
                    <>
                      <div className="txt" style={old ? { textDecoration: "line-through" } : undefined}>{m.text}</div>
                      <div className="src row wrap" style={{ gap: 10 }}>
                        <Importance value={m.importance} />
                        <span>{SOURCE_LABEL[m.source_type] || m.source_type}</span>
                        {m.subject && <span>{m.subject}{m.predicate ? ` · ${m.predicate}` : ""}</span>}
                        {old ? <span>{m.status}{m.valid_to ? ` ${ago(m.valid_to)}` : ""}</span> : <span>{m.uses ? `used ${m.uses}×` : "not used yet"} · {ago(m.updated)}</span>}
                        {m.kind === "reflection" && m.evidence_ids.length > 0 && <span>from {m.evidence_ids.length} memories</span>}
                      </div>
                    </>
                  )}
                </div>
                {editing?.id !== m.id && !old && (
                  <div className="row" style={{ gap: 2 }}>
                    <button className={`btn sm icon ghost${m.pinned ? " lime" : ""}`} aria-label={m.pinned ? "Unpin" : "Pin"} aria-pressed={m.pinned} onClick={() => pin(m)}><Pin /></button>
                    <button className="btn sm icon ghost" aria-label="Edit" onClick={() => setEditing({ id: m.id, text: m.text })}><Pencil /></button>
                    <button className="btn sm icon ghost" aria-label="Forget" onClick={() => forget(m)}><Trash2 /></button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {t && (
        <div className="note row between wrap" style={{ gap: 12 }}>
          <span>Model use today: <span className="mono">{num(t.tokens)}</span> of <span className="mono">{num(t.daily_tokens)}</span> tokens, <span className="mono">{num(t.calls)}</span> of <span className="mono">{num(t.daily_calls)}</span> calls{t.exhausted ? " · budget reached, Irris pauses until midnight UTC" : ""}.</span>
          {isAdmin && <BudgetEdit current={t} onSaved={budget.reload} />}
        </div>
      )}
    </div>
  );
}

function BudgetEdit({ current, onSaved }: { current: UsageToday; onSaved: () => void }) {
  const { toast } = useSession();
  const [open, setOpen] = useState(false);
  const [tokens, setTokens] = useState(String(current.daily_tokens));
  const [calls, setCalls] = useState(String(current.daily_calls));
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await patch("/usage/limits", { daily_tokens: Number(tokens) || null, daily_calls: Number(calls) || null });
      toast("Daily budget saved");
      setOpen(false);
      onSaved();
    } catch (err) {
      toast(errText(err), true);
    }
  };
  if (!open) return <button className="btn sm ghost" onClick={() => setOpen(true)}>Change budget</button>;
  return (
    <form className="row wrap" onSubmit={save}>
      <input className="input mono" style={{ width: 130, height: 32 }} inputMode="numeric" value={tokens} onChange={(e) => setTokens(e.target.value.replace(/\D/g, ""))} aria-label="Daily tokens" />
      <input className="input mono" style={{ width: 90, height: 32 }} inputMode="numeric" value={calls} onChange={(e) => setCalls(e.target.value.replace(/\D/g, ""))} aria-label="Daily calls" />
      <button className="btn sm primary">Save</button>
      <button type="button" className="btn sm ghost" onClick={() => setOpen(false)}>Cancel</button>
    </form>
  );
}

function ExportTab() {
  const { workspace } = useSession();
  const [open, setOpen] = useState(false);
  const download = async () => {
    const res = await fetch("/api/v1/training/export", { headers: { "X-Workspace": workspace?.id || "" } });
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `irris-${(workspace?.name || "workspace").toLowerCase().replace(/\W+/g, "-")}-training.jsonl`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return (
    <div className="stack lg" style={{ maxWidth: 760 }}>
      <div>
        <h2>Training data</h2>
        <p className="muted" style={{ margin: "4px 0 0" }}>
          Irris learns your operation from your data, your instructions, your procedures and your lessons, and uses them in every answer. To fine-tune a model of your own, export everything in chat format.
        </p>
      </div>
      <div className="note">One JSON object per line: every conversation as <span className="mono">{`{"messages": [...]}`}</span>, then each lesson and procedure. Works with most fine-tuning tools (OpenAI, Together, Unsloth, Axolotl).</div>
      <div className="row"><button className="btn primary" onClick={download}><Download /> Download JSONL</button><button className="btn" onClick={() => setOpen(true)}>What's in it?</button></div>
      {open && (
        <Modal title="What's exported" onClose={() => setOpen(false)}>
          <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.8 }}>
            <li>Every conversation with two or more messages, in order</li>
            <li>Every lesson the team taught Irris</li>
            <li>Every procedure with its steps</li>
          </ul>
          <p className="muted">Records and documents aren't included: they stay searchable in Irris and are cited live.</p>
        </Modal>
      )}
    </div>
  );
}

export function Train() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "team";
  return (
    <div className="page">
      <Head title="Train" sub="Teach Irris how your team works. Everything here shapes every answer and every check." />
      <Tabs<Tab>
        tabs={[{ id: "team", label: "Your team" }, { id: "knowledge", label: "Knowledge" }, { id: "procedures", label: "Procedures" }, { id: "lessons", label: "Lessons" }, { id: "memory", label: "Memory" }, { id: "export", label: "Export" }]}
        value={tab}
        onChange={(t) => setParams({ tab: t })}
      />
      {tab === "team" && <TeamTab />}
      {tab === "knowledge" && <KnowledgeTab />}
      {tab === "procedures" && <ProceduresTab />}
      {tab === "lessons" && <LessonsTab />}
      {tab === "memory" && <MemoryTab />}
      {tab === "export" && <ExportTab />}
    </div>
  );
}
