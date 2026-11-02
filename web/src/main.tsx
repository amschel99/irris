import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { Empty, Loading } from "./components/ui";
import { SessionProvider, useSession } from "./lib/session";
import { AskPage } from "./pages/Ask";
import { Login, Signup } from "./pages/Auth";
import { Cam } from "./pages/Cam";
import { Cameras } from "./pages/Cameras";
import { Data } from "./pages/Data";
import { Live } from "./pages/Live";
import { More } from "./pages/More";
import { Overview } from "./pages/Overview";
import { RunDetail, Runs } from "./pages/Runs";
import { Settings } from "./pages/Settings";
import { Train } from "./pages/Train";
import "./styles.css";

function Private({ children }: { children: ReactNode }) {
  const { ready, me, workspace } = useSession();
  if (!ready) return <div className="page"><Loading /></div>;
  if (!me) return <Navigate to="/login" replace />;
  if (!workspace) return <div className="page"><Loading text="Opening your workspace" /></div>;
  return <>{children}</>;
}

function Public({ children }: { children: ReactNode }) {
  const { ready, me } = useSession();
  if (ready && me && !new URLSearchParams(location.search).get("invite")) return <Navigate to="/" replace />;
  return <>{children}</>;
}

function NotFound() {
  return (
    <div className="page">
      <Empty title="This page doesn't exist" action={<Link className="btn sm" to="/">Go to Overview</Link>}>The link may be old, or the page moved.</Empty>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter basename="/app" future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <SessionProvider>
        <Routes>
          <Route path="/login" element={<Public><Login /></Public>} />
          <Route path="/signup" element={<Public><Signup /></Public>} />
          <Route path="/cam" element={<Cam />} />
          <Route element={<Private><Shell /></Private>}>
            <Route index element={<Overview />} />
            <Route path="ask" element={<AskPage />} />
            <Route path="live" element={<Live />} />
            <Route path="cameras" element={<Cameras />} />
            <Route path="data" element={<Data />} />
            <Route path="train" element={<Train />} />
            <Route path="runs" element={<Runs />} />
            <Route path="runs/:id" element={<RunDetail />} />
            <Route path="settings" element={<Settings />} />
            <Route path="more" element={<More />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </SessionProvider>
    </BrowserRouter>
  </StrictMode>,
);
