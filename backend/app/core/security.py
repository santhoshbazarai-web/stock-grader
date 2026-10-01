"""Symmetric encryption for broker access tokens at rest (AGENTS.md rule 8).

Tokens are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using ``FERNET_KEY`` from the
environment. Plaintext tokens must never be logged; ``DecryptionError`` messages never include
token material.
"""

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.settings import get_settings


class DecryptionError(Exception):
    """Ciphertext was tampered with, truncated, or encrypted under a different key."""


class TokenCipher:
    def __init__(self, key: str | bytes) -> None:
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> bytes:
        if not plaintext:
            raise ValueError("refusing to encrypt an empty token")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            raise DecryptionError(
                "could not decrypt token (wrong FERNET_KEY or corrupt data)"
            ) from None

    def __repr__(self) -> str:  # never expose the key
        return "TokenCipher(<redacted>)"


@lru_cache
def get_cipher() -> TokenCipher:
    return TokenCipher(get_settings().fernet_key.get_secret_value())


# ───────────────────────── OAuth state (CSRF) ─────────────────────────


class InvalidStateError(Exception):
    """OAuth ``state`` is forged, for another purpose, or expired."""


class StateSigner:
    """Stateless, signed, expiring OAuth ``state`` values: ``<ts>.<nonce>.<hmac>``.

    The HMAC key is derived from ``FERNET_KEY`` with a purpose label, so it is never the
    encryption key itself.
    """

    def __init__(self, secret: str, *, clock: Callable[[], float] = time.time) -> None:
        self._key = hashlib.sha256(b"oauth-state:" + secret.encode()).digest()
        self._clock = clock

    def _mac(self, purpose: str, ts: str, nonce: str) -> str:
        msg = f"{purpose}.{ts}.{nonce}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()[:32]

    def issue(self, purpose: str) -> str:
        ts = str(int(self._clock()))
        nonce = secrets.token_urlsafe(16)
        return f"{ts}.{nonce}.{self._mac(purpose, ts, nonce)}"

    def verify(self, state: str, purpose: str, *, max_age_s: float) -> None:
        try:
            ts, nonce, mac = state.split(".")
            issued = int(ts)
        except ValueError:
            raise InvalidStateError("malformed state") from None
        if not hmac.compare_digest(mac, self._mac(purpose, ts, nonce)):
            raise InvalidStateError("state signature mismatch")
        age = self._clock() - issued
        if age < 0 or age > max_age_s:
            raise InvalidStateError("state expired")


@lru_cache
def get_state_signer() -> StateSigner:
    return StateSigner(get_settings().fernet_key.get_secret_value())
