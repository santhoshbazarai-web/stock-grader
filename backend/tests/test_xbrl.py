"""Exchange results XBRL parser (app.data.xbrl) against hand-computed fixtures.

Fixtures (tests/fixtures/xbrl) are synthetic filings in the SEBI Ind AS results layout, amounts
in absolute rupees. Expected values below are in ₹ crore, computed by hand from the fixture.
"""

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from app.core.config import load_config
from app.data.xbrl import (
    XbrlFormatError,
    announcement_date,
    parse_instance,
    parse_results,
)
from app.db.enums import StatementType
from tests.conftest import REPO_CONFIG_DIR

FIX = Path(__file__).parent / "fixtures" / "xbrl"
CFG = load_config(REPO_CONFIG_DIR).providers.nse.results
REL = 1e-9  # the parser only divides by 1e7: effectively exact


def approx(v: float) -> object:
    return pytest.approx(v, rel=REL)


def test_q4_consolidated_quarter_row() -> None:
    f = parse_results((FIX / "acme_q4fy24_consolidated.xml").read_bytes(), CFG)
    assert (f.company, f.symbol, f.scrip_code, f.isin) == (
        "Acme Industries Limited", "ACME", "500999", "INE000A01011",
    )  # fmt: skip
    assert f.statement_type is StatementType.CONSOLIDATED and f.audited is True
    assert (f.period_start, f.period_end) == (date(2024, 1, 1), date(2024, 3, 31))
    assert f.board_meeting == date(2024, 5, 10) and not f.is_bank
    q = f.quarter
    assert q is not None
    # Jan-Mar 2024 (context OneD); comparatives (TwoD, ThreeD) and the segment are ignored.
    assert q["revenue"] == approx(1250) and q["other_income"] == approx(30)
    assert q["cogs"] == approx(500 + 100 - 20)  # materials + purchases + change in inventories
    assert q["depreciation"] == approx(40) and q["interest"] == approx(15)
    assert q["pbt"] == approx(295) and q["tax"] == approx(75)
    assert q["pat"] == approx(210)  # owners of the parent, not the 220 incl. minorities
    assert q["minority_interest_pl"] == approx(10)
    assert q["ebit"] == approx(295 + 15)
    assert q["ebitda"] == approx(295 + 15 + 40 - 30)
    assert q["eps_diluted"] == approx(10.50)  # ₹ per share: never scaled
    assert q["shares_diluted_cr"] == approx(210 / 10.5)  # 20 crore shares
    assert q["extra"] is None
    assert "sga" not in q  # not a quarterly column


def test_q4_consolidated_fiscal_year_row() -> None:
    f = parse_results((FIX / "acme_q4fy24_consolidated.xml").read_bytes(), CFG)
    a = f.annual
    assert a is not None and a["fiscal_year"] == 2024
    # FY2023-24 P&L (context FourD)
    assert a["revenue"] == approx(4800) and a["other_income"] == approx(100)
    assert a["cogs"] == approx(2000 + 400 - 50)
    assert a["tax"] == approx(260 + 15)  # no TaxExpense filed: current + deferred
    assert a["pat"] == approx(800) and a["minority_interest_pl"] == approx(25)
    assert a["ebit"] == approx(1100 + 60)
    assert a["ebitda"] == approx(1100 + 60 + 150 - 100)
    assert a["eps_diluted"] == approx(40.0) and a["shares_diluted_cr"] == approx(800 / 40)
    # Cash flow (FourD); capex was filed as a negative number
    assert a["cfo"] == approx(950)
    assert a["purchase_of_fixed_assets"] == approx(300)
    assert a["sale_of_fixed_assets"] == approx(10) and a["dividends_paid"] == approx(200)
    # Balance sheet at 31 Mar 2024 (instant OneI), not the prior year's (TwoI)
    assert a["total_assets"] == approx(4500)
    assert (a["current_assets"], a["current_liabilities"]) == (approx(1600), approx(900))
    assert a["total_equity"] == approx(3000)  # owners' equity, before the 100 of minorities
    assert a["retained_earnings"] == approx(2980) and a["minority_interest_bs"] == approx(100)
    assert a["total_debt"] == approx(400 + 100)
    assert a["cash_and_equivalents"] == approx(150 + 50)
    assert a["non_operating_investments"] == approx(300 + 200)
    assert a["receivables"] == approx(700)  # only the current line is filed
    assert a["inventory"] == approx(600) and a["payables"] == approx(500)
    assert a["net_block"] == approx(2000 + 100)  # PPE + intangibles, not CWIP
    assert a["book_value_per_share"] == approx(3000 / 20)
    assert a["sga"] is None  # not in the results format (→ data gap)


def test_q2_standalone_has_no_annual_row_and_no_symbol() -> None:
    f = parse_results((FIX / "acme_q2fy25_standalone.xml").read_bytes(), CFG)
    assert f.statement_type is StatementType.STANDALONE and f.audited is False
    assert f.symbol is None and f.scrip_code == "500999"  # BSE-style filing
    assert f.annual is None  # the 6-month year-to-date context is neither a quarter nor a year
    q = f.quarter
    assert q is not None and f.periods() == ["quarter 2024-09-30"]
    assert q["revenue"] == approx(1300)
    assert q["cogs"] == approx(520)  # only materials consumed is filed
    assert q["pat"] == approx(224)  # standalone: profit for the period
    assert q["minority_interest_pl"] is None
    assert q["ebitda"] == approx(300 + 10 + 45 - 20)
    assert q["shares_diluted_cr"] == approx(224 / 11.2)


def test_bank_filing_maps_interest_and_bank_extras() -> None:
    f = parse_results((FIX / "bankx_q4fy24_standalone.xml").read_bytes(), CFG)
    assert f.is_bank and f.symbol == "BANKX"
    q, a = f.quarter, f.annual
    assert q is not None and a is not None
    assert q["revenue"] == approx(10000)  # interest earned
    assert q["interest"] == approx(5500)  # interest expended
    assert q["pbt"] == approx(3200) and q["pat"] == approx(2400)
    assert q["extra"] == {
        "interest_earned": approx(10000),
        "interest_expended": approx(5500),
        "operating_expenses": approx(2500),
        "loan_loss_provisions": approx(800),
        "gross_npa": approx(6000),
        "net_npa": approx(1500),
        "crar_pct": approx(16.85),  # filed as the fraction 0.1685
        # balances at 31 Mar (instant OneI) belong to the quarter ending that day too
        "advances": approx(300000),
        "deposits": approx(350000),
        "investments": approx(100000),
    }
    assert a["revenue"] == approx(38000) and a["pat"] == approx(9000)
    assert a["total_assets"] == approx(500000)
    extra = a["extra"]
    assert extra["interest_earned"] == approx(38000)
    assert extra["advances"] == approx(300000) and extra["deposits"] == approx(350000)
    # Balances at 31 Mar reported only in the quarter's context carry over to the year
    assert extra["gross_npa"] == approx(6000) and extra["crar_pct"] == approx(16.85)
    assert "loan_loss_provisions" in extra and extra["loan_loss_provisions"] == approx(2500)


def _doc(body: str, extra_ns: str = "") -> bytes:
    return (
        '<?xml version="1.0"?>\n'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
        f'xmlns:f="http://www.bseindia.com/xbrl/fin/2024-01-01/in-bse-fin" {extra_ns}>'
        '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>'
        f"{body}</xbrli:xbrl>"
    ).encode()


def _fact(name: str, context: str, rupees: int) -> str:
    return f'<f:{name} contextRef="{context}" unitRef="INR">{rupees}</f:{name}>'


def _ctx(cid: str, start: str, end: str) -> str:
    return (
        f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">1'
        f"</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>{start}"
        f"</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>"
    )


def test_period_inferred_when_not_stated_and_namespace_year_ignored() -> None:
    body = (
        _ctx("a", "2024-04-01", "2024-06-30")
        + _ctx("b", "2023-04-01", "2023-06-30")
        + _fact("RevenueFromOperations", "a", 5_000_000_000)
        + _fact("RevenueFromOperations", "b", 4_000_000_000)
    )
    f = parse_results(_doc(body), CFG)
    assert f.period_end == date(2024, 6, 30) and f.quarter is not None
    assert f.quarter["revenue"] == approx(500) and f.annual is None
    assert f.statement_type is None
    assert any("not stated" in w for w in f.warnings)
    assert any("inferred" in w for w in f.warnings)


def test_period_from_the_exchange_listing_is_used_when_given() -> None:
    body = (
        _ctx("a", "2024-04-01", "2024-06-30")
        + _ctx("b", "2024-07-01", "2024-09-30")
        + _fact("ProfitBeforeTax", "a", 100_000_000)
        + _fact("ProfitBeforeTax", "b", 200_000_000)
    )
    f = parse_results(_doc(body), CFG, period_end=date(2024, 6, 30))
    assert f.quarter is not None and f.quarter["pbt"] == approx(10)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"not xml at all", "not XML"),
        (b"<html><body>Access denied</body></html>", "not an XBRL instance"),
        (_doc(""), "no facts"),
        (
            _doc(_ctx("a", "2024-04-01", "2024-06-30") + _fact("EmployeeBenefitExpense", "a", 1)),
            "no quarter or fiscal-year results",
        ),
        (  # external entity (XXE): rejected before anything is resolved
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
            b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance">&e;</xbrli:xbrl>',
            "unsafe XML",
        ),
        (  # entity expansion bomb
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaa">'
            b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance">&b;</xbrli:xbrl>',
            "unsafe XML",
        ),
    ],
)
def test_bad_documents_are_rejected(content: bytes, message: str) -> None:
    with pytest.raises(XbrlFormatError, match=message):
        parse_results(content, CFG)


def test_instance_model_skips_nil_and_dimensional_facts() -> None:
    inst = parse_instance((FIX / "acme_q4fy24_consolidated.xml").read_bytes())
    assert inst.contexts["Seg1D"].dimensional and not inst.contexts["OneD"].dimensional
    assert inst.contexts["OneI"].instant and inst.contexts["FourD"].days == 366  # leap year
    names = {f.name for f in inst.facts}
    assert "ExceptionalItemsBeforeTax" not in names  # xsi:nil
    eps = next(f for f in inst.facts if f.name.startswith("DilutedEarnings"))
    assert eps.unit == "INRPershares"  # divide unit: not scaled like INR


@pytest.mark.parametrize(
    ("disseminated", "board", "expected"),
    [
        (datetime(2024, 5, 10, 9, 0, tzinfo=UTC), None, date(2024, 5, 10)),  # 14:30 IST
        (datetime(2024, 5, 10, 10, 0, tzinfo=UTC), None, date(2024, 5, 11)),  # 15:30 IST
        (datetime(2024, 5, 10, 18, 45, tzinfo=UTC), None, date(2024, 5, 11)),  # 00:15 IST, 11th
        (datetime(2024, 5, 10, 16, 45), None, date(2024, 5, 11)),  # naive = IST, after close
        (None, date(2024, 5, 10), date(2024, 5, 11)),  # upload: board meeting + 1 day
        (None, None, None),
    ],
)
def test_announcement_date_is_point_in_time(
    disseminated: datetime | None, board: date | None, expected: date | None
) -> None:
    assert announcement_date(disseminated, board, time(15, 30)) == expected
