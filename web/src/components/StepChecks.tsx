import { Check, ChevronDown, Plus, Trash2, X } from "lucide-react";
import { useState } from "react";
import type { GuidedStep, Instrument, Intervention, Proposal, Question, Severity, StepKind } from "../lib/guidanceTypes";
import "../procedures.css";

const SEVERITIES: { id: Severity; label: string }[] = [
  { id: "critical", label: "Critical: speak at once" },
  { id: "major", label: "Major: after a pause" },
  { id: "minor", label: "Minor: let them fix it" },
];
const KINDS: { id: StepKind; label: string }[] = [
  { id: "action", label: "Action (checklist)" },
  { id: "measurement", label: "Measurement (reading)" },
  { id: "timing", label: "Timing" },
  { id: "order", label: "Order" },
];
const INTERVENTIONS: { id: Intervention; label: string }[] = [
  { id: "notify_if_missed", label: "Ask if it seems missed" },
  { id: "remind_before", label: "Remind before it starts" },
  { id: "none", label: "Stay quiet" },
];

const csv = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);

/** The camera-check fields of one step: yes/no questions, severity, order and the rest. Collapsed by default. */
export function StepChecks({ step, index, total, instruments, onChange }: {
  step: GuidedStep; index: number; total: number; instruments: Instrument[]; onChange: (p: Partial<GuidedStep>) => void;
}) {
  const [open, setOpen] = useState(false);
  const qs: Question[] = step.questions || [];
  const setQ = (i: number, p: Partial<Question>) => onChange({ questions: qs.map((q, j) => (j === i ? { ...q, ...p } : q)) });
  const summary = [
    qs.length ? `${qs.length} question${qs.length > 1 ? "s" : ""}` : "questions from the check",
    step.severity && step.severity !== "major" ? step.severity : "",
    step.reversible === false ? "can't be undone" : "",
    step.median_s != null ? `~${Math.round(step.median_s)} s` : "",
    step.instrument_id ? "instrument" : "",
  ].filter(Boolean).join(" · ");
  const others = Array.from({ length: total }, (_, i) => i).filter((i) => i !== index);
  const prereqs = step.prereqs ?? (index > 0 ? [index - 1] : []);

  return (
    <div className={`step-checks${open ? " open" : ""}`}>
      <button type="button" className="step-checks-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <ChevronDown size={14} className="chev" />
        <span>Camera checks</span>
        <span className="faint">{summary}</span>
      </button>
      {open && (
        <div className="step-checks-body">
          <div className="sc-label">Yes/no questions the camera answers <span className="faint">· concrete, visible things</span></div>
          {qs.map((q, i) => (
            <div className="q-row" key={i}>
              <input className="input" value={q.q} onChange={(e) => setQ(i, { q: e.target.value })}
                placeholder="Is the cap screwed down flush?" aria-label={`Question ${i + 1}`} />
              <label className="must" title="Must be yes for the step to pass">
                <input type="checkbox" checked={q.must} onChange={(e) => setQ(i, { must: e.target.checked })} /> must
              </label>
              <button type="button" className="btn sm icon ghost" aria-label="Remove question"
                onClick={() => onChange({ questions: qs.filter((_, j) => j !== i) })}><Trash2 /></button>
            </div>
          ))}
          {qs.length < 4 && (
            <button type="button" className="btn sm ghost" onClick={() => onChange({ questions: [...qs, { q: "", must: true }] })}>
              <Plus /> Question
            </button>
          )}
          <div className="sc-grid">
            <label className="sc-field"><span>Severity</span>
              <select className="select input" value={step.severity || "major"} onChange={(e) => onChange({ severity: e.target.value as Severity })}>
                {SEVERITIES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
              </select></label>
            <label className="sc-field"><span>Kind</span>
              <select className="select input" value={step.kind || (step.reading ? "measurement" : "action")} onChange={(e) => onChange({ kind: e.target.value as StepKind })}>
                {KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
              </select></label>
            <label className="sc-field"><span>If it's missed</span>
              <select className="select input" value={step.intervention || (step.reversible === false ? "remind_before" : "notify_if_missed")}
                onChange={(e) => onChange({ intervention: e.target.value as Intervention })}>
                {INTERVENTIONS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
              </select></label>
            <label className="sc-field check"><input type="checkbox" checked={step.reversible === false}
              onChange={(e) => onChange({ reversible: !e.target.checked })} /> <span>Can't be undone</span></label>
          </div>
          <div className="sc-grid">
            <label className="sc-field"><span>Comes after steps</span>
              <input className="input mono" value={prereqs.map((p) => p + 1).join(", ")} placeholder="none: any time"
                onChange={(e) => onChange({ prereqs: csv(e.target.value).map((x) => Number(x) - 1).filter((n) => others.includes(n)) })} /></label>
            <label className="sc-field"><span>Objects in hand</span>
              <input className="input" value={(step.expected_objects || []).join(", ")} placeholder="scissors, bottle"
                onChange={(e) => onChange({ expected_objects: csv(e.target.value) })} /></label>
            {(step.reading || step.kind === "measurement") && (
              <label className="sc-field"><span>Read with instrument</span>
                <select className="select input" value={step.instrument_id || ""} onChange={(e) => onChange({ instrument_id: e.target.value || null })}>
                  <option value="">Vision model (less reliable)</option>
                  {instruments.map((i) => <option key={i.id} value={i.id}>{i.name} · {i.type}</option>)}
                </select></label>
            )}
          </div>
          <label className="sc-field"><span>Typical mistakes <span className="faint">· one per line</span></span>
            <textarea className="textarea" rows={2} value={(step.typical_errors || []).join("\n")} placeholder="Cross-threaded cap"
              onChange={(e) => onChange({ typical_errors: e.target.value.split("\n").map((x) => x.trim()).filter(Boolean) })} /></label>
          {(step.median_s != null || step.prototype) && (
            <div className="faint mono learned">
              learned: {step.median_s != null ? `${step.median_s} s ± ${step.std_s ?? 0}` : ""}
              {step.prototype ? ` · "done" looks like ${step.prototype.n} examples` : ""}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const FIELD_LABEL: Record<string, string> = {
  questions: "Questions", typical_errors: "Typical mistakes", kind: "Kind", severity: "Severity", reversible: "Undoable",
  prereqs: "Comes after", median_s: "Typical time", prototype: "What done looks like",
};

function show(field: string, v: unknown): string {
  if (v === undefined || v === null || v === "") return "—";
  if (field === "questions" && Array.isArray(v)) return (v as Question[]).map((q) => `${q.q}${q.must ? "" : " (optional)"}`).join(" / ");
  if (field === "prereqs" && Array.isArray(v)) return v.length ? (v as number[]).map((n) => `step ${n + 1}`).join(", ") : "any time";
  if (field === "reversible") return v ? "yes" : "no";
  if (field === "median_s") return `${v} s`;
  if (field === "prototype") return typeof v === "string" ? v : v ? "yes" : "—";
  if (Array.isArray(v)) return v.join(" / ");
  return String(v);
}

/** A reviewable diff of a proposal (drafted checks or what was learned from runs). Nothing is saved until applied. */
export function ProposalReview({ title, proposal, onApply, onClose }: {
  title: string; proposal: Proposal; onApply: (steps: GuidedStep[]) => void; onClose: () => void;
}) {
  return (
    <div className="proposal">
      <div className="row between">
        <h3>{title}</h3>
        <button type="button" className="btn sm icon ghost" aria-label="Close" onClick={onClose}><X /></button>
      </div>
      {proposal.notes.map((n, i) => <div className="note" key={i}>{n}</div>)}
      {!proposal.changes.length ? (
        <p className="muted" style={{ margin: 0 }}>Nothing to change.</p>
      ) : (
        <ul className="proposal-list">
          {proposal.changes.map((c) => (
            <li key={c.index}>
              <div className="pl-step"><span className="mono faint">{String(c.index + 1).padStart(2, "0")}</span> {c.instruction}</div>
              {Object.entries(c).filter(([k]) => k !== "index" && k !== "instruction").map(([k, v]) => {
                const d = v as { from: unknown; to: unknown };
                return (
                  <div className="pl-field" key={k}>
                    <span className="pl-k">{FIELD_LABEL[k] || k}</span>
                    <span className="pl-from">{show(k, d.from)}</span>
                    <span className="pl-to">{show(k, d.to)}</span>
                  </div>
                );
              })}
            </li>
          ))}
        </ul>
      )}
      <div className="row">
        <button type="button" className="btn primary" disabled={!proposal.changes.length} onClick={() => onApply(proposal.steps)}>
          <Check /> Apply to the editor
        </button>
        <button type="button" className="btn" onClick={onClose}>Discard</button>
        <span className="faint" style={{ fontSize: 12 }}>Then review and save the procedure.</span>
      </div>
    </div>
  );
}
