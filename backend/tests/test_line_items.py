"""fin_line_items (SPEC v0.2 §3.4, §3.6): long format, comparatives, restatement versions, and
backtests reading the version available at each date."""

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select

from app.backtest.pit import announced_by, latest_known, versioned_frame
from app.backtest.runner import _line_items
from app.core.config import JobName
from app.db.enums import PeriodType, StatementType
from app.db.models import FinAnnual, FinLineItem, FinQuarterly, Instrument, ResultFiling
from app.fundamentals.xbrl_map import get_xbrl_map
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, run_job
from app.reports.data import load_financials
from tests.jobs_support import Env

FIX = Path(__file__).parent / "fixtures" / "xbrl"
IST = ZoneInfo("Asia/Kolkata")
ARCH = "https://nsearchives.nseindia.com/corporate/xbrl"
Q4FY24 = f"{ARCH}/ACME_Q4FY24.xml"
Q4FY23 = f"{ARCH}/ACME_Q4FY23.xml"
CR = 10_000_000
FY23 = date(2023, 3, 31)


def q4fy23(fy_revenue_cr: float) -> bytes:
    """The original FY2023 results (Jan-Mar 2023 quarter and FY2022-23), consolidated. The
    quarter matches the FY2024 filing's comparative column; the year's revenue is a parameter
    (the FY2024 filing restates it at 4,080 cr)."""

    def ctx(cid: str, start: str, end: str) -> str:
        return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">500999'
                f"</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>{start}"
                f"</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period>"
                "</xbrli:context>")  # fmt: skip

    def amt(name: str, cid: str, crore: float) -> str:
        value = round(crore * CR)
        return f'<f:{name} contextRef="{cid}" unitRef="INR" decimals="-5">{value}</f:{name}>'

    def txt(name: str, value: str) -> str:
        return f'<f:{name} contextRef="Q">{value}</f:{name}>'

    body = (
        ctx("Q", "2023-01-01", "2023-03-31") + ctx("Y", "2022-04-01", "2023-03-31")
        + txt("Symbol", "ACME") + txt("NatureOfReportStandaloneConsolidated", "Consolidated")
        + txt("DateOfStartOfReportingPeriod", "2023-01-01")
        + txt("DateOfEndOfReportingPeriod", "2023-03-31")
        + amt("RevenueFromOperations", "Q", 1250 * 0.9) + amt("ProfitBeforeTax", "Q", 295 * 0.9)
        + amt("ProfitOrLossAttributableToOwnersOfParent", "Q", 210 * 0.9)
        + amt("RevenueFromOperations", "Y", fy_revenue_cr)
        + amt("ProfitBeforeTax", "Y", 1100 * 0.85)
        + amt("ProfitOrLossAttributableToOwnersOfParent", "Y", 800 * 0.85)
    )  # fmt: skip
    return (
        '<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
        'xmlns:f="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin">'
        '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>'
        f"{body}</xbrli:xbrl>"
    ).encode()


def listing(url: str, start: str, end: str, when: datetime) -> dict[str, Any]:
    return {"url": url, "period_start": date.fromisoformat(start),
            "period_end": date.fromisoformat(end), "statement_type": "consolidated",
            "audited": True, "is_bank": False, "disseminated_at": when}  # fmt: skip


@pytest.fixture
def acme(env: Env) -> Env:
    env.nse.filings["ACME"] = [
        listing(Q4FY24, "2024-01-01", "2024-03-31", datetime(2024, 5, 10, 16, 5, tzinfo=IST)),
        listing(Q4FY23, "2023-01-01", "2023-03-31", datetime(2023, 5, 12, 10, 0, tzinfo=IST)),
    ]
    env.nse.documents[Q4FY24] = (FIX / "acme_q4fy24_consolidated.xml").read_bytes()
    env.nse.documents[Q4FY23] = q4fy23(4000)
    return env


def run(env: Env) -> None:
    run_job(REGISTRY[JobName.RESULTS_WATCH], env.ctx, JobOptions(symbols=("ACME",)))


def versions(env: Env, end: date, ptype: PeriodType, code: str) -> list[tuple[Any, ...]]:
    with env.session() as s:
        rows = s.scalars(
            select(FinLineItem)
            .where(FinLineItem.period_end == end, FinLineItem.period_type == ptype,
                   FinLineItem.item_code == code)
            .order_by(FinLineItem.version)
        ).all()  # fmt: skip
        docs = {f.id: f.document for f in s.scalars(select(ResultFiling))}
    return [(r.version, r.value_inr / CR, docs.get(r.filing_id or -1), r.usable_from) for r in rows]


def test_every_period_of_a_filing_is_stored_long_format(env: Env) -> None:
    env.nse.filings["ACME"] = [
        listing(Q4FY24, "2024-01-01", "2024-03-31", datetime(2024, 5, 10, 16, 5, tzinfo=IST))
    ]
    env.nse.documents[Q4FY24] = (FIX / "acme_q4fy24_consolidated.xml").read_bytes()
    run(env)
    with env.session() as s:
        rows = s.scalars(select(FinLineItem)).all()
        filing = s.scalars(select(ResultFiling)).one()
    periods = {(r.period_type.value, r.period_end.isoformat()) for r in rows}
    assert periods == {
        ("quarter", "2024-03-31"), ("quarter", "2023-12-31"), ("quarter", "2023-03-31"),
        ("year", "2024-03-31"), ("year", "2023-03-31"),
        ("instant", "2024-03-31"), ("instant", "2023-03-31"),
    }  # fmt: skip
    rev = next(r for r in rows if r.item_code == "revenue" and r.period_type is PeriodType.YEAR
               and r.period_end == date(2024, 3, 31))  # fmt: skip
    assert (rev.value_inr, rev.unit, rev.basis, rev.version) == (
        4800 * CR, "amount", StatementType.CONSOLIDATED, 1,
    )  # fmt: skip
    assert (rev.filing_id, rev.source, rev.isin) == (filing.id, "nse_xbrl", "INE000A01011")
    assert rev.announced_at == datetime(2024, 5, 10, 16, 5, tzinfo=IST)
    assert rev.usable_from == date(2024, 5, 11) and rev.map_version == get_xbrl_map().version
    assert rev.tag == "ind_as:RevenueFromOperations" and rev.statement.value == "pl"
    assert all(r.statement.value == "bs" for r in rows if r.period_type is PeriodType.INSTANT)
    eps = next(r for r in rows if r.item_code == "eps_diluted" and r.period_end == date(2024, 3, 31)
               and r.period_type is PeriodType.QUARTER)  # fmt: skip
    assert (eps.value_inr, eps.unit) == (10.5, "per_share")
    # comparatives become wide rows too, dated by the filing that first showed them
    with env.session() as s:
        fy23 = s.scalars(select(FinAnnual).where(FinAnnual.period_end == FY23)).one()
    assert fy23.revenue == pytest.approx(4800 * 0.85) and fy23.announcement_date == date(
        2024, 5, 11
    )


@pytest.mark.parametrize("first", [Q4FY24, Q4FY23])
def test_restatement_is_a_new_version_whatever_the_download_order(acme: Env, first: str) -> None:
    if first == Q4FY23:  # the older filing first: only it is listed on the first run
        later = acme.nse.filings["ACME"].pop(0)
        run(acme)
        acme.nse.filings["ACME"].append(later)
    run(acme)  # newest first on a backfill: FY2024 filing, then FY2023's own

    assert versions(acme, FY23, PeriodType.YEAR, "revenue") == [
        (1, pytest.approx(4000), Q4FY23, date(2023, 5, 12)),  # as first published
        (2, pytest.approx(4080), Q4FY24, date(2024, 5, 11)),  # restated in FY2024's comparative
    ]
    # the same figure repeated in a later comparative is not a new version
    assert versions(acme, FY23, PeriodType.QUARTER, "revenue") == [
        (1, pytest.approx(1125), Q4FY23, date(2023, 5, 12)),
    ]
    with acme.session() as s:  # analysis: the latest version, first known 12 May 2023
        fy23 = s.scalars(select(FinAnnual).where(FinAnnual.period_end == FY23)).one()
        q = s.scalars(select(FinQuarterly).where(FinQuarterly.period_end == FY23)).one()
    assert fy23.revenue == pytest.approx(4080) and fy23.announcement_date == date(2023, 5, 12)
    assert q.revenue == pytest.approx(1125) and q.announcement_date == date(2023, 5, 12)


def test_backtests_see_the_version_available_at_each_date(acme: Env) -> None:
    run(acme)
    with acme.session() as s:
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == "ACME"))
        assert iid is not None
        wide, _, _ = load_financials(s, FinAnnual, iid, "fin_annual")
        items = _line_items(s, iid, StatementType.CONSOLIDATED)
    frame = versioned_frame(wide, items, "fin_annual", get_xbrl_map())

    def fy23_revenue(on: str) -> float | None:
        known = latest_known(announced_by(frame, pd.Timestamp(on)))
        rows = known.loc[known.index == pd.Timestamp(FY23)]
        return None if rows.empty else float(rows["revenue"].iloc[0])

    assert fy23_revenue("2023-05-11") is None  # not yet published
    assert fy23_revenue("2023-06-01") == pytest.approx(4000)  # the original figure
    assert fy23_revenue("2024-05-10") == pytest.approx(4000)  # restatement not yet public
    assert fy23_revenue("2024-06-01") == pytest.approx(4080)
    fy24 = latest_known(announced_by(frame, pd.Timestamp("2024-06-01"))).loc[
        pd.Timestamp("2024-03-31")
    ]
    assert fy24["revenue"] == pytest.approx(4800) and fy24["fiscal_year"] == 2024


def test_rounding_noise_and_reparsing_add_no_versions(acme: Env) -> None:
    acme.nse.documents[Q4FY23] = q4fy23(4080 - 0.004)  # ₹40,000 off: rounding, not a restatement
    run(acme)
    assert [v[0] for v in versions(acme, FY23, PeriodType.YEAR, "revenue")] == [1]
    with acme.session() as s:
        before = s.scalar(select(FinLineItem.id).order_by(FinLineItem.id.desc()).limit(1))
    from app.jobs.cli import main

    main(["xbrl-reparse", "--symbols", "ACME"], context_factory=lambda: acme.ctx)
    assert [v[0] for v in versions(acme, FY23, PeriodType.YEAR, "revenue")] == [1]
    with acme.session() as s:
        after = s.scalar(select(FinLineItem.id).order_by(FinLineItem.id.desc()).limit(1))
    assert after == before  # nothing rewritten: same figures, same versions


# ───────────── FY derived from quarters (SPEC §3.6 step 5) ─────────────


def results_doc(start: str, end: str, periods: dict[tuple[str, str], dict[str, float]]) -> bytes:
    """A consolidated results filing for the reporting period start..end with the given
    duration contexts ((start, end) → element → ₹ crore)."""
    ctxs, facts = [], []
    for i, ((s_, e_), values) in enumerate(periods.items()):
        cid = f"C{i}"
        ctxs.append(f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">'
                    f"500999</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>"
                    f"{s_}</xbrli:startDate><xbrli:endDate>{e_}</xbrli:endDate></xbrli:period>"
                    "</xbrli:context>")  # fmt: skip
        facts += [f'<f:{n} contextRef="{cid}" unitRef="INR" decimals="-5">{round(v * CR)}</f:{n}>'
                  for n, v in values.items()]  # fmt: skip
    info = "".join(
        f'<f:{n} contextRef="C0">{v}</f:{n}>'
        for n, v in (("Symbol", "ACME"), ("NatureOfReportStandaloneConsolidated", "Consolidated"),
                     ("DateOfStartOfReportingPeriod", start), ("DateOfEndOfReportingPeriod", end))
    )  # fmt: skip
    return (
        '<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
        'xmlns:f="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin">'
        '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>'
        + "".join(ctxs)
        + info
        + "".join(facts)
        + "</xbrli:xbrl>"
    ).encode()


QUARTERS_FY25 = [  # (start, end, revenue, PBT, PAT, published), ₹ crore
    ("2024-04-01", "2024-06-30", 1000.0, 200.0, 150.0, datetime(2024, 7, 20, 12, 0, tzinfo=IST)),
    ("2024-07-01", "2024-09-30", 1100.0, 220.0, 165.0, datetime(2024, 10, 22, 12, 0, tzinfo=IST)),
    ("2024-10-01", "2024-12-31", 1200.0, 240.0, 180.0, datetime(2025, 1, 21, 12, 0, tzinfo=IST)),
    ("2025-01-01", "2025-03-31", 1300.0, 260.0, 195.0, datetime(2025, 5, 6, 12, 0, tzinfo=IST)),
]
FY25 = date(2025, 3, 31)


def quarterly_only(env: Env, quarters: list[tuple[Any, ...]]) -> None:
    env.nse.filings["ACME"] = []
    for i, (start, end, rev, pbt, pat, when) in enumerate(quarters):
        url = f"{ARCH}/ACME_FY25_Q{i + 1}.xml"
        env.nse.filings["ACME"].append(listing(url, start, end, when))
        env.nse.documents[url] = results_doc(start, end, {(start, end): {
            "RevenueFromOperations": rev, "ProfitBeforeTax": pbt,
            "ProfitOrLossAttributableToOwnersOfParent": pat, "OtherIncome": 10.0}})  # fmt: skip


def test_a_year_without_an_annual_filing_is_summed_from_its_quarters_and_flagged(env: Env) -> None:
    quarterly_only(env, QUARTERS_FY25)
    run(env)
    rev = versions(env, FY25, PeriodType.YEAR, "revenue")
    assert rev == [(1, pytest.approx(1000 + 1100 + 1200 + 1300), None, date(2025, 5, 6))]
    with env.session() as s:
        derived_pat = (FinLineItem.derived.is_(True)) & (FinLineItem.item_code == "pat")
        item = s.scalars(select(FinLineItem).where(derived_pat)).one()
        year = s.scalars(select(FinAnnual).where(FinAnnual.period_end == FY25)).one()
    assert (item.source, item.value_inr, item.filing_id) == ("derived", 690 * CR, None)
    assert item.tag == "sum of quarters 2024-06-30, 2024-09-30, 2024-12-31, 2025-03-31"
    assert year.is_derived and year.fiscal_year == 2025
    assert (year.revenue, year.pat, year.other_income) == (4600, 690, 40)
    assert year.announcement_date == date(2025, 5, 6)  # when the last quarter was published
    assert year.eps_diluted is None  # EPS is not summed across quarters
    assert year.ebitda is None  # finance cost and D&A were not filed, so not derivable


def test_a_later_annual_filing_supersedes_the_derived_year(env: Env) -> None:
    quarterly_only(env, QUARTERS_FY25)
    run(env)
    annual = f"{ARCH}/ACME_FY25_ANNUAL.xml"  # audited annual results, filed later
    env.nse.filings["ACME"].append(
        listing(annual, "2024-04-01", "2025-03-31", datetime(2025, 5, 28, 12, 0, tzinfo=IST))
    )
    year_values = {
        "RevenueFromOperations": 4600.0,
        "ProfitBeforeTax": 920.0,
        "ProfitOrLossAttributableToOwnersOfParent": 690.0,
    }
    env.nse.documents[annual] = results_doc(
        "2024-04-01", "2025-03-31", {("2024-04-01", "2025-03-31"): year_values}
    )
    run(env)
    # the filed figure equals the derived sum, yet it is the latest version: filed beats derived
    rev = versions(env, FY25, PeriodType.YEAR, "revenue")
    assert [(v, d) for v, _, _, d in rev] == [(1, date(2025, 5, 6)), (2, date(2025, 5, 28))]
    assert rev[1][2] == annual
    with env.session() as s:
        year = s.scalars(select(FinAnnual).where(FinAnnual.period_end == FY25)).one()
    assert not year.is_derived and year.revenue == pytest.approx(4600)
    assert year.announcement_date == date(2025, 5, 6)  # first known: the derived total


def test_no_derivation_with_a_missing_quarter_or_a_filed_year(env: Env) -> None:
    quarterly_only(env, QUARTERS_FY25[:3])  # the Jan-Mar quarter is missing
    run(env)
    assert versions(env, FY25, PeriodType.YEAR, "revenue") == []
    with env.session() as s:
        assert s.scalars(select(FinAnnual)).all() == []
    # a Q4 filing that states the year: its own figures, nothing derived
    env.nse.filings["ACME"] = [
        listing(Q4FY24, "2024-01-01", "2024-03-31", datetime(2024, 5, 10, 16, 5, tzinfo=IST))
    ]
    env.nse.documents[Q4FY24] = (FIX / "acme_q4fy24_consolidated.xml").read_bytes()
    run(env)
    with env.session() as s:
        assert s.scalars(select(FinLineItem).where(FinLineItem.derived.is_(True))).all() == []
        assert (
            not s.scalars(select(FinAnnual).where(FinAnnual.period_end == date(2024, 3, 31)))
            .one()
            .is_derived
        )
