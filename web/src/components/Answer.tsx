import { ArrowRight, FileText } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { renderMarkdown } from "../lib/markdown";
import type { Source } from "../lib/types";
import { Mark } from "./ui";
import { RecordDrawer } from "./RecordDrawer";

/** An assistant message: markdown with citation chips, its sources, and any actions it took. */
export function Answer({ text, sources = [], actions = [], seconds }: { text: string; sources?: Source[]; actions?: any[]; seconds?: number }) {
  const [open, setOpen] = useState<Source | null>(null);
  const html = useMemo(() => renderMarkdown(text, new Set(sources.map((s) => s.n))), [text, sources]);
  const byN = useMemo(() => new Map(sources.map((s) => [s.n, s])), [sources]);

  const onClick = (e: React.MouseEvent | React.KeyboardEvent) => {
    const el = (e.target as HTMLElement).closest("[data-cite]") as HTMLElement | null;
    if (!el) return;
    if ("key" in e && e.key !== "Enter" && e.key !== " ") return;
    const s = byN.get(Number(el.dataset.cite));
    if (s) {
      e.preventDefault();
      setOpen(s);
    }
  };

  return (
    <div className="msg-ai">
      <div className="who"><Mark size={14} /> IRRIS{seconds ? ` · ${seconds}s` : ""}</div>
      <div className="md" onClick={onClick} onKeyDown={onClick} dangerouslySetInnerHTML={{ __html: html }} />
      {actions.filter((a) => a.type === "run_started").map((a) => (
        <Link key={a.run_id} className="btn sm primary" style={{ alignSelf: "flex-start" }} to={`/live?run=${a.run_id}`}>
          Open {a.procedure} in Live <ArrowRight />
        </Link>
      ))}
      {sources.length > 0 && (
        <div className="sources">
          {sources.map((s) => (
            <button key={s.n} className="source-chip" onClick={() => setOpen(s)} title={s.excerpt}>
              <span className="mono">{s.n}</span>
              <FileText size={12} />
              <span>{s.title}{s.page ? `, p. ${s.page}` : ""}</span>
            </button>
          ))}
        </div>
      )}
      {open && <RecordDrawer id={open.record_id} page={open.page} excerpt={open.excerpt} onClose={() => setOpen(null)} />}
    </div>
  );
}
