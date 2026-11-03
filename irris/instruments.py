"""Calibrated instruments (docs/GUIDANCE_SPEC.md): read by geometry and OCR, not by the vision model.

Small vision models misread analog dials most of the time (MeasureBench, CVPR 2026: even frontier models ~30% on real
dials; digital displays ~80%). So each instrument is calibrated once, and read with plain code:

- analog: the person taps the dial centre, the minimum mark and the maximum mark and types their values; the needle is
  the darkest (or lightest) ray from the centre, and its angle maps linearly onto the scale;
- digital: OCR inside the display's region, kept only when it matches the display's format, and accepted when two of
  the last three frames agree.

The vision model is only a coarse fallback, and its value always needs a person to confirm it. Any value within 10%
of a limit (of the limit range) also needs confirming.
"""
from __future__ import annotations

import io
import math
import re
from typing import Any

from irris.db import Database, dumps, loads, new_id, now

TYPES = ("digital", "analog")


class InstrumentError(ValueError):
    pass


# ================================================================ storage
def _pt(v, name: str) -> list[float]:
    try:
        x, y = float(v[0]), float(v[1])
    except (TypeError, ValueError, IndexError, KeyError):
        raise InstrumentError(f"{name} must be [x, y] in 0-1 image coordinates")
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise InstrumentError(f"{name} must be [x, y] in 0-1 image coordinates")
    return [x, y]


def clean(body: dict) -> dict:
    name = str(body.get("name") or "").strip()[:80]
    if not name:
        raise InstrumentError("an instrument needs a name")
    kind = body.get("type")
    if kind not in TYPES:
        raise InstrumentError("type must be digital or analog")
    cfg: dict[str, Any] = {"unit": str(body.get("unit") or "")[:16]}
    roi = body.get("roi")
    if roi is not None:
        try:
            x, y, w, h = (float(v) for v in roi)
        except (TypeError, ValueError):
            raise InstrumentError("roi must be [x, y, w, h] in 0-1")
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1):
            raise InstrumentError("roi must be [x, y, w, h] in 0-1")
        cfg["roi"] = [x, y, min(w, 1 - x), min(h, 1 - y)]
    lim = body.get("limits")
    if isinstance(lim, dict):
        cfg["limits"] = {k: float(lim[k]) for k in ("min", "max") if lim.get(k) is not None}
    if kind == "digital":
        fmt = str(body.get("format") or "").strip()
        if fmt:
            try:
                re.compile(fmt)
            except re.error as e:
                raise InstrumentError(f"format isn't a valid pattern: {e}")
        cfg["format"] = fmt
    else:
        c = body.get("calib")
        if not isinstance(c, dict):
            raise InstrumentError("an analog instrument needs calib: center, min {point, value}, max {point, value}")
        try:
            calib = {"center": _pt(c.get("center"), "calib.center"),
                     "min": {"point": _pt((c.get("min") or {}).get("point"), "calib.min.point"),
                             "value": float((c.get("min") or {}).get("value"))},
                     "max": {"point": _pt((c.get("max") or {}).get("point"), "calib.max.point"),
                             "value": float((c.get("max") or {}).get("value"))},
                     "scale": "linear",
                     "direction": "ccw" if c.get("direction") == "ccw" else "cw",
                     "needle": "light" if c.get("needle") == "light" else "dark"}
        except (TypeError, ValueError):
            raise InstrumentError("calib values must be numbers")
        if calib["min"]["value"] == calib["max"]["value"]:
            raise InstrumentError("calib min and max values must differ")
        cfg["calib"] = calib
    return {"name": name, "type": kind, "camera_id": str(body.get("camera_id") or ""), "config": cfg}


def api(row: dict) -> dict:
    cfg = loads(row["config"], {})
    from irris import ops
    out = {"id": row["id"], "name": row["name"], "type": row["type"], "camera_id": row["camera_id"], **cfg,
           "created": row["created"], "updated": row["updated"]}
    out["status_info"] = ops.instrument_status(out)
    return out


def create(db: Database, workspace_id: str, body: dict) -> dict:
    c = clean(body)
    iid, t = new_id(), now()
    db.execute("INSERT INTO instruments (id, workspace_id, camera_id, name, type, config, created, updated) "
               "VALUES (?,?,?,?,?,?,?,?)", [iid, workspace_id, c["camera_id"], c["name"], c["type"], dumps(c["config"]), t, t])
    return get(db, workspace_id, iid)


def update(db: Database, workspace_id: str, iid: str, body: dict) -> dict:
    cur = get(db, workspace_id, iid)
    merged = {**cur, **body}
    c = clean(merged)
    db.execute("UPDATE instruments SET camera_id = ?, name = ?, type = ?, config = ?, updated = ? WHERE id = ?",
               [c["camera_id"], c["name"], c["type"], dumps(c["config"]), now(), iid])
    return get(db, workspace_id, iid)


def get(db: Database, workspace_id: str, iid: str) -> dict:
    row = db.one("SELECT * FROM instruments WHERE id = ? AND workspace_id = ?", [iid, workspace_id])
    if not row:
        from irris.store import NotFound
        raise NotFound("instrument not found")
    return api(row)


def list_all(db: Database, workspace_id: str) -> list[dict]:
    return [api(r) for r in db.execute("SELECT * FROM instruments WHERE workspace_id = ? ORDER BY name", [workspace_id])]


def delete(db: Database, workspace_id: str, iid: str) -> None:
    get(db, workspace_id, iid)
    db.execute("DELETE FROM instruments WHERE id = ?", [iid])


# ================================================================ analog: the needle by geometry
def _gray(jpeg: bytes):
    import numpy as np
    from PIL import Image
    return np.asarray(Image.open(io.BytesIO(jpeg)).convert("L"), dtype=np.float32)


def _angle(cx: float, cy: float, x: float, y: float) -> float:
    """Image angle in degrees, 0 = 3 o'clock, increasing clockwise (y points down)."""
    return math.degrees(math.atan2(y - cy, x - cx)) % 360


def needle_value(jpeg: bytes, calib: dict) -> dict | None:
    """{"value", "angle", "confidence"} from the calibrated dial, or None when no needle stands out."""
    import numpy as np
    a = _gray(jpeg)
    H, W = a.shape
    cx, cy = calib["center"][0] * W, calib["center"][1] * H
    mx, my = calib["min"]["point"][0] * W, calib["min"]["point"][1] * H
    Mx, My = calib["max"]["point"][0] * W, calib["max"]["point"][1] * H
    r = min(math.hypot(mx - cx, my - cy), math.hypot(Mx - cx, My - cy))
    if r < 12:
        return None
    a_min, a_max = _angle(cx, cy, mx, my), _angle(cx, cy, Mx, My)
    cw = calib.get("direction", "cw") == "cw"
    sweep = ((a_max - a_min) if cw else (a_min - a_max)) % 360 or 360
    radii = np.linspace(0.18 * r, 0.82 * r, 32)
    margin = 6.0
    offs = np.arange(-margin, sweep + margin + 0.01, 0.5)
    means = []
    for off in offs:
        ang = math.radians((a_min + off) if cw else (a_min - off))
        xs = np.clip((cx + radii * math.cos(ang)).astype(int), 0, W - 1)
        ys = np.clip((cy + radii * math.sin(ang)).astype(int), 0, H - 1)
        means.append(float(a[ys, xs].mean()))
    means = np.asarray(means)
    light = calib.get("needle") == "light"
    k = int(means.argmax() if light else means.argmin())
    med = float(np.median(means))
    contrast = abs(means[k] - med) / max(med if not light else 255 - med, 1.0)
    if contrast < 0.12:
        return None
    lo, hi = max(0, k - 2), min(len(means), k + 3)       # sub-step refinement: weighted centre of the dip
    w = (means[lo:hi] - med) if light else (med - means[lo:hi])
    w = np.clip(w, 0, None)
    off = float((offs[lo:hi] * w).sum() / w.sum()) if w.sum() > 0 else float(offs[k])
    off = min(max(off, 0.0), sweep)
    v0, v1 = calib["min"]["value"], calib["max"]["value"]
    value = v0 + (v1 - v0) * off / sweep
    return {"value": round(value, 3), "angle": round(off, 2), "confidence": round(min(0.95, 0.5 + contrast), 2)}


# ================================================================ digital: OCR, format, agreement
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def _crop(jpeg: bytes, roi: list[float] | None) -> bytes:
    if not roi:
        return jpeg
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    W, H = img.size
    x, y, w, h = roi
    box = (int(x * W), int(y * H), int(min(1.0, x + w) * W), int(min(1.0, y + h) * H))
    img = img.crop(box)
    if max(img.size) < 320:
        s = 320 / max(img.size)
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


def display_value(jpeg: bytes, inst: dict, ocr=None) -> dict | None:
    """One frame: OCR inside the ROI; the highest-confidence number that matches the format."""
    from irris import perception
    texts = (ocr or perception.ocr)(_crop(jpeg, inst.get("roi")))
    if not texts:
        return None
    fmt = re.compile(inst["format"]) if inst.get("format") else None
    best = None
    for t in texts:
        raw = str(t.get("text") or "").replace(" ", "")
        for m in _NUM.finditer(raw):
            tok = m.group(0).replace(",", ".")
            if fmt and not fmt.fullmatch(tok):
                continue
            conf = float(t.get("conf") or 0.5)
            if best is None or conf > best["confidence"]:
                best = {"value": float(tok), "text": tok, "confidence": round(conf, 2)}
    return best


def _agree(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-6, 0.005 * max(abs(a), abs(b)))


def near_limit(value: float, limits: dict | None) -> bool:
    if not limits or limits.get("min") is None or limits.get("max") is None:
        return False
    lo, hi = float(limits["min"]), float(limits["max"])
    band = 0.1 * abs(hi - lo)
    return min(abs(value - lo), abs(value - hi)) <= band


def read(inst: dict, frames: list[bytes], *, limits: dict | None = None, ocr=None) -> dict:
    """Read an instrument from the most recent frames (newest last).
    -> {value, unit, method, confidence, needs_confirm, reason} (value None when nothing could be read)."""
    unit = inst.get("unit", "")
    limits = limits or inst.get("limits")
    recent = frames[-3:]
    vals: list[dict] = []
    for f in recent:
        try:
            v = needle_value(f, inst["calib"]) if inst["type"] == "analog" else display_value(f, inst, ocr)
        except Exception:  # noqa: BLE001 - a bad frame is just no reading
            v = None
        if v:
            vals.append(v)
    method = "needle" if inst["type"] == "analog" else "ocr"
    if not vals:
        return {"value": None, "unit": unit, "method": method, "confidence": 0.0, "needs_confirm": True,
                "reason": "couldn't read it" + (" (no needle stands out)" if inst["type"] == "analog" else
                                                " (no number matching the display's format)")}
    best, agreeing = None, 0
    for v in vals:
        n = sum(1 for w in vals if _agree(v["value"], w["value"]))
        if n > agreeing or (n == agreeing and best and v["confidence"] > best["confidence"]):
            best, agreeing = v, n
    out = {"value": best["value"], "unit": unit, "method": method, "confidence": best["confidence"],
           "needs_confirm": False, "reason": "", "frames": len(recent), "agreeing": agreeing}
    if len(recent) >= 2 and agreeing < 2:
        out.update(needs_confirm=True, confidence=min(out["confidence"], 0.4),
                   reason="the last frames disagree")
    elif len(recent) < 2:
        out.update(needs_confirm=True, reason="only one frame to read")
    if near_limit(out["value"], limits):
        out.update(needs_confirm=True, reason=(out["reason"] + "; " if out["reason"] else "") + "close to a limit")
    return out
