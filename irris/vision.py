"""Cameras and the vision model.

Phone cameras stream JPEG frames over a WebSocket into CameraHub (latest frame per camera, in memory). External
cameras are read on demand: an HTTP snapshot URL, an MJPEG stream (the first frame is taken) or, if OpenCV is
installed, RTSP. The vision model is any OpenAI-compatible chat API that accepts images.
"""
from __future__ import annotations

import asyncio
import io
import time

import httpx

from irris import llm

FRESH_SECONDS = 10
STATUSES = {"ok", "warning", "danger", "unsure"}
RESULTS = {"pass", "fail", "unsure", "danger"}


class CameraHub:
    def __init__(self):
        self._frames: dict[str, tuple[bytes, float]] = {}
        self.viewers: dict[str, int] = {}

    def put(self, camera_id: str, jpeg: bytes) -> None:
        self._frames[camera_id] = (jpeg, time.time())

    def get(self, camera_id: str, max_age: float = FRESH_SECONDS) -> bytes | None:
        item = self._frames.get(camera_id)
        if not item or time.time() - item[1] > max_age:
            return None
        return item[0]

    def age(self, camera_id: str) -> float | None:
        item = self._frames.get(camera_id)
        return None if not item else round(time.time() - item[1], 1)


hub = CameraHub()


def shrink(jpeg: bytes, max_side: int = 1024) -> bytes:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg))
        if max(img.size) <= max_side and img.format == "JPEG":
            return jpeg
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=82)
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


def _reading(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    try:
        return {"value": float(value["value"]), "unit": str(value.get("unit") or ""),
                "confidence": max(0.0, min(1.0, float(value.get("confidence", 0.5))))}
    except (KeyError, TypeError, ValueError):
        return None


async def look(models: list[llm.Model], jpeg: bytes, question: str, context: str = "") -> dict:
    content = [{"type": "text", "text": (context + "\n\n" if context else "") + f"Question: {question}"},
               llm.image_part(shrink(jpeg))]
    msg = await llm.chat_chain(models, [{"role": "system", "content": LOOK_PROMPT}, {"role": "user", "content": content}],
                         max_tokens=400, timeout=180)
    raw = msg.get("content") or ""
    out = llm.parse_json(raw)
    status = out.get("status") if out.get("status") in STATUSES else "unsure"
    return {"say": str(out.get("say") or raw).strip()[:1200], "status": status, "reading": _reading(out.get("reading")),
            "model": msg.get("_model", "")}


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


async def check_step(models: list[llm.Model], jpeg: bytes, step: dict, index: int, total: int, knowledge: str = "") -> dict:
    content = [{"type": "text", "text": step_context(step, index, total, knowledge)}, llm.image_part(shrink(jpeg))]
    msg = await llm.chat_chain(models, [{"role": "system", "content": CHECK_PROMPT}, {"role": "user", "content": content}],
                         max_tokens=400, timeout=180)
    raw = msg.get("content") or ""
    out = llm.parse_json(raw)
    result = out.get("result") if out.get("result") in RESULTS else "unsure"
    say = str(out.get("say") or raw).strip()[:800]
    reading = _reading(out.get("reading"))
    ok, why = judge_reading(step, reading)
    if result == "pass" and ok is False:
        if reading is None:
            result, say = "unsure", "I can't read the value yet. Point the camera at the display."
        else:
            result, say = "fail", f"Out of spec: {why}."
    return {"result": result, "say": say, "reading": reading, "model": msg.get("_model", "")}
