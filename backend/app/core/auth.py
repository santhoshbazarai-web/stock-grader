"""Single-user authentication (SPEC §8): one password (``APP_PASSWORD``), a signed session.

- ``POST /api/auth/login`` checks the password in constant time and issues a session token:
  a signed, expiring ``<ts>.<nonce>.<hmac>`` (see :class:`app.core.security.StateSigner`). It is
  set as an HttpOnly ``SameSite=Lax`` cookie and also returned for API clients, which send it
  as ``Authorization: Bearer <token>``.
- The signing key is derived from ``FERNET_KEY`` *and* ``APP_PASSWORD``, so changing the password
  invalidates every existing session. Nothing is stored server-side.
- Failed logins are counted per client IP in Redis; ``LOGIN_MAX_FAILURES`` failures lock that IP
  out for ``LOGIN_LOCKOUT_S`` seconds (HTTP 429).
- The password and tokens are never logged.
"""

import hashlib
import hmac
import ipaddress
import time
from collections.abc import Callable
from functools import lru_cache

from redis import Redis

from app.core.security import InvalidStateError, StateSigner
from app.core.settings import Settings, get_settings

SESSION_COOKIE = "sg_session"
SESSION_PURPOSE = "session"


class SessionSigner:
    def __init__(self, settings: Settings, *, clock: Callable[[], float] = time.time) -> None:
        pw_hash = hashlib.sha256(settings.app_password.get_secret_value().encode()).hexdigest()
        secret = f"{settings.fernet_key.get_secret_value()}\x00session\x00{pw_hash}"
        self._signer = StateSigner(secret, clock=clock)
        self._max_age_s = settings.session_ttl_hours * 3600

    @property
    def max_age_s(self) -> int:
        return int(self._max_age_s)

    def issue(self) -> str:
        return self._signer.issue(SESSION_PURPOSE)

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            self._signer.verify(token, SESSION_PURPOSE, max_age_s=self._max_age_s)
        except InvalidStateError:
            return False
        return True


@lru_cache
def get_session_signer() -> SessionSigner:
    return SessionSigner(get_settings())


def password_ok(candidate: str, settings: Settings) -> bool:
    expected = settings.app_password.get_secret_value().encode()
    return hmac.compare_digest(candidate.encode(), expected)


class LoginThrottle:
    """Per-IP failed-login counter with a fixed lockout window."""

    def __init__(self, redis: Redis, *, max_failures: int, lockout_s: int) -> None:
        self._redis = redis
        self._max = max_failures
        self._lockout_s = lockout_s

    @staticmethod
    def _key(client: str) -> str:
        return f"auth:fail:{client}"

    def locked_for(self, client: str) -> int:
        """Seconds until ``client`` may try again (0 = not locked)."""
        count = self._redis.get(self._key(client))
        if count is None or int(count) < self._max:
            return 0
        return max(int(self._redis.ttl(self._key(client))), 1)

    def failure(self, client: str) -> None:
        key = self._key(client)
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, self._lockout_s)
        pipe.execute()

    def success(self, client: str) -> None:
        self._redis.delete(self._key(client))


def _networks(entries: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(e.strip(), strict=False) for e in entries if e.strip()]


def client_ip(peer: str | None, forwarded_for: str | None, trusted: list[str]) -> str:
    """The client address for per-IP throttling.

    Requests reach the API through the web app's /api proxy (and in production a TLS proxy),
    so the direct peer is usually a proxy. ``X-Forwarded-For`` is only believed when the peer
    is a trusted proxy; then the right-most address that is not itself a trusted proxy is the
    client (anything to its left could have been supplied by the client).
    """
    if not peer:
        return "unknown"
    nets = _networks(trusted)

    def is_trusted(addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(ip in n for n in nets)

    if not forwarded_for or not is_trusted(peer):
        return peer
    for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
        if not is_trusted(hop):
            return hop
    return peer
