"""Speech: listen (speech-to-text) and speak (text-to-speech).

Free defaults run on the server's CPU, like Iriis Voice: faster-whisper small.en for listening, Kokoro (ONNX) for
speaking with Piper as its fallback. A workspace may configure a paid provider for either; whenever that provider
fails, the free default takes over, so speech never stops working because a key expired.

Premium providers:
  stt  openai    any OpenAI-compatible /audio/transcriptions (OpenAI, Groq Whisper, a self-hosted server)
       deepgram  Deepgram /v1/listen
  tts  openai    any OpenAI-compatible /audio/speech (OpenAI, Groq Orpheus, a self-hosted server)
       elevenlabs ElevenLabs text-to-speech

Env: TTS_DEFAULT (kokoro | piper: which free voice goes first), STT_MODEL (faster-whisper model name or directory, default small.en), STT_THREADS, KOKORO_MODEL,
KOKORO_VOICES, KOKORO_VOICE (default af_heart), PIPER_MODEL, VOICE_MODELS_DIR (default ./models).
"""
from __future__ import annotations

import io
import os
import re
import threading
import time
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx

from irris.config import ROOT
from irris.db import Database, dumps, loads, now
from irris.security import decrypt, encrypt

MODELS_DIR = Path(os.environ.get("VOICE_MODELS_DIR") or ROOT / "models")
MAX_TTS_CHARS = 1200
STT_PROVIDERS = ("default", "openai", "deepgram")
TTS_PROVIDERS = ("default", "openai", "elevenlabs")
KOKORO_VOICES = {
    "af_heart": "American, warm (default)", "af_bella": "American, bright", "af_nicole": "American, soft",
    "af_sky": "American, light", "am_adam": "American, deep", "am_michael": "American, calm",
    "bf_emma": "British, clear", "bf_isabella": "British, warm", "bm_george": "British, steady",
    "bm_lewis": "British, low",
}


class VoiceError(Exception):
    pass


def _env_path(name: str, default: str) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else MODELS_DIR / default


# ================================================================ settings (per workspace)
PUBLIC_FIELDS = ("provider", "base_url", "model", "voice")


def get_settings(db: Database, workspace_id: str) -> dict:
    row = db.one("SELECT config FROM media_settings WHERE workspace_id = ?", [workspace_id])
    cfg = loads(row["config"] if row else "", {}) or {}
    cfg.setdefault("stt", {"provider": "default"})
    cfg.setdefault("tts", {"provider": "default", "voice": ""})
    return cfg


def public_settings(cfg: dict) -> dict:
    out = {}
    for kind in ("stt", "tts"):
        c = cfg.get(kind) or {}
        out[kind] = {k: c.get(k, "") for k in PUBLIC_FIELDS}
        out[kind]["provider"] = c.get("provider") or "default"
        out[kind]["has_key"] = bool(c.get("api_key"))
    out["speak_replies"] = bool(cfg.get("speak_replies", False))
    return out


def save_settings(db: Database, workspace_id: str, body: dict) -> dict:
    cfg = get_settings(db, workspace_id)
    for kind, allowed in (("stt", STT_PROVIDERS), ("tts", TTS_PROVIDERS)):
        new = body.get(kind)
        if not isinstance(new, dict):
            continue
        provider = new.get("provider") or "default"
        if provider not in allowed:
            raise VoiceError(f"{kind} provider must be one of {', '.join(allowed)}")
        cur = dict(cfg.get(kind) or {})
        if provider != cur.get("provider"):
            cur.pop("api_key", None)           # a key belongs to one provider
        cur["provider"] = provider
        for k in ("base_url", "model", "voice"):
            if k in new:
                cur[k] = str(new[k] or "").strip()[:300]
        if new.get("api_key"):
            cur["api_key"] = encrypt(str(new["api_key"]).strip())
        if provider == "default":
            cur = {"provider": "default", "voice": cur.get("voice", "") if kind == "tts" else ""}
        cfg[kind] = cur
    if "speak_replies" in body:
        cfg["speak_replies"] = bool(body["speak_replies"])
    t = now()
    if db.one("SELECT workspace_id FROM media_settings WHERE workspace_id = ?", [workspace_id]):
        db.execute("UPDATE media_settings SET config = ?, updated = ? WHERE workspace_id = ?", [dumps(cfg), t, workspace_id])
    else:
        db.execute("INSERT INTO media_settings (workspace_id, config, updated) VALUES (?,?,?)", [workspace_id, dumps(cfg), t])
    return cfg


# ================================================================ default engines (lazy, one instance each)
_lock = threading.Lock()
_engines: dict[str, Any] = {}
_failed: dict[str, str] = {}


def _load(name: str, factory):
    with _lock:
        if name in _engines:
            return _engines[name]
        if name in _failed:
            raise VoiceError(_failed[name])
        try:
            _engines[name] = factory()
        except Exception as e:  # noqa: BLE001 - a missing model or package disables that engine, with the reason
            _failed[name] = f"{name} unavailable: {type(e).__name__}: {str(e)[:160]}"
            raise VoiceError(_failed[name])
        return _engines[name]


def _whisper():
    from faster_whisper import WhisperModel
    raw = os.environ.get("STT_MODEL", "").strip()
    local = MODELS_DIR / "stt" / "faster-whisper-small.en"
    model = raw or (str(local) if local.exists() else "small.en")
    threads = int(os.environ.get("STT_THREADS", "0") or 0) or max(1, min(4, os.cpu_count() or 2))
    return WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)


def _kokoro():
    from kokoro_onnx import Kokoro
    model = _env_path("KOKORO_MODEL", "tts/kokoro-v1.0.int8.onnx")
    voices = _env_path("KOKORO_VOICES", "tts/voices-v1.0.bin")
    for p in (model, voices):
        if not p.exists():
            raise FileNotFoundError(f"{p} is missing")
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = int(os.environ.get("TTS_THREADS", "2"))   # leave the cores to the vision model
    opts.inter_op_num_threads = 1
    opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return Kokoro.from_session(ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"]), str(voices))


def _piper():
    from piper import PiperVoice
    model = _env_path("PIPER_MODEL", "tts/en_GB-alba-medium.onnx")
    if not model.exists():
        raise FileNotFoundError(f"{model} is missing")
    return PiperVoice.load(str(model))


def status() -> dict:
    """Which free engines are installed (models present), without loading them."""
    def has(mod: str) -> bool:
        import importlib.util
        return importlib.util.find_spec(mod) is not None
    stt_model = os.environ.get("STT_MODEL", "").strip()
    stt_local = MODELS_DIR / "stt" / "faster-whisper-small.en"
    return {
        "stt": {"whisper": has("faster_whisper") and (bool(stt_model) or stt_local.exists()),
                "loaded": "whisper" in _engines, "error": _failed.get("whisper", "")},
        "tts": {"kokoro": has("kokoro_onnx") and _env_path("KOKORO_MODEL", "tts/kokoro-v1.0.int8.onnx").exists(),
                "piper": has("piper") and _env_path("PIPER_MODEL", "tts/en_GB-alba-medium.onnx").exists(),
                "loaded": [k for k in ("kokoro", "piper") if k in _engines],
                "error": _failed.get("kokoro", "") or _failed.get("piper", "")},
    }


def warm() -> None:
    """Load the free engines in the background so the first request isn't slow."""
    for name, factory in (("kokoro", _kokoro), ("whisper", _whisper)):
        try:
            _load(name, factory)
        except VoiceError:
            pass


# ================================================================ speech-to-text
def decode_audio(audio: bytes, rate: int = 16000):
    """Any container the browser records (WebM/Opus, MP4/AAC, Ogg, WAV) -> mono float32 at `rate`.
    Done here, not by faster-whisper, whose decoder passes an argument newer PyAV no longer accepts."""
    import av
    import numpy as np
    out = []
    with av.open(io.BytesIO(audio), mode="r") as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("no audio track")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        for frame in container.decode(stream):
            for f in resampler.resample(frame):
                out.append(f.to_ndarray().reshape(-1))
        for f in resampler.resample(None):
            out.append(f.to_ndarray().reshape(-1))
    if not out:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(out).astype(np.float32) / 32768.0


def _local_stt(audio: bytes) -> str:
    model = _load("whisper", _whisper)
    samples = decode_audio(audio)
    if samples.size < 1600:              # under 0.1 s: nothing was said
        return ""
    segments, _info = model.transcribe(samples, language="en", beam_size=1, vad_filter=True,
                                       condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segments).strip()


async def _premium_stt(cfg: dict, audio: bytes, mime: str, filename: str) -> str:
    key = decrypt(cfg.get("api_key", ""))
    provider = cfg.get("provider")
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=6)) as client:
        if provider == "deepgram":
            model = cfg.get("model") or "nova-3"
            r = await client.post(f"https://api.deepgram.com/v1/listen?model={model}&smart_format=true",
                                  content=audio, headers={"Authorization": f"Token {key}", "Content-Type": mime})
            r.raise_for_status()
            alts = r.json()["results"]["channels"][0]["alternatives"]
            return (alts[0].get("transcript") or "").strip() if alts else ""
        base = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        r = await client.post(f"{base}/audio/transcriptions", headers={"Authorization": f"Bearer {key}"} if key else {},
                              files={"file": (filename, audio, mime)},
                              data={"model": cfg.get("model") or "whisper-1", "response_format": "json"})
        r.raise_for_status()
        return str(r.json().get("text") or "").strip()


async def transcribe(settings: dict, audio: bytes, mime: str = "audio/webm", filename: str = "speech.webm") -> dict:
    import asyncio
    if not audio:
        raise VoiceError("no audio")
    cfg = settings.get("stt") or {}
    t0 = time.perf_counter()
    notes = []
    if cfg.get("provider") in ("openai", "deepgram"):
        try:
            text = await _premium_stt(cfg, audio, mime, filename)
            return {"text": text, "engine": cfg["provider"], "ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as e:  # noqa: BLE001 - fall back to the free engine
            notes.append(f"{cfg['provider']} failed ({type(e).__name__}); used the free engine")
    try:
        text = await asyncio.to_thread(_local_stt, audio)
    except VoiceError:
        raise
    except Exception as e:  # noqa: BLE001 - undecodable audio
        raise VoiceError(f"couldn't transcribe that audio: {type(e).__name__}")
    return {"text": text, "engine": "whisper", "ms": int((time.perf_counter() - t0) * 1000),
            **({"note": notes[0]} if notes else {})}


# ================================================================ text-to-speech
def wav_bytes(samples, sample_rate: int) -> bytes:
    import numpy as np
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def clean_for_speech(text: str) -> str:
    """Markdown and citations read badly aloud."""
    text = re.sub(r"\[(\d+)\]", "", text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#>|]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:MAX_TTS_CHARS]


def _kokoro_say(text: str, voice: str) -> bytes:
    k = _load("kokoro", _kokoro)
    voice = voice if voice in KOKORO_VOICES else os.environ.get("KOKORO_VOICE", "af_heart")
    samples, sr = k.create(text, voice=voice, speed=1.0, lang="en-gb" if voice.startswith("b") else "en-us")
    return wav_bytes(samples, sr)


def _piper_say(text: str) -> bytes:
    import numpy as np
    v = _load("piper", _piper)
    chunks = [c.audio_float_array for c in v.synthesize(text)]
    return wav_bytes(np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32), v.config.sample_rate)


async def _premium_tts(cfg: dict, text: str, voice: str) -> tuple[bytes, str]:
    key = decrypt(cfg.get("api_key", ""))
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=6)) as client:
        if cfg.get("provider") == "elevenlabs":
            vid = voice or cfg.get("voice") or "21m00Tcm4TlvDq8ikWAM"
            r = await client.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=mp3_44100_128",
                                  headers={"xi-api-key": key},
                                  json={"text": text, "model_id": cfg.get("model") or "eleven_flash_v2_5"})
            r.raise_for_status()
            return r.content, "audio/mpeg"
        base = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        r = await client.post(f"{base}/audio/speech", headers={"Authorization": f"Bearer {key}"} if key else {},
                              json={"model": cfg.get("model") or "gpt-4o-mini-tts", "input": text,
                                    "voice": voice or cfg.get("voice") or "alloy", "response_format": "wav"})
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "audio/wav").split(";")[0]


async def synthesize(settings: dict, text: str, voice: str = "") -> dict:
    """{"audio": bytes, "mime", "engine", "ms"}; premium first (if set), then Kokoro, then Piper."""
    import asyncio
    text = clean_for_speech(text)
    if not text:
        raise VoiceError("nothing to say")
    cfg = settings.get("tts") or {}
    t0 = time.perf_counter()
    note = ""
    if cfg.get("provider") in ("openai", "elevenlabs"):
        try:
            audio, mime = await _premium_tts(cfg, text, voice)
            return {"audio": audio, "mime": mime, "engine": cfg["provider"], "ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as e:  # noqa: BLE001 - fall back to the free voices
            note = f"{cfg['provider']} failed ({type(e).__name__}); used the free voice"
            voice = ""
    default_voice = voice or (cfg.get("voice") if cfg.get("provider") in (None, "", "default") else "") or ""
    errors = []
    engines = [("kokoro", lambda: _kokoro_say(text, default_voice)), ("piper", lambda: _piper_say(text))]
    if os.environ.get("TTS_DEFAULT", "kokoro") == "piper":
        engines.reverse()
    for name, fn in engines:
        key = (name, default_voice if name == "kokoro" else "", text)
        if key in _tts_cache:
            _tts_cache.move_to_end(key)
            out = {"audio": _tts_cache[key], "mime": "audio/wav", "engine": name, "ms": 0, "cached": True}
            if note:
                out["note"] = note
            return out
        try:
            audio = await asyncio.to_thread(fn)
            _tts_cache[key] = audio
            while len(_tts_cache) > TTS_CACHE:
                _tts_cache.popitem(last=False)
            out = {"audio": audio, "mime": "audio/wav", "engine": name, "ms": int((time.perf_counter() - t0) * 1000)}
            if note:
                out["note"] = note
            return out
        except VoiceError as e:
            errors.append(str(e))
    raise VoiceError("no voice is available on the server: " + "; ".join(errors))


_tts_cache: "OrderedDict[tuple, bytes]" = OrderedDict()       # step instructions and verdicts repeat a lot
TTS_CACHE = 96


def voices(settings: dict) -> list[dict]:
    out = [{"id": k, "label": v, "engine": "kokoro"} for k, v in KOKORO_VOICES.items()]
    cfg = settings.get("tts") or {}
    if cfg.get("provider") in ("openai", "elevenlabs") and cfg.get("voice"):
        out.insert(0, {"id": cfg["voice"], "label": f"{cfg['provider']} voice", "engine": cfg["provider"]})
    return out
