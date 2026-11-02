"""Settings, all from the environment (a .env file in the project root is read too)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'irris.db'}"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY"))
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL"))
    # The reasoning model: any OpenAI-compatible chat API with tool calling (DeepSeek by default).
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "https://api.deepseek.com"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY") or _env("DEEPSEEK_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "deepseek-chat"))
    # The eyes: any OpenAI-compatible chat API that takes images (Ollama, vLLM, OpenRouter, ...).
    vlm_base_url: str = field(default_factory=lambda: _env("VLM_BASE_URL"))
    vlm_api_key: str = field(default_factory=lambda: _env("VLM_API_KEY"))
    vlm_model: str = field(default_factory=lambda: _env("VLM_MODEL", "qwen3.5:2b"))
    # Fallbacks when the primary model can't be reached. In the Docker image a CPU Ollama runs next to the app
    # (LOCAL_OLLAMA=1), so vision keeps working without a GPU, just slower.
    vlm_fallback_base_url: str = field(default_factory=lambda: _env(
        "VLM_FALLBACK_BASE_URL", "http://127.0.0.1:11434/v1" if _env("LOCAL_OLLAMA") == "1" else ""))
    vlm_fallback_model: str = field(default_factory=lambda: _env("VLM_FALLBACK_MODEL", "qwen3.5:2b"))
    llm_fallback_base_url: str = field(default_factory=lambda: _env("LLM_FALLBACK_BASE_URL"))
    llm_fallback_api_key: str = field(default_factory=lambda: _env("LLM_FALLBACK_API_KEY"))
    llm_fallback_model: str = field(default_factory=lambda: _env("LLM_FALLBACK_MODEL"))
    max_upload_mb: int = field(default_factory=lambda: int(_env("MAX_UPLOAD_MB", "50")))
    cookie_secure: bool = field(default_factory=lambda: _env("COOKIE_SECURE", "auto") != "false")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgres://", "postgresql://"))


settings = Settings()
