"""Read-only Telegram bot (P23): command parsing, formatting, the Bot API client (token never
leaked), and polling: allow-list, stale commands, offsets, answers from stored reports,
single poller."""

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
import responses
from redis import Redis

from app.alerts.bot import (
    LOCK_KEY,
    OFFSET_KEY,
    STATUS_KEY,
    TelegramApiError,
    TelegramBot,
    TelegramBotApi,
    bot_status,
    buyzone_rows,
    clip,
    format_buyzone,
    format_grade,
    format_status,
    parse_command,
)
from app.core.settings import get_settings
from app.db.models import Notification
from app.devtools.synthetic import seed_company, seed_index
from app.reports.build import build_report
from app.reports.data import load_stock_data
from app.reports.service import persist
from tests.jobs_support import Env

OWNER = "424242"
TOKEN = "123456:SECRET-bot-token"


def payload(symbol: str = "TCS", *, grade: str = "A", cmp: float = 3650.0,
            low: float | None = 3600.0, high: float | None = 3700.0,
            status: str = "zone") -> dict[str, Any]:  # fmt: skip
    return {
        "symbol": symbol, "name": f"{symbol} Ltd", "as_of": "2024-06-13", "cmp": cmp,
        "grade": grade, "grade_label": {"A_plus": "A+"}.get(grade, grade),
        "action": "buy_on_pullback", "zone": "fair", "invalidation": 3450.0,
        "levels": {"fair_value": 4100.0, "baseline": 3200.0, "top_band": 4800.0,
                   "confidence": "medium"},
        "buy_zone": {"status": status, "low": low, "high": high, "reasons": ["stage 4"]},
        "red_flags": ["x"], "reconciliation_issues": [],
    }  # fmt: skip


# ───────────────────────── pure ─────────────────────────


@pytest.mark.parametrize(
    ("text", "name", "args"),
    [("/grade TCS", "grade", "TCS"), ("/grade@StockGraderBot  hdfc   bank", "grade", "hdfc bank"),
     ("/BUYZONE", "buyzone", ""), ("/status now", "status", "now")],
)  # fmt: skip
def test_parse_command(text: str, name: str, args: str) -> None:
    cmd = parse_command(text)
    assert cmd is not None and (cmd.name, cmd.args) == (name, args)


def test_plain_text_is_not_a_command() -> None:
    assert parse_command("hello") is None and parse_command(None) is None


def test_format_grade() -> None:
    assert format_grade(payload()) == (
        "TCS · TCS Ltd\n"
        "Grade A · Buy on Pullback · zone Fair\n"
        "CMP ₹3,650 · FV ₹4,100 (confidence medium)\n"
        "Baseline ₹3,200 · Top band ₹4,800\n"
        "Buy zone ₹3,600 to ₹3,700 (in the zone) · invalidation ₹3,450\n"
        "⚠ 1 red flag(s)\n"
        "Report as of 2024-06-13"
    )
    no_zone = format_grade(payload(status="suppressed", low=None, high=None))
    assert "Buy zone: none (stage 4)" in no_zone


def test_buyzone_rows_and_format() -> None:
    rows = buyzone_rows([
        payload("INSIDE_B", grade="B"),
        payload("NEAR_A", grade="A", cmp=3800.0),  # 2.6% above 3,700
        payload("INSIDE_A", grade="A_plus"),
        payload("FAR", cmp=4000.0),  # 7.5% above
        payload("BELOW", cmp=3500.0),  # under the zone: not a buy
        payload("NONE", status="none", low=None, high=None),
    ], near_pct=0.03)  # fmt: skip
    assert [r.symbol for r in rows] == ["INSIDE_A", "INSIDE_B", "NEAR_A"]
    text = format_buyzone(rows, near_pct=0.03, limit=2)
    assert text.splitlines() == [
        "In or within 3% of the buy zone (3):",
        "INSIDE_A A+ · in zone ₹3,600 to ₹3,700 · CMP ₹3,650 · Buy on Pullback",
        "INSIDE_B B · in zone ₹3,600 to ₹3,700 · CMP ₹3,650 · Buy on Pullback",
        "…and 1 more in the app's screener",
    ]
    assert format_buyzone([], near_pct=0.03, limit=5).startswith("No stock is in its buy zone")


def test_format_status_and_clip() -> None:
    now = datetime(2024, 6, 14, 4, 0, tzinfo=UTC)
    text = format_status({
        "last_price_date": "2024-06-13", "reports": 6, "last_report_date": "2024-06-13",
        "jobs": {"eod_prices": {"status": "success", "at": now - timedelta(hours=10),
                                "error": None},
                 "events": None},
        "brokers": {"fyers": "token expired — reconnect"},
        "open_gaps": 3, "queued_runs": 0, "unread": 2}, now)  # fmt: skip
    assert text.splitlines() == [
        "Status at 14 Jun 09:30 IST",
        "Prices to 2024-06-13 · 6 reports (latest 2024-06-13)",
        "eod_prices: success 10h ago",
        "events: never run",
        "fyers: token expired — reconnect",
        "Open data gaps 3 · pipeline runs queued 0 · unread notifications 2",
    ]
    assert clip("x" * 50, 10) == "x" * 9 + "…"


# ───────────────────────── Bot API client ─────────────────────────

API_ROOT = f"https://api.telegram.org/bot{TOKEN}"


@responses.activate
def test_api_client_and_token_hygiene() -> None:
    api = TelegramBotApi(TOKEN, timeout_s=5)
    assert TOKEN not in repr(api)
    responses.add(responses.POST, f"{API_ROOT}/getUpdates",
                  json={"ok": True, "result": [{"update_id": 7}]})  # fmt: skip
    assert api.get_updates(5, 30) == [{"update_id": 7}]
    sent = json.loads(responses.calls[0].request.body or b"{}")
    assert sent == {"timeout": 30, "allowed_updates": ["message"], "offset": 5}

    responses.replace(responses.POST, f"{API_ROOT}/getUpdates", status=409,
                      json={"ok": False, "description": "Conflict: terminated by other "
                            "getUpdates request"})  # fmt: skip
    with pytest.raises(TelegramApiError) as err:
        api.get_updates(None, 30)
    assert err.value.conflict and "Conflict" in str(err.value) and TOKEN not in str(err.value)

    responses.add(responses.POST, f"{API_ROOT}/sendMessage", body=requests.ConnectionError("boom"))
    with pytest.raises(TelegramApiError, match="sendMessage: ConnectionError") as err2:
        api.send_message(OWNER, "hi")
    assert TOKEN not in str(err2.value)


# ───────────────────────── polling ─────────────────────────


@dataclass
class FakeApi:
    batches: list[list[dict[str, Any]]] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    offsets: list[int | None] = field(default_factory=list)

    def get_updates(self, offset: int | None, timeout_s: int) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []

    def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


NOW = datetime(2024, 6, 14, 13, 0, tzinfo=UTC)


def msg(uid: int, text: str, chat: str = OWNER, age_s: int = 5) -> dict[str, Any]:
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "chat": {"id": int(chat)},
            "date": int(NOW.timestamp()) - age_s,
            "text": text,
        },
    }


@pytest.fixture
def bot_env(env: Env, redis_client: Redis) -> Env:
    for key in (OFFSET_KEY, STATUS_KEY, STATUS_KEY + ":counts", LOCK_KEY):
        redis_client.delete(key)
    with env.session() as s:
        seed_index(s, env.ctx.config.jobs.universe_index)
        seed_company(s, "SYNTH", name="Synthetic Industries")
        s.commit()
        persist(s, build_report(load_stock_data(s, "SYNTH", env.ctx.config), env.ctx.config))
        s.add(Notification(kind="test", title="t", body="b", telegram="disabled"))
        s.commit()
    return env


def bot(env: Env, api: FakeApi) -> TelegramBot:
    return TelegramBot(
        api,
        chat_id=OWNER,
        config=env.ctx.config,
        settings=get_settings(),
        session_factory=env.Session,
        redis=env.ctx.redis,
        clock=lambda: NOW,
    )


def test_owner_commands_are_answered(bot_env: Env) -> None:
    api = FakeApi([[msg(10, "/grade synth"), msg(11, "/buyzone"), msg(12, "/status"),
                    msg(13, "/help"), msg(14, "/nope"), msg(15, "just chatting")]])  # fmt: skip
    b = bot(bot_env, api)
    assert b.poll_once() == 5
    replies = [t for _, t in api.sent]
    assert all(chat == OWNER for chat, _ in api.sent)
    assert replies[0].startswith("SYNTH · Synthetic Industries\nGrade ")
    assert "Report as of " in replies[0]
    assert replies[1].startswith(("In or within 3% of the buy zone", "No stock is in its buy zone"))
    assert replies[2].startswith("Status at 14 Jun 18:30 IST")
    assert "unread notifications 1" in replies[2] and "eod_prices: never run" in replies[2]
    assert replies[3].startswith("Stock Grader (read-only)")
    assert replies[4].startswith("Unknown command /nope.")
    assert bot_env.ctx.redis.get(OFFSET_KEY) == b"16"
    st = bot_status(bot_env.ctx.redis)
    assert st["last_poll_at"] == NOW.isoformat() and st["last_error"] is None

    # the next poll continues after the acknowledged updates: nothing is answered twice
    assert b.poll_once() == 0 and api.offsets == [None, 16]


def test_strangers_and_stale_commands_get_no_reply(bot_env: Env) -> None:
    api = FakeApi([[msg(20, "/status", chat="999"), msg(21, "/grade SYNTH", age_s=3600),
                    msg(22, "/grade NOSUCHCO")]])  # fmt: skip
    assert bot(bot_env, api).poll_once() == 1
    assert api.sent == [(OWNER, "No NSE stock matches “NOSUCHCO”.")]
    assert bot_status(bot_env.ctx.redis)["ignored_messages"] == 1


def test_grade_by_name_and_missing_report(bot_env: Env) -> None:
    with bot_env.session() as s:
        seed_company(s, "NOREPORT", name="Unbuilt Company", seed=5)
        s.commit()
    api = FakeApi([[msg(30, "/grade"), msg(31, "/grade NOREPORT")]])
    bot(bot_env, api).poll_once()
    assert api.sent[0][1].startswith("Usage: /grade SYMBOL")
    assert api.sent[1][1] == ("NOREPORT: no report yet. Open /stocks/NOREPORT in the app to "
                              "build one (the bot is read-only).")  # fmt: skip


def test_run_loop_backs_off_and_releases_lock(bot_env: Env) -> None:
    calls: list[int] = []
    stop = threading.Event()

    class FlakyApi(FakeApi):
        def get_updates(self, offset: int | None, timeout_s: int) -> list[dict[str, Any]]:
            calls.append(1)
            if len(calls) == 1:
                raise TelegramApiError("getUpdates: HTTP 409 Conflict", conflict=True)
            stop.set()
            return []

    c = bot_env.ctx.config
    fast = c.jobs.telegram_bot.model_copy(update={"error_backoff_s": 0.01})
    cfg = c.model_copy(update={"jobs": c.jobs.model_copy(update={"telegram_bot": fast})})
    b = TelegramBot(FlakyApi(), chat_id=OWNER, config=cfg, settings=get_settings(),
                    session_factory=bot_env.Session, redis=bot_env.ctx.redis)  # fmt: skip
    b.run(stop)
    assert len(calls) == 2
    st = bot_status(bot_env.ctx.redis)
    # the 409 was recorded, then cleared by the next successful poll
    assert st["last_error_at"] and st["last_error"] is None and st["state"] == "stopped"
    assert bot_env.ctx.redis.get(LOCK_KEY) is None


def test_second_poller_stands_by(bot_env: Env) -> None:
    other = bot_env.ctx.redis.lock(LOCK_KEY, timeout=60)
    assert other.acquire(blocking=False)
    stop = threading.Event()
    api = FakeApi()
    t = threading.Thread(target=bot(bot_env, api).run, args=(stop,))
    t.start()
    stop.wait(0.3)
    stop.set()
    t.join(5)
    other.release()
    assert api.offsets == [] and bot_status(bot_env.ctx.redis)["state"] == "stopped"
