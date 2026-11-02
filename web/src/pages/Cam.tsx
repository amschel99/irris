import { Camera, Pause, Play, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Mark, Spinner } from "../components/ui";
import { grabJpeg, keepAwake, openCamera, stopCamera } from "../lib/camera";
import { errText, useInterval } from "../lib/util";

/** The paired phone's page: no sign-in, the pairing token is the credential. */
export function Cam() {
  const [params] = useSearchParams();
  const t = params.get("t") || "";
  const [info, setInfo] = useState<{ name: string; workspace: string } | null>(null);
  const [error, setError] = useState("");
  const [state, setState] = useState<"idle" | "starting" | "live" | "paused">("idle");
  const [sent, setSent] = useState(0);
  const [lastOk, setLastOk] = useState(0);
  const [facing, setFacing] = useState<"environment" | "user">("environment");
  const video = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);
  const release = useRef<() => void>(() => {});

  useEffect(() => {
    if (!t) {
      setError("This page needs a pairing link. Open Cameras in Irris on a computer and pair this phone.");
      return;
    }
    fetch(`/api/v1/cam/info?t=${encodeURIComponent(t)}`)
      .then(async (r) => {
        const b = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(b.detail || "This pairing link isn't valid.");
        setInfo(b);
      })
      .catch((e) => setError(errText(e)));
    return () => {
      stopCamera(stream.current);
      release.current();
    };
  }, [t]);

  const start = async (face = facing) => {
    setError("");
    setState("starting");
    try {
      stopCamera(stream.current);
      stream.current = await openCamera(face);
      if (video.current) {
        video.current.srcObject = stream.current;
        await video.current.play().catch(() => {});
      }
      release.current();
      release.current = await keepAwake();
      setState("live");
    } catch (e) {
      setError(errText(e));
      setState("idle");
    }
  };

  const pause = () => {
    stopCamera(stream.current);
    stream.current = null;
    release.current();
    setState("paused");
  };

  useInterval(async () => {
    if (state !== "live" || !video.current) return;
    const blob = await grabJpeg(video.current, 960, 0.75);
    if (!blob) return;
    try {
      const r = await fetch(`/api/v1/cam/frame?t=${encodeURIComponent(t)}`, { method: "POST", body: blob, headers: { "Content-Type": "image/jpeg" } });
      if (r.status === 401) {
        pause();
        setError("This phone was unpaired. Pair it again from Cameras in Irris.");
        return;
      }
      if (r.ok) {
        setSent((n) => n + 1);
        setLastOk(Date.now());
      }
    } catch {
      /* offline for a moment: keep trying */
    }
  }, state === "live" ? 1000 : null);

  const healthy = state === "live" && Date.now() - lastOk < 4000;

  return (
    <div className="camera-page">
      <video ref={video} playsInline muted autoPlay style={{ opacity: state === "live" ? 1 : 0 }} />
      <div className="top">
        <Mark size={18} />
        <span style={{ fontWeight: 600, letterSpacing: "0.3em", fontSize: 12 }}>IRRIS</span>
        <div className="grow" />
        {state === "live" && (
          <span className="mono row" style={{ fontSize: 11, gap: 6 }}>
            <span className={`dot ${healthy ? "live" : ""}`} /> {healthy ? "STREAMING" : "CONNECTING"} · {sent}
          </span>
        )}
      </div>
      <div className="bottom">
        {info && <div className="mono" style={{ fontSize: 11, color: "#A3A3A8", letterSpacing: "0.06em" }}>{info.workspace.toUpperCase()} · {info.name.toUpperCase()}</div>}
        {error ? (
          <div className="big" style={{ color: "#FF6A5C", maxWidth: 360 }}>{error}</div>
        ) : state === "live" ? (
          <div className="big">{healthy ? "Irris can see. Keep this page open." : "Connecting…"}</div>
        ) : state === "starting" ? (
          <div className="row"><Spinner /> Opening the camera…</div>
        ) : info ? (
          <div className="stack" style={{ alignItems: "center" }}>
            <div className="big">Point this phone at the work.</div>
            <p style={{ margin: 0, color: "#A3A3A8", maxWidth: 320 }}>It sends one frame a second to {info.workspace}. Your team watches and runs procedures from their screens.</p>
          </div>
        ) : !error ? (
          <div className="row"><Spinner /> Checking the link…</div>
        ) : null}
        {info && (
          <div className="row" style={{ marginTop: 6 }}>
            {state === "live" ? (
              <>
                <button className="btn lg" style={{ color: "#F5F5F3", borderColor: "rgba(245,245,243,.3)" }} onClick={pause}><Pause /> Pause</button>
                <button className="btn lg icon" style={{ color: "#F5F5F3", borderColor: "rgba(245,245,243,.3)" }} aria-label="Switch camera" onClick={() => { const f = facing === "environment" ? "user" : "environment"; setFacing(f); start(f); }}><RefreshCw /></button>
              </>
            ) : (
              <button className="btn lg" style={{ background: "#D7FF3D", color: "#0A0A0B", borderColor: "#D7FF3D" }} onClick={() => start()} disabled={state === "starting"}>
                {state === "paused" ? <Play /> : <Camera />} {state === "paused" ? "Resume" : "Start camera"}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
