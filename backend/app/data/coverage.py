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
from app.db.models import FinAnnual, FinLineItem, Instrument, PdfLineCandidate

# statement → the line items that count for it (P&L: quarter or year flows; balance sheet:
# period-end balances; cash flow: year flows)
StatementName = Literal["P&L", "BS", "CF"]
STATEMENTS: dict[StatementName, tuple[LineStatement, tuple[PeriodType, ...]]] = {
    "P&L": (LineStatement.PL, (PeriodType.QUARTER, PeriodType.YEAR)),
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

    def row(self) -> str:
        span = f"FY{self.earliest_fy}-FY{self.latest_fy}" if self.earliest_fy else "—"
        extra = f" (+{self.derived_years} derived)" if self.derived_years else ""
        return f"{self.symbol:<14} {self.basis.value:<13} {self.statement:<4} {span:<14} " \
               f"{self.years:>3} yr{extra}"  # fmt: skip


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
                filed = {_fy(pe, fy_end_month) for pe, only_derived in rows if not only_derived}
                derived = {_fy(pe, fy_end_month) for pe, only_derived in rows if only_derived}
                years = filed | derived
                rows_out.append(Coverage(
                    symbol, basis, name, min(years, default=None), max(years, default=None),
                    len(filed), len(derived - filed),
                ))  # fmt: skip
            per_basis[basis] = rows_out
        found = [b for b, rows in per_basis.items() if any(r.earliest_fy for r in rows)]
        for basis in found or [StatementType.CONSOLIDATED]:
            out += per_basis[basis]
    return out


# ───────────────────────── the stock page grid (§3.6 step 4) ─────────────────────────

# Source of a cell, best first: exchange XBRL, annual-report PDF, FY summed from quarters, or a
# wide-table source (Screener upload, yfinance) with no line items behind it.
_LINE_SOURCES = {"nse_xbrl": "xbrl", "upload_xbrl": "xbrl", PDF_SOURCE: "pdf", "derived": "derived"}
SOURCE_ORDER = ("xbrl", "pdf", "derived", "screener", "yfinance", "nse")
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


@dataclass(frozen=True)
class BasisGrid:
    basis: StatementType
    cells: list[GridCell]


def coverage_grid(
    session: Session, instrument_id: int, years: list[int], fy_end_month: int
) -> list[BasisGrid]:
    """Years by statements per basis, for the stock page. A basis with nothing stored is left
    out unless neither has anything (then consolidated, all gaps)."""
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
            )
            for y in years for name in STATEMENTS
        ]  # fmt: skip
        out.append(BasisGrid(basis, cells))
    kept = [g for g in out if any(c.sources or c.pending_review for c in g.cells)]
    return kept or out[:1]
