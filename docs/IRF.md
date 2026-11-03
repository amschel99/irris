# IRF: the Irris Record Format, version 1.0

IRF is how anything gets into Irris. It accepts any data: a sensor reading, a PLC event, a row from your
Postgres, a maintenance log, a PDF manual, a photo, an SOP. Each one becomes a **record**, a small JSON envelope
around whatever data you have. Irris indexes every record so the assistant can find it, cite it and reason with it.

You don't have to convert anything first. Send IRF if you can; send anything else and Irris wraps it in IRF for you.

## The envelope

```json
{
  "irf": "1.0",
  "type": "reading",
  "source": "line-3/plc",
  "id": "press-7-temp-2026-11-02T08:15:00Z",
  "time": "2026-11-02T08:15:00Z",
  "subject": "asset:press-7",
  "title": "Press 7 platen temperature",
  "text": "Platen at 182 C during cycle 4410",
  "data": { "value": 182.4, "unit": "C", "cycle": 4410 },
  "units": { "value": "C" },
  "tags": ["thermal", "line-3"],
  "links": [{ "rel": "part_of", "to": "asset:line-3" }]
}
```

| Field | Required | Meaning |
|---|---|---|
| `irf` | no | Format version. Defaults to `"1.0"`. |
| `type` | **yes** | What the record is. Any lowercase word works. The types below get special handling. |
| `source` | no | Where it came from, as a path: `erp/orders`, `line-3/plc`, `upload`. Defaults to the ingest channel. |
| `id` | no | A stable id **from your system**. Sending the same `source` + `id` again updates the record instead of duplicating it. |
| `time` | no | When it happened (ISO 8601 or Unix seconds). Defaults to when Irris received it. |
| `subject` | no | What it's about, as `kind:name`: `asset:press-7`, `batch:B-1042`, `part:PN-2231`, `person:jane`. Records that share a subject are linked. |
| `title` | no | A short human name. |
| `text` | no | Human-readable content. For documents this is the extracted text. |
| `data` | no | **Any JSON.** Your fields, as they are. Nothing is renamed or dropped. |
| `units` | no | Units for fields in `data`, as `{"field": "unit"}`. |
| `tags` | no | Free labels. |
| `links` | no | Relations to other subjects or records: `[{"rel": "...", "to": "kind:name"}]`. |

A record needs a `type` and at least one of `text`, `data` or `title`. Everything else is optional.

## Types with special handling

| `type` | Irris does this |
|---|---|
| `document` | Splits `text` into passages so answers cite the exact part (and page, if `data.page` is set). |
| `reading` | Treats `data.value` + `data.unit` as a measurement of `subject`; shown in charts and checked against limits. |
| `event` | An alarm, fault, stop or state change. Shown on the timeline. |
| `procedure` | An SOP. `data.steps` is a list of `{"instruction", "check"?, "reading"?: {"min", "max", "unit"}, "caution"?}`. Becomes a guided procedure. |
| `asset` | A machine, line, tool or instrument. Becomes a subject other records point at. |
| `lesson` | A correction or rule the team taught Irris. Always considered when answering. |
| `row` | A table row from a connected database or a CSV. `data` holds the columns. |

Any other type (`work_order`, `quality_check`, `shipment`...) is stored, indexed and searchable as is.

## Ways to send data

| Channel | What you send | What happens |
|---|---|---|
| `POST /api/v1/ingest` | One IRF record, a list of them, or `{"records": [...]}` | Stored as sent. |
| `POST /api/v1/ingest` with `Content-Type: application/x-ndjson` | One record per line | Stored as sent. Good for streams and big batches. |
| `POST /api/v1/ingest?type=<t>&source=<s>` with **any other JSON** | Any object or array | Each object becomes one record of type `<t>` (default `row`), with the object as `data`. |
| `POST /api/v1/ingest/file` | PDF, Word, Excel, CSV, JSON, Markdown, text, images | Documents become `document` records; CSV and Excel rows become `row` records; images become `image` records. |
| Connected Postgres (Sources → Postgres) | A read-only connection string | Chosen tables sync as `row` records, and the assistant can run read-only queries live. |
| Webhook (Sources → Webhook) | Any JSON, from any system | The same as `POST /api/v1/ingest`, authenticated by the webhook's own secret in its URL. |
| MCP tool `ingest` | Records from an AI agent | The same as `POST /api/v1/ingest`. |
| Connected MCP server (Data → MCP servers) | Any MCP server's URL and token | Irris lists its tools; the assistant calls them live; a tool or resource can be pulled in once or on a schedule (every 5 min to daily). JSON results keep their shape (IRF if they are IRF, else one record per object); text becomes a document. |

## How Irris normalises what it receives

1. A value that isn't IRF is wrapped: `{"type": <type param or "row">, "data": <the value>}`.
2. `time` becomes UTC; `type` becomes lowercase; unknown top-level fields move into `data`.
3. The record gets a searchable body made of its title, text, subject, tags and every `data` field as `key: value`.
4. A record with an existing `source` + `id` replaces the old one.

The original payload is always kept unchanged in `data`, so nothing is lost in translation.
