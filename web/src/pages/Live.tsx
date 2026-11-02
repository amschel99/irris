import { Camera as CamIcon, Check, ChevronLeft, CircleStop, Play, ScanLine, SkipForward, Volume2, VolumeX, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Answer } from "../components/Answer";
import { Empty, ErrorBox, Field, Loading, ResultTag, Spinner, Toggle } from "../components/ui";
import { api, frameUrl, get, post } from "../lib/api";
import { grabJpeg, openCamera, speak, stopCamera } from "../lib/camera";
import { useSession } from "../lib/session";
import type { Camera, Measured, Procedure, Run } from "../lib/types";
import { deviceName, duration, errText, num, useInterval, useLoad } from "../lib/util";
import { Composer, Thinking, useAsk } from "./Ask";

const DEVICE = "device";
const AUTO_MS = 8000;

interface Verdict {
  result: string;
  say: string;
  reading: Measured | null;
  at: number;
}

function specText(r?: { min?: number; max?: number; unit?: string; label?: string }) {
  if (!r) return "";
  const u = r.unit ? ` ${r.unit}` : "";
  if (r.min !== undefined && r.max !== undefined) return `${r.min}–${r.max}${u}`;
  if (r.min !== undefined) return `≥ ${r.min}${u}`;
  if (r.max !== undefined) return `≤ ${r.max}${u}`;
  return "";
}

export function Live() {
  const { workspace, toast } = useSession();
  const [params, setParams] = useSearchParams();
  const cams = useLoad<{ cameras: Camera[] }>(() => get("/cameras"), [workspace?.id]);
  const procs = useLoad<{ procedures: Procedure[] }>(() => get("/procedures"), [workspace?.id]);

  const [source, setSource] = useState<string>(params.get("camera") || "");
  const [deviceCam, setDeviceCam] = useState<string>("");
  const [camError, setCamError] = useState("");
  const [streaming, setStreaming] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [tick, setTick] = useState(0);
  const [imgOk, setImgOk] = useState(false);

  const [run, setRun] = useState<Run | null>(null);
  const [runLoading, setRunLoading] = useState(!!params.get("run"));
  const [procId, setProcId] = useState("");
  const [label, setLabel] = useState("");
  const [checking, setChecking] = useState(false);
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [auto, setAuto] = useState(false);
  const [voice, setVoice] = useState(true);
  const [tab, setTab] = useState<"steps" | "ask">("steps");
  const [value, setValue] = useState("");
  const [unit, setUnit] = useState("");
  const [note, setNote] = useState("");
  const chat = useAsk();
  const [conv, setConv] = useState<string | null>(null);

  const cameraId = source === DEVICE ? deviceCam : source;
  const cameraList = cams.data?.cameras || [];
  const remote = cameraList.find((c) => c.id === source);
  const live = source === DEVICE ? streaming : !!remote && (remote.kind === "url" || imgOk || !!remote.live);

  // default source: a live remote camera, else this device
  useEffect(() => {
    if (source || !cams.data) return;
    const liveCam = cams.data.cameras.find((c) => c.live || c.kind === "url");
    setSource(liveCam ? liveCam.id : DEVICE);
  }, [cams.data, source]);

  // this device's camera
  const startDevice = useCallback(async () => {
    setCamError("");
    try {
      const stream = await openCamera("environment");
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        await videoRef.current.play().catch(() => {});
      }
      let id = deviceCam;
      if (!id) {
        const name = deviceName();
        const existing = (await get<{ cameras: Camera[] }>("/cameras")).cameras.find((c) => c.kind === "phone" && c.name === name);
        id = existing ? existing.id : (await post("/cameras", { kind: "phone", name })).id;
        setDeviceCam(id);
        cams.reload();
      }
      setStreaming(true);
    } catch (e) {
      setCamError(errText(e));
      setStreaming(false);
    }
  }, [deviceCam, cams]);

  useEffect(() => {
    if (source !== DEVICE) {
      stopCamera(streamRef.current);
      streamRef.current = null;
      setStreaming(false);
    }
  }, [source]);
  useEffect(() => () => stopCamera(streamRef.current), []);

  // upload this device's frames ~1 fps
  useInterval(async () => {
    if (source !== DEVICE || !streaming || !deviceCam || !videoRef.current) return;
    const blob = await grabJpeg(videoRef.current);
    if (!blob) return;
    api(`/cameras/${deviceCam}/frame`, { method: "POST", body: blob, headers: { "Content-Type": "image/jpeg" } }).catch(() => {});
  }, source === DEVICE && streaming ? 1000 : null);

  // poll a remote camera
  useInterval(() => setTick((t) => t + 1), source && source !== DEVICE ? 800 : null);

  // resume a run from ?run=
  useEffect(() => {
    const id = params.get("run");
    if (!id) return;
    setRunLoading(true);
    get<Run>(`/runs/${id}`)
      .then((r) => {
        setRun(r);
        if (r.camera_id && !params.get("camera")) setSource(r.camera_id);
      })
      .catch((e) => toast(errText(e), true))
      .finally(() => setRunLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const current = run && run.status === "active" ? run.steps[run.step] : null;
  useEffect(() => {
    setUnit(current?.reading?.unit || "");
    setValue("");
    setNote("");
  }, [run?.id, run?.step, current?.reading?.unit]);

  const announce = useCallback((v: Verdict) => {
    setVerdict(v);
    if (voice && v.say) speak(v.say);
  }, [voice]);

  const start = async () => {
    if (!procId) return;
    try {
      const r = await post<Run>("/runs", { procedure_id: procId, camera_id: cameraId || "", label });
      setRun(r);
      setVerdict(null);
      setParams((p) => { p.set("run", r.id); return p; }, { replace: true });
      if (voice) speak(`Step 1. ${r.steps[0].instruction}`);
    } catch (e) {
      toast(errText(e), true);
    }
  };

  const check = useCallback(async () => {
    if (!run || run.status !== "active" || checking) return;
    setChecking(true);
    try {
      const out = await post<{ check: Verdict; run: Run }>(`/runs/${run.id}/check`, { camera_id: cameraId || undefined });
      announce({ ...out.check, at: Date.now() });
      const advanced = out.run.step !== run.step || out.run.status !== run.status;
      setRun(out.run);
      if (advanced && out.run.status === "active" && voice) {
        window.setTimeout(() => speak(`Next. ${out.run.steps[out.run.step].instruction}`), 1800);
      }
    } catch (e) {
      const msg = errText(e);
      setAuto(false);
      announce({ result: "unsure", say: msg, reading: null, at: Date.now() });
    } finally {
      setChecking(false);
    }
  }, [run, checking, cameraId, announce, voice]);

  useInterval(check, auto && live && run?.status === "active" ? AUTO_MS : null);

  const mark = async (result: "pass" | "fail" | "skip") => {
    if (!run) return;
    const reading = value.trim() ? { value: Number(value), unit } : undefined;
    if (reading && Number.isNaN(reading.value)) return toast("The reading must be a number", true);
    try {
      const out = await post<{ check: Verdict; run: Run }>(`/runs/${run.id}/check`, { result, reading, note: note || undefined });
      setRun(out.run);
      const say = out.check.result === "fail" && result === "pass" ? `Out of spec: ${out.check.say}` : out.check.say;
      announce({ ...out.check, say: say || (result === "pass" ? "Step passed." : result === "skip" ? "Step skipped." : "Marked as failed."), at: Date.now() });
    } catch (e) {
      toast(errText(e), true);
    }
  };

  const move = async (action: "back" | "stop") => {
    if (!run) return;
    if (action === "stop" && !confirm("Stop this run? Irris writes the batch record with the steps done so far.")) return;
    try {
      setRun(await post<Run>(`/runs/${run.id}/${action}`));
      setVerdict(null);
    } catch (e) {
      toast(errText(e), true);
    }
  };

  const reset = () => {
    setRun(null);
    setVerdict(null);
    setAuto(false);
    setParams((p) => { p.delete("run"); return p; }, { replace: true });
  };

  const sendChat = async (text: string) => {
    const res = await chat.send(text, { camera_id: cameraId || undefined, run_id: run?.id }, conv);
    if (res?.conversation_id) setConv(res.conversation_id);
  };

  const passed = useMemo(() => run?.steps.filter((s) => s.result === "pass").length || 0, [run]);
  const deviations = useMemo(() => run?.log.filter((e) => e.result === "fail" || e.result === "danger").length || 0, [run]);
  const vlmReady = !!(workspace?.defaults.vlm || workspace?.models.vlm?.base_url);
  const verdictTone = verdict ? (verdict.result === "pass" ? "" : verdict.result) : "";

  return (
    <div className="page full">
      <div className="live">
        <section className="stage" aria-label="Camera">
          <div className="stage-top">
            <select className="select input" style={{ maxWidth: 240, height: 32 }} value={source} onChange={(e) => { setImgOk(false); setSource(e.target.value); }} aria-label="Camera">
              <option value={DEVICE}>This device</option>
              {cameraList.filter((c) => c.id !== deviceCam).map((c) => (
                <option key={c.id} value={c.id}>{c.name}{c.kind === "phone" ? (c.live ? " · live" : " · offline") : ""}</option>
              ))}
            </select>
            <div className="grow" />
            {run && (
              <span className="mono desktop-only" style={{ fontSize: 11, color: "#A3A3A8" }}>
                {run.procedure.name.toUpperCase()}{run.label ? ` · ${run.label}` : ""} · {duration(run.started, run.ended)}
              </span>
            )}
            <button className="btn sm icon" onClick={() => setVoice((v) => !v)} aria-label={voice ? "Mute spoken verdicts" : "Speak verdicts"}>
              {voice ? <Volume2 /> : <VolumeX />}
            </button>
          </div>

          <div className="stage-view">
            {source === DEVICE ? (
              <>
                <video ref={videoRef} playsInline muted autoPlay style={{ display: streaming ? "block" : "none" }} />
                {!streaming && (
                  <div className="stage-off">
                    <CamIcon />
                    <h2>Use this device's camera</h2>
                    <p>Point it at the work. Irris sees one frame a second and checks each step against your procedure.</p>
                    {camError && <div className="error-box" style={{ textAlign: "left" }}>{camError}</div>}
                    <div className="row wrap" style={{ justifyContent: "center" }}>
                      <button className="btn primary" onClick={startDevice}><Play /> Start camera</button>
                      <Link className="btn" style={{ color: "var(--paper)", borderColor: "rgba(245,245,243,.2)" }} to="/cameras">Pair a phone instead</Link>
                    </div>
                  </div>
                )}
              </>
            ) : remote ? (
              <>
                <img
                  key={remote.id}
                  src={frameUrl(remote.id, tick)}
                  alt={`Live view from ${remote.name}`}
                  onLoad={() => setImgOk(true)}
                  onError={() => setImgOk(false)}
                  style={{ display: imgOk ? "block" : "none" }}
                />
                {!imgOk && (
                  <div className="stage-off">
                    <CamIcon />
                    <h2>{remote.name} isn't sending video</h2>
                    <p>{remote.kind === "phone" ? "Open its pairing link on the phone and keep the page open." : "Irris can't reach this camera's URL right now."}</p>
                    <Link className="btn" style={{ color: "var(--paper)", borderColor: "rgba(245,245,243,.2)" }} to="/cameras">Cameras</Link>
                  </div>
                )}
              </>
            ) : (
              cams.loading && <Loading />
            )}
            {live && (
              <div className={`reticle${checking ? " checking" : ""}${verdict && ["fail", "danger"].includes(verdict.result) ? " fail" : ""}`} aria-hidden="true">
                <i /><i /><i /><i />
              </div>
            )}
          </div>

          {live && <div className="rec"><span className="dot live" /> LIVE{auto ? " · AUTO-CHECK" : ""}</div>}

          <div className="hud">
            {current && (
              <span className="hud-step">
                STEP {run!.step + 1}/{run!.steps.length} · {checking ? "CHECKING…" : current.reading ? `READ ${specText(current.reading)}` : "WATCHING"}
              </span>
            )}
            {verdict && (
              <div className={`verdict ${verdictTone}`} role="status" key={verdict.at}>
                <div className="k">
                  <span>{verdict.result === "pass" ? "OK" : verdict.result.toUpperCase()}</span>
                  {verdict.reading && <span>{num(verdict.reading.value)} {verdict.reading.unit} · CONF {verdict.reading.confidence.toFixed(2)}</span>}
                </div>
                {verdict.say && <p>{verdict.say}</p>}
              </div>
            )}
          </div>
        </section>

        <aside className="rail">
          <div className="rail-tabs" role="tablist">
            <button role="tab" aria-selected={tab === "steps"} onClick={() => setTab("steps")}>{run ? "Procedure" : "Start"}</button>
            <button role="tab" aria-selected={tab === "ask"} onClick={() => setTab("ask")}>Ask Irris</button>
          </div>

          {tab === "steps" && (
            <>
              <div className="rail-body">
                {runLoading && <Loading />}
                {!run && !runLoading && (
                  <div className="stack lg">
                    <div>
                      <h2 style={{ fontSize: 17, fontWeight: 500 }}>Guided run</h2>
                      <p className="muted" style={{ margin: "4px 0 0" }}>Pick a procedure. Irris walks you through it and checks each step through the camera.</p>
                    </div>
                    {procs.loading && <Loading />}
                    <ErrorBox error={procs.error} retry={procs.reload} />
                    {procs.data && !procs.data.procedures.length && (
                      <Empty title="No procedures yet" action={<Link className="btn sm" to="/train?tab=procedures">Write a procedure</Link>}>
                        Add your SOPs in Train. Each step says what correct looks like, so Irris can check it.
                      </Empty>
                    )}
                    {!!procs.data?.procedures.length && (
                      <div className="stack">
                        <Field label="Procedure">
                          <select className="select input" value={procId} onChange={(e) => setProcId(e.target.value)}>
                            <option value="">Choose a procedure…</option>
                            {procs.data.procedures.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.steps.length} steps</option>)}
                          </select>
                        </Field>
                        <Field label="Batch or job" hint="Goes on the batch record. Optional.">
                          <input className="input mono" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="B-1042" />
                        </Field>
                        <button className="btn primary lg" disabled={!procId} onClick={start}><Play /> Start run</button>
                        {!vlmReady && (
                          <div className="note">No vision model is set up, so you'll mark steps by hand. Add one in <Link to="/settings?tab=models">Settings → Models</Link> to have Irris check them through the camera.</div>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {run && (
                  <div className="stack lg">
                    {run.status !== "active" ? (
                      <div className="stack">
                        <div className="eyebrow">{run.status === "done" ? "Run complete" : "Run stopped"}</div>
                        <h2 style={{ fontSize: 18, fontWeight: 500 }}>{run.procedure.name}</h2>
                        <div className="stats" style={{ gridTemplateColumns: "1fr 1fr" }}>
                          <div className="stat"><span className="num">{passed}/{run.steps.length}</span><span className="lbl">Steps passed</span></div>
                          <div className="stat"><span className={`num${deviations ? " red" : ""}`}>{deviations}</span><span className="lbl">Deviations</span></div>
                        </div>
                        <p className="muted" style={{ margin: 0 }}>Irris wrote the batch record: every step, check, reading and time.</p>
                        <div className="row wrap">
                          <Link className="btn primary" to={`/runs/${run.id}`}>View batch record</Link>
                          <button className="btn" onClick={reset}>New run</button>
                        </div>
                      </div>
                    ) : (
                      current && (
                        <div className="cur-card">
                          <div className="row between">
                            <span className="eyebrow">Step {run.step + 1} of {run.steps.length}</span>
                            {current.checks > 0 && <ResultTag result={current.result} />}
                          </div>
                          <div className="ins">{current.instruction}</div>
                          {(current.check || current.reading || current.caution) && (
                            <dl className="spec">
                              {current.check && <><dt>Correct</dt><dd>{current.check}</dd></>}
                              {current.reading && <><dt>Read</dt><dd className="mono">{current.reading.label ? `${current.reading.label} ` : ""}{specText(current.reading)}</dd></>}
                              {current.caution && <><dt>Caution</dt><dd style={{ color: "var(--amber)" }}>{current.caution}</dd></>}
                            </dl>
                          )}
                        </div>
                      )
                    )}

                    <ol className="steplist" aria-label="Steps">
                      {run.steps.map((s) => {
                        const cls = run.status === "active" && s.index === run.step ? "cur" : s.result || "";
                        return (
                          <li key={s.index} className={cls}>
                            <span className="n">{s.result === "pass" ? <Check size={12} /> : s.result === "fail" || s.result === "danger" ? <X size={12} /> : s.index + 1}</span>
                            <div>
                              <div className="ins">{s.instruction}</div>
                              {(s.say || s.measured) && (
                                <div className="meta">
                                  {s.measured && <span className="mono">{num(s.measured.value)} {s.measured.unit} · </span>}
                                  {s.say}
                                </div>
                              )}
                            </div>
                          </li>
                        );
                      })}
                    </ol>
                  </div>
                )}
              </div>

              {run && run.status === "active" && current && (
                <div className="rail-foot">
                  <div className="row">
                    <button className="btn primary grow" onClick={check} disabled={checking || !live || !vlmReady} title={!vlmReady ? "Set up a vision model in Settings" : !live ? "Start a camera first" : ""}>
                      {checking ? <Spinner /> : <ScanLine />} Check with camera
                    </button>
                    <label className="row nowrap" style={{ gap: 8, fontSize: 13 }}>
                      <Toggle on={auto} onChange={setAuto} label="Auto-check every 8 seconds" /> Auto
                    </label>
                  </div>
                  <div className="row">
                    <input className="input mono" style={{ height: 32 }} inputMode="decimal" placeholder={current.reading ? `Reading ${specText(current.reading)}` : "Reading (optional)"} value={value} onChange={(e) => setValue(e.target.value)} aria-label="Reading" />
                    <input className="input mono" style={{ height: 32, width: 64 }} placeholder="unit" value={unit} onChange={(e) => setUnit(e.target.value)} aria-label="Unit" />
                  </div>
                  <input className="input" style={{ height: 32 }} placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Note" />
                  <div className="row">
                    <button className="btn sm pass grow" onClick={() => mark("pass")}><Check /> Pass</button>
                    <button className="btn sm fail grow" onClick={() => mark("fail")}><X /> Fail</button>
                    <button className="btn sm icon" onClick={() => mark("skip")} aria-label="Skip step" title="Skip"><SkipForward /></button>
                    <button className="btn sm icon" onClick={() => move("back")} aria-label="Previous step" title="Back" disabled={run.step === 0}><ChevronLeft /></button>
                    <button className="btn sm icon danger" onClick={() => move("stop")} aria-label="Stop run" title="Stop"><CircleStop /></button>
                  </div>
                </div>
              )}
            </>
          )}

          {tab === "ask" && (
            <>
              <div className="rail-body">
                <div className="mini-chat">
                  {!chat.messages.length && !chat.pending && (
                    <p className="muted" style={{ margin: 0 }}>
                      Ask about what's in front of you: "is this the right torque?", "what does the manual say about this part?". Irris sees the camera{run ? " and knows which step you're on" : ""}.
                    </p>
                  )}
                  {chat.messages.map((m) =>
                    m.role === "user" ? <div key={m.id} className="msg-user">{m.content}</div> : <Answer key={m.id} text={m.content} sources={m.meta.sources} actions={m.meta.actions} />,
                  )}
                  {chat.pending && <Thinking steps={chat.pending.steps} />}
                  <ErrorBox error={chat.error} />
                </div>
              </div>
              <div className="rail-foot">
                <Composer onSend={sendChat} busy={!!chat.pending} onStop={chat.stop} placeholder="Ask about what the camera sees" />
              </div>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
