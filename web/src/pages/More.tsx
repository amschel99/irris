import { LogOut, Moon, Sun } from "lucide-react";
import { Link } from "react-router-dom";
import { NAV, useTheme } from "../components/Shell";
import { Head } from "../components/ui";
import { useSession } from "../lib/session";
import { initials } from "../lib/util";

/** The phone's menu: everything the bottom bar doesn't fit. */
export function More() {
  const { me, workspace, switchWorkspace, signOut } = useSession();
  const { effective, toggle } = useTheme();
  return (
    <div className="page">
      <Head title="More" sub={me?.user.email} />
      <div className="stack lg">
        <ul className="list">
          {NAV.filter((n) => !["/", "/live", "/ask", "/data"].includes(n.to)).map(({ to, label, icon: Icon }) => (
            <li key={to}><Link to={to} className="row grow" style={{ textDecoration: "none", padding: "4px 0" }}><Icon size={18} /> {label}</Link></li>
          ))}
        </ul>
        <section className="stack">
          <h2>Workspaces</h2>
          <ul className="list">
            {me?.workspaces.map((w) => (
              <li key={w.id} className="clickable" onClick={() => switchWorkspace(w.id)}>
                <span className="ws-avatar">{initials(w.name)}</span>
                <span className="grow">{w.name}</span>
                {w.id === workspace?.id && <span className="dot live" />}
              </li>
            ))}
          </ul>
        </section>
        <div className="row wrap">
          <button className="btn" onClick={toggle}>{effective === "dark" ? <Sun /> : <Moon />} {effective === "dark" ? "Light theme" : "Dark theme"}</button>
          <button className="btn" onClick={signOut}><LogOut /> Sign out</button>
        </div>
      </div>
    </div>
  );
}
