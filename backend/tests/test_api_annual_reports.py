"""Annual-report API (SPEC v0.2 §3.6 steps 3-4): upload, ledger, review queue, the cached
document, and the coverage grid."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.db.enums import StatementType
from app.db.models import AnnualReport, FinAnnual, FinLineItem
from app.jobs.common import ensure_instruments
from tests.api_support import app_client

AR = Path(__file__).parent / "fixtures" / "annual_reports"
FY12 = date(2012, 3, 31)


@pytest.fixture
def client(db: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient")
    yield from app_client(db)


def _upload(client: TestClient, name: str = "acme_ar_fy2012_igaap.pdf", fy: int = 2012,
            published: str | None = "2012-08-01") -> dict:  # type: ignore[type-arg]  # fmt: skip
    data = {"symbol": "acme", "fiscal_year": str(fy)}
    if published:
        data["published_on"] = published
    res = client.post(
        "/api/uploads/annual-report",
        data=data,
        files={"file": (name, (AR / name).read_bytes(), "application/pdf")},
    )
    assert res.status_code == 201, res.text
    return res.json()  # type: ignore[no-any-return]


def test_upload_reads_the_report(client: TestClient, db: Session) -> None:
    body = _upload(client)
    assert body["symbol"] == "ACME" and body["status"] == "parsed" and body["fiscal_year"] == 2012
    assert body["usable_from"] == "2012-08-01" and body["has_document"] and body["page_count"] == 3
    assert body["candidates"]["pending"] >= 1 and body["candidates"]["auto_accepted"] >= 10
    assert {(s["statement"], s["basis"]) for s in body["statements"]} == {
        ("bs", "standalone"), ("cf", "standalone")}  # fmt: skip
    raw = db.scalars(select(AnnualReport.raw_path)).one()
    assert raw and raw.startswith("upload/") and raw.endswith("ACME_AR_FY2012.pdf")
    assert (get_settings().raw_data_dir / raw).read_bytes() == \
        (AR / "acme_ar_fy2012_igaap.pdf").read_bytes()  # fmt: skip
    listed = client.get("/api/annual-reports", params={"symbol": "ACME"}).json()
    assert [r["id"] for r in listed] == [body["id"]]


def test_upload_without_publication_date_warns(client: TestClient) -> None:
    body = _upload(client, published=None)
    assert body["usable_from"] is None
    assert "no publication date: backtests ignore these values" in body["warnings"]


def test_upload_rejects_other_files(client: TestClient) -> None:
    for name, content, code in (("x.txt", b"%PDF", 422), ("x.pdf", b"<html>", 422)):
        form = {"symbol": "ACME", "fiscal_year": "2012"}
        res = client.post(
            "/api/uploads/annual-report",
            data=form,
            files={"file": (name, content, "application/pdf")},
        )
        assert res.status_code == code, (name, res.text)
    scan = client.post(
        "/api/uploads/annual-report",
        data={"symbol": "ACME", "fiscal_year": "2012"},
        files={"file": ("s.pdf", (AR / "scanned_ar.pdf").read_bytes(), "x")},
    )
    assert scan.status_code == 201 and scan.json()["status"] == "failed"
    assert "no text layer" in scan.json()["error"]


def test_review_queue_accept_correct_reject(client: TestClient, db: Session) -> None:
    _upload(client)
    queue = client.get("/api/review/annual-reports", params={"symbol": "ACME"}).json()
    assert queue and all(c["status"] == "pending" for c in queue)
    assert [c["confidence"] for c in queue] == sorted(c["confidence"] for c in queue)
    total = next(c for c in queue if c["item_code"] == "total_assets" and c["period_end"] ==
                 "2012-03-31")  # fmt: skip
    assert total["value_cr"] == pytest.approx(790.0) and total["raw_value"] == 79000
    assert total["pages"] == [2] and total["fiscal_year"] == 2012 and not total["stored"]
    assert any("label weight 0.85" in r for r in total["reasons"])
    summary = client.get("/api/review/annual-reports/summary").json()
    assert summary["pending"] == len(queue) and summary["reports_parsed"] == 1

    url = f"/api/review/annual-reports/{total['id']}"
    out = client.post(url, json={"action": "accept"}).json()
    assert out["status"] == "accepted" and out["stored"]
    out = client.post(url, json={"action": "correct", "value_cr": 791.5}).json()
    assert out["status"] == "corrected" and out["corrected_value_cr"] == pytest.approx(791.5)
    row = db.scalars(select(FinLineItem).where(FinLineItem.item_code == "total_assets",
                                               FinLineItem.period_end == FY12)).one()  # fmt: skip
    assert row.value_inr == pytest.approx(791.5e7) and row.confidence == 1.0
    bad = client.post(url, json={"action": "correct", "value_cr": 1, "item_code": "cfo"})
    assert bad.status_code == 422 and "not a bs amount item" in bad.json()["detail"]
    out = client.post(url, json={"action": "reject"}).json()
    assert out["status"] == "rejected" and not out["stored"]
    assert client.post("/api/review/annual-reports/999999", json={"action": "reject"}) \
        .status_code == 404  # fmt: skip
    assert client.get("/api/review/annual-reports", params={"status": "rejected"}).json()[0][
        "id"] == total["id"]  # fmt: skip


def test_document_and_reparse(client: TestClient) -> None:
    body = _upload(client)
    doc = client.get(f"/api/annual-reports/{body['id']}/document")
    assert doc.status_code == 200 and doc.headers["content-type"] == "application/pdf"
    assert doc.content == (AR / "acme_ar_fy2012_igaap.pdf").read_bytes()
    again = client.post(f"/api/annual-reports/{body['id']}/reparse").json()
    assert again["status"] == "parsed" and again["attempts"] == 2
    assert again["candidates"] == body["candidates"]
    assert client.get("/api/annual-reports/999999/document").status_code == 404


def test_coverage_grid(client: TestClient, db: Session) -> None:
    iid = ensure_instruments(db, ["ACME"])["ACME"]
    db.add(FinAnnual(instrument_id=iid, statement_type=StatementType.STANDALONE,
                     period_end=date(2020, 3, 31), fiscal_year=2020, revenue=100.0,
                     source="screener", fetched_at=date(2024, 1, 1)))  # fmt: skip
    db.commit()
    _upload(client, "acme_ar_fy2024.pdf", 2024, "2024-06-11")
    grid = client.get("/api/stocks/acme/coverage").json()
    assert grid["symbol"] == "ACME" and grid["fy_end_month"] == 3
    assert len(grid["years"]) == 10
    bases = {b["basis"]: {(c["fiscal_year"], c["statement"]): c for c in b["cells"]}
             for b in grid["bases"]}  # fmt: skip
    assert set(bases) == {"consolidated", "standalone"}
    cell = bases["consolidated"][(2024, "BS")]
    assert cell["sources"] == ["pdf"] and cell["items"] > 5 and cell["pending_review"] == 0
    assert bases["consolidated"][(2023, "CF")]["sources"] == ["pdf"]
    assert bases["consolidated"][(2024, "P&L")]["sources"] == []  # a gap
    assert bases["standalone"][(2020, "P&L")]["sources"] == ["screener"]
    assert client.get("/api/stocks/NOPE/coverage").status_code == 404
