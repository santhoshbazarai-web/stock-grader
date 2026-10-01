"""Stock events card and reconciliation banner API (SPEC v0.2 §3.8-3.9)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy.orm import Session

from app.db.enums import EventKind, IssueStatus, PeriodType, StatementType
from app.db.models import Event, ReconciliationIssue
from app.jobs.common import ensure_instruments
from tests.api_support import app_client

NOW = datetime.now(UTC)
TODAY = NOW.date()


@pytest.fixture
def client(db: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient")
    yield from app_client(db)


def _event(iid: int | None, kind: EventKind, day: date, title: str, sid: str, *,
           category: str | None = None, red: bool = False) -> Event:  # fmt: skip
    return Event(exchange="nse", kind=kind, source_id=sid, instrument_id=iid, title=title,
                 category=category, red_flag=red, event_date=day, fetched_at=NOW)  # fmt: skip


def _issue(iid: int, item: str, status: IssueStatus) -> ReconciliationIssue:
    return ReconciliationIssue(
        instrument_id=iid, period_end=date(2024, 3, 31), period_type=PeriodType.YEAR,
        basis=StatementType.CONSOLIDATED, item_code=item, source="yfinance",
        reference_source="nse_xbrl", reference_value_inr=1e9, value_inr=8e8, diff_rel=0.2,
        values={"nse_xbrl": 1e9, "yfinance": 8e8}, cause="basis", status=status,
        reasons=[f"{item} differs", "consolidated / standalone mix-up"], detected_at=NOW,
        checked_at=NOW, resolved_at=NOW if status is IssueStatus.RESOLVED else None,
    )  # fmt: skip


def test_stock_events(client: TestClient, db: Session) -> None:
    ids = ensure_instruments(db, ["ACME", "OTHER"])
    iid = ids["ACME"]
    db.add_all([
        _event(iid, EventKind.BOARD_MEETING, TODAY + timedelta(days=5), "Board meeting: "
               "Financial Results", "bm1", category="board_meeting"),
        _event(iid, EventKind.ANNOUNCEMENT, TODAY - timedelta(days=3), "Resignation of "
               "Statutory Auditors", "a1", category="auditor_resignation", red=True),
        _event(iid, EventKind.BULK_DEAL, TODAY - timedelta(days=1), "Bulk deal: X bought", "d1"),
        _event(iid, EventKind.ANNOUNCEMENT, TODAY - timedelta(days=800), "Old", "a0"),
        _event(ids["OTHER"], EventKind.ANNOUNCEMENT, TODAY, "Not ACME", "o1"),
    ])  # fmt: skip
    db.flush()
    body = client.get("/api/stocks/acme/events").json()
    assert [e["title"] for e in body["upcoming"]] == ["Board meeting: Financial Results"]
    assert [e["title"] for e in body["events"]] == [
        "Bulk deal: X bought",
        "Resignation of Statutory Auditors",
    ]
    assert body["events"][1]["red_flag"] is True
    assert body["events"][1]["category"] == "auditor_resignation"
    only = client.get("/api/stocks/ACME/events", params={"kinds": ["bulk_deal"], "days": 30})
    assert [e["kind"] for e in only.json()["events"]] == ["bulk_deal"]
    assert only.json()["upcoming"] == []
    assert client.get("/api/stocks/NOPE/events").status_code == 404


def test_reconciliation_banner_ignore_reopen(client: TestClient, db: Session) -> None:
    iid = ensure_instruments(db, ["ACME"])["ACME"]
    open_, done = _issue(iid, "revenue", IssueStatus.OPEN), _issue(iid, "pat", IssueStatus.RESOLVED)
    db.add_all([open_, done])
    db.flush()
    body = client.get("/api/stocks/ACME/reconciliation").json()
    assert body["tolerance_rel"] == 0.02 and body["checked_at"] is not None
    assert [i["item_code"] for i in body["open"]] == ["revenue"]
    assert [i["item_code"] for i in body["closed"]] == ["pat"]
    issue = body["open"][0]
    assert issue["cause"] == "basis" and issue["values"] == {"nse_xbrl": 1e9, "yfinance": 8e8}

    res = client.post(f"/api/stocks/ACME/reconciliation/{open_.id}/ignore")
    assert res.status_code == 200 and res.json()["status"] == "ignored"
    assert client.get("/api/stocks/ACME/reconciliation").json()["open"] == []
    res = client.post(f"/api/stocks/ACME/reconciliation/{open_.id}/reopen")
    assert res.json()["status"] == "open" and res.json()["resolved_at"] is None

    other = ensure_instruments(db, ["OTHER"])["OTHER"]
    assert other != iid
    assert client.post(f"/api/stocks/OTHER/reconciliation/{open_.id}/ignore").status_code == 404
    assert client.get("/api/stocks/NOPE/reconciliation").status_code == 404
    empty = client.get("/api/stocks/OTHER/reconciliation").json()
    assert empty == {"symbol": "OTHER", "tolerance_rel": 0.02, "checked_at": None, "open": [],
                     "closed": []}  # fmt: skip
