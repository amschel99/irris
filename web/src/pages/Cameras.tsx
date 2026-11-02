import QRCode from "qrcode";
import { Link2, Plus, RefreshCw, ScanLine, Smartphone, Trash2, Video } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { CopyButton, Empty, ErrorBox, Field, Head, Loading, Modal, Spinner } from "../components/ui";
import { del, frameUrl, get, post } from "../lib/api";
import { useSession } from "../lib/session";
import type { Camera } from "../lib/types";
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
          <p className="muted" style={{ margin: 0 }}>No sign-in on the phone. Mount it facing the work and keep the page open.</p>
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

export function Cameras() {
  const { workspace, toast } = useSession();
  const { data, error, loading, reload } = useLoad<{ cameras: Camera[] }>(() => get("/cameras"), [workspace?.id]);
  const [adding, setAdding] = useState<"phone" | "url" | null>(null);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const [pair, setPair] = useState<{ name: string; url: string } | null>(null);
  const [tick, setTick] = useState(0);
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
    if (!confirm(`Remove ${c.name}?`)) return;
    await del(`/cameras/${c.id}`).catch((e) => toast(errText(e), true));
    reload();
  };

  return (
    <div className="page">
      <Head
        title="Cameras"
        sub="Any phone becomes a camera. Factory cameras connect by URL. Irris sees one frame at a time, only when it's checking or answering."
        actions={<>
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
      {!!data?.cameras.length && (
        <ul className="list">
          {data.cameras.map((c) => {
            const live = c.kind === "url" ? true : !!c.live;
            return (
              <li key={c.id} style={{ alignItems: "center" }}>
                <div className="cam-tile" style={{ width: 132, flex: "none" }}>
                  {live ? <img src={frameUrl(c.id, tick)} alt="" /> : <div className="off">OFFLINE</div>}
                </div>
                <div className="grow stack sm">
                  <div className="row"><span style={{ fontWeight: 500 }}>{c.name}</span><span className="tag">{c.kind === "phone" ? "phone" : "url"}</span></div>
                  <div className="row faint" style={{ fontSize: 13 }}>
                    <span className={`dot ${live ? "live" : ""}`} />
                    {c.kind === "url" ? "Read on demand" : c.live ? `Live · last frame ${c.last_frame_age ?? 0}s ago` : c.last_frame_age !== null ? `Offline · last seen ${ago(Date.now() / 1000 - (c.last_frame_age || 0))}` : "Waiting for the phone"}
                  </div>
                </div>
                <div className="row">
                  <Link className="btn sm" to={`/live?camera=${c.id}`}><ScanLine /> <span className="desktop-only">Live</span></Link>
                  {c.kind === "phone" && <button className="btn sm icon" aria-label="Pair again" title="Pair again (new link)" onClick={() => repair(c)}><RefreshCw /></button>}
                  <button className="btn sm icon danger" aria-label={`Remove ${c.name}`} onClick={() => remove(c)}><Trash2 /></button>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {adding && (
        <Modal title={adding === "phone" ? "Pair a phone" : "Add a camera by URL"} onClose={() => setAdding(null)}>
          <form className="stack" onSubmit={add}>
            <Field label="Name">
              <input className="input" autoFocus required value={name} onChange={(e) => setName(e.target.value)} placeholder={adding === "phone" ? "Bench 2 phone" : "Line 3 overhead"} />
            </Field>
            {adding === "url" && (
              <Field label="Camera URL" hint="A JPEG snapshot URL, an MJPEG stream, or rtsp:// (RTSP needs OpenCV on the server).">
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
    </div>
  );
}
