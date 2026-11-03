"""Cameras and the vision model.

Frames come from paired phones and signed-in browsers (HTTP POST or a WebSocket) into CameraHub: the latest frame
per camera, a short history (for "what just happened?"), a thumbnail for change detection, and subscribers that get
pushed alerts and verdicts. External cameras are read on demand: an HTTP snapshot URL, an MJPEG stream (the first
frame is taken) or, with OpenCV, RTSP.

The vision model is a chain (llm.chain): the GPU / workspace model first, the container's CPU model as the
fallback. Every call is timed and labelled, so the dashboard can show where vision runs and how fast. Calls to the
CPU fallback go one at a time through a priority gate: a person asking a question goes first; background work
(watch rules, coach, auto-checks, reviews) never waits in line: when the CPU model is busy it's skipped.

The fast layer runs without the model: the browser draws object and hand detections live, and frames from a watched
camera get OCR (at most once a second, only when the picture changed) streamed back as live readings.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import re
import time
from collections import OrderedDict, deque
from typing import Any

import httpx

from irris import llm, perception

FRESH_SECONDS = 10
HISTORY_SECONDS = 300
HISTORY_SIDE = 512
STATUSES = {"ok", "warning", "danger", "unsure"}
RESULTS = {"pass", "fail", "unsure", "danger"}


class CameraHub:
    def __init__(self):
        self._frames: dict[str, tuple[bytes, float]] = {}
        self._thumbs: dict[str, bytes] = {}
        self._history: dict[str, deque] = {}
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self.viewers: dict[str, int] = {}

    def put(self, camera_id: str, jpeg: bytes) -> None:
        t = time.time()
        self._frames[camera_id] = (jpeg, t)
        th = perception.thumb(jpeg)
        if th:
            self._thumbs[camera_id] = th
        if self._subs.get(camera_id):
            live_ocr.maybe(camera_id, jpeg, th)
        hist = self._history.setdefault(camera_id, deque())
        if not hist or t - hist[-1][0] >= 1.0:            # at most one history frame a second
            hist.append((t, shrink(jpeg, HISTORY_SIDE, quality=72)))
        while hist and t - hist[0][0] > HISTORY_SECONDS:
            hist.popleft()

    def get(self, camera_id: str, max_age: float = FRESH_SECONDS) -> bytes | None:
        item = self._frames.get(camera_id)
        if not item or time.time() - item[1] > max_age:
            return None
        return item[0]

    def age(self, camera_id: str) -> float | None:
        item = self._frames.get(camera_id)
        return None if not item else round(time.time() - item[1], 1)

    def thumb(self, camera_id: str) -> bytes | None:
        return self._thumbs.get(camera_id)

    def history(self, camera_id: str, seconds: float) -> list[tuple[float, bytes]]:
        since = time.time() - seconds
        return [(t, f) for t, f in self._history.get(camera_id, ()) if t >= since]

    # ---- push to whoever is watching a camera (the phone itself, Live screens)
    def subscribe(self, camera_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subs.setdefault(camera_id, set()).add(q)
        return q

    def unsubscribe(self, camera_id: str, q: asyncio.Queue) -> None:
        self._subs.get(camera_id, set()).discard(q)

    def publish(self, camera_id: str, message: dict) -> None:
        for q in list(self._subs.get(camera_id, ())):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass


hub = CameraHub()


def shrink(jpeg: bytes, max_side: int = 1024, quality: int = 82) -> bytes:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg))
        if max(img.size) <= max_side and img.format == "JPEG":
            return jpeg
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - not an image we can resize; send it as it is
        return jpeg


async def fetch_external(url: str, timeout: float = 8) -> bytes:
    if url.startswith("rtsp://"):
        return await asyncio.to_thread(_rtsp_frame, url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            buf = b""
            async for chunk in r.aiter_bytes():
                buf += chunk
                if "multipart" in ctype:
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                    if end > 0:
                        return buf[start:end + 2]
                if len(buf) > 12_000_000:
                    break
            if buf[:2] == b"\xff\xd8" or "image" in ctype:
                return buf
    raise ValueError("the camera URL didn't return an image")


def _rtsp_frame(url: str) -> bytes:
    try:
        import cv2
    except ImportError:
        raise ValueError("RTSP cameras need OpenCV on the server (pip install opencv-python-headless)")
    cap = cv2.VideoCapture(url)
    try:
        ok, frame = cap.read()
        if not ok:
            raise ValueError("couldn't read a frame from the RTSP stream")
        ok, jpg = cv2.imencode(".jpg", frame)
        return jpg.tobytes()
    finally:
        cap.release()


# ================================================================ per-model notes (the one place for them)
# qwen3.5 (the CPU fallback) thinks out loud unless told not to; Qwen3-VL on a GPU returns boxes on a 0-1000 grid.
MODEL_TWEAKS: list[tuple[str, dict]] = [
    ("qwen3.5", {"suffix": "\nAnswer directly. Do not think out loud.", "box_scale": 1.0}),
    ("qwen3-vl", {"suffix": "", "box_scale": 1000.0}),
    ("qwen2.5vl", {"suffix": "", "box_scale": 1000.0}),
]
IMAGE_SIDE = {"cpu": 512, "gpu": 1280}        # the long side sent to the model; crops carry small text instead
TIMEOUT = {"cpu": 90.0, "gpu": 45.0}            # an interactive look, including the wait for the CPU model
BACKGROUND_TIMEOUT = 240.0


def tweaks(model: str) -> dict:
    m = (model or "").lower()
    for key, t in MODEL_TWEAKS:
        if key in m:
            return t
    return {"suffix": "", "box_scale": 1.0}


def side_for(models: list[llm.Model]) -> int:
    first = next((m for m in models if llm._down.get(m.base_url, 0) <= time.time()), models[0] if models else None)
    return IMAGE_SIDE["cpu" if first is None or where(first) == "cpu" else "gpu"]


# ================================================================ the cheap gate in front of every model call
_gate: dict[str, dict[str, int]] = {}        # workspace id -> {"called", "skipped"}
GATE_CHANGE = 0.025


def gate_count(workspace_id: str, called: bool) -> None:
    g = _gate.setdefault(workspace_id or "-", {"called": 0, "skipped": 0})
    g["called" if called else "skipped"] += 1


def gate_stats(workspace_id: str) -> dict[str, int]:
    return dict(_gate.get(workspace_id, {"called": 0, "skipped": 0}))


def changed(prev: bytes | None, cur: bytes | None, threshold: float = GATE_CHANGE) -> bool:
    return prev is None or cur is None or perception.change(prev, cur) >= threshold


# ================================================================ the model chain, timed and labelled
_stats: dict[str, dict] = {}                  # workspace id -> last call


class VisionBusy(llm.ModelError):
    """Background work skipped because the CPU model is busy (it never queues)."""


class VisionTimeout(llm.ModelError):
    pass


class PriorityGate:
    """One call at a time on the CPU model. Interactive callers queue (first come, first served) and always go
    before background work; background callers only take a free gate with nobody waiting, otherwise they're skipped."""

    def __init__(self):
        self._busy = False
        self._waiters: deque = deque()

    def locked(self) -> bool:
        return self._busy

    def waiting(self) -> int:
        return len(self._waiters)

    def try_acquire(self) -> bool:
        if self._busy or self._waiters:
            return False
        self._busy = True
        return True

    async def acquire_low(self) -> None:
        """Background work that must finish (a video review): wait for a free gate, always letting people go first."""
        while not self.try_acquire():
            await asyncio.sleep(0.25)

    async def acquire(self) -> None:
        if not self._busy and not self._waiters:
            self._busy = True
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await fut
        except BaseException:
            if fut in self._waiters:
                self._waiters.remove(fut)
            elif fut.done() and not fut.cancelled():
                self.release()          # we were handed the gate as we got cancelled: pass it on
            raise

    def release(self) -> None:
        while self._waiters:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_result(True)    # hand over directly; _busy stays True
                return
        self._busy = False


_cpu_gate = PriorityGate()


def where(m: llm.Model) -> str:
    fb = llm.fallback("vlm")
    if fb.ready and m.base_url == fb.base_url:
        return "cpu"
    return "gpu"


async def call(models: list[llm.Model], messages: list[dict], *, workspace_id: str = "", kind: str = "look",
               max_tokens: int = 400, json: bool = True, background: bool | str = False,
               timeout: float | None = None) -> tuple[dict, dict]:
    """chat on the first model in the chain that answers. Returns (message, {model, where, ms}).
    Short, fixed-schema JSON by default; json=False is for the one escalation retry that may reason first.
    background=True: on the CPU model, skip (VisionBusy) instead of waiting when it's busy; background="wait": wait
    at the lowest priority (reviews that must finish). Interactive calls have a
    hard timeout that includes the wait (VisionTimeout); cancelling the caller (a client that left) cancels the call."""
    if not models:
        raise llm.ModelError("no vision model is configured")
    errors = []
    for i, m in enumerate(models):
        last = i == len(models) - 1
        if not last and llm._down.get(m.base_url, 0) > time.time():
            continue
        on_cpu = where(m) == "cpu"
        t0 = time.perf_counter()
        suffix = tweaks(m.model)["suffix"]
        msgs = [{**messages[0], "content": messages[0]["content"] + suffix}, *messages[1:]] \
            if suffix and messages and messages[0]["role"] == "system" else messages
        limit = timeout or (BACKGROUND_TIMEOUT if background else TIMEOUT["cpu" if on_cpu else "gpu"])
        try:
            if on_cpu:
                if background == "wait":
                    await _cpu_gate.acquire_low()
                    try:
                        msg = await llm.chat(m, msgs, max_tokens=max_tokens, timeout=limit, json_mode=json)
                    finally:
                        _cpu_gate.release()
                elif background:
                    if not _cpu_gate.try_acquire():
                        gate_count(workspace_id, False)
                        raise VisionBusy("the vision model is busy; skipped")
                    try:
                        msg = await llm.chat(m, msgs, max_tokens=max_tokens, timeout=limit, json_mode=json)
                    finally:
                        _cpu_gate.release()
                else:
                    async def run_cpu():
                        await _cpu_gate.acquire()
                        try:
                            return await llm.chat(m, msgs, max_tokens=max_tokens, timeout=limit, json_mode=json)
                        finally:
                            _cpu_gate.release()
                    try:
                        msg = await asyncio.wait_for(run_cpu(), limit)
                    except asyncio.TimeoutError:
                        raise VisionTimeout(f"couldn't see in time ({int(limit)} s on CPU)")
            else:
                try:
                    msg = await asyncio.wait_for(llm.chat(m, msgs, max_tokens=max_tokens, timeout=limit, json_mode=json),
                                                 limit + 5)
                except asyncio.TimeoutError:
                    raise llm.ModelError("model unreachable: timeout")
        except (VisionBusy, VisionTimeout):
            raise
        except llm.ModelError as e:
            errors.append(f"{m.model}: {e}")
            if "unreachable" in str(e) or " 5" in str(e)[:16]:
                llm._down[m.base_url] = time.time() + llm.DOWN_SECONDS
            continue
        llm._down.pop(m.base_url, None)
        info = {"model": m.model, "where": "cpu" if on_cpu else "gpu", "ms": int((time.perf_counter() - t0) * 1000),
                "kind": kind, "at": time.time(), "fallback": on_cpu and i > 0}
        if workspace_id:
            _stats[workspace_id] = info
        gate_count(workspace_id, True)
        return msg, info
    raise llm.ModelError("; ".join(errors))


def stats(workspace_id: str) -> dict | None:
    return _stats.get(workspace_id)


def busy_cpu() -> bool:
    return _cpu_gate.locked()


def queue_depth() -> int:
    return _cpu_gate.waiting()


def expected_seconds(models: list[llm.Model]) -> int:
    """A rough answer time for the UI: recent measured looks, else a typical figure."""
    first = next((m for m in models if llm._down.get(m.base_url, 0) <= time.time()), None)
    on_cpu = first is None or where(first) == "cpu"
    recent = [v["ms"] for v in _stats.values() if v.get("where") == ("cpu" if on_cpu else "gpu") and v.get("kind") == "look"]
    if recent:
        return max(1, int(sorted(recent)[len(recent) // 2] / 1000))
    return 30 if on_cpu else 3


# ================================================================ live OCR: the fast layer for displays
class LiveOCR:
    """OCR on a watched camera's frames: at most once a second per camera, only when the picture changed, one at a
    time across all cameras, and never while someone waits for the CPU model. Results are pushed to whoever is
    watching as {"type": "reading"} (a number it could read) or {"type": "text"} (words in view)."""

    INTERVAL = 1.0

    def __init__(self):
        self._last: dict = {}
        self._running = False
        self.enabled = True

    def maybe(self, camera_id: str, jpeg: bytes, thumb: bytes | None) -> None:
        if not self.enabled or self._running or queue_depth():
            return
        t, prev = self._last.get(camera_id, (0.0, None))
        now_ = time.time()
        if now_ - t < self.INTERVAL or (prev is not None and thumb is not None and perception.change(prev, thumb) < 0.02):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._last[camera_id] = (now_, thumb)
        self._running = True
        loop.create_task(self._run(camera_id, jpeg))

    async def _run(self, camera_id: str, jpeg: bytes) -> None:
        try:
            texts = await asyncio.to_thread(perception.ocr, shrink(jpeg, 960))
        except Exception:  # noqa: BLE001 - OCR is a helper
            texts = None
        finally:
            self._running = False
        if not texts:
            return
        r = perception.display_reading(texts)
        words = " ".join(str(t.get("text", "")) for t in texts[:12])[:240]
        if r:
            hub.publish(camera_id, {"type": "reading", "value": r["value"], "unit": r.get("unit", ""),
                                    "confidence": r.get("confidence", 0.7), "text": words, "source": "ocr",
                                    "t": time.time()})
        elif words.strip():
            hub.publish(camera_id, {"type": "text", "text": words, "source": "ocr", "t": time.time()})


live_ocr = LiveOCR()


# ================================================================ prompts
LOOK_PROMPT = """You are Irris, the eyes of a manufacturing and engineering team's AI twin. You see one frame from a
camera on their workbench or production line. Answer the question about what you can actually see. If you can't
make something out, say so and ask them to move the camera closer. Read instruments with value, unit and confidence.
Flag safety issues first. Be brief and direct: 1-3 short sentences, plain words.
Reply as JSON: {"say": str, "status": "ok|warning|danger|unsure",
"reading": {"value": number, "unit": str, "confidence": number} | null}"""

CHECK_PROMPT = """You are Irris, checking one step of a standard operating procedure through a camera. Decide whether the
frame shows the step done correctly. Only judge what is visible; if the frame doesn't show enough, the result is
"unsure" and you say what to show you. If the step needs a reading, read it (value, unit, confidence).
Anything unsafe is "danger". Be brief: one or two short sentences, like a senior technician next to them.
Reply as JSON: {"result": "pass|fail|unsure|danger", "say": str,
"reading": {"value": number, "unit": str, "confidence": number} | null}"""

COACH_PROMPT = """You are Irris, coaching someone live while they work. Look at the frame with their goal in mind. Speak up
ONLY for a mistake, a safety issue, a reading out of range, a finished step or a clearly useful next move. Otherwise
stay quiet. One short sentence.
Reply as JSON: {"speak": bool, "say": str, "status": "ok|warning|danger"}"""

WATCH_PROMPT = """You are Irris, watching a camera for one condition the team asked about. Decide only whether the
condition is true in this frame. If you can't tell, it's false. Describe what you saw in one short sentence.
Reply as JSON: {"match": bool, "say": str, "confidence": number}"""

REVIEW_PROMPT = """You are Irris, reviewing frames from a camera on a workbench or production line, in time order, each
labelled with its time. Describe what happened as a short timeline (only real changes), note any readings you can
see, and any problems: mistakes, safety issues, steps done out of order or skipped. Be factual; don't guess.
Reply as JSON: {"summary": str, "timeline": [{"t": str, "what": str}], "readings": [{"t": str, "value": number,
"unit": str, "what": str}], "problems": [{"t": str, "what": str, "severity": "warning|danger"}]}"""


CAPTION_PROMPT = """Describe this frame for later questions. List every distinct object or part you can see, where it
is in the frame, its state, and any text or numbers on it. Be literal; don't guess.
Reply as JSON: {"objects": [{"label": str, "location": "left|center|right|top|bottom|top-left|top-right|bottom-left|
bottom-right", "state": str, "text_seen": str}], "scene": str}"""

READ_PROMPT = """You are reading one instrument (a gauge, display, scale or meter) in this image. Give its value and unit,
your confidence (0-1), and where the instrument is, as a box with x, y, w, h from 0 to 1 of the image size.
If you can't read it, value is null.
Reply as JSON: {"value": number | null, "unit": str, "confidence": number, "box": {"x": number, "y": number,
"w": number, "h": number} | null}"""

SPATIAL = re.compile(r"\b(where|which|left|right|top|bottom|behind|next to|above|below|locate|find|how many|count)\b", re.I)
_captions: "OrderedDict[str, dict]" = OrderedDict()


def frame_key(jpeg: bytes) -> str:
    return hashlib.sha1(jpeg).hexdigest()


async def caption(models: list[llm.Model], jpeg: bytes, *, workspace_id: str = "") -> dict:
    """A structured caption of the frame, once per frame (cached by its hash). Small models answer "where" and
    "which one" questions far better from their own caption plus the image than from the image alone."""
    key = frame_key(jpeg)
    if key in _captions:
        _captions.move_to_end(key)
        gate_count(workspace_id, False)
        return _captions[key]
    content = [{"type": "text", "text": "Caption this frame."}, llm.image_part(shrink(jpeg, side_for(models)))]
    msg, _info = await call(models, [{"role": "system", "content": CAPTION_PROMPT}, {"role": "user", "content": content}],
                            workspace_id=workspace_id, kind="caption", max_tokens=500)
    out = llm.parse_json(msg.get("content") or "")
    objs = [o for o in (out.get("objects") or []) if isinstance(o, dict) and o.get("label")][:40]
    cap = {"objects": objs, "scene": str(out.get("scene") or "")[:400]}
    _captions[key] = cap
    while len(_captions) > 64:
        _captions.popitem(last=False)
    return cap


def caption_text(cap: dict) -> str:
    lines = [f"Scene: {cap.get('scene', '')}"] if cap.get("scene") else []
    for o in cap.get("objects", []):
        bits = [str(o.get("label")), f"at {o.get('location')}" if o.get("location") else "",
                f"({o.get('state')})" if o.get("state") else "", f"text '{o.get('text_seen')}'" if o.get("text_seen") else ""]
        lines.append("- " + " ".join(b for b in bits if b))
    return "\n".join(lines)


def _box(value, scale: float = 1.0) -> dict | None:
    if not isinstance(value, dict):
        return None
    try:
        b = {k: float(value[k]) / scale for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None
    if scale == 1.0 and max(b.values()) > 1.5:          # a model that answered in pixels or per-mille anyway
        return None
    if b["w"] <= 0.01 or b["h"] <= 0.01 or b["x"] < -0.05 or b["y"] < -0.05:
        return None
    return b


async def _read_once(models: list[llm.Model], image: bytes, *, workspace_id: str, hint: str = "", think: bool = False,
                     background: bool = False) -> dict:
    text = (hint + "\n" if hint else "") + ("Look carefully, reason briefly about the scale and digits, then give the JSON."
                                            if think else "Read the instrument.")
    content = [{"type": "text", "text": text}, llm.image_part(image)]
    msg, info = await call(models, [{"role": "system", "content": READ_PROMPT}, {"role": "user", "content": content}],
                           workspace_id=workspace_id, kind="read", max_tokens=600 if think else 160, json=not think,
                           background=background)
    out = llm.parse_json(msg.get("content") or "")
    r = None
    if out.get("value") is not None:
        r = _reading(out)
    return {"reading": r, "box": _box(out.get("box"), tweaks(info["model"])["box_scale"]), "info": info}


def _agree(a: dict, b: dict, spec: dict | None) -> bool:
    return abs(a["value"] - b["value"]) <= 0.02 * _span(spec, a["value"]) + 1e-9


async def read_verified(models: list[llm.Model], jpeg: bytes, spec: dict | None = None, roi: dict | None = None,
                        *, workspace_id: str = "", hint: str = "", background: bool = False) -> dict:
    """Crop to the instrument, then verify. Read 1: the saved ROI crop, else the whole frame (which also locates the
    instrument). Read 2: a tight crop at higher resolution (the model's box, else OCR's digit box). Agreement with
    confidence >= 0.6 is a verified reading; otherwise one more careful read; still split -> "unsure". OCR digits, when
    available, are a third opinion: a conflict with the model makes the reading unsure. A single uncertain read never
    passes a step or fires an alert."""
    side = side_for(models)
    reads: list[dict] = []
    first_img = perception.crop(jpeg, roi) if roi else shrink(jpeg, side)
    r1 = await _read_once(models, first_img, workspace_id=workspace_id, hint=hint, background=background)
    reads.append(r1)
    box = roi or (r1["box"] if not roi else None)
    if box is None:
        box = await asyncio.to_thread(perception.text_box, jpeg)
    tight = perception.crop(jpeg, box, pad=0.15, min_side=640) if box else shrink(jpeg, min(side * 2, 2048))
    r2 = await _read_once(models, tight, workspace_id=workspace_id, hint=hint, background=background)
    reads.append(r2)
    a, b = r1["reading"], r2["reading"]
    ocr = await asyncio.to_thread(lambda: perception.display_reading(perception.ocr(tight), (spec or {}).get("unit", "")))
    best = None
    verified = False
    note = ""
    if a and b and _agree(a, b, spec) and min(a["confidence"], b["confidence"]) >= 0.6:
        best, verified = {**b, "value": round((a["value"] + b["value"]) / 2, 4)}, True
    else:
        r3 = await _read_once(models, tight, workspace_id=workspace_id, hint=hint, think=True, background=background)
        reads.append(r3)
        c = r3["reading"]
        votes = [x for x in (a, b, c) if x]
        for x in votes:
            agreeing = [y for y in votes if _agree(x, y, spec)]
            if len(agreeing) >= 2 and max(y["confidence"] for y in agreeing) >= 0.6:
                best, verified = {**x, "confidence": max(y["confidence"] for y in agreeing)}, True
                break
        if not verified:
            best = max(votes, key=lambda y: y["confidence"]) if votes else None
            note = "The reads disagree or are uncertain."
    if best and ocr:
        if _agree(best, ocr, spec):
            best = {**best, "confidence": max(best["confidence"], 0.9), "method": "vlm+ocr"}
        elif ocr["confidence"] >= 0.8:
            verified, note = False, f"The display text says {ocr['value']:g} but the model read {best['value']:g}."
    if best:
        best.setdefault("method", "vlm x%d" % len(reads))
        best["verified"] = verified
        if note:
            best["note"] = note
    elif ocr:
        best = {**ocr, "verified": False, "note": "Only OCR could read it."}
    return {"reading": best, "verified": verified, "reads": len(reads), "box": box, "note": note,
            "model": reads[-1]["info"]["model"], "where": reads[-1]["info"]["where"],
            "ms": sum(r["info"]["ms"] for r in reads)}


def _reading(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    try:
        return {"value": float(value["value"]), "unit": str(value.get("unit") or ""),
                "confidence": max(0.0, min(1.0, float(value.get("confidence", 0.5))))}
    except (KeyError, TypeError, ValueError):
        return None


def _span(spec: dict | None, value: float) -> float:
    if spec and spec.get("min") is not None and spec.get("max") is not None:
        return max(abs(float(spec["max"]) - float(spec["min"])), 1e-6)
    if spec and spec.get("scale_min") is not None and spec.get("scale_max") is not None:
        return max(abs(float(spec["scale_max"]) - float(spec["scale_min"])), 1e-6)
    return max(abs(value) * 0.5, 1.0)


async def fast_reading(jpeg: bytes, spec: dict | None = None) -> dict | None:
    """The CPU-only reading: a gauge (when the step describes the dial's scale) or a digital display (OCR)."""
    def work():
        if spec and (spec.get("scale_min") is not None or spec.get("dial_min") is not None):
            g = perception.read_gauge(jpeg, spec)
            if g:
                return g
        return perception.display_reading(perception.ocr(jpeg), (spec or {}).get("unit", ""))
    try:
        return await asyncio.to_thread(work)
    except Exception:  # noqa: BLE001 - perception is a helper, never a blocker
        return None


def combine(vlm: dict | None, fast: dict | None, spec: dict | None) -> dict | None:
    if vlm and fast:
        out = perception.cross_check(fast, vlm, _span(spec, vlm["value"]))
        return out
    return vlm or fast


async def look(models: list[llm.Model], jpeg: bytes, question: str, context: str = "", *, workspace_id: str = "",
               spec: dict | None = None, scene: str = "", background: bool = False) -> dict:
    """One model call. `scene` is what the on-device detector sees right now (objects, hands, activity), a cheap hint
    for the model. On a GPU, "where/which" questions get the frame's self-caption first; on the CPU model only a
    caption that's already cached is used, so an interactive look stays a single call."""
    if scene:
        context = (context + "\n\n" if context else "") + "On-device detector, just now: " + scene[:600]
    first = next((m for m in models if llm._down.get(m.base_url, 0) <= time.time()), None)
    on_gpu = first is not None and where(first) == "gpu"
    if SPATIAL.search(question or "") and (on_gpu or frame_key(jpeg) in _captions):
        try:
            cap = await caption(models, jpeg, workspace_id=workspace_id)
            context = (context + "\n\n" if context else "") + "Your own caption of this frame:\n" + caption_text(cap)
        except llm.ModelError:
            pass
    content = [{"type": "text", "text": (context + "\n\n" if context else "") + f"Question: {question}"},
               llm.image_part(shrink(jpeg, side_for(models)))]
    fast_task = asyncio.create_task(fast_reading(jpeg, spec))
    try:
        msg, info = await call(models, [{"role": "system", "content": LOOK_PROMPT}, {"role": "user", "content": content}],
                               workspace_id=workspace_id, kind="look", max_tokens=150, background=background)
    except BaseException:
        fast_task.cancel()
        raise
    raw = msg.get("content") or ""
    out = llm.parse_json(raw)
    status = out.get("status") if out.get("status") in STATUSES else "unsure"
    reading = _reading(out.get("reading"))
    fast = await fast_task
    if reading or (fast and spec):
        reading = combine(reading, fast, spec)
    return {"say": str(out.get("say") or raw).strip()[:1200], "status": status, "reading": reading,
            "model": info["model"], "where": info["where"], "ms": info["ms"]}


def step_context(step: dict, index: int, total: int, knowledge: str = "") -> str:
    lines = [f"Step {index + 1} of {total}: {step['instruction']}"]
    if step.get("check"):
        lines.append(f"What correct looks like: {step['check']}")
    if step.get("caution"):
        lines.append(f"Caution: {step['caution']}")
    r = step.get("reading")
    if r:
        lines.append(f"Required reading{' (' + r['label'] + ')' if r.get('label') else ''}: between "
                     f"{r.get('min', '-inf')} and {r.get('max', 'inf')} {r.get('unit', '')}")
    if knowledge:
        lines.append("Relevant team knowledge:\n" + knowledge)
    return "\n".join(lines)


def judge_reading(step: dict, reading: dict | None) -> tuple[bool | None, str]:
    """Compare a reading with the step's limits. None = no limits or no reading."""
    spec = step.get("reading")
    if not spec:
        return None, ""
    if not reading:
        return False, "no reading taken"
    v = reading["value"]
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and v < float(lo):
        return False, f"{v:g} {reading['unit']} is below the minimum {lo} {spec.get('unit', '')}".strip()
    if hi is not None and v > float(hi):
        return False, f"{v:g} {reading['unit']} is above the maximum {hi} {spec.get('unit', '')}".strip()
    return True, ""


async def check_step(models: list[llm.Model], jpeg: bytes, step: dict, index: int, total: int, knowledge: str = "",
                     *, workspace_id: str = "", roi: dict | None = None, background: bool = False,
                     scene: str = "") -> dict:
    text = step_context(step, index, total, knowledge)
    if scene:
        text += "\nOn-device detector, just now: " + scene[:600]
    content = [{"type": "text", "text": text}, llm.image_part(shrink(jpeg, side_for(models)))]
    spec = step.get("reading")
    msg, info = await call(models, [{"role": "system", "content": CHECK_PROMPT}, {"role": "user", "content": content}],
                           workspace_id=workspace_id, kind="check", max_tokens=220, background=background)
    raw = msg.get("content") or ""
    out = llm.parse_json(raw)
    result = out.get("result") if out.get("result") in RESULTS else "unsure"
    say = str(out.get("say") or raw).strip()[:800]
    reading = _reading(out.get("reading"))
    verified = None
    if spec and result not in ("danger",):
        v = await read_verified(models, jpeg, spec, roi, workspace_id=workspace_id, background=background,
                                hint=f"The step needs: {spec.get('label') or 'a reading'} in {spec.get('unit', '')}".strip())
        reading, verified = v["reading"], v["verified"]
        info = {**info, "ms": info["ms"] + v["ms"]}
        if reading and not verified and result == "pass":
            result, say = "unsure", (v["note"] or "I'm not sure of that reading.") + " Hold the camera steady on the display."
        if reading is None and result == "pass":
            result = "unsure"
    ok, why = judge_reading(step, reading)
    if result == "pass" and ok is False:
        if reading is None:
            result, say = "unsure", "I can't read the value yet. Point the camera at the display."
        else:
            result, say = "fail", f"Out of spec: {why}."
    return {"result": result, "say": say, "reading": reading, "verified": verified, "model": info["model"],
            "where": info["where"], "ms": info["ms"]}


async def coach(models: list[llm.Model], jpeg: bytes, goal: str, *, workspace_id: str = "") -> dict:
    content = [{"type": "text", "text": f"Their goal: {goal}"}, llm.image_part(shrink(jpeg, side_for(models)))]
    msg, info = await call(models, [{"role": "system", "content": COACH_PROMPT}, {"role": "user", "content": content}],
                           workspace_id=workspace_id, kind="coach", max_tokens=200, background=True)
    out = llm.parse_json(msg.get("content") or "")
    status = out.get("status") if out.get("status") in STATUSES else "ok"
    return {"speak": bool(out.get("speak")) and bool(out.get("say")), "say": str(out.get("say") or "")[:400],
            "status": status, "model": info["model"], "where": info["where"], "ms": info["ms"]}


async def watch_check(models: list[llm.Model], jpeg: bytes, condition: str, *, workspace_id: str = "",
                      background: bool = True) -> dict:
    content = [{"type": "text", "text": f"Condition: {condition}"}, llm.image_part(shrink(jpeg, min(768, side_for(models))))]
    msg, info = await call(models, [{"role": "system", "content": WATCH_PROMPT}, {"role": "user", "content": content}],
                           workspace_id=workspace_id, kind="watch", max_tokens=160, background=background)
    out = llm.parse_json(msg.get("content") or "")
    try:
        conf = max(0.0, min(1.0, float(out.get("confidence", 0.6))))
    except (TypeError, ValueError):
        conf = 0.6
    return {"match": out.get("match") is True, "say": str(out.get("say") or "")[:300], "confidence": conf,
            "model": info["model"], "where": info["where"], "ms": info["ms"]}


def clock(t: float, t0: float = 0) -> str:
    s = max(0, t - t0)
    return f"{int(s // 60)}:{int(s % 60):02d}"


REVIEW_MAX_CALLS = 10
REVIEW_BUDGET_S = {"cpu": 420.0, "gpu": 120.0}

SCAN_PROMPT = """You are skimming short slices of a camera recording, two frames per slice, each frame stamped with its time.
For each slice say whether anything worth a closer look happens: activity, a change, a mistake, a safety issue, a
reading. Reply as JSON: {"slices": [{"t": str, "flag": bool, "why": str}]}"""

GATE_PROMPT = """Check a draft review of a camera recording against the frames it cites. Keep only problems and timeline
entries you can actually see in the frames at those times; drop anything you can't confirm.
Reply as JSON: {"keep_problems": [int], "keep_timeline": [int], "summary": str}"""


async def review(models: list[llm.Model], frames: list[tuple[float, bytes]], question: str = "", context: str = "",
                 *, workspace_id: str = "", relative: bool = True, progress=None, facts: str = "") -> dict:
    """What happened across frames: retrieve -> perceive -> review.

    1. Retrieve: frames at scene changes (change energy), not evenly spaced.
    2. Scan: 6 slices x 2 low-res frames, each stamped with its time; the model flags slices worth a look.
    3. Perceive: up to 16 frames from the flagged windows, stamped, reviewed in batches of 4 ("t=01:15" anchored).
    4. Review gate: the draft is checked against the frames it cites before it's reported.
    Hard caps: 10 model calls and a wall-clock budget; at either cap the answer is forced from what's done."""
    if not frames:
        raise ValueError("no frames to review")
    t_start = time.perf_counter()
    budget = REVIEW_BUDGET_S["cpu" if side_for(models) == IMAGE_SIDE["cpu"] else "gpu"]
    calls = 0
    capped = ""
    t0 = frames[0][0] if relative else 0

    def out_of_budget() -> bool:
        nonlocal capped
        if calls >= REVIEW_MAX_CALLS:
            capped = "call cap"
        elif time.perf_counter() - t_start > budget:
            capped = "time budget"
        return bool(capped)

    async def tick(f: float, note: str):
        if progress:
            await progress(f, note)

    # 1. retrieve
    pool = perception.select_by_change(frames, 24)
    # 2. scan
    flagged: list[float] = []
    slices = [pool[i:i + 2] for i in range(0, len(pool), max(2, len(pool) // 6))][:6]
    if len(pool) > 8:
        content: list[dict] = [{"type": "text", "text": (f"They asked: {question}\n" if question else "") + "Slices:"}]
        for sl in slices:
            for t, f in sl:
                lab = f"t={clock(t, t0)}"
                content += [{"type": "text", "text": lab}, llm.image_part(perception.stamp(f, lab, 256, 60))]
        msg, _ = await call(models, [{"role": "system", "content": SCAN_PROMPT}, {"role": "user", "content": content}],
                            workspace_id=workspace_id, kind="scan", max_tokens=400, background="wait")
        calls += 1
        for sl in llm.parse_json(msg.get("content") or "").get("slices") or []:
            if isinstance(sl, dict) and sl.get("flag"):
                m = re.match(r"t?=?(\d+):(\d{2})", str(sl.get("t", "")))
                if m:
                    flagged.append(int(m.group(1)) * 60 + int(m.group(2)) + t0)
        await tick(0.15, f"scanned {len(slices)} slices, {len(flagged)} flagged")
    # 3. perceive: frames near flagged times first, then the rest of the pool, at most 16
    if flagged:
        near = sorted(pool, key=lambda tf: min(abs(tf[0] - ft) for ft in flagged))
        focus = sorted(near[:16], key=lambda tf: tf[0])
    else:
        focus = pool[:16] if len(pool) <= 16 else perception.select_by_change(pool, 16)
    parts: list[dict] = []
    groups = [focus[i:i + 4] for i in range(0, len(focus), 4)]
    for gi, group in enumerate(groups):
        if out_of_budget():
            break
        content = [{"type": "text", "text": (context + "\n" if context else "") + (f"Facts already observed:\n{facts}\n" if facts else "") +
                    (f"They asked: {question}\n" if question else "") +
                    "Frames: " + ", ".join(f"t={clock(t, t0)}" for t, _ in group)}]
        for t, f in group:
            lab = f"t={clock(t, t0)}"
            content += [{"type": "text", "text": f"Frame {lab}"}, llm.image_part(perception.stamp(f, lab, 448, 70))]
        msg, _info = await call(models, [{"role": "system", "content": REVIEW_PROMPT}, {"role": "user", "content": content}],
                                workspace_id=workspace_id, kind="review", max_tokens=700, background="wait")
        calls += 1
        parts.append(llm.parse_json(msg.get("content") or "") or {"summary": (msg.get("content") or "")[:600]})
        await tick(0.15 + 0.65 * (gi + 1) / len(groups), f"reviewed {min(len(focus), (gi + 1) * 4)} of {len(focus)} frames")
    timeline = [x for p in parts for x in (p.get("timeline") or []) if isinstance(x, dict)]
    readings = [x for p in parts for x in (p.get("readings") or []) if isinstance(x, dict)]
    problems = [x for p in parts for x in (p.get("problems") or []) if isinstance(x, dict)]
    summaries = [str(p.get("summary") or "").strip() for p in parts if p.get("summary")]
    summary = " ".join(summaries)
    gated = False
    # 4. review gate: confirm what's reported against the frames it cites
    if (problems or timeline) and not out_of_budget():
        cited = []
        for x in problems + timeline:
            m = re.match(r"t?=?(\d+):(\d{2})", str(x.get("t", "")))
            if m:
                tt = int(m.group(1)) * 60 + int(m.group(2)) + t0
                cited.append(min(focus, key=lambda tf: abs(tf[0] - tt)))
        cited = list({id(c): c for c in cited}.values())[:6]
        if cited:
            draft = "Problems:\n" + "\n".join(f"{i}. {p.get('t')} {p.get('what')}" for i, p in enumerate(problems)) + \
                    "\nTimeline:\n" + "\n".join(f"{i}. {x.get('t')} {x.get('what')}" for i, x in enumerate(timeline)) + \
                    f"\nSummary: {summary}"
            content = [{"type": "text", "text": draft}]
            for t, f in sorted(cited, key=lambda tf: tf[0]):
                lab = f"t={clock(t, t0)}"
                content += [{"type": "text", "text": f"Frame {lab}"}, llm.image_part(perception.stamp(f, lab, 448, 70))]
            try:
                msg, _ = await call(models, [{"role": "system", "content": GATE_PROMPT}, {"role": "user", "content": content}],
                                    workspace_id=workspace_id, kind="review-gate", max_tokens=400, background="wait")
                calls += 1
                g = llm.parse_json(msg.get("content") or "")
                if isinstance(g.get("keep_problems"), list):
                    problems = [p for i, p in enumerate(problems) if i in g["keep_problems"]]
                    gated = True
                if isinstance(g.get("keep_timeline"), list):
                    timeline = [x for i, x in enumerate(timeline) if i in g["keep_timeline"]] or timeline
                if g.get("summary"):
                    summary = str(g["summary"])
            except llm.ModelError:
                pass
    if len(summaries) > 1 and not gated:
        try:
            msg = await llm.chat_chain(llm.chain(None, "llm") or models, [{"role": "user", "content":
                    "Merge these notes about consecutive parts of one camera recording into one factual summary of "
                    "2-4 sentences" + (f" that answers: {question}" if question else "") + ":\n\n" +
                    "\n".join(f"- {x}" for x in summaries)}], max_tokens=300)
            summary = (msg.get("content") or summary).strip()
        except llm.ModelError:
            pass
    await tick(0.95, "writing it up")
    return {"summary": summary, "timeline": timeline, "readings": readings, "problems": problems,
            "frames": len(focus), "scanned": len(pool), "flagged": len(flagged), "calls": calls, "gated": gated,
            "capped": capped, "seconds": round(time.perf_counter() - t_start, 1),
            "span": clock(frames[-1][0], t0)}


# ================================================================ snapshots (evidence)
def save_snapshot(db, workspace_id: str, jpeg: bytes, name: str) -> str:
    """Keep a frame as evidence (a run step, a watch alert). Returns its file id, served at /api/v1/files/{id}."""
    from irris.db import new_id, now
    fid = new_id()
    small = shrink(jpeg, 1024, 80)
    db.execute("INSERT INTO files (id, workspace_id, name, mime, size, content, created) VALUES (?,?,?,?,?,?,?)",
               [fid, workspace_id, name[:120], "image/jpeg", len(small), small, now()])
    return fid


def sample_video(data: bytes, max_frames: int = 24) -> tuple[list[tuple[float, bytes]], float]:
    """Evenly spaced frames from a video file: [(seconds, jpeg)], duration. Needs PyAV (installed with Whisper)."""
    try:
        import av
    except ImportError:
        raise ValueError("video review needs PyAV on the server")
    try:
        c = av.open(io.BytesIO(data), mode="r")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"can't read that video ({type(e).__name__})") from e
    with c:
        vs = next((s for s in c.streams if s.type == "video"), None)
        if vs is None:
            raise ValueError("no video track in that file")
        frames: list[tuple[float, Any]] = []
        for f in c.decode(vs):
            frames.append((float(f.time or 0.0), f))
            if len(frames) > 2000:
                frames = frames[::2]
        if not frames:
            raise ValueError("the video has no frames")
    duration = max(t for t, _ in frames)
    n = min(max_frames, len(frames))
    idx = [round(i * (len(frames) - 1) / max(1, n - 1)) for i in range(n)] if n > 1 else [0]
    out = []
    for i in dict.fromkeys(idx):
        t, f = frames[i]
        buf = io.BytesIO()
        img = f.to_image().convert("RGB")
        img.thumbnail((640, 640))
        img.save(buf, "JPEG", quality=78)
        out.append((t, buf.getvalue()))
    return out, duration
