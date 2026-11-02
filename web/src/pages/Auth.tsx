import { ArrowRight } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Field, Mark, Spinner } from "../components/ui";
import { post } from "../lib/api";
import { useSession } from "../lib/session";
import type { Me } from "../lib/types";
import { errText } from "../lib/util";

/** The left half of the sign-in screens: a quiet line drawing of a bench, with a reticle reading a gauge. */
function Art() {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const c = ref.current!;
    const ctx = c.getContext("2d")!;
    let raf = 0;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const draw = (t: number) => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = c.clientWidth;
      const h = c.clientHeight;
      if (c.width !== w * dpr) {
        c.width = w * dpr;
        c.height = h * dpr;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      // floor grid in perspective
      ctx.strokeStyle = "rgba(245,245,243,0.06)";
      ctx.lineWidth = 1;
      const hor = h * 0.42;
      for (let i = -14; i <= 14; i++) {
        ctx.beginPath();
        ctx.moveTo(w / 2 + i * 14, hor);
        ctx.lineTo(w / 2 + i * w * 0.18, h);
        ctx.stroke();
      }
      for (let j = 0; j < 12; j++) {
        const y = hor + Math.pow(j / 12, 1.8) * (h - hor);
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }
      // bench + machine block
      ctx.strokeStyle = "rgba(245,245,243,0.32)";
      ctx.lineWidth = 1.2;
      const bx = w * 0.18, by = h * 0.5, bw = w * 0.64, bh = h * 0.05;
      ctx.strokeRect(bx, by, bw, bh);
      ctx.strokeRect(bx + bw * 0.08, by - h * 0.2, bw * 0.38, h * 0.2);
      for (let k = 0; k < 6; k++) ctx.strokeRect(bx + bw * 0.1 + k * bw * 0.058, by - h * 0.17, bw * 0.04, h * 0.05);
      // gauge
      const gx = bx + bw * 0.72, gy = by - h * 0.1, r = Math.min(w, h) * 0.07;
      ctx.beginPath();
      ctx.arc(gx, gy, r, 0, Math.PI * 2);
      ctx.stroke();
      const sweep = reduce ? 0.62 : 0.6 + Math.sin(t / 1400) * 0.04;
      const ang = Math.PI * 0.75 + sweep * Math.PI * 1.5;
      ctx.beginPath();
      ctx.moveTo(gx, gy);
      ctx.lineTo(gx + Math.cos(ang) * r * 0.82, gy + Math.sin(ang) * r * 0.82);
      ctx.strokeStyle = "rgba(245,245,243,0.7)";
      ctx.stroke();
      // reticle around the gauge
      const pad = r * 1.45;
      ctx.strokeStyle = "#D7FF3D";
      ctx.lineWidth = 1.6;
      const L = r * 0.45;
      for (const [sx, sy] of [[-1, -1], [1, -1], [-1, 1], [1, 1]] as const) {
        const cx = gx + sx * pad, cy = gy + sy * pad;
        ctx.beginPath();
        ctx.moveTo(cx - sx * L, cy);
        ctx.lineTo(cx, cy);
        ctx.lineTo(cx, cy - sy * L);
        ctx.stroke();
      }
      ctx.font = "500 11px 'JetBrains Mono', monospace";
      ctx.fillStyle = "#D7FF3D";
      const value = (176 + sweep * 10).toFixed(1);
      ctx.fillText(`PLATEN ${value} °C · CONF 0.97`, gx - pad, gy - pad - 8);
      if (!reduce) raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, []);
  return (
    <div className="auth-art">
      <canvas ref={ref} aria-hidden="true" />
      <div className="cap">
        <h2>Your operation, understood.</h2>
        <p>Connect your data, point a camera at the work, and Irris checks every step against your procedures.</p>
      </div>
    </div>
  );
}

export function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const { signedIn } = useSession();
  const nav = useNavigate();
  const [params] = useSearchParams();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const me = await post<Me>("/auth/login", { email, password });
      await signedIn(me);
      nav(params.get("next") || "/", { replace: true });
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth">
      <Art />
      <div className="auth-form">
        <form onSubmit={submit}>
          <a className="brand" href="/"><Mark /> Irris</a>
          <div>
            <h1>Sign in</h1>
            <p className="muted" style={{ margin: 0 }}>Welcome back.</p>
          </div>
          <Field label="Email">
            <input className="input" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
          </Field>
          <Field label="Password">
            <input className="input" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          {error && <div className="error-box" role="alert">{error}</div>}
          <button className="btn primary lg" disabled={busy}>
            {busy ? <Spinner /> : <>Sign in <ArrowRight /></>}
          </button>
          <p className="muted" style={{ margin: 0 }}>
            New to Irris? <Link to={`/signup${params.get("next") ? `?next=${encodeURIComponent(params.get("next")!)}` : ""}`}>Create an account</Link>
          </p>
        </form>
      </div>
    </div>
  );
}

const INDUSTRIES = [
  "Food and beverage", "Plastics and moulding", "Metal fabrication", "Electronics assembly", "Pharmaceuticals",
  "Cold chain and packaging", "Automotive parts", "Chemicals", "R&D lab", "Other",
];

export function Signup() {
  const [params] = useSearchParams();
  const invite = params.get("invite") || "";
  const [f, setF] = useState({ name: "", email: "", password: "", workspace: "", industry: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const { signedIn } = useSession();
  const nav = useNavigate();
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const me = await post<Me>("/auth/signup", { ...f, invite: invite || undefined });
      await signedIn(me);
      nav(params.get("next") || "/", { replace: true });
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth">
      <Art />
      <div className="auth-form">
        <form onSubmit={submit}>
          <a className="brand" href="/"><Mark /> Irris</a>
          <div>
            <h1>{invite ? "Join your team" : "Create your workspace"}</h1>
            <p className="muted" style={{ margin: 0 }}>
              {invite ? "You've been invited to a workspace on Irris." : "Free to start. You'll connect data and a camera next."}
            </p>
          </div>
          <Field label="Your name">
            <input className="input" autoComplete="name" required value={f.name} onChange={set("name")} autoFocus />
          </Field>
          <Field label="Work email">
            <input className="input" type="email" autoComplete="email" required value={f.email} onChange={set("email")} />
          </Field>
          <Field label="Password" hint="At least 8 characters.">
            <input className="input" type="password" autoComplete="new-password" required minLength={8} value={f.password} onChange={set("password")} />
          </Field>
          {!invite && (
            <div className="grid2" style={{ gap: 12 }}>
              <Field label="Team or company">
                <input className="input" required value={f.workspace} onChange={set("workspace")} placeholder="Line 3" />
              </Field>
              <Field label="Industry">
                <select className="select input" value={f.industry} onChange={set("industry")}>
                  <option value="">Choose…</option>
                  {INDUSTRIES.map((i) => <option key={i}>{i}</option>)}
                </select>
              </Field>
            </div>
          )}
          {error && <div className="error-box" role="alert">{error}</div>}
          <button className="btn primary lg" disabled={busy}>
            {busy ? <Spinner /> : <>{invite ? "Join" : "Create workspace"} <ArrowRight /></>}
          </button>
          <p className="muted" style={{ margin: 0 }}>Have an account? <Link to="/login">Sign in</Link></p>
        </form>
      </div>
    </div>
  );
}
