"""results_backfill: exchange results filings (XBRL) → fin tables, the ledger, merge precedence."""

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import JobName
from app.data.results_store import merge_upsert
from app.db.enums import FilingStatus, StatementType
from app.db.models import (
    DataGap,
    FinAnnual,
    FinQuarterly,
    IndexMembership,
    Instrument,
    ResultFiling,
)
from app.db.upsert import upsert
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, run_job
from tests.jobs_support import TODAY, Env

FIX = Path(__file__).parent / "fixtures" / "xbrl"
IST = ZoneInfo("Asia/Kolkata")
ARCH = "https://nsearchives.nseindia.com/corporate/xbrl"
Q4 = f"{ARCH}/ACME_Q4FY24.xml"
Q2 = f"{ARCH}/ACME_Q2FY25.xml"
BROKEN = f"{ARCH}/ACME_BROKEN.xml"
OLD = f"{ARCH}/ACME_Q4FY12.xml"
FY24 = FinAnnual.period_end == date(2024, 3, 31)
MAR24 = FinQuarterly.period_end == date(2024, 3, 31)


def listing(
    url: str, start: str, end: str, basis: str | None, when: datetime | None
) -> dict[str, Any]:
    return {
        "url": url,
        "period_start": date.fromisoformat(start),
        "period_end": date.fromisoformat(end),
        "statement_type": basis,
        "audited": None,
        "is_bank": False,
        "disseminated_at": when,
    }


def ist(*args: int) -> datetime:
    return datetime(*args, tzinfo=IST)


@pytest.fixture
def acme(env: Env) -> Env:
    env.nse.filings["ACME"] = [
        # 10 May 2024 16:05 IST: after the close → usable from 11 May
        listing(Q4, "2024-01-01", "2024-03-31", "consolidated", ist(2024, 5, 10, 16, 5)),
        listing(Q2, "2024-07-01", "2024-09-30", "standalone", ist(2024, 10, 24, 13, 0)),
        listing(BROKEN, "2023-10-01", "2023-12-31", "consolidated", None),
        listing(OLD, "2012-01-01", "2012-03-31", "consolidated", None),  # beyond history_years
    ]  # fmt: skip
    env.nse.documents[Q4] = (FIX / "acme_q4fy24_consolidated.xml").read_bytes()
    env.nse.documents[Q2] = (FIX / "acme_q2fy25_standalone.xml").read_bytes()
    env.nse.documents[BROKEN] = b"<html>Access denied</html>"
    return env


def run(env: Env, **opts: Any):  # type: ignore[no-untyped-def]
    return run_job(REGISTRY[JobName.RESULTS_BACKFILL], env.ctx, JobOptions(**opts))


def ledger(env: Env) -> dict[str, ResultFiling]:
    with env.session() as s:
        return {r.document: r for r in s.scalars(select(ResultFiling))}


def test_lists_downloads_parses_and_stores_with_announcement_dates(acme: Env) -> None:
    rec = run(acme, symbols=("ACME",))
    d = rec.outcome.details
    assert (d["listed_new"], d["downloaded"], d["parsed"], d["pending_left"]) == (3, 3, 2, 0)
    assert list(d["failed"]) == [BROKEN] and "not an XBRL instance" in d["failed"][BROKEN]
    assert OLD not in acme.nse.document_requests  # older than providers.history_years

    rows = ledger(acme)
    q4 = rows[Q4]
    assert q4.status is FilingStatus.PARSED and q4.attempts == 1
    assert q4.periods == ["quarter 2024-03-31", "year 2024-03-31"]
    assert q4.announcement_date == date(2024, 5, 11) and q4.audited is True
    assert rows[Q2].announcement_date == date(2024, 10, 24)  # 13:00 IST: same day
    assert rows[BROKEN].status is FilingStatus.FAILED

    with acme.session() as s:
        quarters = {(r.statement_type, r.period_end): r for r in s.scalars(select(FinQuarterly))}
        year = s.scalars(select(FinAnnual).where(FY24)).one()
        gaps = {g.field for g in s.scalars(select(DataGap))}
    q = quarters[(StatementType.CONSOLIDATED, date(2024, 3, 31))]
    assert (q.revenue, q.pat, q.source) == (pytest.approx(1250), pytest.approx(210), "nse")
    assert q.announcement_date == date(2024, 5, 11)
    q2 = quarters[(StatementType.STANDALONE, date(2024, 9, 30))]
    assert q2.revenue == pytest.approx(1300) and q2.announcement_date == date(2024, 10, 24)
    assert (year.fiscal_year, year.statement_type) == (2024, StatementType.CONSOLIDATED)
    assert year.revenue == pytest.approx(4800) and year.total_assets == pytest.approx(4500)
    assert year.cfo == pytest.approx(950) and year.announcement_date == date(2024, 5, 11)
    assert "sga" in gaps  # the results format has no SG&A line


def test_reruns_are_incremental_and_failures_stop_after_max_attempts(acme: Env) -> None:
    run(acme, symbols=("ACME",))
    for attempt in (2, 3):
        rec = run(acme, symbols=("ACME",))
        assert rec.outcome.details["listed_new"] == 0
        assert rec.outcome.details["downloaded"] == 1  # only the broken one is retried
        assert ledger(acme)[BROKEN].attempts == attempt
    rec = run(acme, symbols=("ACME",))
    assert rec.outcome.details["downloaded"] == 0  # max_attempts (3) reached
    assert acme.nse.document_requests.count(Q4) == 1


def test_download_budget_takes_the_newest_periods_first(acme: Env) -> None:
    budget = acme.ctx.config.jobs.results_backfill.model_copy(update={"max_downloads_per_run": 1})
    jobs = acme.ctx.config.jobs.model_copy(update={"results_backfill": budget})
    acme.ctx.config = acme.ctx.config.model_copy(update={"jobs": jobs})
    rec = run(acme, symbols=("ACME",))
    assert acme.nse.document_requests == [Q2]  # Sep 2024 before Mar 2024 before Dec 2023
    assert rec.outcome.details["pending_left"] == 2


def test_off_season_rereads_a_symbols_list_only_weekly(acme: Env) -> None:
    # 14 Jun 2024 is outside results_season; the universe comes from index membership
    with acme.session() as s:
        upsert(s, Instrument, [{"symbol": "ACME", "source": "nse"}])
        iid = s.scalar(select(Instrument.id))
        member = {"instrument_id": iid, "index_name": "NIFTY500",
                  "effective_from": date(2024, 1, 1), "source": "nse"}  # fmt: skip
        upsert(s, IndexMembership, [member])
        s.commit()
    run(acme)
    run(acme)
    assert acme.nse.filing_requests == ["ACME"]  # second run: listed < index_recheck_days ago
    run(acme, force=True)
    assert acme.nse.filing_requests == ["ACME", "ACME"]


def test_exchange_figures_win_but_keep_other_sources_fields_and_the_earliest_date(
    acme: Env,
) -> None:
    with acme.session() as s:
        upsert(s, Instrument, [{"symbol": "ACME", "source": "nse"}])
        iid = s.scalar(select(Instrument.id))
        base = {"instrument_id": iid, "statement_type": StatementType.CONSOLIDATED,
                "period_end": date(2024, 3, 31)}  # fmt: skip
        # A Screener upload of FY2024 (no announcement date, has SG&A) ...
        upsert(s, FinAnnual, [{**base, "fiscal_year": 2024, "revenue": 4700.0, "sga": 310.0,
                               "announcement_date": None, "source": "screener"}])  # fmt: skip
        # ... and the Mar-24 quarter first seen via yfinance on 1 Jun
        upsert(s, FinQuarterly, [{**base, "revenue": 1240.0, "announcement_date": date(2024, 6, 1),
                                  "source": "yfinance"}])  # fmt: skip
        s.commit()

    run(acme, symbols=("ACME",))

    with acme.session() as s:
        year = s.scalars(select(FinAnnual).where(FY24)).one()
        mar = FinQuarterly.period_end == date(2024, 3, 31)
        q = s.scalars(select(FinQuarterly).where(mar)).one()
    assert (year.revenue, year.sga, year.source) == (pytest.approx(4800), 310.0, "nse")
    assert year.announcement_date == date(2024, 5, 11)
    assert (q.revenue, q.source) == (pytest.approx(1250), "nse")
    assert q.announcement_date == date(2024, 5, 11)  # earlier than the first-seen 1 Jun

    # A later Screener upload fills only what XBRL lacks; values, source and date stay.
    with acme.session() as s:
        s.execute(FinAnnual.__table__.update().values(sga=None))
        rows = [{**base, "instrument_id": iid, "fiscal_year": 2024, "revenue": 4000.0,
                 "sga": 320.0, "pat": None, "announcement_date": None, "source": "screener",
                 "fetched_at": datetime(2024, 7, 1, tzinfo=IST)}]  # fmt: skip
        assert merge_upsert(s, FinAnnual, rows, source="screener") == 1
        s.commit()
        year = s.scalars(select(FinAnnual).where(FY24)).one()
    assert (year.revenue, year.sga, year.pat) == (pytest.approx(4800), 320.0, pytest.approx(800))
    assert (year.source, year.announcement_date) == ("nse", date(2024, 5, 11))


def test_a_revised_filing_keeps_the_original_announcement_date(acme: Env) -> None:
    run(acme, symbols=("ACME",))
    revised = f"{ARCH}/ACME_Q4FY24_revised.xml"
    acme.nse.filings["ACME"].append(
        listing(revised, "2024-01-01", "2024-03-31", "consolidated",
                ist(2024, 5, 20, 11, 0))
    )  # fmt: skip
    acme.nse.documents[revised] = acme.nse.documents[Q4].replace(b"12500000000", b"12600000000")
    run(acme, symbols=("ACME",))
    with acme.session() as s:
        q = s.scalars(
            select(FinQuarterly).where(
                FinQuarterly.statement_type == StatementType.CONSOLIDATED,
                FinQuarterly.period_end == date(2024, 3, 31),
            )
        ).one()
    assert q.revenue == pytest.approx(1260)  # revised figures
    assert q.announcement_date == date(2024, 5, 11)  # first known, not the revision's 20 May


def test_filing_for_another_company_is_refused(acme: Env) -> None:
    acme.nse.filings["OTHER"] = [listing(Q4, "2024-01-01", "2024-03-31", "consolidated", None)]
    run(acme, symbols=("OTHER",))
    row = ledger(acme)[Q4]
    assert row.status is FilingStatus.FAILED and "document is for ACME, not OTHER" in row.error
    with acme.session() as s:
        assert s.scalars(select(FinQuarterly)).all() == []


def test_fallback_to_yfinance_when_nse_list_fails_then_resolved_by_the_filing(env: Env) -> None:
    env.quarterly.frames["ACME"] = pd.DataFrame(
        {"revenue": [1240.0], "pat": [205.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2024-03-31")]),
    )
    rec = run(env, force=True, symbols=("ACME",))  # no NSE listing for ACME → fallback
    assert rec.outcome.details["index_failed"] == ["ACME"]
    assert rec.outcome.details["fallback_flagged"] == ["ACME"]
    with env.session() as s:
        q = s.scalars(select(FinQuarterly).where(MAR24)).one()
        gap = s.scalars(select(DataGap).where(DataGap.field == "results_filing")).one()
    assert (q.source, q.revenue, q.announcement_date) == ("yfinance", 1240.0, TODAY)
    assert "2024-03-31" in gap.reason and gap.resolved_at is None

    env.nse.filings["ACME"] = [
        listing(Q4, "2024-01-01", "2024-03-31", "consolidated",
                ist(2024, 5, 10, 16, 5))
    ]  # fmt: skip
    env.nse.documents[Q4] = (FIX / "acme_q4fy24_consolidated.xml").read_bytes()
    run(env, force=True, symbols=("ACME",))
    with env.session() as s:
        q = s.scalars(select(FinQuarterly).where(MAR24)).one()
        gap = s.scalars(select(DataGap).where(DataGap.field == "results_filing")).one()
    assert (q.source, q.revenue) == ("nse", pytest.approx(1250))
    assert q.announcement_date == date(2024, 5, 11)  # the real date replaces first-seen
    assert gap.resolved_at is not None


# ───────────── raw cache (SPEC §3.2a) ─────────────


def test_documents_are_cached_before_parsing(acme: Env) -> None:
    run(acme, symbols=("ACME",))
    store = acme.ctx.raw_store
    assert store is not None
    rows = ledger(acme)
    assert rows[Q4].raw_path == "nse/2024/06/14/ACME_Q4FY24.xml"  # fetched 14 Jun (IST)
    assert store.read(rows[Q4].raw_path) == acme.nse.documents[Q4]
    # even a document that then fails to parse is kept, so a map fix can re-read it
    assert rows[BROKEN].raw_path is not None and store.read(rows[BROKEN].raw_path).startswith(
        b"<html"
    )
    assert OLD not in rows  # beyond history_years: never listed, never downloaded


def test_an_unwritable_cache_fails_the_filing_instead_of_parsing_uncached(acme: Env) -> None:
    root = acme.ctx.raw_store.root if acme.ctx.raw_store else None
    assert root is not None
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("not a directory")
    run(acme, symbols=("ACME",))
    row = ledger(acme)[Q4]
    assert row.status is FilingStatus.FAILED and "raw cache not writable" in (row.error or "")
    with acme.session() as s:
        assert s.scalars(select(FinQuarterly)).all() == []


def test_reparse_rebuilds_from_the_cache_without_downloading(
    acme: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.jobs.cli import main

    run(acme, symbols=("ACME",))
    downloads = list(acme.nse.document_requests)
    with acme.session() as s:
        s.execute(FinQuarterly.__table__.delete())
        s.execute(FinAnnual.__table__.delete())
        s.commit()
    assert main(["xbrl-reparse", "--symbols", "ACME"], context_factory=lambda: acme.ctx) == 1
    out = capsys.readouterr()
    assert "re-parsed 3 cached filing(s): 2 stored, 1 failed" in out.out
    assert "not an XBRL instance" in out.err  # the broken document, still broken
    assert acme.nse.document_requests == downloads  # nothing fetched
    with acme.session() as s:
        assert s.scalars(select(FinAnnual).where(FY24)).one().revenue == pytest.approx(4800)
