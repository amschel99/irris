"""Passwords, tokens and encryption of stored secrets (database connection strings, model API keys)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from irris.config import ROOT, settings


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def token(prefix: str = "", nbytes: int = 24) -> str:
    return prefix + secrets.token_urlsafe(nbytes)


def sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _secret_key() -> bytes:
    key = settings.secret_key
    if not key:
        if settings.is_postgres:
            raise RuntimeError("SECRET_KEY must be set when using Postgres (stored secrets are encrypted with it)")
        path = ROOT / "data" / "secret.key"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        key = path.read_text(encoding="utf-8").strip()
    return base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())


def encrypt(plain: str) -> str:
    return Fernet(_secret_key()).encrypt(plain.encode()).decode() if plain else ""


def decrypt(cipher: str) -> str:
    if not cipher:
        return ""
    try:
        return Fernet(_secret_key()).decrypt(cipher.encode()).decode()
    except InvalidToken:
        return ""
