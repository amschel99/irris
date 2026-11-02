import { ExternalLink, KeyRound, Trash2, UserPlus } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Code, CopyButton, Empty, ErrorBox, Field, Head, Loading, Spinner, Tabs } from "../components/ui";
import { del, get, patch, post } from "../lib/api";
import { useSession } from "../lib/session";
import { ago, errText, useLoad } from "../lib/util";

type Tab = "workspace" | "members" | "models" | "developers" | "account";

function WorkspaceTab() {
  const { workspace, refreshWorkspace, refresh, toast, isAdmin } = useSession();
  const [name, setName] = useState(workspace?.name || "");
  const [industry, setIndustry] = useState(workspace?.industry || "");
  useEffect(() => { setName(workspace?.name || ""); setIndustry(workspace?.industry || ""); }, [workspace?.id]);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await patch("/workspace", { name, industry });
      await refreshWorkspace();
      await refresh();
      toast("Saved");
    } catch (err) {
      toast(errText(err), true);
    }
  };
  return (
    <form className="stack lg" style={{ maxWidth: 520 }} onSubmit={save}>
      <Field label="Workspace name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} disabled={!isAdmin} /></Field>
      <Field label="Industry"><input className="input" value={industry} onChange={(e) => setIndustry(e.target.value)} disabled={!isAdmin} placeholder="Plastics and moulding" /></Field>
      <dl className="kv">
        <dt>Workspace id</dt><dd className="mono">{workspace?.id}</dd>
        <dt>Your role</dt><dd>{workspace?.role}</dd>
      </dl>
      {isAdmin && <div><button className="btn primary">Save</button></div>}
    </form>
  );
}

function MembersTab() {
  const { workspace, isAdmin, toast } = useSession();
  const { data, error, loading, reload } = useLoad<{ members: { id: string; email: string; name: string; role: string; created: number }[]; invites: { email: string; role: string; link: string; created: number }[] }>(() => get("/members"), [workspace?.id]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [link, setLink] = useState("");
  const invite = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const r = await post("/members", { email, role });
      if (r.link) setLink(location.origin + r.link);
      else toast(`${r.added} added`);
      setEmail("");
      reload();
    } catch (err) {
      toast(errText(err), true);
    }
  };
  return (
    <div className="stack lg" style={{ maxWidth: 760 }}>
      {isAdmin && (
        <form className="row wrap" onSubmit={invite}>
          <input className="input grow" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="colleague@plant.co" aria-label="Email" style={{ minWidth: 220 }} />
          <select className="select input" style={{ width: 130 }} value={role} onChange={(e) => setRole(e.target.value)} aria-label="Role">
            <option value="member">Member</option>
            <option value="admin">Admin</option>
          </select>
          <button className="btn primary"><UserPlus /> Invite</button>
        </form>
      )}
      {link && (
        <div className="stack sm">
          <div className="note" style={{ color: "var(--fg)" }}>They don't have an account yet. Send them this sign-up link:</div>
          <div className="row"><input className="input mono" readOnly value={link} onFocus={(e) => e.target.select()} /><CopyButton text={link} /></div>
        </div>
      )}
      {loading && !data && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && (
        <ul className="list">
          {data.members.map((m) => (
            <li key={m.id}>
              <div className="grow"><div style={{ fontWeight: 500 }}>{m.name || m.email}</div><div className="faint" style={{ fontSize: 13 }}>{m.email}</div></div>
              <span className="tag">{m.role}</span>
              {isAdmin && m.role !== "owner" && (
                <button className="btn sm icon ghost" aria-label={`Remove ${m.email}`} onClick={async () => { if (confirm(`Remove ${m.email}?`)) { await del(`/members/${m.id}`).catch((e) => toast(errText(e), true)); reload(); } }}><Trash2 /></button>
              )}
            </li>
          ))}
          {data.invites.map((i) => (
            <li key={i.link}>
              <div className="grow"><div>{i.email}</div><div className="faint" style={{ fontSize: 13 }}>Invited {ago(i.created)} · hasn't joined yet</div></div>
              <span className="tag">{i.role}</span>
              <CopyButton text={location.origin + i.link} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ModelForm({ kind }: { kind: "llm" | "vlm" }) {
  const { workspace, refreshWorkspace, toast, isAdmin } = useSession();
  const own = workspace?.models[kind];
  const [base, setBase] = useState(own?.base_url || "");
  const [model, setModel] = useState(own?.model || "");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { setBase(own?.base_url || ""); setModel(own?.model || ""); setKey(""); }, [workspace?.id, own?.base_url, own?.model]);
  const def = kind === "llm" ? workspace?.defaults.llm : workspace?.defaults.vlm;
  const defModel = kind === "llm" ? workspace?.defaults.llm_model : workspace?.defaults.vlm_model;

  const save = async (reset = false) => {
    setBusy(true);
    try {
      await patch("/workspace", { models: { [kind]: reset ? {} : { base_url: base, model, api_key: key || undefined } } });
      await refreshWorkspace();
      setKey("");
      toast(reset ? "Back to the server default" : "Model saved");
    } catch (e) {
      toast(errText(e), true);
    } finally {
      setBusy(false);
    }
  };

  const state = own?.base_url ? `Your own: ${own.model || "default model"} at ${own.base_url}` : def ? `Server default: ${defModel}` : "Not set up";
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>{kind === "llm" ? "Reasoning model" : "Vision model"}</h2>
          <div className="faint" style={{ fontSize: 13 }}>{kind === "llm" ? "Plans, uses tools, writes answers. Any OpenAI-compatible chat API with tool calling." : "Looks through cameras and checks steps. Any OpenAI-compatible chat API that accepts images."}</div>
        </div>
        <span className={`tag ${own?.base_url || def ? "lime" : "amber"}`}>{own?.base_url || def ? "ready" : "off"}</span>
      </div>
      <div className="panel-body stack">
        <div className="muted" style={{ fontSize: 13 }}>{state}</div>
        {isAdmin && (
          <>
            <div className="grid2">
              <Field label="Base URL"><input className="input mono" value={base} onChange={(e) => setBase(e.target.value)} placeholder={kind === "llm" ? "https://api.deepseek.com" : "http://gpu-box:11434/v1"} spellCheck={false} /></Field>
              <Field label="Model"><input className="input mono" value={model} onChange={(e) => setModel(e.target.value)} placeholder={kind === "llm" ? "deepseek-chat" : "qwen3.5:2b"} spellCheck={false} /></Field>
            </div>
            <Field label="API key" hint={own?.has_key ? "A key is saved. Leave empty to keep it." : "Stored encrypted. Leave empty for servers that need none (Ollama)."}>
              <input className="input mono" type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={own?.has_key ? "••••••••" : "sk-…"} autoComplete="off" />
            </Field>
            {kind === "vlm" && (
              <div className="note">
                Running vision on your own GPU: install <a href="https://ollama.com" target="_blank" rel="noreferrer">Ollama</a> on the GPU machine, run <span className="mono">ollama pull qwen3.5:2b</span>, and put its address here as <span className="mono">http://&lt;host&gt;:11434/v1</span> (expose it with a tunnel if Irris runs in the cloud). On CPU expect 10–40 s per check; on a GPU, about a second.
              </div>
            )}
            <div className="row">
              <button className="btn primary" onClick={() => save()} disabled={busy || !base}>{busy ? <Spinner /> : "Save"}</button>
              {own?.base_url && <button className="btn" onClick={() => save(true)} disabled={busy}>Use the server default</button>}
            </div>
          </>
        )}
      </div>
    </section>
  );
}

function DevelopersTab() {
  const { workspace, isAdmin, toast } = useSession();
  const { data, reload } = useLoad<{ keys: { id: string; name: string; prefix: string; created: number; last_used: number | null }[] }>(() => get("/keys"), [workspace?.id]);
  const [name, setName] = useState("");
  const [fresh, setFresh] = useState("");
  const origin = location.origin;
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const k = await post("/keys", { name: name || "API key" });
      setFresh(k.key);
      setName("");
      reload();
    } catch (err) {
      toast(errText(err), true);
    }
  };
  const key = fresh || "irr_…";
  return (
    <div className="stack lg">
      <section className="stack">
        <div className="section-head" style={{ marginBottom: 0 }}>
          <div><h2>API keys</h2><p>A key opens this workspace's API and MCP server. Give each system its own key so you can revoke one without touching the others.</p></div>
          <div className="row"><a className="btn sm" href="/developers" target="_blank" rel="noreferrer"><ExternalLink /> Developer docs</a><a className="btn sm" href="/api/docs" target="_blank" rel="noreferrer"><ExternalLink /> API reference</a></div>
        </div>
        {isAdmin && (
          <form className="row" onSubmit={create}>
            <input className="input" style={{ maxWidth: 320 }} value={name} onChange={(e) => setName(e.target.value)} placeholder="PLC bridge" aria-label="Key name" />
            <button className="btn primary"><KeyRound /> Create key</button>
          </form>
        )}
        {fresh && (
          <div className="stack sm">
            <div className="note" style={{ color: "var(--fg)" }}>Copy this key now. It's shown only once.</div>
            <div className="row"><input className="input mono" readOnly value={fresh} onFocus={(e) => e.target.select()} aria-label="New API key" /><CopyButton text={fresh} /></div>
          </div>
        )}
        {data && !data.keys.length && <Empty title="No keys yet">Create one to send data from your systems or connect an AI agent over MCP.</Empty>}
        {!!data?.keys.length && (
          <ul className="list">
            {data.keys.map((k) => (
              <li key={k.id}>
                <KeyRound size={15} className="faint" />
                <div className="grow"><div style={{ fontWeight: 500 }}>{k.name}</div><div className="faint mono" style={{ fontSize: 12 }}>{k.prefix}… · created {ago(k.created)} · {k.last_used ? `used ${ago(k.last_used)}` : "never used"}</div></div>
                {isAdmin && <button className="btn sm danger" onClick={async () => { if (confirm(`Revoke "${k.name}"? Anything using it stops working.`)) { await del(`/keys/${k.id}`); reload(); } }}>Revoke</button>}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="stack">
        <div><h2>MCP</h2><p className="muted" style={{ margin: "4px 0 0" }}>Connect Claude, Cursor or any MCP client to this workspace. Tools: ask, ingest, search, records, sql, look, procedures, start_run, check_step, cameras.</p></div>
        <h3>Claude Code</h3>
        <Code>{`claude mcp add --transport http irris ${origin}/mcp --header "Authorization: Bearer ${key}"`}</Code>
        <h3>Claude Desktop, Cursor and other clients</h3>
        <Code>{JSON.stringify({ mcpServers: { irris: { type: "http", url: `${origin}/mcp`, headers: { Authorization: `Bearer ${key}` } } } }, null, 2)}</Code>
        <h3>Clients that only take a URL</h3>
        <Code>{`${origin}/mcp?key=${key}`}</Code>
        <p className="faint" style={{ margin: 0, fontSize: 12 }}>A key in a URL can end up in logs. Prefer the header when the client supports it.</p>
      </section>
    </div>
  );
}

function AccountTab() {
  const { me, refresh, toast } = useSession();
  const [name, setName] = useState(me?.user.name || "");
  const [pw, setPw] = useState({ password: "", new_password: "" });
  const saveName = async (e: React.FormEvent) => {
    e.preventDefault();
    try { await post("/me", { name }); await refresh(); toast("Saved"); } catch (err) { toast(errText(err), true); }
  };
  const savePw = async (e: React.FormEvent) => {
    e.preventDefault();
    try { await post("/me", pw); setPw({ password: "", new_password: "" }); toast("Password changed"); } catch (err) { toast(errText(err), true); }
  };
  return (
    <div className="stack lg" style={{ maxWidth: 520 }}>
      <form className="stack" onSubmit={saveName}>
        <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Email"><input className="input" value={me?.user.email || ""} disabled /></Field>
        <div><button className="btn">Save</button></div>
      </form>
      <form className="stack section" onSubmit={savePw}>
        <h2>Change password</h2>
        <Field label="Current password"><input className="input" type="password" autoComplete="current-password" required value={pw.password} onChange={(e) => setPw({ ...pw, password: e.target.value })} /></Field>
        <Field label="New password" hint="At least 8 characters."><input className="input" type="password" autoComplete="new-password" minLength={8} required value={pw.new_password} onChange={(e) => setPw({ ...pw, new_password: e.target.value })} /></Field>
        <div><button className="btn">Change password</button></div>
      </form>
    </div>
  );
}

export function Settings() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "workspace";
  return (
    <div className="page">
      <Head title="Settings" />
      <Tabs<Tab>
        tabs={[{ id: "workspace", label: "Workspace" }, { id: "members", label: "Members" }, { id: "models", label: "Models" }, { id: "developers", label: "Developers" }, { id: "account", label: "Account" }]}
        value={tab}
        onChange={(t) => setParams({ tab: t })}
      />
      {tab === "workspace" && <WorkspaceTab />}
      {tab === "members" && <MembersTab />}
      {tab === "models" && <div className="stack lg" style={{ maxWidth: 820 }}><ModelForm kind="llm" /><ModelForm kind="vlm" /></div>}
      {tab === "developers" && <DevelopersTab />}
      {tab === "account" && <AccountTab />}
    </div>
  );
}

