import { ExternalLink, Trash2 } from "lucide-react";
import { del, get } from "../lib/api";
import { useSession } from "../lib/session";
import type { RecordFull } from "../lib/types";
import { errText, stamp, useLoad } from "../lib/util";
import { Drawer, ErrorBox, Json, Loading, TypeTag } from "./ui";

/** A record's full detail. `page` highlights the cited page of a document. */
export function RecordDrawer({ id, page, excerpt, onClose, onDeleted }: {
  id: string; page?: number | null; excerpt?: string; onClose: () => void; onDeleted?: () => void;
}) {
  const { toast } = useSession();
  const virtual = id.startsWith("sql:") || id.startsWith("camera:") || id.startsWith("stats:");
  const { data, error, loading } = useLoad<RecordFull | null>(() => (virtual ? Promise.resolve(null) : get(`/records/${id}`)), [id]);

  if (virtual) {
    return (
      <Drawer title={<h2>{id.startsWith("sql:") ? "Database query" : id.startsWith("stats:") ? "Statistics" : "Camera"}</h2>} onClose={onClose}>
        <p className="muted" style={{ marginTop: 0 }}>
          {id.startsWith("sql:") ? "Irris ran this read-only query on your connected database:" : id.startsWith("stats:") ? "Irris computed these numbers from your records:" : "What Irris saw through the camera:"}
        </p>
        <pre className="code" style={{ whiteSpace: "pre-wrap" }}>{excerpt}</pre>
      </Drawer>
    );
  }

  const fileId = data?.data?.file_id as string | undefined;
  const remove = async () => {
    if (!confirm("Delete this record? Irris will forget it.")) return;
    try {
      await del(`/records/${id}`);
      toast("Record deleted");
      onDeleted?.();
      onClose();
    } catch (e) {
      toast(errText(e), true);
    }
  };

  return (
    <Drawer
      title={
        <div className="stack sm">
          <div className="row">{data && <TypeTag type={data.type} />}{page ? <span className="tag">p. {page}</span> : null}</div>
          <h2 style={{ fontSize: 16 }}>{data?.title || data?.subject || (data ? `${data.type} record` : "Record")}</h2>
        </div>
      }
      onClose={onClose}
      actions={data && <button className="btn sm danger icon" aria-label="Delete record" onClick={remove}><Trash2 /></button>}
    >
      {loading && <Loading />}
      <ErrorBox error={error} />
      {data && (
        <div className="stack lg">
          {excerpt && (
            <div className="stack sm">
              <div className="eyebrow">Cited passage</div>
              <div className="note" style={{ whiteSpace: "pre-wrap", color: "var(--fg)" }}>{excerpt}</div>
            </div>
          )}
          <dl className="kv">
            <dt>Source</dt><dd className="mono">{data.source}</dd>
            {data.subject && <><dt>Subject</dt><dd className="mono">{data.subject}</dd></>}
            {data.external_id && <><dt>External id</dt><dd className="mono">{data.external_id}</dd></>}
            <dt>Time</dt><dd className="mono">{stamp(data.time)}</dd>
            <dt>Record id</dt><dd className="mono faint">{data.id}</dd>
            {data.tags.length > 0 && <><dt>Tags</dt><dd className="row wrap">{data.tags.map((t) => <span key={t} className="tag">{t}</span>)}</dd></>}
            {data.links.length > 0 && <><dt>Links</dt><dd className="mono">{data.links.map((l) => `${l.rel} → ${l.to}`).join(", ")}</dd></>}
          </dl>
          {fileId && (
            <a className="btn sm" href={`/api/v1/files/${fileId}`} target="_blank" rel="noreferrer">
              <ExternalLink /> Open original file
            </a>
          )}
          {data.text && (
            <div className="stack sm">
              <div className="eyebrow">Text</div>
              <pre className="code" style={{ whiteSpace: "pre-wrap", maxHeight: 360 }}>{data.text.slice(0, 20000)}{data.text.length > 20000 ? "\n…" : ""}</pre>
            </div>
          )}
          {Object.keys(data.data).length > 0 && (
            <div className="stack sm">
              <div className="eyebrow">Data</div>
              <Json value={data.data} />
            </div>
          )}
        </div>
      )}
    </Drawer>
  );
}
