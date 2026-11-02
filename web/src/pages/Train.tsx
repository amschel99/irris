import { ArrowDown, ArrowUp, Download, Plus, Sparkles, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Empty, ErrorBox, Field, Head, Loading, Modal, Spinner, Tabs } from "../components/ui";
import { del, get, patch, post, put } from "../lib/api";
import { useSession } from "../lib/session";
import type { Procedure, Step } from "../lib/types";
import { ago, errText, useLoad } from "../lib/util";

type Tab = "team" | "procedures" | "lessons" | "export";

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
          Irris learns your plant from your data, your instructions, your procedures and your lessons, and uses them in every answer. To fine-tune a model of your own, export everything in chat format.
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
        tabs={[{ id: "team", label: "Your team" }, { id: "procedures", label: "Procedures" }, { id: "lessons", label: "Lessons" }, { id: "export", label: "Export" }]}
        value={tab}
        onChange={(t) => setParams({ tab: t })}
      />
      {tab === "team" && <TeamTab />}
      {tab === "procedures" && <ProceduresTab />}
      {tab === "lessons" && <LessonsTab />}
      {tab === "export" && <ExportTab />}
    </div>
  );
}
