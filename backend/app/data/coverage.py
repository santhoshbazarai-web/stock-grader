"""Fundamentals coverage per stock: which fiscal years each statement has in fin_line_items
(P17 audit item 8; a first cut of the SPEC v0.2 §3.6 step 4 coverage grid)."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.enums import LineStatement, PeriodType, StatementType
from app.db.models import FinLineItem, Instrument

# statement → the line items that count for it (P&L: quarter or year flows; balance sheet:
# period-end balances; cash flow: year flows)
STATEMENTS: dict[str, tuple[LineStatement, tuple[PeriodType, ...]]] = {
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
