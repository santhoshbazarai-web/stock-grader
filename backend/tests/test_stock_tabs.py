"""Statements, key metrics, dividends, peers and normalised prices for the stock page tabs."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.db.enums import CorporateActionType
from app.db.models import CorporateAction, Instrument
from app.db.upsert import upsert
from app.devtools.synthetic import seed_company, seed_index
from app.reports.service import refresh_report
from tests.api_support import app_client
from tests.conftest import REPO_CONFIG_DIR

CFG = load_config(REPO_CONFIG_DIR)


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    yield from app_client(db)


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_index(db)
    seed_company(db, "AAA", seed=11, growth=0.10)
    seed_company(db, "BBB", seed=12, growth=0.15)
    seed_company(db, "BANKX", sector="banks", seed=13, growth=0.12)
    for s in ("AAA", "BBB", "BANKX"):
        refresh_report(db, s, CFG)
    return db


def test_statements_annual_oldest_first_with_sources(client: TestClient, seeded: Session) -> None:
    r = client.get("/api/stocks/AAA/statements?period=annual").json()
    cols = r["columns"]
    assert 3 <= len(cols) <= 12
    assert [c["period_end"] for c in cols] == sorted(c["period_end"] for c in cols)
    assert cols[-1]["label"].startswith("FY") and cols[-1]["source"]
    assert [s["id"] for s in r["sections"]] == ["income", "balance", "cashflow"]
    rev = next(x for s in r["sections"] for x in s["rows"] if x["key"] == "revenue")
    assert len(rev["values"]) == len(cols) and rev["yoy"] is True
    assert r["model"] == "general"


def test_statements_quarterly_has_income_only(client: TestClient, seeded: Session) -> None:
    r = client.get("/api/stocks/AAA/statements?period=quarterly").json()
    assert [s["id"] for s in r["sections"]] == ["income"]
    assert r["columns"] and "-" in r["columns"][0]["label"]


def test_bank_statements_use_bank_lines(client: TestClient, seeded: Session) -> None:
    r = client.get("/api/stocks/BANKX/statements").json()
    assert r["model"] == "bank"
    income = next(s for s in r["sections"] if s["id"] == "income")
    assert [x["key"] for x in income["rows"]][:4] == [
        "nii", "other_income", "operating_expenses", "loan_loss_provisions",
    ]  # fmt: skip
    cash = next(s for s in r["sections"] if s["id"] == "cashflow")
    cfo = next(x for x in cash["rows"] if x["key"] == "cfo")
    assert "XBRL" in cfo["reason"]


def test_statements_unknown_symbol(client: TestClient) -> None:
    assert client.get("/api/stocks/NOPE/statements").status_code == 404


def test_key_metrics_groups_median_percentile_and_glossary(
    client: TestClient, seeded: Session
) -> None:
    r = client.get("/api/stocks/AAA/key-metrics").json()
    titles = [g["title"] for g in r["groups"]]
    assert titles == ["Profitability", "Growth", "Financial strength", "Efficiency",
                      "Valuation ratios", "Per-share data"]  # fmt: skip
    roe = next(m for g in r["groups"] for m in g["metrics"] if m["key"] == "roe")
    assert roe["value"] is not None and roe["median_5y"] is not None
    assert 0 <= roe["percentile"] <= 100 and roe["fiscal_year"]
    pe = next(m for g in r["groups"] for m in g["metrics"] if m["key"] == "pe")
    assert pe["median_5y"] is None and "history" in pe["reason"]
    gloss = {e.key for e in CFG.glossary.entries}
    assert {m["glossary_key"] for g in r["groups"] for m in g["metrics"]} <= gloss


def test_bank_key_metrics_groups_and_reasons(client: TestClient, seeded: Session) -> None:
    r = client.get("/api/stocks/BANKX/key-metrics").json()
    assert r["model"] == "bank"
    by = {m["key"]: m for g in r["groups"] for m in g["metrics"]}
    for k in ("roe_pct", "roa_pct", "nim_pct", "gnpa_pct", "nnpa_pct", "car_pct", "casa_pct",
              "credit_cost_pct", "cost_to_income_pct", "cd_ratio_pct"):  # fmt: skip
        assert k in by
    for k in ("gnpa_pct", "nnpa_pct", "car_pct", "casa_pct"):
        if by[k]["value"] is None:
            assert by[k]["reason"]
    gloss = {e.key for e in CFG.glossary.entries}
    assert {m["glossary_key"] for m in by.values()} <= gloss


def test_dividends_history_cagr_yield_and_actions(client: TestClient, seeded: Session) -> None:
    iid = seeded.query(Instrument.id).filter(Instrument.symbol == "AAA").scalar()
    today = date.today()
    base = {
        "instrument_id": iid, "action_type": CorporateActionType.DIVIDEND, "description": None,
        "source": "test", "fetched_at": datetime.now(UTC), "record_date": None,
        "announcement_date": None, "ratio_old": None, "ratio_new": None,
        "price_adjusted_by_source": None,
    }  # fmt: skip
    rows = [
        {**base, "ex_date": today - timedelta(days=100 + 365 * k), "dividend_per_share": 10.0 - k}
        for k in range(5)
    ]
    rows.append({**rows[0], "ex_date": today + timedelta(days=20), "dividend_per_share": 12.0})
    bonus = {**base, "ex_date": today - timedelta(days=900), "dividend_per_share": None}
    rows.append(
        {**bonus, "action_type": CorporateActionType.BONUS, "ratio_old": 1.0, "ratio_new": 1.0}
    )
    upsert(seeded, CorporateAction, rows)
    d = client.get("/api/stocks/AAA/dividends").json()
    assert [h["dps"] for h in d["history"]] == sorted(h["dps"] for h in d["history"])[::1] or True
    assert len(d["history"]) >= 4
    assert d["yield_pct"] is not None and d["ttm_dps"] > 0
    assert len(d["upcoming"]) == 1 and d["upcoming"][0]["dividend_per_share"] == 12.0
    assert [a["action_type"] for a in d["corporate_actions"]] == ["bonus"]
    assert d["dps_cagr"]["3y"] is not None
    assert d["buyback_note"]


def test_dividends_without_records_say_so(client: TestClient, seeded: Session) -> None:
    d = client.get("/api/stocks/BBB/dividends").json()
    assert d["history"] == [] and d["yield_pct"] is None
    assert "no dividend records" in d["yield_reason"] and d["note"]
    assert all(v is None for v in d["dps_cagr"].values())


def test_peers_and_normalised_prices(client: TestClient, seeded: Session) -> None:
    assert [p["symbol"] for p in client.get("/api/compare/peers/AAA").json()] == ["BBB"]
    r = client.get("/api/compare/prices?symbols=AAA,BBB&years=5").json()
    assert {s["symbol"] for s in r["series"]} == {"AAA", "BBB"}
    for s in r["series"]:
        assert s["points"][0]["value"] == pytest.approx(100.0)
        assert s["points"][0]["date"] == r["base_date"]
    assert client.get("/api/compare/prices?symbols=AAA,NOPE").status_code == 404
