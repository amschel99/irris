import {
  Activity, Camera, ChevronsUpDown, Database, GraduationCap, LayoutGrid, LogOut, Menu, MessageSquare, Moon, Plus, ScanLine, Settings, Sun,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { post } from "../lib/api";
import { useSession } from "../lib/session";
import { errText, initials } from "../lib/util";
import { Field, Mark, Modal } from "./ui";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutGrid, end: true },
  { to: "/live", label: "Live", icon: ScanLine },
  { to: "/ask", label: "Ask", icon: MessageSquare },
  { to: "/data", label: "Data", icon: Database },
  { to: "/train", label: "Train", icon: GraduationCap },
  { to: "/runs", label: "Runs", icon: Activity },
  { to: "/cameras", label: "Cameras", icon: Camera },
  { to: "/settings", label: "Settings", icon: Settings },
];

const MOBILE = ["/", "/live", "/ask", "/data"];
export { NAV, useTheme };

function useTheme() {
  const [theme, setTheme] = useState<string>(() => document.documentElement.dataset.theme || "");
  const effective = theme || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const toggle = () => {
    const next = effective === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("irris.theme", next);
    } catch {
      /* fine */
    }
    setTheme(next);
  };
  return { effective, toggle };
}

function WorkspaceSwitch() {
  const { me, workspace, switchWorkspace, refresh, signOut, toast } = useSession();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const { effective, toggle } = useTheme();

  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    window.addEventListener("mousedown", h);
    return () => window.removeEventListener("mousedown", h);
  }, [open]);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const ws = await post("/workspaces", { name });
      await refresh();
      await switchWorkspace(ws.id);
      setCreating(false);
      setName("");
      toast("Workspace created");
    } catch (err) {
      toast(errText(err), true);
    }
  };

  return (
    <div className="ws-switch" ref={ref}>
      <button className="ws-btn" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-haspopup="menu">
        <span className="ws-avatar">{initials(workspace?.name || "?")}</span>
        <span className="grow">
          <div className="ws-name">{workspace?.name || "—"}</div>
          <div className="faint" style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{me?.user.email}</div>
        </span>
        <ChevronsUpDown size={14} className="faint" />
      </button>
      {open && (
        <div className="ws-menu" role="menu">
          <div className="eyebrow" style={{ padding: "4px 8px 6px" }}>Workspaces</div>
          {me?.workspaces.map((w) => (
            <button key={w.id} role="menuitem" onClick={() => { setOpen(false); switchWorkspace(w.id); }}>
              <span className="ws-avatar">{initials(w.name)}</span>
              <span className="grow ws-name">{w.name}</span>
              {w.id === workspace?.id && <span className="dot live" />}
            </button>
          ))}
          <button role="menuitem" onClick={() => { setOpen(false); setCreating(true); }}>
            <Plus size={15} /> New workspace
          </button>
          <hr />
          <button role="menuitem" onClick={toggle}>
            {effective === "dark" ? <Sun size={15} /> : <Moon size={15} />} {effective === "dark" ? "Light theme" : "Dark theme"}
          </button>
          <button role="menuitem" onClick={signOut}>
            <LogOut size={15} /> Sign out
          </button>
        </div>
      )}
      {creating && (
        <Modal title="New workspace" onClose={() => setCreating(false)}>
          <form className="stack" onSubmit={create}>
            <Field label="Name" hint="One workspace per site, line or team. Data never crosses workspaces.">
              <input className="input" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Nairobi line 3" required />
            </Field>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button type="button" className="btn" onClick={() => setCreating(false)}>Cancel</button>
              <button className="btn primary">Create</button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}

export function Shell() {
  const { workspace } = useSession();
  return (
    <div className="shell">
      <aside className="side">
        <a className="brand" href="/" aria-label="Irris website">
          <Mark /> Irris
        </a>
        <nav className="nav" aria-label="Main">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end}>
              <Icon /> {label}
            </NavLink>
          ))}
        </nav>
        <div className="side-foot">
          <WorkspaceSwitch />
        </div>
      </aside>
      <div className="main">
        <div className="mobile-top">
          <span className="brand" style={{ padding: 0 }}><Mark /> Irris</span>
          <span className="faint" style={{ fontSize: 13 }}>{workspace?.name}</span>
        </div>
        <Outlet />
      </div>
      <nav className="bottom-bar" aria-label="Main">
        {NAV.filter((n) => MOBILE.includes(n.to)).map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end}>
            <Icon /> {label}
          </NavLink>
        ))}
        <NavLink to="/more">
          <Menu /> More
        </NavLink>
      </nav>
    </div>
  );
}
