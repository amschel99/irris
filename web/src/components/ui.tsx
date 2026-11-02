import { Check, Copy, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { copy } from "../lib/util";

export function Mark({ size = 20 }: { size?: number }) {
  return (
    <svg viewBox="0 0 32 32" fill="none" width={size} height={size} aria-hidden="true">
      <circle cx="12.5" cy="16" r="9" stroke="currentColor" strokeWidth="2.2" />
      <circle cx="19.5" cy="16" r="9" stroke="currentColor" strokeWidth="2.2" />
      <path d="M16 7.71A9 9 0 0 1 16 24.29A9 9 0 0 1 16 7.71Z" fill="var(--lime, #D7FF3D)" />
    </svg>
  );
}

export function Spinner({ lg }: { lg?: boolean }) {
  return <span className={`spinner${lg ? " lg" : ""}`} aria-hidden="true" />;
}

export function Loading({ text = "Loading" }: { text?: string }) {
  return (
    <div className="loading" role="status">
      <Spinner /> {text}…
    </div>
  );
}

export function ErrorBox({ error, retry }: { error: string; retry?: () => void }) {
  if (!error) return null;
  return (
    <div className="error-box row between" role="alert">
      <span>{error}</span>
      {retry && (
        <button className="btn sm ghost" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function Empty({ title, children, action, center }: { title: string; children?: ReactNode; action?: ReactNode; center?: boolean }) {
  return (
    <div className={`empty${center ? " center" : ""}`}>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Head({ title, sub, eyebrow, actions }: { title: string; sub?: ReactNode; eyebrow?: string; actions?: ReactNode }) {
  return (
    <header className="head">
      <div>
        {eyebrow && <div className="eyebrow" style={{ marginBottom: 8 }}>{eyebrow}</div>}
        <h1>{title}</h1>
        {sub && <p>{sub}</p>}
      </div>
      {actions && <div className="row wrap">{actions}</div>}
    </header>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}

export function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button type="button" className="toggle" role="switch" aria-checked={on} aria-label={label} onClick={() => onChange(!on)} />
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: string }[]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function CopyButton({ text, label }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className={`btn sm ${label ? "" : "icon"} copy`}
      aria-label={label || "Copy"}
      onClick={async () => {
        if (await copy(text)) {
          setDone(true);
          window.setTimeout(() => setDone(false), 1400);
        }
      }}
    >
      {done ? <Check /> : <Copy />}
      {label && <span>{done ? "Copied" : label}</span>}
    </button>
  );
}

export function Code({ children }: { children: string }) {
  return (
    <div className="code-wrap">
      <pre className="code">{children}</pre>
      <CopyButton text={children} />
    </div>
  );
}

function useEscape(onClose: () => void) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
}

export function Drawer({ title, onClose, children, actions }: { title: ReactNode; onClose: () => void; children: ReactNode; actions?: ReactNode }) {
  useEscape(onClose);
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true">
        <div className="drawer-head">
          <div className="grow">{title}</div>
          {actions}
          <button className="btn icon ghost" aria-label="Close" onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="drawer-body">{children}</div>
      </aside>
    </>
  );
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEscape(onClose);
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="panel-head">
          <h2>{title}</h2>
          <button className="btn icon ghost sm" aria-label="Close" onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="panel-body">{children}</div>
      </div>
    </>
  );
}

const TYPE_TONE: Record<string, string> = { batch_record: "lime", lesson: "", alarm: "red", event: "amber" };
export function TypeTag({ type }: { type: string }) {
  return <span className={`tag ${TYPE_TONE[type] || ""}`}>{type}</span>;
}

export function ResultTag({ result }: { result: string | null }) {
  if (!result) return <span className="tag">pending</span>;
  const tone = result === "pass" ? "lime" : result === "fail" || result === "danger" ? "red" : result === "unsure" ? "amber" : "";
  return <span className={`tag ${tone}`}>{result}</span>;
}

export function StatusTag({ status, deviations = 0 }: { status: string; deviations?: number }) {
  if (status === "active") return <span className="tag lime">in progress</span>;
  if (status === "done") return deviations ? <span className="tag amber">done · {deviations} dev</span> : <span className="tag">done</span>;
  return <span className="tag">{status}</span>;
}

/** Pretty JSON with typed colours. */
export function Json({ value }: { value: unknown }) {
  const render = (v: unknown, indent: number): ReactNode => {
    const pad = "  ".repeat(indent);
    if (v === null) return <span className="b">null</span>;
    if (typeof v === "number") return <span className="n">{v}</span>;
    if (typeof v === "boolean") return <span className="b">{String(v)}</span>;
    if (typeof v === "string") return <span className="s">"{v}"</span>;
    if (Array.isArray(v)) {
      if (!v.length) return "[]";
      return (
        <>
          {"[\n"}
          {v.map((x, i) => (
            <span key={i}>
              {pad + "  "}
              {render(x, indent + 1)}
              {i < v.length - 1 ? ",\n" : "\n"}
            </span>
          ))}
          {pad + "]"}
        </>
      );
    }
    if (typeof v === "object") {
      const entries = Object.entries(v as Record<string, unknown>);
      if (!entries.length) return "{}";
      return (
        <>
          {"{\n"}
          {entries.map(([k, x], i) => (
            <span key={k}>
              {pad + "  "}
              <span className="k">"{k}"</span>: {render(x, indent + 1)}
              {i < entries.length - 1 ? ",\n" : "\n"}
            </span>
          ))}
          {pad + "}"}
        </>
      );
    }
    return String(v);
  };
  return <pre className="code json">{render(value, 0)}</pre>;
}

export function Sparkline({ values, width = 120, height = 28, min, max }: { values: number[]; width?: number; height?: number; min?: number; max?: number }) {
  if (values.length < 2) return <svg width={width} height={height} aria-hidden="true" />;
  const lo = Math.min(...values, min ?? Infinity);
  const hi = Math.max(...values, max ?? -Infinity);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * (width - 4) + 2, height - 3 - ((v - lo) / span) * (height - 6)]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true" style={{ overflow: "visible" }}>
      <path d={d} fill="none" stroke="var(--fg-2)" strokeWidth="1.25" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={last[0]} cy={last[1]} r="2.5" fill="var(--fg)" />
    </svg>
  );
}
