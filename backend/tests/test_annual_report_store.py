"""Annual-report PDF gap filler in the database (SPEC v0.2 §3.6 step 3): the annual_reports job,
the review queue, and how PDF values sit next to exchange XBRL values in fin_line_items."""

import io
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.config import JobName
from app.data.annual_report import AnnualReportExtraction, ExtractedValue
from app.data.annual_report_store import (
    ingest_report,
    review,
    save_candidates,
    sync_report,
)
from app.data.results_store import PDF_SOURCE
from app.db.enums import FilingStatus, PeriodType, ReviewStatus, StatementType
from app.db.models import (
    AnnualReport,
    FinAnnual,
    FinLineItem,
    PdfLineCandidate,
)
from app.fundamentals.pdf_labels import get_pdf_labels
from app.fundamentals.xbrl_map import get_xbrl_map
from app.jobs.common import ensure_instruments
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, run_job
from tests.jobs_support import NOW, Env

AR = Path(__file__).parent / "fixtures" / "annual_reports"
XBRL = Path(__file__).parent / "fixtures" / "xbrl"
IST = ZoneInfo("Asia/Kolkata")
CR = 1e7
FY24, FY23, FY12 = date(2024, 3, 31), date(2023, 3, 31), date(2012, 3, 31)
AR_URL = "https://nsearchives.nseindia.com/annual_reports/AR_ACME_2023_2024.pdf"
Q4FY24 = "https://nsearchives.nseindia.com/corporate/xbrl/ACME_Q4FY24.xml"


def run(env: Env, job: JobName = JobName.ANNUAL_REPORTS) -> Any:
    return run_job(REGISTRY[job], env.ctx, JobOptions(symbols=("ACME",)))


def list_fy2024(env: Env, content: bytes | None = None) -> None:
    when = datetime(2024, 6, 10, 18, 0, tzinfo=IST)
    env.nse.reports["ACME"] = [{"url": AR_URL, "fiscal_year": 2024, "disseminated_at": when}]
    env.nse.report_documents[AR_URL] = content or (AR / "acme_ar_fy2024.pdf").read_bytes()


def xbrl_fy2024(env: Env) -> None:
    env.nse.filings["ACME"] = [{
        "url": Q4FY24, "period_start": date(2024, 1, 1), "period_end": FY24,
        "statement_type": "consolidated", "audited": True, "is_bank": False,
        "disseminated_at": datetime(2024, 5, 10, 16, 5, tzinfo=IST),
    }]  # fmt: skip
    env.nse.documents[Q4FY24] = (XBRL / "acme_q4fy24_consolidated.xml").read_bytes()


def items(env: Env, basis: StatementType, end: date, code: str) -> list[FinLineItem]:
    with env.session() as s:
        return list(s.scalars(
            select(FinLineItem).where(FinLineItem.basis == basis, FinLineItem.period_end == end,
                                      FinLineItem.item_code == code,
                                      FinLineItem.period_type != PeriodType.QUARTER)
            .order_by(FinLineItem.version)
        ).all())  # fmt: skip


def candidate(env: Env, basis: StatementType, end: date, code: str) -> PdfLineCandidate:
    with env.session() as s:
        return s.scalars(
            select(PdfLineCandidate).where(PdfLineCandidate.basis == basis,
                                           PdfLineCandidate.period_end == end,
                                           PdfLineCandidate.item_code == code)
        ).one()  # fmt: skip


# ───────────────────────── the job ─────────────────────────


def test_job_reads_the_report_for_gap_years(env: Env) -> None:
    list_fy2024(env)
    outcome = run(env)
    assert outcome.status.value == "success", outcome
    # FY2024's report covers FY2024 and (comparative column) FY2023: fetched once
    assert env.nse.report_requests == [AR_URL]
    with env.session() as s:
        report = s.scalars(select(AnnualReport)).one()
        cands = s.scalars(select(PdfLineCandidate)).all()
    assert report.status is FilingStatus.PARSED and report.fiscal_year == 2024
    assert report.usable_from == date(2024, 6, 11)  # after the close: next day (rule 4)
    assert report.raw_path and report.raw_path.startswith("nse/2024/06/14/")
    assert report.page_count == 8 and report.labels_version == get_pdf_labels().version
    assert {(x["statement"], x["basis"]) for x in report.statements or []} == {
        ("bs", "standalone"), ("cf", "standalone"), ("bs", "consolidated"),
        ("cf", "consolidated")}  # fmt: skip
    assert all(c.status is ReviewStatus.AUTO_ACCEPTED and c.stored for c in cands)

    debt = items(env, StatementType.STANDALONE, FY24, "total_debt")
    assert len(debt) == 1
    d = debt[0]
    assert (d.value_inr, d.source, d.annual_report_id, d.version) == (700 * CR, PDF_SOURCE,
                                                                     report.id, 1)  # fmt: skip
    assert d.usable_from == date(2024, 6, 11) and d.confidence == pytest.approx(1.0)
    assert d.period_type is PeriodType.INSTANT and d.tag.startswith("pdf p.3: ")
    cfo23 = items(env, StatementType.CONSOLIDATED, FY23, "cfo")
    assert [(r.value_inr, r.period_type) for r in cfo23] == [(880 * CR, PeriodType.YEAR)]
    # no P&L for these years: the fiscal-year balance sheets still make year rows (book value
    # for valuation), with the P&L left empty (never 0)
    with env.session() as s:
        rows = s.scalars(select(FinAnnual)).all()
    assert len(rows) == 4 and {r.source for r in rows} == {PDF_SOURCE}
    assert all(r.revenue is None and r.pat is None for r in rows)
    assert all(r.period_end.month == 3 for r in rows)
    # gap years with no report listed are reported, not guessed
    assert outcome.outcome is not None
    assert 2020 in outcome.outcome.details["gap_years_without_report"]["ACME"]


def test_job_skips_years_without_gaps_and_reads_zip(env: Env) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "x")
        zf.writestr("AR_2024.pdf", (AR / "acme_ar_fy2024.pdf").read_bytes())
    list_fy2024(env, buf.getvalue())
    run(env)
    assert env.nse.report_requests == [AR_URL]
    with env.session() as s:
        assert s.scalars(select(AnnualReport.status)).one() is FilingStatus.PARSED
    run(env)  # FY2024 and FY2023 now filled; the report is not fetched again
    assert env.nse.report_requests == [AR_URL]


def test_unreadable_report_fails_and_is_retried_up_to_max_attempts(env: Env) -> None:
    list_fy2024(env, (AR / "scanned_ar.pdf").read_bytes())
    for _ in range(4):
        run(env)
    with env.session() as s:
        report = s.scalars(select(AnnualReport)).one()
    assert report.status is FilingStatus.FAILED and "no text layer" in (report.error or "")
    assert report.attempts == env.ctx.config.jobs.annual_reports.max_attempts == 3


# ───────────────────────── gap filler only ─────────────────────────


def test_xbrl_figures_win_and_pdf_fills_the_rest(env: Env) -> None:
    xbrl_fy2024(env)
    run(env, JobName.RESULTS_BACKFILL)
    list_fy2024(env)
    _ingest(env)
    xbrl_assets = items(env, StatementType.CONSOLIDATED, FY24, "total_assets")
    assert [(r.value_inr, r.source) for r in xbrl_assets] == [(4500 * CR, "nse_xbrl")]
    c = candidate(env, StatementType.CONSOLIDATED, FY24, "total_assets")
    assert c.status is ReviewStatus.AUTO_ACCEPTED and not c.stored
    assert c.note == "not stored: the exchange XBRL already has this figure"
    # the results XBRL covers every consolidated item here: nothing of that basis is stored
    with env.session() as s:
        cons = s.scalars(select(PdfLineCandidate).where(
            PdfLineCandidate.basis == StatementType.CONSOLIDATED)).all()  # fmt: skip
        pdf_rows = s.scalars(select(FinLineItem).where(FinLineItem.source == PDF_SOURCE)).all()
        row = s.scalars(select(FinAnnual).where(FinAnnual.period_end == FY24,
                        FinAnnual.statement_type == StatementType.CONSOLIDATED)).one()  # fmt: skip
    assert cons and not any(c.stored for c in cons)
    assert {r.basis for r in pdf_rows} == {StatementType.STANDALONE}
    assert row.total_assets == pytest.approx(4500) and row.source == "nse"


def test_xbrl_arriving_later_replaces_pdf_values(env: Env) -> None:
    list_fy2024(env)
    _ingest(env)
    assert [r.source for r in items(env, StatementType.CONSOLIDATED, FY24, "total_assets")] == [
        PDF_SOURCE]  # fmt: skip
    xbrl_fy2024(env)
    run(env, JobName.RESULTS_BACKFILL)
    assert [(r.value_inr, r.source, r.version) for r in
            items(env, StatementType.CONSOLIDATED, FY24, "total_assets")] == [
        (4500 * CR, "nse_xbrl", 1)]  # fmt: skip
    c = candidate(env, StatementType.CONSOLIDATED, FY24, "total_assets")
    assert not c.stored and c.note == "superseded by an exchange XBRL figure"
    # the standalone basis (no XBRL here) keeps its PDF values
    assert items(env, StatementType.STANDALONE, FY24, "total_assets")[0].source == PDF_SOURCE


def _ingest(env: Env, name: str = "acme_ar_fy2024.pdf", fiscal_year: int = 2024,
            usable_from: date | None = None) -> int:  # fmt: skip
    """Read a report as the upload endpoint does."""
    with env.session() as s:
        iid = ensure_instruments(s, ["ACME"])["ACME"]
        report = AnnualReport(instrument_id=iid, exchange="upload", document=f"upload:{name}",
                              fiscal_year=fiscal_year, status=FilingStatus.PENDING,
                              usable_from=usable_from or date(2024, 6, 11))  # fmt: skip
        s.add(report)
        s.flush()
        ingest_report(s, report, content=(AR / name).read_bytes(),
                      nse=env.ctx.config.providers.nse, labels=get_pdf_labels(),
                      xmap=get_xbrl_map(), now=NOW)  # fmt: skip
        s.commit()
        return report.id


# ───────────────────────── review queue ─────────────────────────


def _review(env: Env, cid: int, action: str, value_cr: float | None = None,
            item_code: str | None = None) -> None:  # fmt: skip
    with env.session() as s:
        c = s.get(PdfLineCandidate, cid)
        assert c is not None
        review(s, c, action=action, value_cr=value_cr, item_code=item_code,
               results_cfg=env.ctx.config.providers.nse.results, xmap=get_xbrl_map(),
               labels_version=get_pdf_labels().version, now=NOW)  # fmt: skip
        s.commit()


@pytest.fixture
def igaap(env: Env) -> Env:
    """The FY2012 Indian GAAP report, with a Screener-style FY2012 row to fill."""
    with env.session() as s:
        iid = ensure_instruments(s, ["ACME"])["ACME"]
        s.add(FinAnnual(instrument_id=iid, statement_type=StatementType.STANDALONE,
                        period_end=FY12, fiscal_year=2012, revenue=900.0, pat=80.0,
                        source="screener", fetched_at=NOW))  # fmt: skip
        s.commit()
    _ingest(env, "acme_ar_fy2012_igaap.pdf", 2012, usable_from=date(2012, 8, 1))
    return env


def test_low_confidence_values_wait_for_review(igaap: Env) -> None:
    c = candidate(igaap, StatementType.STANDALONE, FY12, "total_assets")
    assert c.status is ReviewStatus.PENDING and not c.stored and c.confidence < 0.9
    assert items(igaap, StatementType.STANDALONE, FY12, "total_assets") == []
    # confident values are in, scaled from lakhs, and fill the existing wide row
    assert items(igaap, StatementType.STANDALONE, FY12, "total_debt")[0].value_inr == \
        pytest.approx(15000 * 1e5)  # fmt: skip
    with igaap.session() as s:
        row = s.scalars(select(FinAnnual).where(FinAnnual.period_end == FY12)).one()
    assert row.total_debt == pytest.approx(150)  # 15,000 lakh and row.total_assets is None
    assert row.source == "screener" and row.revenue == pytest.approx(900)


def test_accept_correct_reject(igaap: Env) -> None:
    c = candidate(igaap, StatementType.STANDALONE, FY12, "total_assets")
    _review(igaap, c.id, "accept")
    stored = items(igaap, StatementType.STANDALONE, FY12, "total_assets")
    assert [(r.value_inr, r.confidence) for r in stored] == [(79000 * 1e5, 1.0)]
    assert candidate(igaap, StatementType.STANDALONE, FY12, "total_assets").stored

    _review(igaap, c.id, "correct", value_cr=7950.5)
    stored = items(igaap, StatementType.STANDALONE, FY12, "total_assets")
    assert [(r.value_inr, r.version) for r in stored] == [(pytest.approx(7950.5 * CR), 1)]
    with igaap.session() as s:
        row = s.scalars(select(FinAnnual).where(FinAnnual.period_end == FY12)).one()
    assert row.total_assets == pytest.approx(7950.5)

    _review(igaap, c.id, "reject")
    assert items(igaap, StatementType.STANDALONE, FY12, "total_assets") == []
    with igaap.session() as s:
        row = s.scalars(select(FinAnnual).where(FinAnnual.period_end == FY12)).one()
        assert row.total_assets is None  # a rejected value is removed, not left behind
        assert row.total_debt == pytest.approx(150)  # 15,000 lakh
    c = candidate(igaap, StatementType.STANDALONE, FY12, "total_assets")
    assert c.status is ReviewStatus.REJECTED and c.reviewed_at is not None and not c.stored


def test_correct_to_another_item_and_bad_requests(igaap: Env) -> None:
    c = candidate(igaap, StatementType.STANDALONE, FY12, "total_assets")
    with pytest.raises(ValueError, match="not a bs amount item"):
        _review(igaap, c.id, "correct", value_cr=1.0, item_code="cfo")
    with pytest.raises(ValueError, match="already has a total_debt"):
        _review(igaap, c.id, "correct", value_cr=1.0, item_code="total_debt")
    with pytest.raises(ValueError, match="required"):
        _review(igaap, c.id, "correct")
    with pytest.raises(ValueError, match="unknown action"):
        _review(igaap, c.id, "maybe")
    _review(igaap, c.id, "correct", value_cr=5000, item_code="current_assets")
    assert items(igaap, StatementType.STANDALONE, FY12, "total_assets") == []
    assert items(igaap, StatementType.STANDALONE, FY12, "current_assets")[0].value_inr == \
        pytest.approx(5000 * CR)  # fmt: skip


def test_rereading_keeps_the_owners_decisions(igaap: Env) -> None:
    c = candidate(igaap, StatementType.STANDALONE, FY12, "total_assets")
    _review(igaap, c.id, "reject")
    with igaap.session() as s:
        report = s.scalars(select(AnnualReport)).one()
        ingest_report(s, report, content=(AR / "acme_ar_fy2012_igaap.pdf").read_bytes(),
                      nse=igaap.ctx.config.providers.nse, labels=get_pdf_labels(),
                      xmap=get_xbrl_map(), now=NOW)  # fmt: skip
        s.commit()
    assert candidate(igaap, StatementType.STANDALONE, FY12, "total_assets").status is \
        ReviewStatus.REJECTED  # fmt: skip
    assert items(igaap, StatementType.STANDALONE, FY12, "total_assets") == []
    assert len(items(igaap, StatementType.STANDALONE, FY12, "total_debt")) == 1  # no duplicate


# ───────────────────────── versions across reports ─────────────────────────


def _value(end: date, code: str, crore: float) -> ExtractedValue:
    return ExtractedValue("bs", "standalone", end, "instant", code, crore * CR, crore, code, [3],
                          "pdfplumber", 0.99, ["test"])  # fmt: skip


def _report(env: Env, fy: int, usable: date, values: list[ExtractedValue]) -> None:
    with env.session() as s:
        iid = ensure_instruments(s, ["ACME"])["ACME"]
        report = AnnualReport(instrument_id=iid, exchange="nse", document=f"ar{fy}",
                              fiscal_year=fy, status=FilingStatus.PARSED, usable_from=usable,
                              disseminated_at=datetime(2000, 1, 1, tzinfo=UTC))  # fmt: skip
        s.add(report)
        s.flush()
        cfg = env.ctx.config.providers.nse
        save_candidates(s, report, AnnualReportExtraction(values, [], [], 1, 1),
                        cfg.annual_reports)  # fmt: skip
        sync_report(s, report, results_cfg=cfg.results, xmap=get_xbrl_map(), labels_version=1,
                    now=NOW)  # fmt: skip
        s.commit()


@pytest.mark.parametrize("newest_first", [False, True])
def test_restated_comparative_is_a_new_version(env: Env, newest_first: bool) -> None:
    fy13 = date(2013, 3, 31)
    reports = [
        (2013, date(2013, 8, 1), [_value(fy13, "total_debt", 100.0)]),
        (
            2014,
            date(2014, 8, 1),
            [
                _value(fy13, "total_debt", 110.0),  # restated comparative
                _value(fy13, "inventory", 50.0),
            ],
        ),
    ]
    for fy, usable, values in reversed(reports) if newest_first else reports:
        _report(env, fy, usable, values)
    got = items(env, StatementType.STANDALONE, fy13, "total_debt")
    assert [(r.version, r.value_inr / CR, r.usable_from) for r in got] == [
        (1, pytest.approx(100.0), date(2013, 8, 1)),
        (2, pytest.approx(110.0), date(2014, 8, 1)),
    ]
    _report(env, 2015, date(2015, 8, 1), [_value(fy13, "inventory", 50.0)])  # same figure
    assert len(items(env, StatementType.STANDALONE, fy13, "inventory")) == 1


# ───────────────────────── CLI ─────────────────────────


def test_pdf_inspect_needs_no_database(capsys: pytest.CaptureFixture[str]) -> None:
    from app.jobs.cli import main

    assert main(["pdf-inspect", str(AR / "acme_ar_fy2012_igaap.pdf"), "--fy", "2012"]) == 0
    out = capsys.readouterr().out
    assert "standalone balance sheet: pages [2] (pdfplumber); unit '(Rs. in Lakhs)'" in out
    assert "? 2012-03-31 total_assets" in out  # below auto_accept: review
    assert main(["pdf-inspect", str(AR / "scanned_ar.pdf")]) == 1
    assert "no text layer" in capsys.readouterr().err


def test_pdf_reparse_rereads_cached_reports(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    from app.jobs.cli import main

    list_fy2024(env)
    run(env)
    with env.session() as s:
        before = s.scalars(select(FinLineItem.id).order_by(FinLineItem.id)).all()
    assert main(["pdf-reparse", "--symbols", "ACME"], context_factory=lambda: env.ctx) == 0
    assert "re-read 1 cached annual report(s): 1 read, 0 failed" in capsys.readouterr().out
    with env.session() as s:
        report = s.scalars(select(AnnualReport)).one()
        after = s.scalars(select(FinLineItem.id).order_by(FinLineItem.id)).all()
    assert report.attempts == 2 and after == before  # same values: nothing rewritten


def test_the_same_line_read_twice_keeps_the_most_confident(env: Env) -> None:
    """Regression: a report showing one line twice (balance sheet + a schedule) made the
    candidate upsert touch a row twice (psycopg CardinalityViolation, the PDF step crashed)."""
    from dataclasses import replace

    fy = date(2014, 3, 31)
    low = replace(_value(fy, "total_debt", 90.0), confidence=0.60, raw_label="schedule")
    high = replace(_value(fy, "total_debt", 100.0), confidence=0.95, raw_label="balance sheet")
    _report(env, 2014, date(2014, 8, 1), [low, high, replace(low, confidence=0.70)])
    with env.session() as s:
        rows = s.scalars(select(PdfLineCandidate).where(PdfLineCandidate.item_code ==
                                                        "total_debt")).all()  # fmt: skip
    assert len(rows) == 1
    assert rows[0].confidence == 0.95 and rows[0].raw_label == "balance sheet"
    assert rows[0].value_inr == pytest.approx(100.0 * CR)


def test_best_per_key_prefers_confidence_then_a_value() -> None:
    from app.data.annual_report_store import best_per_key

    base = {"basis": "standalone", "statement": "bs", "period_end": date(2014, 3, 31),
            "item_code": "total_debt"}  # fmt: skip
    rows = [{**base, "confidence": 0.8, "value_inr": None, "n": 1},
            {**base, "confidence": 0.8, "value_inr": 5.0, "n": 2},
            {**base, "confidence": 0.8, "value_inr": 6.0, "n": 3},
            {**base, "item_code": "cash", "confidence": 0.1, "value_inr": 1.0, "n": 4}]  # fmt: skip
    assert [r["n"] for r in best_per_key(rows)] == [2, 4]


def test_best_per_key_treats_equal_keys_of_different_types_as_one() -> None:
    import pandas as pd

    from app.data.annual_report_store import best_per_key
    from app.db.enums import LineStatement, StatementType

    day = date(2024, 3, 31)
    a = {"basis": StatementType.CONSOLIDATED, "statement": LineStatement.BS, "period_end": day,
         "item_code": "deposits", "confidence": 0.7, "value_inr": 1.0, "n": 1}  # fmt: skip
    b = {**a, "basis": "consolidated", "statement": "bs", "period_end": pd.Timestamp(day),
         "confidence": 0.9, "n": 2}  # fmt: skip
    assert [r["n"] for r in best_per_key([a, b])] == [2]


def test_a_report_that_cannot_be_stored_does_not_hide_the_others(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.jobs import annual_reports as job

    url23 = AR_URL.replace("2023_2024", "2022_2023")
    list_fy2024(env)
    when23 = datetime(2023, 6, 10, tzinfo=IST)
    env.nse.reports["ACME"].append({"url": url23, "fiscal_year": 2023, "disseminated_at": when23})
    env.nse.report_documents[url23] = env.nse.report_documents[AR_URL]
    real = job.ingest_report

    def flaky(session: Any, row: AnnualReport, **kw: Any) -> None:
        if row.fiscal_year == 2024:  # read first (newest year first): a database error
            raise RuntimeError("ON CONFLICT DO UPDATE command cannot affect row a second time")
        real(session, row, **kw)

    monkeypatch.setattr(job, "ingest_report", flaky)
    outcome = run(env)
    d = outcome.outcome.details
    assert d["downloaded"] == 2 and d["parsed"] == 1
    assert d["failed"] == {AR_URL: "RuntimeError: ON CONFLICT DO UPDATE command cannot affect "
                                   "row a second time"}  # fmt: skip
    with env.session() as s:
        rows = {r.fiscal_year: r for r in s.scalars(select(AnnualReport))}
    assert rows[2024].status is FilingStatus.FAILED and rows[2024].attempts == 1
    assert rows[2023].status is FilingStatus.PARSED
