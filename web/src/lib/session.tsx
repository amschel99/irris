import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { get, getWorkspaceId, post, setUnauthorizedHandler, setWorkspaceId } from "./api";
import type { Me, Workspace } from "./types";

interface Toast {
  id: number;
  text: string;
  err?: boolean;
}

interface SessionValue {
  me: Me | null;
  workspace: Workspace | null;
  ready: boolean;
  refresh: () => Promise<void>;
  refreshWorkspace: () => Promise<void>;
  switchWorkspace: (id: string) => Promise<void>;
  signedIn: (me: Me) => Promise<void>;
  signOut: () => Promise<void>;
  toast: (text: string, err?: boolean) => void;
  isAdmin: boolean;
}

const Ctx = createContext<SessionValue | null>(null);

export function useSession(): SessionValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSession outside SessionProvider");
  return v;
}

const PUBLIC = ["/login", "/signup", "/cam"];
const isPublicPath = (path: string) => PUBLIC.some((p) => path === p || path.startsWith(p + "/"));

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [ready, setReady] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nav = useNavigate();
  const loc = useLocation();


  const toast = useCallback((text: string, err = false) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-2), { id, text, err }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), err ? 5200 : 2600);
  }, []);

  const loadWorkspace = useCallback(async (m: Me) => {
    let id = getWorkspaceId();
    if (!m.workspaces.some((w) => w.id === id)) {
      id = m.workspaces[0]?.id || "";
      setWorkspaceId(id);
    }
    setWorkspace(id ? await get<Workspace>("/workspace") : null);
  }, []);

  const refresh = useCallback(async () => {
    const m = await get<Me>("/me");
    setMe(m);
    await loadWorkspace(m);
  }, [loadWorkspace]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setMe(null);
      setWorkspace(null);
      const here = window.location.pathname.replace(/^\/app/, "") || "/";
      if (!isPublicPath(here)) {
        nav(`/login?next=${encodeURIComponent(here + window.location.search)}`, { replace: true });
      }
    });
  }, [nav]);

  useEffect(() => {
    if (loc.pathname === "/cam") {
      setReady(true);
      return;
    }
    (async () => {
      try {
        await refresh();
      } catch {
        /* 401 handled by the handler above */
      } finally {
        setReady(true);
      }
    })();
    // only on first load
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const value = useMemo<SessionValue>(
    () => ({
      me,
      workspace,
      ready,
      refresh,
      toast,
      isAdmin: workspace?.role === "owner" || workspace?.role === "admin",
      refreshWorkspace: async () => setWorkspace(await get<Workspace>("/workspace")),
      switchWorkspace: async (id: string) => {
        setWorkspaceId(id);
        setWorkspace(await get<Workspace>("/workspace"));
        nav("/");
      },
      signedIn: async (m: Me) => {
        setMe(m);
        await loadWorkspace(m);
      },
      signOut: async () => {
        await post("/auth/logout").catch(() => {});
        setMe(null);
        setWorkspace(null);
        nav("/login");
      },
    }),
    [me, workspace, ready, refresh, toast, nav, loadWorkspace],
  );

  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast${t.err ? " err" : ""}`}>{t.text}</div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
