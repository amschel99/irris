import { ArrowLeft, Printer, ScanLine } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Empty, ErrorBox, Head, Loading, ResultTag, StatusTag } from "../components/ui";
import { get } from "../lib/api";
import { useSession } from "../lib/session";
import type { RecordSummary, Run, RunSummary } from "../lib/types";
import { ago, duration, num, stamp, useLoad } from "../lib/util";

export function Runs() {
  const { workspace } = useSession();
  const nav = useNavigate();
  const { data, error, loading, reload } = useLoad<{ runs: RunSummary[] }>(() => get("/runs"), [workspace?.id]);
  return (
    <div className="page">
      <Head title="Runs" sub="Every guided run, with each check Irris made and the batch record it wrote." actions={<Link className="btn primary" to="/live"><ScanLine /> New run</Link>} />
      {loading && !data && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && !data.runs.length && (
        <Empty title="No runs yet" action={<Link className="btn sm" to="/live">Start one in Live</Link>}>
          Pick a procedure in Live. Irris checks each step through the camera and writes the batch record when you finish.
        </Empty>
      )}
      {!!data?.runs.length && (
        <div className="table-wrap">
          <table className="t">
            <thead><tr><th>Procedure</th><th>Batch or job</th><th>Started</th><th>Duration</th><th>Checks</th><th>Deviations</th><th>Status</th></tr></thead>
            <tbody>
              {data.runs.map((r) => (
                <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => nav(r.status === "active" ? `/live?run=${r.id}` : `/runs/${r.id}`)}>
                  <td style={{ fontWeight: 500 }}>{r.procedure.name}</td>
                  <td className="mono">{r.label || <span className="faint">—</span>}</td>
                  <td className="mono nowrap">{ago(r.started)}</td>
                  <td className="mono">{duration(r.started, r.ended)}</td>
                  <td className="mono">{r.checks}</td>
                  <td className={`mono${r.deviations ? " red" : ""}`}>{r.deviations}</td>
                  <td><StatusTag status={r.status} deviations={r.deviations} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function RunDetail() {
  const { id = "" } = useParams();
  const run = useLoad<Run>(() => get(`/runs/${id}`), [id]);
  const batch = useLoad<{ records: RecordSummary[] }>(() => get(`/records?type=batch_record&subject=${encodeURIComponent(`run:${id}`)}`), [id]);
  const r = run.data;
  const deviations = r?.log.filter((e) => e.result === "fail" || e.result === "danger") || [];
  const passed = r?.steps.filter((s) => s.result === "pass").length || 0;

  return (
    <div className="page">
      <Link to="/runs" className="btn sm ghost" style={{ marginBottom: 16 }}><ArrowLeft /> Runs</Link>
      {run.loading && !r && <Loading />}
      <ErrorBox error={run.error} retry={run.reload} />
      {r && (
        <>
          <Head
            eyebrow={`Batch record${r.label ? ` · ${r.label}` : ""}`}
            title={r.procedure.name}
            sub={<>Started {stamp(r.started)} · {duration(r.started, r.ended)} · run <span className="mono">{r.id}</span></>}
            actions={<>
              {r.status === "active" && <Link className="btn primary" to={`/live?run=${r.id}`}><ScanLine /> Continue in Live</Link>}
              <button className="btn" onClick={() => window.print()}><Printer /> Print</button>
            </>}
          />
          <div className="stack lg">
            <div className="stats">
              <div className="stat"><span className="num">{passed}/{r.steps.length}</span><span className="lbl">Steps passed</span></div>
              <div className="stat"><span className={`num${deviations.length ? " red" : ""}`}>{deviations.length}</span><span className="lbl">Deviations</span></div>
              <div className="stat"><span className="num">{r.log.length}</span><span className="lbl">Checks</span></div>
              <div className="stat"><span className="num" style={{ fontSize: 18, paddingTop: 8 }}><StatusTag status={r.status} deviations={deviations.length} /></span><span className="lbl">Status</span></div>
            </div>

            <section className="stack">
              <h2>Steps</h2>
              <div className="table-wrap">
                <table className="t">
                  <thead><tr><th>#</th><th>Instruction</th><th>Result</th><th>Reading</th><th>Note</th></tr></thead>
                  <tbody>
                    {r.steps.map((s) => (
                      <tr key={s.index}>
                        <td className="mono">{s.index + 1}</td>
                        <td>{s.instruction}{s.reading && <div className="faint mono" style={{ fontSize: 11 }}>spec {s.reading.min ?? "−∞"}–{s.reading.max ?? "∞"} {s.reading.unit}</div>}</td>
                        <td><ResultTag result={s.result} /></td>
                        <td className="mono">{s.measured ? `${num(s.measured.value)} ${s.measured.unit}` : <span className="faint">—</span>}</td>
                        <td style={{ maxWidth: 360 }}>{s.say}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="stack">
              <h2>Every check</h2>
              {r.log.length ? (
                <ul className="timeline">
                  {r.log.map((e, i) => (
                    <li key={i}>
                      <span className="when">{new Date(e.time * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>
                      <div className="what row top">
                        <div className="grow">
                          <div className="t1">Step {e.step + 1} · {e.say || e.result}</div>
                          <div className="t2 mono">{e.by}{e.reading ? ` · ${num(e.reading.value)} ${e.reading.unit}` : ""}{e.reading && e.by === "vision" ? ` · conf ${e.reading.confidence.toFixed(2)}` : ""}</div>
                        </div>
                        <ResultTag result={e.result} />
                      </div>
                    </li>
                  ))}
                </ul>
              ) : <Empty title="No checks yet">Checks appear here as steps are checked by the camera or marked by hand.</Empty>}
            </section>

            {batch.data?.records[0] && (
              <div className="note">This batch record is saved as record <span className="mono">{batch.data.records[0].id}</span>, so Irris can search it and cite it ("why did batch {r.label || "this"} fail?").</div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
