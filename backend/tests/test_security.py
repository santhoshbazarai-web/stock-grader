import pytest
from cryptography.fernet import Fernet

from app.core.security import DecryptionError, TokenCipher, get_cipher


def test_roundtrip() -> None:
    cipher = TokenCipher(Fernet.generate_key())
    ct = cipher.encrypt("access-token-123")
    assert b"access-token-123" not in ct
    assert cipher.decrypt(ct) == "access-token-123"


def test_ciphertext_is_randomised() -> None:
    cipher = TokenCipher(Fernet.generate_key())
    assert cipher.encrypt("t") != cipher.encrypt("t")


def test_wrong_key_fails() -> None:
    ct = TokenCipher(Fernet.generate_key()).encrypt("secret")
    with pytest.raises(DecryptionError) as exc:
        TokenCipher(Fernet.generate_key()).decrypt(ct)
    assert "secret" not in str(exc.value)


def test_tampered_ciphertext_fails() -> None:
    cipher = TokenCipher(Fernet.generate_key())
    ct = bytearray(cipher.encrypt("secret"))
    ct[-5] ^= 0x01
    with pytest.raises(DecryptionError):
        cipher.decrypt(bytes(ct))


def test_empty_token_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        TokenCipher(Fernet.generate_key()).encrypt("")


def test_repr_redacts_key() -> None:
    key = Fernet.generate_key()
    assert key.decode() not in repr(TokenCipher(key))


def test_get_cipher_uses_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key()
    monkeypatch.setenv("FERNET_KEY", key.decode())
    from app.core.settings import get_settings

    get_settings.cache_clear()
    get_cipher.cache_clear()
    ct = get_cipher().encrypt("tok")
    assert TokenCipher(key).decrypt(ct) == "tok"
