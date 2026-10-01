"""Optional Telegram delivery (Bot API ``sendMessage``).

Configured with ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``. The token is part of the
request URL, so it must never reach logs or stored errors: failures are reported as
``"<ExceptionType>"`` or ``"HTTP <status>: <telegram description>"`` only.
"""

import logging
from dataclasses import dataclass

import requests

from app.core.settings import Settings

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"


@dataclass(frozen=True)
class Delivery:
    status: str  # sent | failed | disabled
    error: str | None = None


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout_s: float,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout_s
        self._http = session or requests.Session()

    def __repr__(self) -> str:  # never expose the token
        return f"TelegramNotifier(chat_id={self._chat_id!r}, token=<redacted>)"

    def send(self, text: str) -> Delivery:
        url = f"{API}/bot{self._token}/sendMessage"
        try:
            resp = self._http.post(
                url,
                json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            error = type(exc).__name__  # the message would contain the URL (and the token)
            logger.warning("Telegram delivery failed: %s", error)
            return Delivery("failed", error)
        if resp.status_code != 200:
            try:
                desc = str(resp.json().get("description", ""))[:200]
            except ValueError:
                desc = ""
            error = f"HTTP {resp.status_code}: {desc}".rstrip(": ")
            logger.warning("Telegram delivery failed: %s", error)
            return Delivery("failed", error)
        return Delivery("sent")


def build_notifier(settings: Settings, timeout_s: float) -> TelegramNotifier | None:
    """``None`` unless both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
    if settings.telegram_bot_token is None or not settings.telegram_chat_id:
        return None
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        return None
    return TelegramNotifier(token, settings.telegram_chat_id, timeout_s=timeout_s)
