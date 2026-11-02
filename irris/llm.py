"""Model clients: any OpenAI-compatible chat API. A workspace may bring its own models; otherwise the defaults."""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass

import httpx

from irris.config import settings
from irris.db import loads
from irris.security import decrypt


class ModelError(Exception):
    pass


@dataclass
class Model:
    base_url: str
    api_key: str
    model: str

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.model)


def resolve(workspace: dict | None, kind: str) -> Model:
    """kind = "llm" (reasoning) or "vlm" (vision). Workspace settings win over the server defaults."""
    own = (loads(workspace.get("models"), {}) if workspace else {}).get(kind) or {}
    if kind == "llm":
        base, key, model = settings.llm_base_url, settings.llm_api_key, settings.llm_model
    else:
        base, key, model = settings.vlm_base_url, settings.vlm_api_key, settings.vlm_model
    if own.get("base_url"):
        base, key = own["base_url"], decrypt(own.get("api_key", ""))
    if own.get("model"):
        model = own["model"]
    if kind == "llm" and not key and "deepseek.com" in base:
        base = ""      # the default API needs a key; without one, the assistant answers from search alone
    return Model(base.rstrip("/"), key, model)


def fallback(kind: str) -> Model:
    """The server's own model, used when the primary one (e.g. a GPU box) is down: the CPU Ollama in the container."""
    if kind == "vlm":
        return Model(settings.vlm_fallback_base_url.rstrip("/"), "", settings.vlm_fallback_model)
    return Model(settings.llm_fallback_base_url.rstrip("/"), settings.llm_fallback_api_key, settings.llm_fallback_model)


def chain(workspace: dict | None, kind: str) -> list[Model]:
    """Models to try in order: the workspace's or the server's primary model, then the server's fallback."""
    out = [m for m in (resolve(workspace, kind), fallback(kind)) if m.ready]
    return [m for i, m in enumerate(out) if m.base_url not in {o.base_url for o in out[:i]}]


def available(workspace: dict | None, kind: str) -> bool:
    return bool(chain(workspace, kind))


_down: dict[str, float] = {}       # base_url -> time until which it's skipped
DOWN_SECONDS = 60


async def chat_chain(models: list[Model], messages: list[dict], tools: list[dict] | None = None, **kw) -> dict:
    """chat() on the first model that answers. A model that can't be reached is skipped for a minute."""
    if not models:
        raise ModelError("no model is configured")
    errors = []
    for i, m in enumerate(models):
        last = i == len(models) - 1
        if not last and _down.get(m.base_url, 0) > time.time():
            continue
        try:
            msg = await chat(m, messages, tools, **kw)
            _down.pop(m.base_url, None)
            msg["_model"] = m.model
            return msg
        except ModelError as e:
            errors.append(f"{m.model}: {e}")
            if "unreachable" in str(e) or " 5" in str(e)[:16]:
                _down[m.base_url] = time.time() + DOWN_SECONDS
    raise ModelError("; ".join(errors))


def _url(base: str) -> str:
    return base + ("/chat/completions" if re.search(r"/v\d+$", base) else "/v1/chat/completions")


async def chat(m: Model, messages: list[dict], tools: list[dict] | None = None, *, json_mode: bool = False,
               max_tokens: int = 1200, temperature: float = 0.2, timeout: float = 120) -> dict:
    if not m.ready:
        raise ModelError("no model is configured")
    body: dict = {"model": m.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {m.api_key}"} if m.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5)) as client:
            r = await client.post(_url(m.base_url), json=body, headers=headers)
    except httpx.HTTPError as e:
        raise ModelError(f"model unreachable: {type(e).__name__}")
    if r.status_code >= 400:
        raise ModelError(f"model error {r.status_code}: {r.text[:300]}")
    try:
        return r.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError):
        raise ModelError("model returned an unexpected response")


def image_part(jpeg: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}


def parse_json(text: str) -> dict:
    """Small models wrap JSON in prose or code fences; take the first object we can parse."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                value = json.loads(m.group(0))
                return value if isinstance(value, dict) else {}
            except ValueError:
                pass
    return {}
