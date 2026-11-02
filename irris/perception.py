"""Fast, cheap perception on the CPU, used next to the vision model (like Iriis Visual's fast layer):

- frame change: a tiny grayscale thumbnail per frame, so watch rules and coaching only spend a vision-model call
  when the scene actually changed
- analog gauges: OpenCV finds the dial and the needle and turns its angle into a value; the vision model's own
  reading is cross-checked against it, and a disagreement is flagged instead of trusted
- displays and written questions: OCR (RapidOCR, ONNX) reads digital displays and notes held up to the camera

Every piece is optional: without OpenCV or RapidOCR installed, the functions return None and Irris relies on the
vision model alone.
"""
from __future__ import annotations

import io
import math
import re
import threading
from dataclasses import dataclass
from typing import Any

_ocr = None
_ocr_failed = ""
_ocr_lock = threading.Lock()


# ================================================================ frame change
def thumb(jpeg: bytes, side: int = 24) -> bytes | None:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg)).convert("L").resize((side, side))
        return img.tobytes()
    except Exception:  # noqa: BLE001
        return None


def change(a: bytes | None, b: bytes | None) -> float:
    """0 = identical, 1 = completely different (mean absolute difference of two thumbnails)."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (255 * len(a))


# ================================================================ analog gauges (OpenCV)
@dataclass
class GaugeSpec:
    min: float = 0.0
    max: float = 100.0
    angle_min: float = 225.0       # maths convention: 0 = 3 o'clock, 90 = 12 o'clock, counter-clockwise positive
    angle_max: float = -45.0
    unit: str = ""

    @classmethod
    def from_reading(cls, r: dict[str, Any] | None) -> "GaugeSpec":
        r = r or {}
        return cls(float(r.get("scale_min", r.get("dial_min", 0))), float(r.get("scale_max", r.get("dial_max", 100))),
                   float(r.get("angle_min", 225)), float(r.get("angle_max", -45)), str(r.get("unit", "")))


def angle_to_value(angle: float, g: GaugeSpec) -> float | None:
    sweep = (g.angle_min - g.angle_max) % 360 or 360
    off = (g.angle_min - angle) % 360
    if off > sweep + 10:
        return None
    off = min(off, sweep)
    return g.min + (g.max - g.min) * off / sweep


def read_gauge(jpeg: bytes, spec: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """{value, unit, confidence, method} from an analog dial, or None (no OpenCV, no dial, no needle)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    circles = cv2.HoughCircles(cv2.medianBlur(gray, 5), cv2.HOUGH_GRADIENT, dp=1.2, minDist=min(h, w) // 2,
                               param1=120, param2=60, minRadius=min(h, w) // 6, maxRadius=min(h, w) // 2)
    if circles is None:
        return None
    cx, cy, r = (int(v) for v in max(np.round(circles[0]).astype(int), key=lambda c: c[2]))
    mask = np.zeros_like(gray)
    cv2.circle(mask, (cx, cy), int(r * 0.9), 255, -1)
    edges = cv2.bitwise_and(cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 50, 150), mask)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=40, minLineLength=int(r * 0.35), maxLineGap=int(r * 0.05))
    if lines is None:
        return None
    best, best_len = None, 0.0
    for x1, y1, x2, y2 in lines[:, 0]:
        d1, d2 = math.hypot(x1 - cx, y1 - cy), math.hypot(x2 - cx, y2 - cy)
        if min(d1, d2) > r * 0.25:
            continue
        if abs((y2 - y1) * cx - (x2 - x1) * cy + x2 * y1 - y2 * x1) / (math.hypot(x2 - x1, y2 - y1) or 1) > r * 0.08:
            continue
        far = (x2, y2) if d2 > d1 else (x1, y1)
        if max(d1, d2) > best_len:
            best, best_len = far, max(d1, d2)
    if best is None:
        return None
    g = GaugeSpec.from_reading(spec)
    angle = math.degrees(math.atan2(cy - best[1], best[0] - cx))
    value = angle_to_value(angle, g)
    if value is None:
        return None
    conf = min(1.0, best_len / (r * 0.8))
    return {"value": round(value, 2), "unit": g.unit, "confidence": round(0.5 + 0.4 * conf, 2), "method": "gauge-cv"}


def cross_check(cv: dict[str, Any] | None, vlm: dict[str, Any] | None, span: float) -> dict[str, Any] | None:
    """Agree within 5 % of the scale -> a confident average; disagree -> flagged with low confidence."""
    if cv and vlm:
        diff = abs(cv["value"] - vlm["value"])
        unit = cv.get("unit") or vlm.get("unit", "")
        if diff <= 0.05 * span:
            return {"value": round((cv["value"] + vlm["value"]) / 2, 2), "unit": unit,
                    "confidence": max(cv["confidence"], vlm.get("confidence", 0), 0.9), "method": "gauge-cv+vlm"}
        return {"value": vlm["value"], "unit": unit, "confidence": 0.3, "method": "gauge-cv vs vlm",
                "note": f"The needle says {cv['value']:g} but the model read {vlm['value']:g}. Check the dial."}
    return cv or vlm


# ================================================================ OCR (optional)
def _engine():
    global _ocr, _ocr_failed
    with _ocr_lock:
        if _ocr is None and not _ocr_failed:
            try:
                from rapidocr_onnxruntime import RapidOCR
                _ocr = RapidOCR()
            except Exception as e:  # noqa: BLE001 - OCR is optional
                _ocr_failed = f"{type(e).__name__}: {str(e)[:120]}"
        return _ocr


def ocr(jpeg: bytes, min_conf: float = 0.6) -> list[dict[str, Any]] | None:
    """[{text, conf}] read in the frame, or None when OCR isn't installed."""
    eng = _engine()
    if eng is None:
        return None
    try:
        result, _ = eng(jpeg)
    except Exception:  # noqa: BLE001 - a frame OCR can't parse is just "no text"
        return []
    out = []
    for item in result or []:
        try:
            _box, text, conf = item
        except (TypeError, ValueError):
            continue
        if float(conf) >= min_conf and str(text).strip():
            out.append({"text": str(text).strip(), "conf": round(float(conf), 2)})
    return out


_NUM = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*(°\s*[CF]|[a-zA-Z%°/]{1,5})?")


def display_reading(texts: list[dict[str, Any]] | None, unit_hint: str = "") -> dict[str, Any] | None:
    """The most likely number on a digital display, from OCR text."""
    best = None
    for t in texts or []:
        m = _NUM.search(t["text"].replace(" ", ""))
        if not m:
            continue
        try:
            value = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        unit = (m.group(2) or unit_hint or "").replace(" ", "")
        score = t["conf"] + (0.2 if unit_hint and unit_hint.lower() in unit.lower() else 0) + 0.05 * len(m.group(1))
        if not best or score > best[0]:
            best = (score, {"value": value, "unit": unit, "confidence": round(min(0.95, t["conf"]), 2), "method": "ocr"})
    return best[1] if best else None


def written_question(texts: list[dict[str, Any]] | None) -> str:
    """A question written on paper or a screen held up to the camera ("notice mode")."""
    words = " ".join(t["text"] for t in texts or [] if t["conf"] >= 0.75)
    if "?" in words and len(words) >= 8:
        return words[:300]
    return ""


def status() -> dict[str, Any]:
    import importlib.util
    return {"opencv": importlib.util.find_spec("cv2") is not None,
            "ocr": importlib.util.find_spec("rapidocr_onnxruntime") is not None and not _ocr_failed,
            "ocr_error": _ocr_failed}


# ================================================================ regions of interest, crops, timestamps
def crop(jpeg: bytes, box: dict | None, *, pad: float = 0.08, min_side: int = 512) -> bytes:
    """Cut a normalised box {x, y, w, h} (0-1) out of a frame, padded, and upscale small crops so text stays legible
    (a tight crop beats downscaling the whole frame for small displays)."""
    if not box:
        return jpeg
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        W, H = img.size
        x0 = max(0.0, float(box["x"]) - pad * float(box["w"]))
        y0 = max(0.0, float(box["y"]) - pad * float(box["h"]))
        x1 = min(1.0, float(box["x"]) + float(box["w"]) * (1 + pad))
        y1 = min(1.0, float(box["y"]) + float(box["h"]) * (1 + pad))
        if x1 - x0 < 0.02 or y1 - y0 < 0.02:
            return jpeg
        part = img.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)))
        side = max(part.size)
        if side < min_side:
            k = min_side / side
            part = part.resize((int(part.size[0] * k), int(part.size[1] * k)), Image.LANCZOS)
        out = io.BytesIO()
        part.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - a bad box falls back to the whole frame
        return jpeg


def roi_thumb(jpeg: bytes, box: dict | None, side: int = 24) -> bytes | None:
    """The change-detection thumbnail of just the region that matters for a step (or the whole frame)."""
    return thumb(crop(jpeg, box, pad=0, min_side=0) if box else jpeg, side)


def text_box(jpeg: bytes) -> dict | None:
    """The box around the digits OCR found (normalised), to crop a display before reading it."""
    eng = _engine()
    if eng is None:
        return None
    try:
        from PIL import Image
        W, H = Image.open(io.BytesIO(jpeg)).size
        result, _ = eng(jpeg)
    except Exception:  # noqa: BLE001
        return None
    pts = []
    for item in result or []:
        try:
            quad, text, conf = item
        except (TypeError, ValueError):
            continue
        if float(conf) >= 0.6 and re.search(r"\d", str(text)):
            pts += [(float(px), float(py)) for px, py in quad]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return {"x": min(xs) / W, "y": min(ys) / H, "w": (max(xs) - min(xs)) / W, "h": (max(ys) - min(ys)) / H}


def stamp(jpeg: bytes, label: str, side: int = 448, quality: int = 72) -> bytes:
    """Resize a frame and burn a timestamp into its corner, so a model's answer is anchored to the time it saw."""
    try:
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        img.thumbnail((side, side))
        d = ImageDraw.Draw(img)
        w = 8 + 8 * len(label)
        d.rectangle((0, 0, w, 20), fill=(0, 0, 0))
        d.text((5, 4), label, fill=(215, 255, 61))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return jpeg


def change_energy(frames: list[tuple[float, bytes]]) -> list[float]:
    """How much each frame differs from the one before (the first frame counts as a full change)."""
    thumbs = [thumb(f) for _, f in frames]
    return [1.0] + [change(thumbs[i - 1], thumbs[i]) for i in range(1, len(thumbs))]


def select_by_change(frames: list[tuple[float, bytes]], k: int) -> list[tuple[float, bytes]]:
    """Pick k frames at scene changes instead of evenly: the first and last frame, then the biggest changes, spread
    out so one long movement doesn't take every slot. Returned in time order."""
    if len(frames) <= k:
        return frames
    energy = change_energy(frames)
    keep = {0, len(frames) - 1}
    gap = max(1, len(frames) // (k * 2))
    for i in sorted(range(len(frames)), key=lambda j: -energy[j]):
        if len(keep) >= k:
            break
        if all(abs(i - j) >= gap for j in keep):
            keep.add(i)
    for i in range(len(frames)):            # quiet video: fill evenly
        if len(keep) >= k:
            break
        if i % max(1, len(frames) // k) == 0:
            keep.add(i)
    return [frames[i] for i in sorted(keep)][:k]
