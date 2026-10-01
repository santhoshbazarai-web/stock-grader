"""Read-only Telegram bot (SPEC §3.10, P23): ``/grade SYMBOL``, ``/buyzone``, ``/status``,
``/help``.

- **Outbound only.** The bot long-polls ``getUpdates`` (one HTTPS request open at a time, at most
  ``telegram_bot.poll_timeout_s``); no webhook, no public endpoint, nothing listens.
- **Allow-listed.** Only messages from ``TELEGRAM_CHAT_ID`` (the owner) are answered. Anything
  else is dropped without a reply, so the bot does not reveal that it exists. Commands older
  than ``max_message_age_s`` (sent while the bot was down) are skipped.
- **Read-only.** Answers come from stored reports, job runs and token status; the bot never
  starts a pipeline, places anything at a broker or changes data.
- **One poller.** A Redis lock keeps two workers from polling the same bot (Telegram answers a
  second poller with 409). The update offset is kept in Redis, so a restart does not answer
  the same message twice; a heartbeat (``telegram:bot:status``) feeds the Settings page.
- **Token hygiene.** The bot token is part of every request URL, so errors are reported as the
  exception type or Telegram's own description only, never the URL.

Formatting is pure (``format_*``); ``answer`` reads the database.
"""

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests
from redis import Redis
from redis.exceptions import LockError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts.telegram import API
from app.core.config import AppConfig, TelegramBotConfig
from app.core.security import get_cipher
from app.core.settings import Settings
from app.data.broker_tokens import BrokerTokenStore, broker_configured
from app.data.search import search
from app.db.enums import Broker, PipelineStatus
from app.db.models import DataGap, Instrument, JobRun, Notification, PipelineRun, PriceDaily
from app.reports.service import latest_payloads
from app.technical.buy_zone import distance_to_buy_zone

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
OFFSET_KEY = "telegram:bot:offset"
STATUS_KEY = "telegram:bot:status"
LOCK_KEY = "telegram:bot:lock"
GRADE_ORDER = {"A_plus": 0, "A": 1, "B": 2, "C": 3, "D": 4}
HELP = (
    "Stock Grader (read-only)\n"
    "/grade SYMBOL — grade, action, zone, FV and buy zone from the latest report "
    "(symbol, BSE code or company name)\n"
    "/buyzone — stocks in their buy zone or just above it\n"
    "/status — data freshness, job runs, broker tokens\n"
    "/help — this message"
)


class TelegramApiError(Exception):
    """A failed Bot API call. The message never contains the request URL (it holds the token)."""

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict  # 409: a webhook is set or another poller is running


class BotApi(Protocol):
    def get_updates(self, offset: int | None, timeout_s: int) -> list[dict[str, Any]]: ...

    def send_message(self, chat_id: str, text: str) -> None: ...


class TelegramBotApi:
    """The two Bot API methods the bot uses (``getUpdates``, ``sendMessage``)."""

    def __init__(self, token: str, *, timeout_s: float,
                 session: requests.Session | None = None) -> None:  # fmt: skip
        self._token = token
        self._timeout = timeout_s
        self._http = session or requests.Session()

    def __repr__(self) -> str:  # never expose the token
        return "TelegramBotApi(token=<redacted>)"

    def _call(self, method: str, payload: dict[str, Any], timeout: float) -> Any:
        try:
            resp = self._http.post(f"{API}/bot{self._token}/{method}", json=payload,
                                   timeout=timeout)  # fmt: skip
        except requests.RequestException as exc:
            raise TelegramApiError(f"{method}: {type(exc).__name__}") from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not body.get("ok"):
            desc = str(body.get("description", ""))[:200]
            raise TelegramApiError(f"{method}: HTTP {resp.status_code} {desc}".strip(),
                                   conflict=resp.status_code == 409)  # fmt: skip
        return body.get("result")

    def get_updates(self, offset: int | None, timeout_s: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout_s, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout_s + self._timeout)
        return [u for u in result or [] if isinstance(u, dict)]

    def send_message(self, chat_id: str, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": True}, self._timeout)  # fmt: skip


# ───────────────────────── parsing + formatting (pure) ─────────────────────────


@dataclass(frozen=True)
class Command:
    name: str  # grade | buyzone | status | help | start | unknown
    args: str


_COMMAND = re.compile(r"^/([A-Za-z_]+)(?:@\w+)?(?:\s+(.*))?$", re.DOTALL)


def parse_command(text: str | None) -> Command | None:
    """``/grade@MyBot hdfc bank`` → Command("grade", "hdfc bank"); plain text is not a command."""
    m = _COMMAND.match((text or "").strip())
    if m is None:
        return None
    return Command(m.group(1).lower(), " ".join((m.group(2) or "").split()))


def _inr(v: Any) -> str:
    return f"₹{v:,.0f}" if isinstance(v, int | float) else "n/a"


def _words(v: str | None) -> str:
    if not v:
        return "n/a"
    return " ".join(w if w in ("on", "of") else w.capitalize() for w in v.split("_"))


def format_grade(p: dict[str, Any]) -> str:
    lv = p.get("levels") or {}
    bz = p.get("buy_zone") or {}
    name = f" · {p['name']}" if p.get("name") else ""
    lines = [
        f"{p['symbol']}{name}",
        f"Grade {p.get('grade_label') or p.get('grade') or 'n/a'} · {_words(p.get('action'))} · "
        f"zone {_words(p.get('zone'))}",
        f"CMP {_inr(p.get('cmp'))} · FV {_inr(lv.get('fair_value'))}"
        + (f" (confidence {lv['confidence']})" if lv.get("confidence") else ""),
        f"Baseline {_inr(lv.get('baseline'))} · Top band {_inr(lv.get('top_band'))}",
    ]
    if bz.get("status") == "zone":
        dist = distance_to_buy_zone(p["cmp"], bz.get("low"), bz.get("high"))
        where = "in the zone" if dist == 0 else (f"{dist:.1%} above" if dist and dist > 0
                                                 else f"{-(dist or 0):.1%} below")  # fmt: skip
        lines.append(f"Buy zone {_inr(bz.get('low'))} to {_inr(bz.get('high'))} ({where})"
                     + (f" · invalidation {_inr(p.get('invalidation'))}"
                        if p.get("invalidation") else ""))  # fmt: skip
    else:
        reason = (bz.get("reasons") or ["none"])[0]
        lines.append(f"Buy zone: none ({reason})")
    flags = len(p.get("red_flags") or [])
    issues = len(p.get("reconciliation_issues") or [])
    if flags or issues:
        lines.append("⚠ " + " · ".join(x for x in (
            f"{flags} red flag(s)" if flags else "",
            f"{issues} source difference(s)" if issues else "") if x))  # fmt: skip
    lines.append(f"Report as of {p.get('as_of')}")
    return "\n".join(lines)


@dataclass(frozen=True)
class BuyZoneRow:
    symbol: str
    grade: str | None
    grade_label: str | None
    action: str | None
    cmp: float
    low: float
    high: float
    distance: float  # 0 inside, > 0 above the zone (fraction of CMP)


def buyzone_rows(payloads: list[dict[str, Any]], near_pct: float) -> list[BuyZoneRow]:
    """Stocks inside their buy zone or at most ``near_pct`` above it: inside first, then by
    grade, then distance."""
    rows = []
    for p in payloads:
        bz = p.get("buy_zone") or {}
        if bz.get("status") != "zone" or bz.get("low") is None or bz.get("high") is None:
            continue
        d = distance_to_buy_zone(p["cmp"], bz["low"], bz["high"])
        if d is None or d < 0 or d > near_pct:
            continue
        rows.append(BuyZoneRow(p["symbol"], p.get("grade"), p.get("grade_label"), p.get("action"),
                               p["cmp"], bz["low"], bz["high"], d))  # fmt: skip
    return sorted(rows, key=lambda r: (r.distance > 0, GRADE_ORDER.get(r.grade or "", 9),
                                       r.distance, r.symbol))  # fmt: skip


def format_buyzone(rows: list[BuyZoneRow], *, near_pct: float, limit: int) -> str:
    if not rows:
        return f"No stock is in its buy zone or within {near_pct:.0%} above it."
    lines = [f"In or within {near_pct:.0%} of the buy zone ({len(rows)}):"]
    for r in rows[:limit]:
        where = "in zone" if r.distance == 0 else f"{r.distance:.1%} above"
        lines.append(
            f"{r.symbol} {r.grade_label or r.grade or '-'} · {where} "
            f"{_inr(r.low)} to {_inr(r.high)} · CMP {_inr(r.cmp)} · {_words(r.action)}"
        )
    if len(rows) > limit:
        lines.append(f"…and {len(rows) - limit} more in the app's screener")
    return "\n".join(lines)


def _ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    s = max(0, int((now - when).total_seconds()))
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def format_status(st: dict[str, Any], now: datetime) -> str:
    lines = [f"Status at {now.astimezone(IST):%d %b %H:%M} IST"]
    lines.append(f"Prices to {st['last_price_date'] or 'none'} · {st['reports']} reports "
                 f"(latest {st['last_report_date'] or 'none'})")  # fmt: skip
    for name, run in st["jobs"].items():
        if run is None:
            lines.append(f"{name}: never run")
        else:
            lines.append(f"{name}: {run['status']} {_ago(run['at'], now)}"
                         + (f" ({run['error']})" if run.get("error") else ""))  # fmt: skip
    for broker, reason in st["brokers"].items():
        lines.append(f"{broker}: {reason}")
    lines.append(f"Open data gaps {st['open_gaps']} · pipeline runs queued {st['queued_runs']} "
                 f"· unread notifications {st['unread']}")  # fmt: skip
    return "\n".join(lines)


def clip(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


# ───────────────────────── answers (database reads) ─────────────────────────


def _grade(session: Session, query: str, config: AppConfig) -> str:
    if not query:
        return "Usage: /grade SYMBOL (e.g. /grade TCS, /grade hdfc bank, /grade 500180)"
    sym = query.strip().upper()
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == sym,
                                                     Instrument.is_index.is_(False)))  # fmt: skip
    if iid is None:
        hits = search(session, query, cfg=config.providers.symbols.search,
                      universe_index=config.jobs.universe_index, limit=3)  # fmt: skip
        listed = [h for h in hits if h.symbol]
        if not listed:
            return f"No NSE stock matches “{query}”."
        sym = listed[0].symbol or sym
        iid = session.scalar(select(Instrument.id).where(Instrument.symbol == sym))
    payload = latest_payloads(session, [iid]).get(iid) if iid is not None else None
    if payload is None:
        return (f"{sym}: no report yet. Open /stocks/{sym} in the app to build one "
                "(the bot is read-only).")  # fmt: skip
    return format_grade(payload)


def _status(session: Session, config: AppConfig, settings: Settings, now: datetime) -> str:
    jobs: dict[str, dict[str, Any] | None] = {}
    for name in config.jobs.telegram_bot.status_jobs:
        run = session.execute(
            select(JobRun.status, JobRun.finished_at, JobRun.started_at, JobRun.error)
            .where(JobRun.job_name == name).order_by(JobRun.started_at.desc()).limit(1)
        ).first()  # fmt: skip
        jobs[name] = None if run is None else {
            "status": run.status.value, "at": run.finished_at or run.started_at,
            "error": (run.error or "")[:80] or None}  # fmt: skip
    # its own short sessions: the store closes the sessions it opens
    store = BrokerTokenStore(lambda: Session(bind=session.get_bind()), get_cipher())
    brokers = {}
    for b in Broker:
        if not config.providers.brokers[b].enabled:
            continue
        brokers[b.value] = (store.status(b).reason if broker_configured(b, settings)
                            else "not configured")  # fmt: skip
    last_price = session.scalar(
        select(func.max(PriceDaily.date))
        .join(Instrument, Instrument.id == PriceDaily.instrument_id)
        .where(Instrument.is_index.is_(False)))  # fmt: skip
    payloads = latest_payloads(session)
    dates: list[str] = [str(p["as_of"]) for p in payloads.values() if p.get("as_of")]
    st = {
        "last_price_date": last_price, "reports": len(payloads),
        "last_report_date": max(dates) if dates else None, "jobs": jobs, "brokers": brokers,
        "open_gaps": session.scalar(select(func.count()).select_from(DataGap)
                                    .where(DataGap.resolved_at.is_(None))) or 0,
        "queued_runs": session.scalar(select(func.count()).select_from(PipelineRun).where(
            PipelineRun.status.in_([PipelineStatus.QUEUED, PipelineStatus.RUNNING]))) or 0,
        "unread": session.scalar(select(func.count()).select_from(Notification)
                                 .where(Notification.read_at.is_(None))) or 0,
    }  # fmt: skip
    return format_status(st, now)


def answer(cmd: Command, session: Session, config: AppConfig, settings: Settings,
           now: datetime) -> str:  # fmt: skip
    cfg = config.jobs.telegram_bot
    if cmd.name == "grade":
        text = _grade(session, cmd.args, config)
    elif cmd.name == "buyzone":
        rows = buyzone_rows(list(latest_payloads(session).values()), cfg.buyzone_near_pct)
        text = format_buyzone(rows, near_pct=cfg.buyzone_near_pct, limit=cfg.buyzone_limit)
    elif cmd.name == "status":
        text = _status(session, config, settings, now)
    elif cmd.name in ("help", "start"):
        text = HELP
    else:
        text = f"Unknown command /{cmd.name}.\n\n{HELP}"
    return clip(text, cfg.max_reply_chars)


# ───────────────────────── polling ─────────────────────────


class TelegramBot:
    def __init__(
        self,
        api: BotApi,
        *,
        chat_id: str,
        config: AppConfig,
        settings: Settings,
        session_factory: Callable[[], Session],
        redis: Redis,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._api = api
        self._chat_id = str(chat_id).strip()
        self._config = config
        self._settings = settings
        self._sessions = session_factory
        self._redis = redis
        self._clock = clock

    @property
    def cfg(self) -> TelegramBotConfig:
        return self._config.jobs.telegram_bot

    def _status(self, **fields: Any) -> None:
        current = self._redis.get(STATUS_KEY)
        st: dict[str, Any] = json.loads(current) if current else {}
        st.update({k: v.isoformat() if isinstance(v, datetime | date) else v
                   for k, v in fields.items()})  # fmt: skip
        self._redis.set(STATUS_KEY, json.dumps(st))

    def poll_once(self) -> int:
        """One long poll; answers allow-listed commands. Returns the number answered."""
        raw = self._redis.get(OFFSET_KEY)
        offset = int(raw) if raw else None
        updates = self._api.get_updates(offset, self.cfg.poll_timeout_s)
        now = self._clock()
        answered = ignored = 0
        for u in updates:
            uid = u.get("update_id")
            if isinstance(uid, int):  # acknowledge first: never answer the same update twice
                self._redis.set(OFFSET_KEY, uid + 1)
            msg = u.get("message")
            if not isinstance(msg, dict):
                continue
            chat = str((msg.get("chat") or {}).get("id", ""))
            if chat != self._chat_id:
                ignored += 1  # not the owner: no reply, nothing revealed
                logger.info("telegram bot: ignored a message from a chat not on the allow-list")
                continue
            sent = msg.get("date")
            if isinstance(sent, int) and (now.timestamp() - sent) > self.cfg.max_message_age_s:
                continue
            cmd = parse_command(msg.get("text"))
            if cmd is None:
                continue
            session = self._sessions()
            try:
                text = answer(cmd, session, self._config, self._settings, now)
            except Exception as exc:  # one bad answer must not stop the bot
                logger.exception("telegram bot: /%s failed", cmd.name)
                text = f"/{cmd.name} failed: {type(exc).__name__}. See the app."
            finally:
                session.close()
            self._api.send_message(self._chat_id, text)
            answered += 1
        self._status(last_poll_at=now, last_error=None,
                     **({"last_command_at": now} if answered else {}))  # fmt: skip
        if ignored:
            self._redis.hincrby(STATUS_KEY + ":counts", "ignored", ignored)
        return answered

    def run(self, stop: threading.Event) -> None:
        """Poll until ``stop``; only the holder of the Redis lock polls."""
        backoff = self.cfg.error_backoff_s
        ttl = self.cfg.poll_timeout_s + 60
        lock = self._redis.lock(LOCK_KEY, timeout=ttl)
        while not stop.is_set():
            if not lock.owned() and not lock.acquire(blocking=False):
                self._status(state="standby (another worker polls)")
                stop.wait(ttl / 2)
                continue
            try:
                lock.extend(ttl, replace_ttl=True)
                self._status(state="polling")
                self.poll_once()
                backoff = self.cfg.error_backoff_s
            except (TelegramApiError, LockError) as exc:
                hint = (" — delete the bot's webhook or stop the other poller"
                        if isinstance(exc, TelegramApiError) and exc.conflict else "")  # fmt: skip
                logger.warning("telegram bot: %s%s; retrying in %.0fs", exc, hint, backoff)
                self._status(last_error=f"{exc}{hint}", last_error_at=self._clock())
                stop.wait(backoff)
                backoff = min(backoff * 2, self.cfg.max_backoff_s)
            except Exception as exc:
                logger.exception("telegram bot loop error")
                self._status(last_error=type(exc).__name__, last_error_at=self._clock())
                stop.wait(backoff)
                backoff = min(backoff * 2, self.cfg.max_backoff_s)
        try:
            if lock.owned():
                lock.release()
        except LockError:
            pass
        self._status(state="stopped")


def build_bot(settings: Settings, config: AppConfig, session_factory: Callable[[], Session],
              redis: Redis) -> TelegramBot | None:  # fmt: skip
    """``None`` unless the bot is enabled and TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set."""
    token = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else ""
    if not (config.jobs.telegram_bot.enabled and token and settings.telegram_chat_id):
        return None
    api = TelegramBotApi(token, timeout_s=config.jobs.alerts.telegram_timeout_s)
    return TelegramBot(
        api,
        chat_id=settings.telegram_chat_id,
        config=config,
        settings=settings,
        session_factory=session_factory,
        redis=redis,
    )


def bot_status(redis: Redis) -> dict[str, Any]:
    raw = redis.get(STATUS_KEY)
    st: dict[str, Any] = json.loads(raw) if raw else {}
    ignored = redis.hget(STATUS_KEY + ":counts", "ignored")
    st["ignored_messages"] = int(ignored) if ignored else 0
    return st
