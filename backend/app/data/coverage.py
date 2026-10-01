"""Fundamentals coverage per stock: which fiscal years each statement has in fin_line_items
(P17 audit item 8, the ``xbrl-coverage`` CLI), and the years-by-statements grid of the stock
page, by source (SPEC v0.2 §3.6 step 4)."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.results_store import PDF_SOURCE
from app.db.enums import LineStatement, PeriodType, ReviewStatus, StatementType
from app.db.models import FinAnnual, FinLineItem, Instrument, PdfLineCandidate, ResultFiling

# statement → the line items that make a fiscal year of it, the same for the CLI and the stock
# page grid (P&L: full-year flows, filed or summed from four quarters; balance sheet:
# balances; cash flow: year flows). Quarters alone don't make a year: they are counted apart.
StatementName = Literal["P&L", "BS", "CF"]
STATEMENTS: dict[StatementName, tuple[LineStatement, tuple[PeriodType, ...]]] = {
    "P&L": (LineStatement.PL, (PeriodType.YEAR,)),
    "BS": (LineStatement.BS, (PeriodType.INSTANT,)),
    "CF": (LineStatement.CF, (PeriodType.YEAR,)),
}


@dataclass(frozen=True)
class Coverage:
    symbol: str
    basis: StatementType
    statement: str  # P&L | BS | CF
    earliest_fy: int | None
    latest_fy: int | None
    years: int  # distinct fiscal years with at least one filed (not derived) value
    derived_years: int  # fiscal years whose figures exist only as derived (summed quarters)
    quarters: int = 0  # P&L only: quarters stored (they make a year only when all four are)

    def row(self) -> str:
        span = f"FY{self.earliest_fy}-FY{self.latest_fy}" if self.earliest_fy else "—"
        extra = f" (+{self.derived_years} derived)" if self.derived_years else ""
        q = f"; {self.quarters} quarter(s)" if self.statement == "P&L" else ""
        return f"{self.symbol:<14} {self.basis.value:<13} {self.statement:<4} {span:<14} " \
               f"{self.years:>3} yr{extra}{q}"  # fmt: skip


def _fy(period_end: date, fy_end_month: int) -> int:
    """Fiscal year (ending calendar year) of a period ending ``period_end``."""
    return period_end.year if period_end.month <= fy_end_month else period_end.year + 1


def coverage(session: Session, symbols: list[str], fy_end_month: int = 3) -> list[Coverage]:
    """Per symbol and basis (a basis with no line items at all is left out, unless neither
    basis has any), per statement: earliest and latest fiscal year and the number of years."""
    out: list[Coverage] = []
    for symbol in symbols:
        iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
        per_basis: dict[StatementType, list[Coverage]] = {}
        for basis in (StatementType.CONSOLIDATED, StatementType.STANDALONE):
            rows_out = []
            for name, (statement, types) in STATEMENTS.items():
                rows = [] if iid is None else session.execute(
                    select(FinLineItem.period_end, func.bool_and(FinLineItem.derived))
                    .where(
                        FinLineItem.instrument_id == iid,
                        FinLineItem.basis == basis,
                        FinLineItem.statement == statement,
                        FinLineItem.period_type.in_(types),
                    )
                    .group_by(FinLineItem.period_end)
                ).all()  # fmt: skip
                pairs: list[tuple[date, bool]] = [(pe, bool(d)) for pe, d in rows]
                if name == "BS":  # a fiscal year's balance sheet is the one at its end
                    pairs = [(pe, d) for pe, d in pairs if pe.month == fy_end_month]
                filed = {_fy(pe, fy_end_month) for pe, only_derived in pairs if not only_derived}
                derived = {_fy(pe, fy_end_month) for pe, only_derived in pairs if only_derived}
                years = filed | derived
                quarters = 0 if iid is None or name != "P&L" else session.scalar(
                    select(func.count(func.distinct(FinLineItem.period_end)))
                    .where(FinLineItem.instrument_id == iid, FinLineItem.basis == basis,
                           FinLineItem.statement == statement,
                           FinLineItem.period_type == PeriodType.QUARTER)
                ) or 0  # fmt: skip
                rows_out.append(Coverage(
                    symbol, basis, name, min(years, default=None), max(years, default=None),
                    len(filed), len(derived - filed), quarters,
                ))  # fmt: skip
            per_basis[basis] = rows_out
        found = [b for b, rows in per_basis.items()
                 if any(r.earliest_fy or r.quarters for r in rows)]  # fmt: skip
        for basis in found or [StatementType.CONSOLIDATED]:
            out += per_basis[basis]
    return out


# ───────────────────────── the stock page grid (§3.6 step 4) ─────────────────────────

# Source of a cell, best first: exchange XBRL, annual-report PDF, FY summed from quarters, or a
# wide-table source (Screener upload, yfinance) with no line items behind it.
_LINE_SOURCES = {
    "nse_xbrl": "xbrl",
    "upload_xbrl": "xbrl",
    "offline_xbrl": "xbrl",
    PDF_SOURCE: "pdf",
    "derived": "derived",
}
SOURCE_ORDER = ("xbrl", "pdf", "derived", "screener", "yfinance", "nse", "offline")
# a wide fin_annual column that shows the statement is present
_WIDE_MARKER: dict[StatementName, str] = {"P&L": "revenue", "BS": "total_assets", "CF": "cfo"}
_NAMES: dict[LineStatement, StatementName] = {
    LineStatement.PL: "P&L",
    LineStatement.BS: "BS",
    LineStatement.CF: "CF",
}


@dataclass(frozen=True)
class GridCell:
    fiscal_year: int
    statement: StatementName
    sources: list[str]  # best first (SOURCE_ORDER); empty = a gap
    items: int  # line items stored for the cell
    pending_review: int  # annual-report values waiting in the review queue
    note: str | None = None  # why a gap is expected (e.g. cash flow not in results XBRL)


@dataclass(frozen=True)
class BasisGrid:
    basis: StatementType
    cells: list[GridCell]


CF_NOT_IN_XBRL = "not in XBRL: use the annual report"


def cf_gap_note(fiscal_year: int, *, bank: bool, cash_flow_from_fy: int | None) -> str | None:
    """A cash-flow gap the results XBRL cannot fill: banks' results have no cash-flow
    statement, and nobody's had one before ``cash_flow_from_fy``."""
    if bank or (cash_flow_from_fy is not None and fiscal_year < cash_flow_from_fy):
        return CF_NOT_IN_XBRL
    return None


def files_as_bank(session: Session, instrument_id: int) -> bool:
    """The exchange lists the company's results in the banking format."""
    q = select(func.bool_or(ResultFiling.is_bank)).where(
        ResultFiling.instrument_id == instrument_id
    )
    return bool(session.scalar(q))


def coverage_grid(
    session: Session,
    instrument_id: int,
    years: list[int],
    fy_end_month: int,
    *,
    bank: bool = False,
    cash_flow_from_fy: int | None = None,
) -> list[BasisGrid]:
    """Years by statements per basis, for the stock page. A basis with nothing stored is left
    out unless neither has anything (then consolidated, all gaps). A cash-flow gap the
    results XBRL cannot fill carries a note (:func:`cf_gap_note`)."""
    ends = {
        y: (pd.Timestamp(year=y, month=fy_end_month, day=1) + pd.offsets.MonthEnd(0)).date()
        for y in years
    }
    by_end = {e: y for y, e in ends.items()}
    out: list[BasisGrid] = []
    for basis in (StatementType.CONSOLIDATED, StatementType.STANDALONE):
        found: dict[tuple[int, StatementName], set[str]] = {}
        counts: dict[tuple[int, StatementName], int] = {}
        for pe, ptype, statement, source, n in session.execute(
            select(FinLineItem.period_end, FinLineItem.period_type, FinLineItem.statement,
                   FinLineItem.source, func.count())
            .where(FinLineItem.instrument_id == instrument_id, FinLineItem.basis == basis,
                   FinLineItem.period_end.in_(list(ends.values())),
                   FinLineItem.period_type != PeriodType.QUARTER)
            .group_by(FinLineItem.period_end, FinLineItem.period_type, FinLineItem.statement,
                      FinLineItem.source)
        ).all():  # fmt: skip
            name = _NAMES.get(statement)
            if name is None or (name == "BS") != (ptype is PeriodType.INSTANT):
                continue
            key = (by_end[pe], name)
            found.setdefault(key, set()).add(_LINE_SOURCES.get(source, source))
            counts[key] = counts.get(key, 0) + n
        for row in session.scalars(
            select(FinAnnual).where(FinAnnual.instrument_id == instrument_id,
                                    FinAnnual.statement_type == basis,
                                    FinAnnual.period_end.in_(list(ends.values())))
        ):  # fmt: skip
            for name, col in _WIDE_MARKER.items():
                key = (by_end[row.period_end], name)
                if getattr(row, col) is not None and key not in found:
                    found[key] = {row.source}
        pending: dict[tuple[int, StatementName], int] = {}
        for pe, statement, n in session.execute(
            select(PdfLineCandidate.period_end, PdfLineCandidate.statement, func.count())
            .where(PdfLineCandidate.instrument_id == instrument_id,
                   PdfLineCandidate.basis == basis,
                   PdfLineCandidate.status == ReviewStatus.PENDING,
                   PdfLineCandidate.period_end.in_(list(ends.values())))
            .group_by(PdfLineCandidate.period_end, PdfLineCandidate.statement)
        ).all():  # fmt: skip
            pending[(by_end[pe], _NAMES[statement])] = n
        cells = [
            GridCell(
                y, name,
                sorted(found.get((y, name), set()),
                       key=lambda s: SOURCE_ORDER.index(s) if s in SOURCE_ORDER else 99),
                counts.get((y, name), 0), pending.get((y, name), 0),
                cf_gap_note(y, bank=bank, cash_flow_from_fy=cash_flow_from_fy)
                if name == "CF" and not found.get((y, name)) else None,
            )
            for y in years for name in STATEMENTS
        ]  # fmt: skip
        out.append(BasisGrid(basis, cells))
    kept = [g for g in out if any(c.sources or c.pending_review for c in g.cells)]
    return kept or out[:1]


def years_summary(session: Session, symbol: str, fy_end_month: int) -> str:
    """``P&L 3 yr, BS 3 yr, CF 0 yr (consolidated)`` for the pipeline panel: fiscal years per
    statement (filed + summed from quarters) of the basis the report uses (consolidated when
    it has anything, rule 5)."""
    rows = coverage(session, [symbol], fy_end_month)
    basis = rows[0].basis if rows else StatementType.CONSOLIDATED
    parts = [f"{r.statement} {r.years + r.derived_years} yr" for r in rows if r.basis == basis]
    return f"{', '.join(parts)} ({basis.value})" if parts else "no statements stored"
