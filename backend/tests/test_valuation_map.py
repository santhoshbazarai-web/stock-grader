"""GET /api/valuation-map: stored latest reports only, grouped data, excluded count, cache."""

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_config
from app.db.models import IndexMembership, Instrument, Report
from app.devtools.synthetic import seed_company, seed_index
from app.reports.service import refresh_report
from tests.api_support import app_client

SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE")


@pytest.fixture
def client(db: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient")
    for k in redis_client.scan_iter("valuation-map:*"):
        redis_client.delete(k)
    yield from app_client(db)
    for k in redis_client.scan_iter("valuation-map:*"):
        redis_client.delete(k)


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_index(db)
    for i, sym in enumerate(SYMBOLS):
        seed_company(db, sym, seed=11 + i, pe=15.0 + 6 * i,
                     sector="banks" if sym == "EEE" else "it_services")  # fmt: skip
        refresh_report(db, sym, get_config())
    ids = dict(db.execute(select(Instrument.symbol, Instrument.id)).all())
    for sym in SYMBOLS[:4]:  # EEE is not an index member
        db.add(IndexMembership(index_name="NIFTY 500", instrument_id=ids[sym],
                               effective_from=date(2020, 1, 1), source="nse"))  # fmt: skip
    # DDD: too little data behind it (below provisional) → counted, not listed
    rep = db.scalar(select(Report).where(Report.instrument_id == ids["DDD"]))
    assert rep is not None
    payload = dict(rep.payload)
    payload["data_depth"] = {"level": "technical_only", "pl_years": 1, "reason": "test"}
    db.execute(update(Report).where(Report.id == rep.id).values(payload=payload))
    db.flush()
    return db


def test_rows_come_from_stored_reports_with_discount_and_excluded(
    client: TestClient, seeded: Session
) -> None:
    body = client.get("/api/valuation-map?universe=NIFTY500").json()
    assert [r["symbol"] for r in body["rows"]] == ["AAA", "BBB", "CCC"]  # EEE not a member
    assert body["excluded"] == 1 and body["total"] == 4 and body["universe_note"] is None
    for r in body["rows"]:
        assert r["fair_value"] > 0 and r["price"] > 0
        assert r["discount_pct"] == pytest.approx(r["price"] / r["fair_value"] - 1)
        assert r["sector"] == "it_services" and r["market_cap_cr"] > 0
        assert r["zone"] and r["grade"] and r["depth"] in ("provisional", "full")
        assert r["confidence"] in ("high", "medium", "low")
        assert r["day_change_pct"] is not None


def test_unknown_universe_falls_back_to_all_stored_reports(
    client: TestClient, seeded: Session
) -> None:
    body = client.get("/api/valuation-map?universe=NIFTY50").json()
    assert {r["symbol"] for r in body["rows"]} == {"AAA", "BBB", "CCC", "EEE"}
    assert body["excluded"] == 1 and "no NIFTY50 membership on file" in body["universe_note"]


def test_result_is_cached_for_five_minutes(
    client: TestClient, seeded: Session, redis_client: Redis
) -> None:
    first = client.get("/api/valuation-map?universe=NIFTY500").json()
    seeded.execute(update(Report).values(payload={}))  # a rebuild would now crash
    assert client.get("/api/valuation-map?universe=NIFTY500").json() == first
    assert 0 < redis_client.ttl("valuation-map:NIFTY500") <= 300
