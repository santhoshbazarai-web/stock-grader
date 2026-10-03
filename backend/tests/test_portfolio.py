"""Manual portfolio: average cost, P&L, XIRR, bonus / split handling, CSV and the API."""

# ruff: noqa: E501
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.db.enums import CorporateActionType
from app.db.models import Alert, CorporateAction, Instrument, PriceDaily, Report
from app.db.upsert import upsert
from app.devtools.synthetic import seed_company, seed_index
from app.jobs.common import ensure_instruments
from app.portfolio.engine import Action, Txn, run, xirr
from app.reports.service import refresh_report
from tests.api_support import app_client
from tests.conftest import REPO_CONFIG_DIR

CFG = load_config(REPO_CONFIG_DIR)
D = date


def t(kind: str, on: date, qty: float | None, price: float | None, fees: float = 0.0) -> Txn:
    return Txn(1, kind, on, qty, price, fees)


def pos(txns: list[Txn], actions: list[Action] | None = None):  # type: ignore[no-untyped-def]
    res = run(txns, actions or [])
    return res.positions[1], res


# ── average cost and P&L ──


def test_average_cost_and_realised_pnl_with_fees() -> None:
    p, res = pos([
        t("buy", D(2024, 1, 1), 10, 100, 10),
        t("buy", D(2024, 2, 1), 10, 120),
        t("sell", D(2024, 3, 1), 5, 130, 5),
    ])  # fmt: skip
    assert (p.qty, p.cost) == (15, pytest.approx(1657.5))
    assert p.avg_cost == pytest.approx(110.5)
    assert p.realised == pytest.approx(5 * 130 - 5 - 5 * 110.5)  # 92.5
    assert res.cash_delta == pytest.approx(-1010 - 1200 + 645)


def test_selling_everything_closes_the_position_and_keeps_realised() -> None:
    p, _ = pos([t("buy", D(2024, 1, 1), 10, 100), t("sell", D(2024, 2, 1), 10, 90)])
    assert p.qty == 0 and p.cost == 0 and p.avg_cost is None
    assert p.realised == pytest.approx(-100)


def test_oversell_is_capped_with_a_warning() -> None:
    p, res = pos([t("buy", D(2024, 1, 1), 10, 100), t("sell", D(2024, 2, 1), 15, 110)])
    assert p.qty == 0 and p.realised == pytest.approx(100)
    assert any("exceeds the 10 held" in w for w in res.warnings)


def test_dividend_defaults_to_shares_held_and_is_income_not_realised() -> None:
    p, res = pos([t("buy", D(2024, 1, 1), 50, 100), t("dividend", D(2024, 6, 1), None, 4)])
    assert p.dividends == pytest.approx(200) and p.realised == 0
    assert res.cash_delta == pytest.approx(-5000 + 200)


def test_same_day_buy_is_processed_before_sell() -> None:
    p, _ = pos([t("sell", D(2024, 1, 1), 5, 110), t("buy", D(2024, 1, 1), 10, 100)])
    assert p.qty == 5 and p.realised == pytest.approx(50)


# ── bonus and splits ──


def act(kind: str, on: date, old: float, new: float) -> Action:
    return Action(1, kind, on, old, new)


def test_stored_split_adjusts_earlier_buys_and_not_later_ones() -> None:
    p, _ = pos(
        [t("buy", D(2024, 1, 1), 10, 100), t("buy", D(2024, 7, 1), 10, 60)],
        [act("split", D(2024, 6, 1), 1, 2)],
    )
    assert p.qty == 30 and p.cost == pytest.approx(1600)
    assert p.avg_cost == pytest.approx(1600 / 30)


def test_stored_bonus_adds_free_shares_and_lowers_average_cost() -> None:
    p, _ = pos([t("buy", D(2024, 1, 1), 10, 100)], [act("bonus", D(2024, 6, 1), 1, 1)])  # 1:1
    assert p.qty == 20 and p.cost == 1000 and p.avg_cost == pytest.approx(50)
    p2, _ = pos([t("buy", D(2024, 1, 1), 10, 100)], [act("bonus", D(2024, 6, 1), 2, 1)])  # 1 for 2
    assert p2.qty == pytest.approx(15)


def test_sale_after_split_realises_against_the_adjusted_average() -> None:
    p, _ = pos(
        [t("buy", D(2024, 1, 1), 10, 100), t("sell", D(2024, 7, 1), 5, 60)],
        [act("split", D(2024, 6, 1), 1, 2)],
    )
    assert p.realised == pytest.approx(5 * 60 - 5 * 50) and p.qty == 15


def test_action_before_any_holding_changes_nothing() -> None:
    p, _ = pos([t("buy", D(2024, 7, 1), 10, 100)], [act("split", D(2024, 6, 1), 1, 2)])
    assert p.qty == 10


def test_manual_bonus_is_ignored_when_a_stored_action_covers_it() -> None:
    manual = t("bonus", D(2024, 6, 3), 1, 1)  # ratio_new=1, ratio_old=1
    p, res = pos([t("buy", D(2024, 1, 1), 10, 100), manual], [act("bonus", D(2024, 6, 1), 1, 1)])
    assert p.qty == 20  # applied once
    assert any("already covers it" in w for w in res.warnings)


def test_manual_split_is_used_when_no_stored_action() -> None:
    p, _ = pos(
        [t("buy", D(2024, 1, 1), 10, 100), t("split", D(2024, 6, 1), 5, 1)]
    )  # 5 new per 1 old
    assert p.qty == 50 and p.avg_cost == pytest.approx(20)


# ── XIRR ──


def test_xirr_one_year_return() -> None:
    r = xirr([(D(2020, 1, 1), -1000), (D(2021, 1, 1), 1100)])
    assert r == pytest.approx(1.1 ** (365 / 366) - 1, abs=1e-6)


def test_xirr_two_buys_and_a_final_value() -> None:
    flows = [(D(2023, 1, 1), -1000), (D(2024, 1, 1), -1000), (D(2025, 1, 1), 2420)]
    r = xirr(flows)
    assert r is not None
    npv = sum(a / (1 + r) ** ((d - D(2023, 1, 1)).days / 365) for d, a in flows)
    assert npv == pytest.approx(0, abs=1e-6) and 0.13 < r < 0.14


@pytest.mark.parametrize(
    "flows", [[], [(D(2024, 1, 1), -100)], [(D(2024, 1, 1), 100), (D(2024, 6, 1), 50)]]
)
def test_xirr_undefined_cases_are_none(flows: list[tuple[date, float]]) -> None:
    assert xirr(flows) is None


# ── API ──


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    yield from app_client(db)


def make(client: TestClient, name: str = "Main", cash: float = 0) -> int:
    return int(
        client.post("/api/portfolios", json={"name": name, "opening_cash": cash}).json()["id"]
    )


def post(client: TestClient, pid: int, **kw: object) -> dict:  # type: ignore[type-arg]
    r = client.post(f"/api/portfolios/{pid}/transactions", json=kw)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def test_portfolio_crud_and_validation(client: TestClient) -> None:
    pid = make(client, "A", 5000)
    assert client.post("/api/portfolios", json={"name": "A"}).status_code == 409
    assert (
        client.put(f"/api/portfolios/{pid}", json={"name": "A2", "opening_cash": 100}).json()[
            "name"
        ]
        == "A2"
    )
    bad = [
        {"symbol": "TCS", "txn_type": "buy", "txn_date": "2024-01-01", "price": 10},  # no quantity
        {"symbol": "TCS", "txn_type": "sell", "txn_date": "2024-01-01", "quantity": 1},  # no price
        {"symbol": "TCS", "txn_type": "buy", "txn_date": "2999-01-01", "quantity": 1, "price": 1},
        {"symbol": "TCS", "txn_type": "dividend", "txn_date": "2024-01-01", "quantity": 1},
        {"symbol": "TCS", "txn_type": "split", "txn_date": "2024-01-01", "quantity": 2},
    ]
    for body in bad:
        assert client.post(f"/api/portfolios/{pid}/transactions", json=body).status_code == 422, (
            body
        )
    tx = post(
        client,
        pid,
        symbol="tcs",
        txn_type="buy",
        txn_date="2024-01-01",
        quantity=10,
        price=100,
        fees=5,
    )
    assert tx["symbol"] == "TCS"
    edited = client.put(
        f"/api/portfolios/{pid}/transactions/{tx['id']}",
        json={
            "symbol": "TCS",
            "txn_type": "buy",
            "txn_date": "2024-01-02",
            "quantity": 12,
            "price": 100,
        },
    ).json()
    assert edited["quantity"] == 12 and edited["txn_date"] == "2024-01-02"
    assert client.delete(f"/api/portfolios/{pid}/transactions/{tx['id']}").status_code == 204
    assert client.get(f"/api/portfolios/{pid}/transactions").json() == []
    assert client.delete(f"/api/portfolios/{pid}").status_code == 204
    assert client.get(f"/api/portfolios/{pid}/view").status_code == 404


def test_csv_import_and_export_round_trip(client: TestClient) -> None:
    pid = make(client)
    csv_text = (
        "Date,Symbol,Type,Qty,Price,Fees,Notes\n"
        "2024-01-05,NSE:INFY-EQ,buy,10,1500.50,12,first\n"
        "05-02-2024,INFY,dividend,,18,0,\n"
        "2024-03-01,INFY,sell,4,1600,3,\n"
        "2024-03-02,INFY,hold,1,1,0,\n"
        "notadate,INFY,buy,1,1,0,\n"
    )
    res = client.post(f"/api/portfolios/{pid}/transactions/import", json={"csv": csv_text}).json()
    assert res["added"] == 3 and len(res["errors"]) == 2
    assert res["errors"][0].startswith("line 5") and res["errors"][1].startswith("line 6")
    txns = client.get(f"/api/portfolios/{pid}/transactions").json()
    assert [x["txn_type"] for x in txns] == ["sell", "dividend", "buy"]
    out = client.get(f"/api/portfolios/{pid}/transactions/export")
    assert out.headers["content-type"].startswith("text/csv")
    lines = out.text.strip().splitlines()
    assert lines[0] == "date,symbol,type,quantity,price,fees,notes" and len(lines) == 4
    pid2 = make(client, "Copy")
    again = client.post(
        f"/api/portfolios/{pid2}/transactions/import", json={"csv": out.text}
    ).json()
    assert again["added"] == 3 and not again["errors"]
    assert client.post(
        f"/api/portfolios/{pid2}/transactions/import", json={"csv": "foo,bar\n1,2\n"}
    ).json()["errors"]


def add_price(db: Session, symbol: str, close: float, on: date) -> None:
    iid = ensure_instruments(db, [symbol])[symbol]
    upsert(db, PriceDaily, [{"instrument_id": iid, "date": on, "open": close, "high": close, "low": close, "close": close, "volume": 1000, "adj_factor": None, "adj_open": None, "adj_high": None, "adj_low": None, "adj_close": close, "adj_volume": None, "source": "test", "fetched_at": datetime.now(UTC)}])  # fmt: skip


def test_view_values_use_stored_prices_and_never_zero_missing(
    client: TestClient, db: Session
) -> None:
    today = date.today()
    add_price(db, "AAA", 120.0, today)
    add_price(db, "AAA", 118.0, today - timedelta(days=3))
    ensure_instruments(db, ["NOPRICE"])
    pid = make(client, "P", cash=10_000)
    post(
        client,
        pid,
        symbol="AAA",
        txn_type="buy",
        txn_date=str(today - timedelta(days=365)),
        quantity=100,
        price=100,
        fees=20,
    )
    post(
        client,
        pid,
        symbol="NOPRICE",
        txn_type="buy",
        txn_date=str(today - timedelta(days=30)),
        quantity=5,
        price=50,
    )
    v = client.get(f"/api/portfolios/{pid}/view").json()
    a = next(p for p in v["positions"] if p["symbol"] == "AAA")
    assert a["price"] == 120.0 and a["value"] == pytest.approx(12_000)
    assert a["avg_cost"] == pytest.approx(100.2) and a["pnl"] == pytest.approx(12_000 - 10_020)
    assert a["pnl_pct"] == pytest.approx((12_000 / 10_020 - 1) * 100)
    n = next(p for p in v["positions"] if p["symbol"] == "NOPRICE")
    assert (
        n["price"] is None
        and n["value"] is None
        and n["pnl"] is None
        and "no stored price" in n["missing"]
    )
    s = v["summary"]
    assert s["unpriced"] == ["NOPRICE"] and s["xirr"] is None and "NOPRICE" in s["xirr_reason"]
    assert s["market_value"] == pytest.approx(12_000)  # priced holdings only
    assert s["cash"] == pytest.approx(10_000 - 10_020 - 250)
    assert s["total_cost"] == pytest.approx(10_020 + 250) and s["holdings"] == 2


def test_view_xirr_and_stored_split(client: TestClient, db: Session) -> None:
    today = date.today()
    add_price(db, "SPL", 55.0, today)
    iid = ensure_instruments(db, ["SPL"])["SPL"]
    upsert(db, CorporateAction, [{"instrument_id": iid, "ex_date": today - timedelta(days=100), "action_type": CorporateActionType.SPLIT, "ratio_old": 1.0, "ratio_new": 2.0, "dividend_per_share": None, "description": None, "record_date": None, "announcement_date": None, "price_adjusted_by_source": None, "source": "test", "fetched_at": datetime.now(UTC)}])  # fmt: skip
    pid = make(client)
    post(
        client,
        pid,
        symbol="SPL",
        txn_type="buy",
        txn_date=str(today - timedelta(days=365)),
        quantity=10,
        price=100,
    )
    v = client.get(f"/api/portfolios/{pid}/view").json()
    p = v["positions"][0]
    assert p["quantity"] == 20 and p["avg_cost"] == pytest.approx(50)
    assert p["value"] == pytest.approx(1100) and p["pnl"] == pytest.approx(100)
    assert v["summary"]["xirr"] == pytest.approx(1.1 ** (365 / 365) - 1, abs=0.01)


def test_flags_for_grade_drop_premium_zone_and_recent_alert(
    client: TestClient, db: Session
) -> None:
    today = date.today()
    seed_index(db)
    seed_company(db, "FLG", seed=31)
    refresh_report(db, "FLG", CFG)
    iid = db.query(Instrument.id).filter(Instrument.symbol == "FLG").scalar()
    rep = db.query(Report).filter(Report.instrument_id == iid).one()
    old = dict(rep.payload, grade="A", zone="fair")
    new = dict(rep.payload, grade="C", zone="premium")
    rep.payload = new
    db.add(Report(instrument_id=iid, as_of=rep.as_of - timedelta(days=30), payload=old))
    db.add(
        Alert(
            instrument_id=iid,
            alert_type="crosses_fv",
            is_active=True,
            last_triggered_at=datetime.now(UTC) - timedelta(days=1),
            last_triggered_price=100.0,
        )
    )
    db.flush()
    add_price(db, "FLG", 100.0, today)
    pid = make(client)
    post(
        client,
        pid,
        symbol="FLG",
        txn_type="buy",
        txn_date=str(today - timedelta(days=60)),
        quantity=1,
        price=90,
    )
    flags = client.get(f"/api/portfolios/{pid}/view").json()["positions"][0]["flags"]
    assert any(f.startswith("Grade dropped A to C") for f in flags)
    assert any("Premium" in f for f in flags)
    assert any(f.startswith("Alert fired: crosses fv") for f in flags)
    # a stable holding has no flags
    rep.payload = old
    db.flush()
    assert (
        client.get(f"/api/portfolios/{pid}/view")
        .json()["positions"][0]["flags"][0]
        .startswith("Alert")
    )
