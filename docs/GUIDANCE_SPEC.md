# Guidance v2: the contract between the backend and the Live screen

Seven research-backed features (see the research notes in the PR): a step tracker that validates itself, yes/no
checklists with voting and abstention, calibrated instrument reading, warnings before a mistake, interruption
manners, procedures learned from demo runs, and recovery plans. All additive and backward compatible: old
procedures (steps with just `instruction`, `check`, `caution`, `reading`) keep working.

## Step fields (in `procedure.steps[i]`)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `instruction`, `check`, `caution`, `reading` | as before | | |
| `questions` | `[{"q": str, "must": bool}]` | derived from `check` | 2-4 visible yes/no questions. Pass = every `must` is yes; fail = any `must` is no; else unsure |
| `typical_errors` | `[str]` | `[]` | What usually goes wrong here; used in prompts and warnings |
| `kind` | `action \| measurement \| timing \| order` | `action` (`measurement` if `reading`) | Picks the checker: measurement -> instrument/OCR, action -> checklist |
| `severity` | `critical \| major \| minor` | `major` | Interruption timing (see below) |
| `reversible` | bool | `true` | Irreversible steps get reminders in advance |
| `prereqs` | `[int]` (step indexes) | `[i-1]` | Steps that must be complete first. `[]` = can happen any time |
| `zone` | `{"camera_id": str, "polygon": [[x, y], ...]}` or null | null | Work area for this step, normalised 0-1 image coordinates |
| `expected_objects` | `[str]` | `[]` | Labels the hands should hold/touch in this step (COCO labels or free text) |
| `instrument_id` | str or null | null | A calibrated instrument to read for this step's `reading` |
| `median_s`, `std_s` | number or null | null | Typical duration from good runs |
| `intervention` | `remind_before \| notify_if_missed \| none` | `notify_if_missed` (`remind_before` if not reversible) | |

## Instruments

`GET/POST /api/v1/instruments`, `PUT/DELETE /api/v1/instruments/{id}`

```json
{"id": "...", "camera_id": "...", "name": "Platen thermometer", "type": "digital|analog",
 "roi": [x, y, w, h],                       // normalised
 "format": "^\\d{1,3}\\.\\d$", "unit": "C",  // digital
 "calib": {"center": [x, y], "min": {"point": [x, y], "value": 0}, "max": {"point": [x, y], "value": 300},
           "scale": "linear"}}               // analog: 3 taps + 2 values
```

`POST /api/v1/instruments/{id}/read` (body `{}` uses the camera's latest frame) ->
`{"value": 182.4, "unit": "C", "method": "ocr|needle|vlm", "confidence": 0.0-1.0, "needs_confirm": bool, "reason": str}`.
Digital: OCR inside the ROI + format check + 2-of-3 agreement across recent frames. Analog: needle angle by geometry
from the calibration. The vision model is only a coarse fallback and always `needs_confirm`. Any value within 10% of
a step limit range of a limit -> `needs_confirm`.

## Run state additions (`GET /api/v1/runs/{id}`, pushed over the camera WebSocket as `run`)

Each step gains `status` (`not_started|in_progress|complete|error|skipped`), `progress` (0-1), `evidence`
(`{"score": 0-1, "reasons": ["blurry", "object not visible", ...]}`), `votes` (per question yes/no/unsure counts) and
`last_check`. The run gains `phase` (`working|idle`), `plan_version`, and `recovery` (null or a plan, below).

Rules enforced in code (the model only proposes):
- a step can only become `complete` when its prereqs are complete;
- `complete` needs two passing observations at least 3 s apart (or one manual pass);
- complete steps are never reopened by the model (only by a person);
- "nothing happening" is `idle`, never a deviation.

## Speech suggestions

The server never speaks by itself; it attaches `say` items and the browser applies the interruption policy:

```json
{"say": [{"text": "Wrong part: that's the 100 ohm.", "severity": "critical|major|minor", "topic": "step3:wrong-object",
          "kind": "warning|reminder|question|confirm", "step": 3}]}
```

Browser policy: critical -> speak now (even over the user); major -> after 700 ms of silence; minor -> wait 15 s and
drop it if the issue cleared; one line per topic per 25 s; at most 12 words; reminders phrased as questions
("Have you purged the line?").

## Learning and recovery endpoints

- `POST /api/v1/procedures/{id}/draft-checks` -> fills `questions`, `typical_errors`, `kind`, `severity`, `reversible`
  for every step with the reasoning model; returns the proposal (the author saves it).
- `POST /api/v1/procedures/{id}/learn` -> from the procedure's finished runs: `prereqs` (an edge i->j when i finished
  before j in >=95% of runs containing both), `median_s`/`std_s`, and prototype embeddings; returns a proposal.
- `PATCH /api/v1/procedures/{id}/steps/{i}` -> partial update of one step (used for zones and instrument links).
- `POST /api/v1/runs/{id}/recover` (also triggered on a confirmed deviation) -> `{"what", "impact": [step indexes],
  "why", "fix": [str], "insert": [steps]}`; accepted recovery steps are inserted into the live run, marked
  `"recovery": true`.
