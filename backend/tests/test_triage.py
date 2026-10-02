"""Coverage-grid triage (app/data/triage.py, SPEC §3.6 step 4): an HDFCBANK FY2024
consolidated balance-sheet cell with annual-report values waiting for review. Figures in
₹ crore from the Indian API fixture (total assets 40,30,194; deposits 23,76,887)."""

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.triage import PdfValue, triage
from app.db.enums import FilingStatus, LineStatement, PeriodType, ReviewStatus, StatementType
from app.db.models import AnnualReport, FinLineItem, PdfLineCandidate
from app.jobs.common import ensure_instruments
from tests.api_support import app_client

CR = 1e7
FY24 = date(2024, 3, 31)
PRECEDENCE = ["nse", "offline", "indianapi", "annual_report_pdf"]


def test_triage_defaults_follow_precedence_unless_the_pdf_is_confident() -> None:
    pdf = [
        PdfValue(1, "total_assets", FY24, 4_030_194.3 * CR, 0.72, "Total Assets"),
        PdfValue(2, "deposits", FY24, 2_376_900.0 * CR, 0.92, "Deposits"),
        PdfValue(3, "borrowings", FY24, 730_615.0 * CR, 0.60, "Borrowings"),
        PdfValue(4, "investments", FY24, None, 0.95, "Investments"),
    ]
    stored = {
        ("total_assets", FY24): {"indianapi": 4_030_194.0 * CR, "nse": 4_030_194.0 * CR},
        ("deposits", FY24): {"indianapi": 2_376_887.0 * CR},
        ("investments", FY24): {"indianapi": 1_005_682.0 * CR},
    }
    rows = {r.item_code: r for r in triage(pdf, stored, precedence=PRECEDENCE,
                                           pdf_confidence_min=0.85)}  # fmt: skip
    ta = rows["total_assets"]
    assert ta.other_source == "nse" and ta.default == "other"  # XBRL beats Indian API and PDF
    assert ta.difference_cr == pytest.approx(0.3, abs=1e-6)
    assert ta.difference_pct == pytest.approx(0.3 / 4_030_194.0, rel=1e-3)
    assert ta.reason == "precedence nse > offline > indianapi > annual_report_pdf: nse (PDF " \
        "confidence 72%)"  # fmt: skip
    dep = rows["deposits"]
    assert dep.default == "pdf" and dep.reason == "PDF confidence 92% >= 85%"
    assert dep.difference_cr == pytest.approx(13.0, abs=1e-6)
    assert rows["borrowings"].default == "pdf" and rows["borrowings"].other_source is None
    assert rows["borrowings"].reason == "only source for this item"
    assert rows["investments"].default == "other"  # no unit read: never the PDF


@pytest.fixture
def client(db: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient")
    yield from app_client(db)


def _seed_hdfc_cell(db: Session) -> dict[str, int]:
    iid = ensure_instruments(db, ["HDFCBANK"])["HDFCBANK"]
    ar = AnnualReport(instrument_id=iid, exchange="upload", document="upload:hdfc-fy24",
                      fiscal_year=2024, status=FilingStatus.PARSED,
                      usable_from=date(2024, 7, 1))  # fmt: skip
    db.add(ar)
    db.flush()
    ids = {}
    for item, value, conf in (("total_assets", 4_030_194.3, 0.72), ("deposits", 2_376_900.0, 0.92)):
        c = PdfLineCandidate(
            annual_report_id=ar.id, instrument_id=iid, statement=LineStatement.BS,
            basis=StatementType.CONSOLIDATED, period_end=FY24, period_type=PeriodType.INSTANT,
            item_code=item, value_inr=value * CR, raw_value=value, raw_label=item, pages=[5],
            method="pdfplumber", confidence=conf, reasons=[], status=ReviewStatus.PENDING,
        )  # fmt: skip
        db.add(c)
        db.flush()
        ids[item] = c.id
    for item, value in (("total_assets", 4_030_194.0), ("deposits", 2_376_887.0)):
        db.add(FinLineItem(
            instrument_id=iid, period_end=FY24, period_type=PeriodType.INSTANT,
            statement=LineStatement.BS, basis=StatementType.CONSOLIDATED, item_code=item,
            value_inr=value * CR, unit="amount", version=1, source="indianapi",
            vendor_reclassified=True,
        ))  # fmt: skip
    db.commit()
    return ids


def test_hdfcbank_fy2024_bs_cell_triage(client: TestClient, db: Session) -> None:
    ids = _seed_hdfc_cell(db)
    url = "/api/stocks/HDFCBANK/coverage/2024/BS/triage"
    rows = {r["item_code"]: r for r in client.get(url).json()}
    assert set(rows) == {"total_assets", "deposits"}
    ta = rows["total_assets"]
    assert ta["other_source"] == "indianapi" and ta["default"] == "other"
    assert ta["pdf_value_cr"] == pytest.approx(4_030_194.3)
    assert ta["other_value_cr"] == pytest.approx(4_030_194.0)
    assert ta["difference_cr"] == pytest.approx(0.3, abs=1e-6)
    assert rows["deposits"]["default"] == "pdf"
    # one click: use the Indian API figure for total assets → the PDF value is rejected
    left = client.post(url, json={"decisions": [{"candidate_id": ids["total_assets"],
                                                 "use": "other"}]}).json()  # fmt: skip
    assert [r["item_code"] for r in left] == ["deposits"]
    c = db.get(PdfLineCandidate, ids["total_assets"])
    db.refresh(c)
    assert c is not None and c.status is ReviewStatus.REJECTED and c.note == "triage: use indianapi"
    # the rest by default: deposits from the confident PDF (accepted, stored)
    assert client.post(url, json={"apply_defaults": True}).json() == []
    d = db.get(PdfLineCandidate, ids["deposits"])
    db.refresh(d)
    assert d is not None and d.status is ReviewStatus.ACCEPTED
    pdf_rows = db.scalars(
        select(FinLineItem).where(FinLineItem.source == "annual_report_pdf")
    ).all()
    assert all(r.item_code != "total_assets" for r in pdf_rows)
    # a value from another cell is refused
    bad = client.post(url, json={"decisions": [{"candidate_id": 999999, "use": "pdf"}]})
    assert bad.status_code == 422
    assert client.get("/api/stocks/NOPE/coverage/2024/BS/triage").status_code == 404
    assert client.get("/api/stocks/HDFCBANK/coverage/2024/XX/triage").status_code == 422
