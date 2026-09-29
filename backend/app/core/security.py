"""Symmetric encryption for broker access tokens at rest (AGENTS.md rule 8).

Tokens are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using ``FERNET_KEY`` from the
environment. Plaintext tokens must never be logged; ``DecryptionError`` messages never include
token material.
"""

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
