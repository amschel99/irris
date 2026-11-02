import QRCode from "qrcode";
import { Eye, FileVideo, Link2, Plus, RefreshCw, ScanLine, Smartphone, Trash2, Video } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { VisionBadge } from "../components/media";
import { RecordDrawer } from "../components/RecordDrawer";
import { CopyButton, Empty, ErrorBox, Field, Head, Loading, Modal, Spinner, Toggle } from "../components/ui";
import { del, frameUrl, get, getWorkspaceId, patch, post, put } from "../lib/api";
import { useJob, useVisionStatus, type CameraState, type WatchRule } from "../lib/media";
import { useSession } from "../lib/session";
import type { Camera, Procedure } from "../lib/types";
import { ago, errText, useInterval, useLoad } from "../lib/util";

function QR({ text }: { text: string }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    if (ref.current) QRCode.toCanvas(ref.current, text, { width: 192, margin: 0, color: { dark: "#0A0A0B", light: "#FFFFFF" } });
  }, [text]);
  return <div className="qr"><canvas ref={ref} aria-label="QR code with the pairing link" /></div>;
}

function Pairing({ name, url, onClose }: { name: string; url: string; onClose: () => void }) {
  const insecure = location.protocol !== "https:" && !["localhost", "127.0.0.1"].includes(location.hostname);
  return (
    <Modal title={`Pair ${name}`} onClose={onClose}>
      <div className="stack lg" style={{ alignItems: "center", textAlign: "center" }}>
        <QR text={url} />
        <div className="stack sm">
          <p style={{ margin: 0 }}>Scan this with the phone's camera, or open the link on the phone.</p>
          <p className="muted" style={{ margin: 0 }}>No sign-in on the phone. Mount it facing the work and keep the page open. Irris's checks and alerts show and speak on it.</p>
        </div>
        <div className="row" style={{ width: "100%" }}>
          <input className="input mono" readOnly value={url} onFocus={(e) => e.target.select()} aria-label="Pairing link" />
          <CopyButton text={url} />
        </div>
        {insecure && <div className="note" style={{ textAlign: "left" }}>Phones only allow the camera over HTTPS. Open Irris through its https:// address before pairing.</div>}
        <p className="faint" style={{ margin: 0, fontSize: 12 }}>Anyone with this link can stream video into this workspace. Re-pair to cancel it.</p>
      </div>
    </Modal>
  );
}

const INTERVALS = [10, 30, 60, 300, 900];
const every = (s: number) => (s < 60 ? `${s} s` : `${s / 60} min`);

function WatchPanel({ cam }: { cam: Camera }) {
  const { toast } = useSession();
  const [state, setState] = useState<CameraState | null>(null);
  const [cond, setCond] = useState("");
  const [interval, setIntervalS] = useState(30);
  const [severity, setSeverity] = useState<"info" | "warning" | "danger">("warning");
  const [speak, setSpeak] = useState(true);
  const [busy, setBusy] = useState("");
  const load = () => get<CameraState>(`/cameras/${cam.id}/state`).then(setState).catch(() => {});
  useEffect(() => { load(); }, [cam.id]);
  useInterval(load, 15000);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy("add");
    try {
      await post("/watch", { camera_id: cam.id, condition: cond, interval_s: interval, severity, speak });
      setCond("");
      load();
    } catch (err) {
      toast(errText(err), true);
    } finally {
      setBusy("");
    }
  };
  const test = async (r: WatchRule) => {
    setBusy(r.id);
    try {
      const out = await post<{ matched: boolean; say: string; ms: number; where: string }>(`/watch/${r.id}/test`);
      toast(`${out.matched ? "True now" : "Not now"}: ${out.say} (${out.where.toUpperCase()}, ${(out.ms / 1000).toFixed(1)} s)`);
      load();
    } catch (err) {
      toast(errText(err), true);
    } finally {
      setBusy("");
    }
  };
  const setCam = async (body: Partial<CameraState>) => {
    try {
      const s = await put<CameraState>(`/cameras/${cam.id}/state`, body);
      setState((c) => ({ ...(c || { rules: [] }), ...s }));
    } catch (err) {
      toast(errText(err), true);
    }
  };

  if (!state) return <Loading />;
  const rules = state.rules || [];
  return (
    <div className="stack">
      <div className="row wrap" style={{ gap: 18 }}>
        <label className="row" style={{ gap: 8, fontSize: 13 }}><Toggle on={!!state.coach_s} onChange={(v) => setCam({ coach_s: v ? 25 : 0 })} label="Coach mode" /> Coach</label>
        <label className="row" style={{ gap: 8, fontSize: 13 }}><Toggle on={state.notice} onChange={(v) => setCam({ notice: v })} label="Notice mode" /> Answer questions written in view</label>
      </div>
      {!!state.coach_s && (
        <Field label="Goal" hint="Coach mode looks every 25 s and speaks only for mistakes, safety, readings or a finished step.">
          <input className="input" defaultValue={state.goal} onBlur={(e) => e.target.value !== state.goal && setCam({ goal: e.target.value })} placeholder="Assemble the heater block" />
        </Field>
      )}
      <div>
        <div className="eyebrow" style={{ marginBottom: 4 }}>Watch for</div>
        {!rules.length && <p className="faint" style={{ margin: "4px 0 0", fontSize: 13 }}>Nothing yet. Describe something Irris should keep an eye on, in plain words.</p>}
        {rules.map((r) => (
          <div key={r.id} className={`rule${r.active ? " active" : ""}`}>
            <span className="cond">{r.condition}</span>
            <span className="row" style={{ gap: 6 }}>
              <Toggle on={r.enabled} onChange={async (v) => { await patch(`/watch/${r.id}`, { enabled: v }).catch((e) => toast(errText(e), true)); load(); }} label={`Watch: ${r.condition}`} />
              <button className="btn sm" onClick={() => test(r)} disabled={busy === r.id}>{busy === r.id ? <Spinner /> : "Test"}</button>
              <button className="btn sm icon ghost" aria-label="Delete rule" onClick={async () => { await del(`/watch/${r.id}`); load(); }}><Trash2 /></button>
            </span>
            <span className="meta">
              <span className={`tag ${r.severity === "danger" ? "red" : r.severity === "warning" ? "amber" : ""}`}>{r.severity}</span>
              <span>every {every(r.interval_s)}</span>
              {r.speak && <span>spoken</span>}
              <span>{r.last_check ? `checked ${ago(r.last_check)}` : "not checked yet"}</span>
              {r.matches > 0 && <span>{r.matches} hit{r.matches === 1 ? "" : "s"}{r.last_match ? `, last ${ago(r.last_match)}` : ""}</span>}
              {r.last_say && <span className="muted">“{r.last_say}”</span>}
            </span>
          </div>
        ))}
      </div>
      <form className="stack sm" onSubmit={add}>
        <input className="input" value={cond} onChange={(e) => setCond(e.target.value)} placeholder="Someone reaches into the press without gloves" aria-label="Condition to watch for" />
        <div className="row wrap">
          <select className="select input" style={{ width: 120 }} value={interval} onChange={(e) => setIntervalS(Number(e.target.value))} aria-label="How often">
            {INTERVALS.map((s) => <option key={s} value={s}>every {every(s)}</option>)}
          </select>
          <div className="seg" role="group" aria-label="Severity">
            {(["info", "warning", "danger"] as const).map((s) => <button key={s} type="button" aria-pressed={severity === s} onClick={() => setSeverity(s)}>{s}</button>)}
          </div>
          <label className="row" style={{ gap: 8, fontSize: 13 }}><Toggle on={speak} onChange={setSpeak} label="Speak the alert" /> Speak</label>
          <span className="grow" />
          <button className="btn primary sm" disabled={cond.trim().length < 4 || busy === "add"}><Eye /> Watch</button>
        </div>
      </form>
    </div>
  );
}

function VideoReview({ onClose }: { onClose: () => void }) {
  const { workspace } = useSession();
  const procs = useLoad<{ procedures: Procedure[] }>(() => get("/procedures"), [workspace?.id]);
  const [file, setFile] = useState<File | null>(null);
  const [question, setQuestion] = useState("");
  const [proc, setProc] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const job = useJob(jobId);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setErr("");
    const fd = new FormData();
    fd.append("file", file);
    fd.append("question", question);
    fd.append("procedure_id", proc);
    try {
      const r = await fetch("/api/v1/video/review", { method: "POST", credentials: "same-origin", headers: { "X-Workspace": getWorkspaceId() }, body: fd });
      const b = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(b.detail || `Upload failed (${r.status})`);
      setJobId(b.id);
    } catch (e2) {
      setErr(errText(e2));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Review a video" onClose={onClose}>
      {!jobId ? (
        <form className="stack" onSubmit={submit}>
          <p className="muted" style={{ margin: 0 }}>Irris watches the recording and writes what happened: a timeline, readings, and anything done wrong or out of order.</p>
          <Field label="Video"><input className="input" type="file" accept="video/*" onChange={(e) => setFile(e.target.files?.[0] || null)} /></Field>
          <Field label="Compare against a procedure" hint="Optional. Irris flags skipped or out-of-order steps.">
            <select className="select input" value={proc} onChange={(e) => setProc(e.target.value)}>
              <option value="">None</option>
              {procs.data?.procedures.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </Field>
          <Field label="Anything specific?"><input className="input" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="Did they wear gloves the whole time?" /></Field>
          {err && <div className="error-box">{err}</div>}
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="btn" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={!file || busy}>{busy ? <Spinner /> : <><FileVideo /> Review</>}</button>
          </div>
        </form>
      ) : (
        <div className="stack">
          {(!job || job.status === "running") && <div className="row muted"><Spinner /> {job ? `${job.note} · ${Math.round(job.progress * 100)}%` : "Starting…"}</div>}
          {job?.status === "failed" && <div className="error-box">{job.error}</div>}
          {job?.status === "done" && (
            <>
              <p style={{ margin: 0 }}>{job.result.summary}</p>
              <ul className="timeline">
                {job.result.timeline?.map((t, i) => <li key={i}><span className="t">{t.t}</span>{t.what}</li>)}
                {job.result.problems?.map((p, i) => <li key={`p${i}`} className="problem"><span className="t">{p.t}</span>{p.what}</li>)}
              </ul>
              {job.result.record_id && <button className="btn sm" onClick={() => setOpen(job.result.record_id!)}>Open the saved review</button>}
            </>
          )}
          <p className="faint" style={{ margin: 0, fontSize: 12 }}>You can close this; the review keeps going and you'll get an alert when it's ready.</p>
        </div>
      )}
      {open && <RecordDrawer id={open} onClose={() => setOpen(null)} />}
    </Modal>
  );
}

export function Cameras() {
  const { workspace, toast } = useSession();
  const { data, error, loading, reload } = useLoad<{ cameras: Camera[] }>(() => get("/cameras"), [workspace?.id]);
  const vstatus = useVisionStatus(15000);
  const [adding, setAdding] = useState<"phone" | "url" | null>(null);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const [pair, setPair] = useState<{ name: string; url: string } | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [tick, setTick] = useState(0);
  const [openWatch, setOpenWatch] = useState<string | null>(null);
  useInterval(() => { setTick((t) => t + 1); reload(); }, 3000);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setFormError("");
    try {
      const cam = await post("/cameras", adding === "url" ? { kind: "url", name, url } : { kind: "phone", name });
      if (cam.pair_url) setPair({ name: cam.name, url: location.origin + cam.pair_url });
      else toast("Camera added");
      setAdding(null);
      setName("");
      setUrl("");
      reload();
    } catch (err) {
      setFormError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  const repair = async (c: Camera) => {
    try {
      const r = await post(`/cameras/${c.id}/pair`);
      setPair({ name: c.name, url: location.origin + r.pair_url });
    } catch (e) {
      toast(errText(e), true);
    }
  };

  const remove = async (c: Camera) => {
    if (!confirm(`Remove ${c.name}? Its watch rules stop too.`)) return;
    await del(`/cameras/${c.id}`).catch((e) => toast(errText(e), true));
    reload();
  };

  return (
    <div className="page">
      <Head
        title="Cameras"
        sub="Any phone becomes a camera. Factory cameras connect by URL. Each camera can watch for things in plain words, coach, and answer questions written in view."
        actions={<>
          <VisionBadge status={vstatus} />
          <button className="btn" onClick={() => setReviewing(true)}><FileVideo /> Review a video</button>
          <button className="btn" onClick={() => setAdding("url")}><Link2 /> Add by URL</button>
          <button className="btn primary" onClick={() => setAdding("phone")}><Smartphone /> Pair a phone</button>
        </>}
      />
      {loading && !data && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && !data.cameras.length && (
        <Empty title="No cameras yet" action={<button className="btn sm primary" onClick={() => setAdding("phone")}><Plus /> Pair a phone</button>}>
          Pair a phone: scan a QR code, mount the phone over the bench, done. Or add an IP camera's snapshot, MJPEG or RTSP URL.
        </Empty>
      )}
      {data?.cameras.map((c) => {
        const live = c.kind === "url" ? true : !!c.live;
        return (
          <article key={c.id} className="cam-card">
            <div className="cam-tile">
              {live ? <img src={frameUrl(c.id, tick)} alt={`Latest frame from ${c.name}`} /> : <div className="off">OFFLINE</div>}
            </div>
            <div className="cam-card-body">
              <div className="row between wrap">
                <div className="stack sm" style={{ minWidth: 0 }}>
                  <div className="row"><span style={{ fontWeight: 500 }}>{c.name}</span><span className="tag">{c.kind === "phone" ? "phone" : "url"}</span></div>
                  <div className="row faint" style={{ fontSize: 13 }}>
                    <span className={`dot ${live ? "live" : ""}`} />
                    {c.kind === "url" ? "Read on demand" : c.live ? `Live · last frame ${c.last_frame_age ?? 0}s ago` : c.last_frame_age !== null ? `Offline · last seen ${ago(Date.now() / 1000 - (c.last_frame_age || 0))}` : "Waiting for the phone"}
                  </div>
                </div>
                <div className="row">
                  <Link className="btn sm" to={`/live?camera=${c.id}`}><ScanLine /> <span className="desktop-only">Live</span></Link>
                  <button className={`btn sm${openWatch === c.id ? " primary" : ""}`} onClick={() => setOpenWatch(openWatch === c.id ? null : c.id)} aria-expanded={openWatch === c.id}><Eye /> <span className="desktop-only">Watch</span></button>
                  {c.kind === "phone" && <button className="btn sm icon" aria-label="Pair again" title="Pair again (new link)" onClick={() => repair(c)}><RefreshCw /></button>}
                  <button className="btn sm icon danger" aria-label={`Remove ${c.name}`} onClick={() => remove(c)}><Trash2 /></button>
                </div>
              </div>
              {openWatch === c.id && <WatchPanel cam={c} />}
            </div>
          </article>
        );
      })}

      {adding && (
        <Modal title={adding === "phone" ? "Pair a phone" : "Add a camera by URL"} onClose={() => setAdding(null)}>
          <form className="stack" onSubmit={add}>
            <Field label="Name">
              <input className="input" autoFocus required value={name} onChange={(e) => setName(e.target.value)} placeholder={adding === "phone" ? "Bench 2 phone" : "Line 3 overhead"} />
            </Field>
            {adding === "url" && (
              <Field label="Camera URL" hint="A JPEG snapshot URL, an MJPEG stream, or rtsp://.">
                <input className="input mono" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://192.168.1.40/snapshot.jpg" />
              </Field>
            )}
            {formError && <div className="error-box">{formError}</div>}
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button type="button" className="btn" onClick={() => setAdding(null)}>Cancel</button>
              <button className="btn primary" disabled={busy}>{busy ? <Spinner /> : adding === "phone" ? <><Smartphone /> Get pairing link</> : <><Video /> Add camera</>}</button>
            </div>
          </form>
        </Modal>
      )}
      {pair && <Pairing name={pair.name} url={pair.url} onClose={() => setPair(null)} />}
      {reviewing && <VideoReview onClose={() => setReviewing(false)} />}
    </div>
  );
}
