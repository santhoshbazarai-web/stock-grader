"""Broker access tokens, encrypted at rest with expiry (SPEC §3.3, AGENTS.md rule 8).

Plaintext tokens exist only in memory: they are encrypted before they reach the database
and are never logged.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.security import DecryptionError, TokenCipher
from app.db.enums import Broker
from app.db.models import BrokerToken
from app.db.upsert import upsert

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenStatus:
    broker: Broker
    connected: bool
    expires_at: datetime | None
    reason: str


class BrokerTokenStore:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        cipher: TokenCipher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._clock = clock

    def save(self, broker: Broker, access_token: str, expires_at: datetime) -> None:
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        session = self._session_factory()
        try:
            upsert(
                session,
                BrokerToken,
                [
                    {
                        "broker": broker,
                        "access_token_encrypted": self._cipher.encrypt(access_token),
                        "expires_at": expires_at,
                    }
                ],
            )
            session.commit()
        finally:
            session.close()
        logger.info("stored %s token (expires %s)", broker, expires_at.isoformat())

    def _row(self, broker: Broker) -> BrokerToken | None:
        session = self._session_factory()
        try:
            return session.get(BrokerToken, broker)
        finally:
            session.close()

    def get_valid(self, broker: Broker) -> str | None:
        """The decrypted token, or ``None`` if missing, expired or undecryptable."""
        row = self._row(broker)
        if row is None or row.expires_at <= self._clock():
            return None
        try:
            return self._cipher.decrypt(row.access_token_encrypted)
        except DecryptionError:
            logger.warning("%s token cannot be decrypted (FERNET_KEY changed?)", broker)
            return None

    def status(self, broker: Broker) -> TokenStatus:
        row = self._row(broker)
        if row is None:
            return TokenStatus(broker, False, None, "not connected")
        if row.expires_at <= self._clock():
            return TokenStatus(broker, False, row.expires_at, "token expired — reconnect")
        return TokenStatus(broker, True, row.expires_at, "connected")

    def delete(self, broker: Broker) -> None:
        session = self._session_factory()
        try:
            session.execute(delete(BrokerToken).where(BrokerToken.broker == broker))
            session.commit()
        finally:
            session.close()

    def all_statuses(self) -> list[TokenStatus]:
        return [self.status(b) for b in Broker]


__all__ = ["BrokerTokenStore", "TokenStatus"]
