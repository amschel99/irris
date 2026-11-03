import { Play, Plug, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { Code, Empty, ErrorBox, Field, Loading, Modal, Spinner } from "./ui";
import { del, get, post } from "../lib/api";
import { useSession } from "../lib/session";
import { ago, errText, useLoad } from "../lib/util";

type Tool = { name: string; description: string; inputSchema: { properties?: Record<string, { type?: string; description?: string }> } };
type Sync = { id: string; tool: string; resource: string; arguments: Record<string, unknown>; every_minutes: number;
  type: string; last_run: number | null; last_status: string; next_run: number };
type Server = { id: string; name: string; url: string; status: string; last_sync: number | null; has_auth: boolean;
  server: { name?: string; title?: string; version?: string }; tools: Tool[];
  resources: { uri: string; name: string; mimeType: string }[]; syncs: Sync[] };

const EVERY = [[5, "5 min"], [15, "15 min"], [60, "hour"], [360, "6 hours"], [1440, "day"]] as const;

function parseArgs(text: string): Record<string, unknown> {
  if (!text.trim()) return {};
  const v = JSON.parse(text);
  if (typeof v !== "object" || Array.isArray(v) || v === null) throw new Error("arguments must be a JSON object");
  return v;
}

function TryTool({ server, onChange }: { server: Server; onChange: () => void }) {
  const { toast, isAdmin } = useSession();
  const [target, setTarget] = useState(server.tools[0] ? `tool:${server.tools[0].name}` : server.resources[0] ? `res:${server.resources[0].uri}` : "");
  const [args, setArgs] = useState("{}");
  const [type, setType] = useState("row");
  const [every, setEvery] = useState(60);
  const [busy, setBusy] = useState("");
  const [out, setOut] = useState<string>("");
  const isTool = target.startsWith("tool:");
  const name = target.slice(isTool ? 5 : 4);
  const tool = server.tools.find((t) => t.name === name);
  const body = () => ({ ...(isTool ? { tool: name } : { resource: name }), arguments: parseArgs(args), type });

  const run = async (save: boolean) => {
    setBusy(save ? "save" : "try");
    try {
      const r = await post<{ result: { content: unknown }; saved?: { created: number; updated: number } }>(`/mcp-servers/${server.id}/call`, { ...body(), save });
      setOut(typeof r.result.content === "string" ? r.result.content : JSON.stringify(r.result.content, null, 2));
      if (r.saved) toast(`Saved: ${r.saved.created} new, ${r.saved.updated} updated`);
    } catch (e) {
      toast(errText(e), true);
    } finally {
      setBusy("");
    }
  };
  const schedule = async () => {
    setBusy("sync");
    try {
      await post(`/mcp-servers/${server.id}/syncs`, { ...body(), every_minutes: every });
      toast("Sync scheduled");
      onChange();
    } catch (e) {
      toast(errText(e), true);
    } finally {
      setBusy("");
    }
  };

  if (!target) return <div className="note">This server offers no tools or resources.</div>;
  return (
    <div className="stack sm">
      <div className="grid2" style={{ gap: 12 }}>
        <Field label="Tool or resource">
          <select className="select input" value={target} onChange={(e) => setTarget(e.target.value)}>
            {server.tools.map((t) => <option key={t.name} value={`tool:${t.name}`}>{t.name}</option>)}
            {server.resources.map((r) => <option key={r.uri} value={`res:${r.uri}`}>{r.name} (resource)</option>)}
          </select>
        </Field>
        <Field label="Save as record type" hint="What the rows become in Irris, e.g. work_order.">
          <input className="input mono" value={type} onChange={(e) => setType(e.target.value)} />
        </Field>
      </div>
      {tool?.description && <p className="muted" style={{ margin: 0, fontSize: 13 }}>{tool.description}</p>}
      {isTool && (
        <Field label="Arguments (JSON)" hint={tool?.inputSchema?.properties ? `Takes: ${Object.keys(tool.inputSchema.properties).join(", ") || "nothing"}` : undefined}>
          <textarea className="input mono" rows={3} value={args} onChange={(e) => setArgs(e.target.value)} spellCheck={false} />
        </Field>
      )}
      <div className="row wrap" style={{ justifyContent: "flex-end", gap: 8 }}>
        <button className="btn sm" onClick={() => run(false)} disabled={!!busy}>{busy === "try" ? <Spinner /> : <Play />} Try</button>
        <button className="btn sm" onClick={() => run(true)} disabled={!!busy}>{busy === "save" ? <Spinner /> : null} Pull into Irris</button>
        {isAdmin && (
          <span className="row" style={{ gap: 6 }}>
            <select className="select input" style={{ width: "auto" }} value={every} onChange={(e) => setEvery(+e.target.value)} aria-label="Sync interval">
              {EVERY.map(([m, l]) => <option key={m} value={m}>every {l}</option>)}
            </select>
            <button className="btn sm primary" onClick={schedule} disabled={!!busy}>{busy === "sync" ? <Spinner /> : <RefreshCw />} Sync on a schedule</button>
          </span>
        )}
      </div>
      {out && <Code>{out.length > 6000 ? out.slice(0, 6000) + "\n…" : out}</Code>}
    </div>
  );
}

function ServerCard({ server, onChange }: { server: Server; onChange: () => void }) {
  const { toast, isAdmin } = useSession();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState("");

  const refresh = async () => {
    setBusy("refresh");
    try { await post(`/mcp-servers/${server.id}/refresh`); onChange(); } catch (e) { toast(errText(e), true); } finally { setBusy(""); }
  };
  const remove = async () => {
    if (!confirm(`Disconnect ${server.name}? Records already pulled in stay.`)) return;
    await del(`/mcp-servers/${server.id}`).catch((e) => toast(errText(e), true));
    onChange();
  };
  const runSync = async (s: Sync) => {
    setBusy(s.id);
    try {
      const r = await post<{ status: string }>(`/mcp-servers/${server.id}/syncs/${s.id}/run`);
      toast(r.status, r.status.startsWith("error"));
      onChange();
    } catch (e) { toast(errText(e), true); } finally { setBusy(""); }
  };
  const dropSync = async (s: Sync) => {
    await del(`/mcp-servers/${server.id}/syncs/${s.id}`).catch((e) => toast(errText(e), true));
    onChange();
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <div className="row">
          <Plug size={16} />
          <div>
            <div style={{ fontWeight: 500 }}>{server.name}</div>
            <div className="faint mono" style={{ fontSize: 12 }}>{server.url}</div>
            <div className="faint" style={{ fontSize: 12 }}>{server.status}{server.last_sync ? ` · last sync ${ago(server.last_sync)}` : ""}</div>
          </div>
        </div>
        <div className="row">
          <button className="btn sm" onClick={() => setOpen((o) => !o)}>{open ? "Close" : "Use a tool"}</button>
          <button className="btn sm icon" aria-label="Refresh tools" onClick={refresh} disabled={!!busy}>{busy === "refresh" ? <Spinner /> : <RefreshCw />}</button>
          {isAdmin && <button className="btn sm icon danger" aria-label="Disconnect" onClick={remove}><Trash2 /></button>}
        </div>
      </div>
      <div className="panel-body stack">
        <div className="row wrap" style={{ gap: 6 }}>
          {server.tools.slice(0, 24).map((t) => <span key={t.name} className="tag mono" title={t.description}>{t.name}</span>)}
          {server.tools.length > 24 && <span className="faint">+{server.tools.length - 24} more</span>}
        </div>
        {open && <TryTool server={server} onChange={onChange} />}
        {server.syncs.length > 0 && (
          <div className="table-wrap">
            <table className="t">
              <thead><tr><th>Sync</th><th>Every</th><th>Last run</th><th /></tr></thead>
              <tbody>
                {server.syncs.map((s) => (
                  <tr key={s.id}>
                    <td className="mono">{s.tool || s.resource} → {s.type}</td>
                    <td>{s.every_minutes >= 60 ? `${s.every_minutes / 60} h` : `${s.every_minutes} min`}</td>
                    <td className={s.last_status.startsWith("error") ? "" : "faint"} style={{ fontSize: 12, color: s.last_status.startsWith("error") ? "var(--red)" : undefined }}>
                      {s.last_run ? `${ago(s.last_run)} · ${s.last_status}` : "not run yet"}
                    </td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <button className="btn sm" onClick={() => runSync(s)} disabled={!!busy}>{busy === s.id ? <Spinner /> : <Play />} Run</button>
                      {isAdmin && <button className="btn sm icon danger" aria-label="Remove sync" onClick={() => dropSync(s)}><Trash2 /></button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export function McpServersTab() {
  const { workspace, isAdmin } = useSession();
  const { data, error, loading, reload } = useLoad<{ servers: Server[] }>(() => get("/mcp-servers"), [workspace?.id]);
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ name: "", url: "", token: "" });
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");

  const connect = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setFormError("");
    try {
      await post("/mcp-servers", f);
      setOpen(false);
      setF({ name: "", url: "", token: "" });
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
          <h2>MCP servers</h2>
          <p>Connect any MCP server: your ERP, MES, LIMS, historian or another AI service. Irris can call its tools when you ask, and pull its data into your records on a schedule.</p>
        </div>
        {isAdmin && <button className="btn primary" onClick={() => setOpen(true)}><Plus /> Connect a server</button>}
      </div>
      {loading && !data && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && !data.servers.length && (
        <Empty title="No MCP server connected" action={isAdmin ? <button className="btn sm" onClick={() => setOpen(true)}>Connect a server</button> : undefined}>
          Paste the server's MCP URL and its token. Irris lists its tools, then you can try them, pull results in, or ask Irris questions that use them.
        </Empty>
      )}
      {data?.servers.map((s) => <ServerCard key={s.id} server={s} onChange={reload} />)}
      {open && (
        <Modal title="Connect an MCP server" onClose={() => setOpen(false)}>
          <form className="stack" onSubmit={connect}>
            <Field label="Name" hint="Optional; the server's own name is used otherwise.">
              <input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="ERP" />
            </Field>
            <Field label="MCP URL" hint="Streamable HTTP, e.g. https://erp.example.com/mcp. Public addresses only.">
              <input className="input mono" required value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} placeholder="https://…/mcp" autoComplete="off" spellCheck={false} />
            </Field>
            <Field label="Token" hint="Sent as Authorization: Bearer …. Stored encrypted, never shown again.">
              <input className="input mono" type="password" value={f.token} onChange={(e) => setF({ ...f, token: e.target.value })} autoComplete="off" />
            </Field>
            {formError && <div className="error-box">{formError}</div>}
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button type="button" className="btn" onClick={() => setOpen(false)}>Cancel</button>
              <button className="btn primary" disabled={busy}>{busy ? <><Spinner /> Connecting…</> : "Connect"}</button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
