import { ArrowRight, Check, CornerDownLeft, ScanLine, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { RecordDrawer } from "../components/RecordDrawer";
import { Empty, ErrorBox, Loading, Sparkline, StatusTag, TypeTag } from "../components/ui";
import { frameUrl, get } from "../lib/api";
import { useSession } from "../lib/session";
import type { Camera, RecordSummary, RunSummary } from "../lib/types";
import { ago, clock, num, useInterval, useLoad } from "../lib/util";

interface OverviewData {
  stats: { records: number; last_24h: number; by_type: { type: string; n: number }[] };
  recent: RecordSummary[];
  runs: RunSummary[];
  cameras: Camera[];
  procedures: number;
  sources: number;
  readings: { time: number; subject: string; title: string; value: number | null; unit: string | null }[];
  week: { runs: number; done: number; deviations: number; checks: number };
}

const SUGGEST = [
  "What changed on the line in the last 24 hours?",
  "Which batches had deviations this week?",
  "Summarise our mould change procedure",
  "What does the manual say about hold pressure?",
];

function CamTile({ cam, tick }: { cam: Camera; tick: number }) {
  const [broken, setBroken] = useState(false);
  const live = cam.kind === "url" || cam.live;
  return (
    <Link className="cam-tile" to={`/live?camera=${cam.id}`} aria-label={`Open ${cam.name} in Live`}>
      {live && !broken ? (
        <img src={frameUrl(cam.id, tick)} alt="" onError={() => setBroken(true)} onLoad={() => setBroken(false)} />
      ) : (
        <div className="off">{cam.kind === "phone" ? "PHONE OFFLINE" : "NO SIGNAL"}</div>
      )}
      <div className="cam-label">
        <span className={`dot ${live && !broken ? "live" : ""}`} /> {cam.name.toUpperCase()}
      </div>
    </Link>
  );
}

export function Overview() {
  const { workspace, me } = useSession();
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const { data, error, loading, reload } = useLoad<OverviewData>(() => get("/overview"), [workspace?.id]);
  useInterval(() => setTick((t) => t + 1), data?.cameras.length ? 2500 : null);
  useInterval(reload, 30000);

  const series = useMemo(() => {
    const by = new Map<string, { label: string; unit: string; values: number[]; last: number }>();
    for (const r of [...(data?.readings || [])].reverse()) {
      if (typeof r.value !== "number") continue;
      const key = r.subject || r.title;
      const s = by.get(key) || { label: key, unit: r.unit || "", values: [], last: r.time };
      s.values.push(r.value);
      s.last = r.time;
      by.set(key, s);
    }
    return [...by.values()].slice(0, 6);
  }, [data]);

  const counts = useMemo(() => Object.fromEntries((data?.stats.by_type || []).map((t) => [t.type, t.n])), [data]);
  const dataCount = (data?.stats.records || 0) - (counts.procedure || 0) - (counts.lesson || 0) - (counts.batch_record || 0) - (counts.reading || 0);
  const steps = [
    { done: !!workspace?.about?.trim(), title: "Describe your team", text: "What you make, your lines and machines, how you work.", to: "/train" },
    { done: dataCount > 0 || (data?.sources || 0) > 0, title: "Connect your data", text: "Upload manuals and logs, or connect Postgres or a webhook.", to: "/data" },
    { done: (data?.cameras.length || 0) > 0 || (data?.procedures || 0) > 0, title: "Add a camera or a procedure", text: "Pair a phone as a camera, or write your first SOP.", to: (data?.cameras.length || 0) > 0 ? "/train" : "/cameras" },
  ];
  const onboarding = data && steps.some((s) => !s.done);
  const first = me?.user.name?.split(" ")[0];

  const ask = (text: string) => text.trim() && nav(`/ask?q=${encodeURIComponent(text.trim())}`);

  return (
    <div className="page">
      <header className="head">
        <div>
          <div className="eyebrow" style={{ marginBottom: 8 }}>{workspace?.name}{workspace?.industry ? ` · ${workspace.industry}` : ""}</div>
          <h1>{first ? `Good to see you, ${first}.` : "Overview"}</h1>
        </div>
        <Link className="btn primary" to="/live"><ScanLine /> Start live guidance</Link>
      </header>

      <div className="stack lg">
        <form onSubmit={(e) => { e.preventDefault(); ask(q); }} className="stack">
          <label className="ask-bar">
            <Sparkles />
            <span className="sr">Ask Irris</span>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Irris anything about your plant, data or procedures" />
            <button className="btn primary icon" aria-label="Ask" disabled={!q.trim()}><CornerDownLeft /></button>
          </label>
          <div className="chips">
            {SUGGEST.map((s) => <button type="button" key={s} className="chip" onClick={() => ask(s)}>{s}</button>)}
          </div>
        </form>

        {loading && !data && <Loading />}
        <ErrorBox error={error} retry={reload} />

        {onboarding && (
          <section className="stack">
            <div className="row between"><h2>Set up Irris</h2><span className="faint mono" style={{ fontSize: 12 }}>{steps.filter((s) => s.done).length}/3</span></div>
            <div className="onboard">
              {steps.map((s, i) => (
                <Link key={s.title} to={s.to} className={`onboard-step${s.done ? " done" : ""}`}>
                  <span className="n">{s.done ? <Check size={13} /> : `0${i + 1}`} {s.done ? "DONE" : "TO DO"}</span>
                  <h3>{s.title}</h3>
                  <p>{s.text}</p>
                </Link>
              ))}
            </div>
          </section>
        )}

        {data && (
          <>
            <section className="stack">
              <h2>This week</h2>
              <div className="stats">
                <div className="stat"><span className="num">{data.week.runs}</span><span className="lbl">Guided runs</span></div>
                <div className="stat"><span className="num">{data.week.checks}</span><span className="lbl">Steps checked</span></div>
                <div className="stat"><span className={`num${data.week.deviations ? " red" : ""}`}>{data.week.deviations}</span><span className="lbl">Deviations caught</span></div>
                <div className="stat"><span className="num">{num(data.stats.records)}</span><span className="lbl">Records Irris knows · {data.stats.last_24h} today</span></div>
              </div>
            </section>

            <section className="stack">
              <div className="row between"><h2>Cameras</h2><Link className="btn sm ghost" to="/cameras">Manage <ArrowRight /></Link></div>
              {data.cameras.length ? (
                <div className="cams">{data.cameras.map((c) => <CamTile key={c.id} cam={c} tick={tick} />)}</div>
              ) : (
                <Empty title="No cameras yet" action={<Link className="btn sm" to="/cameras">Add a camera</Link>}>
                  Pair any phone as a camera in under a minute, or add a factory camera by its URL.
                </Empty>
              )}
            </section>

            <div className="grid2" style={{ gap: 32 }}>
              <section className="stack">
                <div className="row between"><h2>Recent runs</h2><Link className="btn sm ghost" to="/runs">All runs <ArrowRight /></Link></div>
                {data.runs.length ? (
                  <ul className="list">
                    {data.runs.map((r) => (
                      <li key={r.id} className="clickable" onClick={() => nav(r.status === "active" ? `/live?run=${r.id}` : `/runs/${r.id}`)}>
                        <div className="grow">
                          <div style={{ fontWeight: 500 }}>{r.procedure.name}</div>
                          <div className="faint mono" style={{ fontSize: 12 }}>{r.label || r.id.slice(0, 8)} · {ago(r.started)}</div>
                        </div>
                        <StatusTag status={r.status} deviations={r.deviations} />
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Empty title="No runs yet">Pick a procedure in Live and Irris will check each step through the camera.</Empty>
                )}
              </section>

              <section className="stack">
                <h2>Readings</h2>
                {series.length ? (
                  <div>
                    {series.map((s) => (
                      <div className="spark-row" key={s.label}>
                        <div className="grow"><div className="mono" style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.label}</div><div className="faint" style={{ fontSize: 12 }}>{ago(s.last)}</div></div>
                        <Sparkline values={s.values.slice(-30)} />
                        <div className="v">{num(s.values[s.values.length - 1])}<span className="faint"> {s.unit}</span></div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <Empty title="No readings yet">Readings appear here when Irris reads an instrument during a run, or when you send <span className="mono">reading</span> records.</Empty>
                )}
              </section>
            </div>

            <section className="stack">
              <div className="row between"><h2>Latest in your data</h2><Link className="btn sm ghost" to="/data?tab=records">Browse <ArrowRight /></Link></div>
              {data.recent.length ? (
                <ul className="timeline">
                  {data.recent.map((r) => (
                    <li key={r.id} className="clickable" style={{ cursor: "pointer" }} onClick={() => setOpen(r.id)}>
                      <span className="when">{clock(r.time)}</span>
                      <div className="what row">
                        <div className="grow">
                          <div className="t1">{r.title || r.subject || r.preview || r.type}</div>
                          <div className="t2 mono">{r.source}</div>
                        </div>
                        <TypeTag type={r.type} />
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty title="Nothing here yet" action={<Link className="btn sm" to="/data">Connect data</Link>}>
                  Upload a manual, a CSV of batches or a maintenance log, and Irris starts learning your plant.
                </Empty>
              )}
            </section>
          </>
        )}
      </div>
      {open && <RecordDrawer id={open} onClose={() => setOpen(null)} onDeleted={reload} />}
    </div>
  );
}
