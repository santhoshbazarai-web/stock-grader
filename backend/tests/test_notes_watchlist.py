"""Notes, glossary, multiple watchlists (CSV import), alerts API and the new alert types."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.alerts.evaluate import Levels, evaluate
from app.core.config import load_config
from app.db.enums import EventKind
from app.db.models import Event, Instrument, Report
from app.jobs.common import ensure_instruments
from tests.api_support import app_client
from tests.conftest import REPO_CONFIG_DIR

CFG = load_config(REPO_CONFIG_DIR)
A = CFG.jobs.alerts
NOW = datetime(2024, 6, 14, 5, 0, tzinfo=UTC)


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    yield from app_client(db)


def report(db: Session, symbol: str, as_of: date, **payload: object) -> None:
    iid = ensure_instruments(db, [symbol])[symbol]
    db.add(Report(instrument_id=iid, as_of=as_of, payload={"as_of": str(as_of), **payload}))
    db.flush()


# ── notes ──


def test_notes_crud_and_filters(client: TestClient) -> None:
    a = client.post("/api/notes", json={"symbol": "tcs", "body": "**Cheap** after results"}).json()
    client.post("/api/notes", json={"symbol": "INFY", "body": "watch margins"})
    assert a["symbol"] == "TCS" and a["created_at"]
    assert len(client.get("/api/notes").json()) == 2
    assert [n["symbol"] for n in client.get("/api/notes?symbol=tcs").json()] == ["TCS"]
    assert [n["symbol"] for n in client.get("/api/notes?q=margins").json()] == ["INFY"]
    upd = client.put(f"/api/notes/{a['id']}", json={"body": "edited"}).json()
    assert upd["body"] == "edited"
    assert client.delete(f"/api/notes/{a['id']}").status_code == 204
    assert client.delete(f"/api/notes/{a['id']}").status_code == 404
    assert client.post("/api/notes", json={"symbol": "TCS", "body": ""}).status_code == 422


# ── glossary ──


def test_glossary_covers_every_screener_field_and_is_sorted(client: TestClient) -> None:
    entries = client.get("/api/glossary").json()
    keys = {e["key"] for e in entries}
    assert {f.key for f in CFG.screener_fields.fields} <= keys
    terms = [e["term"].lower() for e in entries]
    assert terms == sorted(terms)
    assert all(e["definition"] and e["why"] for e in entries)


def test_glossary_missing_field_is_a_config_error() -> None:
    raw = CFG.model_dump(by_alias=True)
    raw["glossary"]["entries"] = [e for e in raw["glossary"]["entries"] if e["key"] != "roe"]
    with pytest.raises(ValueError, match="roe"):
        type(CFG).model_validate(raw)


# ── watchlists ──


def test_multiple_lists_and_csv_import(client: TestClient) -> None:
    lists = client.get("/api/watchlists").json()
    assert [w["name"] for w in lists] == ["Default"]
    new = client.post("/api/watchlists", json={"name": "Banks"}).json()
    assert client.post("/api/watchlists", json={"name": "Banks"}).status_code == 409
    res = client.post(
        "/api/watchlist/import",
        json={
            "list_id": new["id"],
            "csv": "Symbol,Qty\nhdfcbank,10\nNSE:ICICIBANK-EQ,5\nbad symbol!,1\nHDFCBANK,2\n",
        },
    ).json()
    assert res["added"] == ["HDFCBANK", "ICICIBANK"] and res["invalid"] == ["BAD SYMBOL!"]
    again = client.post(
        "/api/watchlist/import", json={"list_id": new["id"], "csv": "HDFCBANK\nSBIN\n"}
    ).json()
    assert again["already_there"] == ["HDFCBANK"] and again["added"] == ["SBIN"]
    client.post("/api/watchlist", json={"symbol": "TCS"})  # Default list
    assert [r["symbol"] for r in client.get("/api/watchlist").json()] == ["TCS"]
    banks = client.get(f"/api/watchlist?list_id={new['id']}").json()
    assert [r["symbol"] for r in banks] == ["HDFCBANK", "ICICIBANK", "SBIN"]
    counts = {w["name"]: w["count"] for w in client.get("/api/watchlists").json()}
    assert counts == {"Default": 1, "Banks": 3}
    assert client.delete(f"/api/watchlist/SBIN?list_id={new['id']}").status_code == 204
    assert client.delete(f"/api/watchlists/{new['id']}").status_code == 204
    assert client.delete(f"/api/watchlists/{lists[0]['id']}").status_code == 409
    assert client.get(f"/api/watchlist?list_id={new['id']}").status_code == 404


def test_watchlist_columns_discount_results_and_change(client: TestClient, db: Session) -> None:
    report(db, "TCS", date(2024, 5, 1), grade="B", zone="fair", action="hold", cmp=100.0,
           levels={"fair_value": 100.0})  # fmt: skip
    report(db, "TCS", date(2024, 6, 1), grade="A", zone="discount", action="buy", cmp=90.0,
           levels={"fair_value": 120.0})  # fmt: skip
    iid = db.query(Instrument.id).filter(Instrument.symbol == "TCS").scalar()
    day = date.today() + timedelta(days=5)
    db.add(Event(exchange="nse", kind=EventKind.BOARD_MEETING, source_id="x", instrument_id=iid,
                 title="Board meeting: financial results", event_date=day))  # fmt: skip
    db.flush()
    client.post("/api/watchlist", json={"symbol": "TCS"})
    client.post("/api/alerts", json={"symbol": "TCS", "alert_type": "crosses_fv"})
    row = client.get("/api/watchlist").json()[0]
    assert row["discount_pct"] == pytest.approx(-25.0)
    assert row["next_results_date"] == str(day)
    assert "Grade: B → A" in row["since_last_report"]
    assert any(c.startswith("Fair value: ₹100 → ₹120") for c in row["since_last_report"])
    assert row["active_alerts"] == 1


# ── alerts ──


def test_alert_types_filters_and_condition_text(client: TestClient, db: Session) -> None:
    report(db, "TCS", date(2024, 6, 1), levels={"fair_value": 120.0, "top_band": 150.0})
    r = client.post(
        "/api/alerts", json={"symbol": "TCS", "alert_type": "price_below", "threshold": 95.5}
    )
    assert r.status_code == 201 and r.json()["condition"] == "Price falls to or below ₹95.50"
    assert (
        client.post("/api/alerts", json={"symbol": "TCS", "alert_type": "price_above"}).status_code
        == 422
    )
    fv = client.post("/api/alerts", json={"symbol": "TCS", "alert_type": "crosses_fv"}).json()
    assert fv["condition"] == "Price crosses fair value (₹120.00)" and fv["status"] == "active"
    client.post(
        "/api/alerts", json={"symbol": "INFY", "alert_type": "results_date", "threshold": 3}
    )
    client.post(
        "/api/alerts", json={"symbol": "INFY", "alert_type": "crosses_fv", "is_active": False}
    )
    assert len(client.get("/api/alerts").json()) == 4
    assert len(client.get("/api/alerts?symbol=infy").json()) == 2
    assert [a["symbol"] for a in client.get("/api/alerts?status=paused").json()] == ["INFY"]
    assert len(client.get("/api/alerts?alert_type=price_below").json()) == 1
    # re-creating a price alert replaces its target (one per stock and type)
    r2 = client.post(
        "/api/alerts", json={"symbol": "TCS", "alert_type": "price_below", "threshold": 90}
    ).json()
    assert r2["threshold"] == 90 and len(client.get("/api/alerts?symbol=TCS").json()) == 2


def ev(kind: str, price: float, state: dict | None = None, **kw: object):  # type: ignore[no-untyped-def]
    return evaluate(kind, price, Levels(), state, A, now=NOW, last_fired_at=None, **kw)  # type: ignore[arg-type]


def test_price_alert_fires_on_transition_only() -> None:
    first = ev("price_above", 101, None, threshold=100)
    assert first.fired and "at or above" in (first.message or "")
    assert not ev("price_above", 102, first.state, threshold=100).fired  # still above
    assert not ev("price_above", 99, first.state, threshold=100).fired
    assert ev("price_below", 90, None, threshold=95).fired
    assert not ev("price_below", 99, None, threshold=95).fired
    assert not ev("price_above", 101, None).fired  # no threshold set


def test_results_alert_fires_once_per_date() -> None:
    d = NOW.date() + timedelta(days=2)
    first = ev(
        "results_date", 100, None, next_results=d
    )  # default lead: jobs.alerts.results_days_before
    assert first.fired
    assert not ev("results_date", 100, first.state, next_results=d).fired
    assert not ev("results_date", 100, None, next_results=NOW.date() + timedelta(days=20)).fired
    assert ev(
        "results_date", 100, None, threshold=30, next_results=NOW.date() + timedelta(days=20)
    ).fired
    assert not ev("results_date", 100, None).fired
