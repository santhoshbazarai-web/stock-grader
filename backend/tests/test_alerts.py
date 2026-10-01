"""P14: alert evaluation, LTP fill-in across providers, Telegram delivery, the
alerts_intraday job and the notifications API."""

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import requests
import responses
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.evaluate import Levels, evaluate, inside_zone, side_of
from app.alerts.telegram import Delivery, TelegramNotifier, build_notifier
from app.core.config import JobName, Provider, load_config
from app.core.settings import get_settings
from app.data.gaps import InMemoryGapRecorder
from app.data.providers.base import ProviderError
from app.data.router import DataRouter
from app.db.models import Alert, Instrument, Notification, Report
from app.db.upsert import upsert
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, run_job
from tests.conftest import REPO_CONFIG_DIR
from tests.jobs_support import Env, NoLimit

CFG = load_config(REPO_CONFIG_DIR)
A = CFG.jobs.alerts  # hysteresis 0.2%, cooldown 60 min
NOW = datetime(2024, 6, 14, 5, 0, tzinfo=UTC)  # Fri 10:30 IST
ZONE = Levels(buy_zone_low=95, buy_zone_high=105, fair_value=100, top_band=130, invalidation=90)


def ev(
    kind: str,
    price: float,
    state: dict[str, Any] | None,
    *,
    levels: Levels = ZONE,
    fired_ago_min: float | None = None,
):  # type: ignore[no-untyped-def]
    last = NOW - timedelta(minutes=fired_ago_min) if fired_ago_min is not None else None
    return evaluate(kind, price, levels, state, A, now=NOW, last_fired_at=last)


# ───────────────────────── evaluator ─────────────────────────


def test_hysteresis_helpers() -> None:
    # level 100, band 0.2%: above > 100.2, below < 99.8, in between = on the line
    assert side_of(100.21, 100, 0.002) == "above"
    assert side_of(99.79, 100, 0.002) == "below"
    assert side_of(100.1, 100, 0.002) is None
    # zone [95, 105]: inside inclusive; outside beyond 105.21 / below 94.81
    assert inside_zone(95, 95, 105, 0.002) is True and inside_zone(105, 95, 105, 0.002) is True
    assert inside_zone(105.1, 95, 105, 0.002) is None
    assert inside_zone(105.3, 95, 105, 0.002) is False
    assert inside_zone(94.7, 95, 105, 0.002) is False


@pytest.mark.parametrize(
    ("state", "price", "fired", "inside"),
    [
        (None, 100, True, True),  # first look, already in zone: tell once
        ({"inside": True}, 101, False, True),  # still inside: no repeat
        ({"inside": False}, 100, True, True),  # entered
        ({"inside": False}, 105.1, False, False),  # in the hysteresis ring: stays outside
        ({"inside": True}, 105.1, False, True),  # ring: stays inside
        ({"inside": True}, 110, False, False),  # left the zone (re-arms)
        (None, 120, False, False),
    ],
)
def test_enters_buy_zone(
    state: dict[str, Any] | None, price: float, fired: bool, inside: bool
) -> None:
    e = ev("enters_buy_zone", price, state)
    assert e.fired is fired and e.state["inside"] is inside and e.state["last_price"] == price
    if fired:
        assert e.message == f"entered the buy zone ₹95.00 to ₹105.00 at ₹{price:,.2f}"


@pytest.mark.parametrize(
    ("kind", "state", "price", "fired", "side", "message"),
    [
        ("crosses_fv", None, 101, False, "above", None),  # first look only records
        ("crosses_fv", {"side": "above"}, 99, True, "below",
         "crossed below fair value ₹100.00 at ₹99.00"),
        ("crosses_fv", {"side": "below"}, 100.3, True, "above",
         "crossed above fair value ₹100.00 at ₹100.30"),
        ("crosses_fv", {"side": "above"}, 99.9, False, "above", None),  # on the line: no flip
        ("crosses_fv", {"side": "above"}, 105, False, "above", None),
        ("crosses_top_band", {"side": "below"}, 131, True, "above",
         "crossed above top band ₹130.00 at ₹131.00"),
        ("crosses_invalidation", {"side": "above"}, 89, True, "below",
         "crossed below invalidation ₹90.00 at ₹89.00"),
        ("crosses_fv", None, 100.1, False, None, None),  # first look on the line: side unknown
    ],
)  # fmt: skip
def test_level_crossings(
    kind: str, state: dict[str, Any] | None, price: float, fired: bool, side: str | None,
    message: str | None,
) -> None:  # fmt: skip
    e = ev(kind, price, state)
    assert e.fired is fired and e.state["side"] == side and e.message == message
    assert e.reasons


def test_cooldown_suppresses_but_records_the_transition() -> None:
    e = ev("crosses_fv", 99, {"side": "above"}, fired_ago_min=30)  # < 60 min
    assert not e.fired and e.state["side"] == "below"  # consumed: not replayed later
    assert "cooldown" in e.reasons[-1]
    assert ev("crosses_fv", 99, {"side": "above"}, fired_ago_min=61).fired
    assert not ev("enters_buy_zone", 100, {"inside": False}, fired_ago_min=5).fired


def test_missing_levels_never_fire() -> None:
    none = Levels()
    for kind, text in [
        ("enters_buy_zone", "no technical buy zone"),
        ("crosses_fv", "no fair value"),
        ("crosses_top_band", "no top band"),
        ("crosses_invalidation", "no invalidation"),
    ]:
        e = ev(kind, 100, {"side": "above", "inside": False}, levels=none)
        assert not e.fired and text in e.reasons[0]
    assert "unknown alert type" in ev("sideways", 100, None).reasons[0]


# ───────────────────────── LTP fill-in ─────────────────────────


@dataclass
class Quotes:
    name: Provider
    quotes: dict[str, float] = field(default_factory=dict)
    asked: list[list[str]] = field(default_factory=list)
    fail: bool = False

    def daily_ohlcv(self, symbol: str, start: date, end: date) -> Any:
        raise NotImplementedError

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        self.asked.append(list(symbols))
        if self.fail:
            raise ProviderError("quotes down")
        return {s: self.quotes[s] for s in symbols if s in self.quotes}


def router(fyers: Quotes, kite: Quotes, gaps: InMemoryGapRecorder | None = None) -> DataRouter:
    return DataRouter(
        {Provider.FYERS: fyers, Provider.KITE: kite},
        CFG.providers,
        limiter=NoLimit(),
        gaps=gaps or InMemoryGapRecorder(),
        sleep=lambda _: None,
    )


def test_ltp_fills_missing_symbols_from_the_next_provider() -> None:
    fyers = Quotes(Provider.FYERS, {"AAA": 100.0})
    kite = Quotes(Provider.KITE, {"AAA": 999.0, "BBB": 200.0})
    gaps = InMemoryGapRecorder()
    prices, reasons = router(fyers, kite, gaps).ltp_filled(["AAA", "BBB", "CCC"])
    assert prices == {"AAA": (100.0, Provider.FYERS), "BBB": (200.0, Provider.KITE)}
    assert kite.asked == [["BBB", "CCC"]]  # Kite only for what Fyers did not price
    assert any("no LTP for CCC" in r for r in reasons)
    assert len(gaps.open) == 1


def test_ltp_falls_back_when_fyers_fails() -> None:
    fyers = Quotes(Provider.FYERS, fail=True)
    kite = Quotes(Provider.KITE, {"AAA": 101.0})
    prices, _ = router(fyers, kite).ltp_filled(["AAA"])
    assert prices == {"AAA": (101.0, Provider.KITE)}
    assert len(fyers.asked) == CFG.providers.retry.max_attempts  # retried, then fell back


# ───────────────────────── Telegram ─────────────────────────

TOKEN = "123456:SECRET-token-value"
SEND = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def test_telegram_sends_and_reports_failures_without_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    t = TelegramNotifier(TOKEN, "42", timeout_s=5)
    assert TOKEN not in repr(t)
    with responses.RequestsMock() as rsps:
        rsps.post(SEND, json={"ok": True}, status=200)
        assert t.send("hello") == Delivery("sent")
        body = rsps.calls[0].request.body
        assert b'"chat_id": "42"' in body and b'"text": "hello"' in body
    with responses.RequestsMock() as rsps:
        rsps.post(
            SEND, json={"ok": False, "description": "Bad Request: chat not found"}, status=400
        )
        assert t.send("x") == Delivery("failed", "HTTP 400: Bad Request: chat not found")
    with caplog.at_level(logging.DEBUG), responses.RequestsMock() as rsps:
        rsps.post(SEND, body=requests.ConnectionError(f"cannot reach {SEND}"))
        d = t.send("x")
    assert d == Delivery("failed", "ConnectionError")
    assert TOKEN not in caplog.text


def test_build_notifier_needs_token_and_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_notifier(get_settings(), 5) is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    get_settings.cache_clear()
    assert build_notifier(get_settings(), 5) is None  # no chat id
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    get_settings.cache_clear()
    assert isinstance(build_notifier(get_settings(), 5), TelegramNotifier)


# ───────────────────────── job ─────────────────────────


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> Delivery:
        self.sent.append(text)
        return Delivery("sent")


def seed(s: Session, symbol: str, payload: dict[str, Any]) -> int:
    upsert(s, Instrument, [{"symbol": symbol, "source": "nse"}])
    iid = s.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
    assert iid is not None
    s.add(Report(instrument_id=iid, as_of=date(2024, 6, 13), payload=payload))
    return iid


def payload(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "as_of": "2024-06-13",
        "levels": {"fair_value": 100.0, "top_band": 130.0},
        "buy_zone": {"status": "zone", "low": 95.0, "high": 105.0},
        "invalidation": 90.0,
    }


@pytest.fixture
def alert_env(env: Env) -> tuple[Env, Quotes, Quotes, FakeNotifier]:
    fyers, kite = Quotes(Provider.FYERS), Quotes(Provider.KITE)
    notifier = FakeNotifier()
    env.ctx = replace(
        env.ctx,
        router=router(fyers, kite),
        clock=lambda: NOW,
        notifier=notifier,  # type: ignore[arg-type]
    )
    with env.session() as s:
        a = seed(s, "AAA", payload("AAA"))
        b = seed(s, "BBB", payload("BBB"))
        s.add_all(
            [
                Alert(instrument_id=a, alert_type="crosses_fv", is_active=True),
                Alert(instrument_id=a, alert_type="enters_buy_zone", is_active=True),
                Alert(instrument_id=b, alert_type="crosses_invalidation", is_active=True),
                Alert(instrument_id=b, alert_type="crosses_top_band", is_active=False),
            ]
        )
        s.commit()
    return env, fyers, kite, notifier


def run(env: Env, **opts: Any):  # type: ignore[no-untyped-def]
    return run_job(REGISTRY[JobName.ALERTS_INTRADAY], env.ctx, JobOptions(**opts))


def test_job_fires_on_transitions_and_notifies(
    alert_env: tuple[Env, Quotes, Quotes, FakeNotifier],
) -> None:
    env, fyers, kite, notifier = alert_env
    fyers.quotes = {"AAA": 101.0}  # Fyers misses BBB → Kite
    kite.quotes = {"BBB": 95.0}

    first = run(env).outcome
    assert first is not None
    d = first.details
    assert d["checked"] == 3  # the paused alert is ignored
    assert d["priced_by"] == {"fyers": 1, "kite": 1}
    # AAA at 101: inside the zone on first look → fires; FV and invalidation only record sides
    assert [(f["symbol"], f["alert"]) for f in d["fired"]] == [("AAA", "enters_buy_zone")]

    fyers.quotes = {"AAA": 99.0}  # crosses below FV (still inside the zone: no repeat)
    kite.quotes = {"BBB": 89.0}  # crosses below invalidation
    second = run(env).outcome
    assert second is not None
    assert sorted((f["symbol"], f["alert"]) for f in second.details["fired"]) == [
        ("AAA", "crosses_fv"),
        ("BBB", "crosses_invalidation"),
    ]
    with env.session() as s:
        notes = s.scalars(select(Notification).order_by(Notification.id)).all()
        assert [n.title for n in notes] == [
            "AAA entered the buy zone",
            "AAA crossed fair value",
            "BBB crossed invalidation",
        ]
        assert notes[1].body == (
            "AAA crossed below fair value ₹100.00 at ₹99.00 "
            "(levels from the 2024-06-13 report; price via fyers)"
        )
        assert notes[2].body.endswith("price via kite)")
        assert all(n.telegram == "sent" and n.read_at is None for n in notes)
        fv = s.scalars(select(Alert).where(Alert.alert_type == "crosses_fv")).one()
        assert fv.last_triggered_price == 99.0 and fv.state is not None
        assert fv.state["side"] == "below" and fv.state["source"] == "fyers"
    assert len(notifier.sent) == 3 and notifier.sent[0].startswith("AAA entered the buy zone\n")

    third = run(env).outcome  # nothing moved: nothing fires again
    assert third is not None and third.details["fired"] == []


def test_job_respects_market_hours(alert_env: tuple[Env, Quotes, Quotes, FakeNotifier]) -> None:
    env, fyers, _, _ = alert_env
    fyers.quotes = {"AAA": 101.0, "BBB": 95.0}
    env.ctx = replace(env.ctx, clock=lambda: datetime(2024, 6, 14, 13, 0, tzinfo=UTC))  # 18:30
    out = run(env)
    assert out.outcome is not None and "outside market hours (09:15-15:30 IST)" in str(
        out.outcome.skipped_reason
    )
    assert fyers.asked == []
    env.ctx = replace(env.ctx, clock=lambda: datetime(2024, 6, 15, 5, 0, tzinfo=UTC))  # Saturday
    out2 = run(env)
    assert out2.outcome is not None and out2.outcome.skipped_reason == "weekend: market closed"
    forced = run(env, force=True)  # --force ignores the window
    assert forced.outcome is not None and forced.outcome.details["checked"] == 3


def test_job_notes_unpriced_symbols_and_empty_alert_set(
    alert_env: tuple[Env, Quotes, Quotes, FakeNotifier],
) -> None:
    env, fyers, _, _ = alert_env
    fyers.quotes = {"AAA": 101.0}
    out = run(env).outcome
    assert out is not None and out.details["unpriced"] == ["BBB"]
    assert out.details["notes"]["BBB:crosses_invalidation"] == "no live price"
    with env.session() as s:
        for a in s.scalars(select(Alert)):
            a.is_active = False
        s.commit()
    empty = run(env).outcome
    assert empty is not None and empty.skipped_reason == "no active alerts"
