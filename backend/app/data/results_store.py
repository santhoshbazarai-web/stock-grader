"""Writing exchange results (XBRL) into fin_quarterly / fin_annual, merged with other sources.

Precedence, per (instrument, statement type, period end) row:

- **Exchange filings win.** A parsed XBRL filing overwrites the columns it has values for and
  sets ``source='nse'``. Columns it lacks (e.g. ``sga``, which the results format does not
  carry) keep what another source stored: a missing value never blanks a stored one.
- **Other sources only fill gaps** in exchange-filed rows (:func:`merge_upsert`): a later
  Screener upload adds what XBRL lacks and changes nothing else, not even ``source``.
- **The earliest announcement date is kept.** A company may revise a filing, and a fallback
  source may have stored the period first with its first-seen date; the earliest known date
  is when the market could first have used the figures (rule 4).
"""

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Provider
from app.data.canonical import Table, fields_for
from app.data.xbrl import ResultsFiling
from app.db.enums import StatementType
from app.db.models import FinAnnual, FinQuarterly
from app.db.upsert import upsert

EXCHANGE_SOURCE = Provider.NSE.value

FinModel = type[FinAnnual] | type[FinQuarterly]


def _existing(
    session: Session, model: FinModel, iid: int, basis: StatementType, period_end: date
) -> FinAnnual | FinQuarterly | None:
    row = session.scalar(
        select(model).where(
            model.instrument_id == iid,
            model.statement_type == basis,
            model.period_end == period_end,
        )
    )
    return cast(FinAnnual | FinQuarterly | None, row)  # select(model) over a union → Base


def _earliest(a: date | None, b: date | None) -> date | None:
    return min((d for d in (a, b) if d is not None), default=None)


def store_filing(
    session: Session,
    *,
    instrument_id: int,
    filing: ResultsFiling,
    statement_type: StatementType,
    announcement: date | None,
    fetched_at: datetime,
) -> list[str]:
    """Upsert the filing's quarter and fiscal-year rows. Returns the periods written.
    Does not commit."""
    targets: tuple[tuple[FinModel, Table, dict[str, Any] | None], ...] = (
        (FinQuarterly, "fin_quarterly", filing.quarter),
        (FinAnnual, "fin_annual", filing.annual),
    )
    written = []
    for model, table, rec in targets:
        if rec is None:
            continue
        old = _existing(session, model, instrument_id, statement_type, filing.period_end)
        row: dict[str, Any] = {
            "instrument_id": instrument_id,
            "statement_type": statement_type,
            "period_end": filing.period_end,
            "source": EXCHANGE_SOURCE,
            "fetched_at": fetched_at,
            "announcement_date": _earliest(announcement, old.announcement_date if old else None),
            **{c: rec[c] for c in fields_for(table) if rec.get(c) is not None},
        }
        extra = {**((old.extra if old else None) or {}), **(rec.get("extra") or {})}
        if extra:
            row["extra"] = extra
        if table == "fin_annual":
            row["fiscal_year"] = rec["fiscal_year"]
        upsert(session, model, [row])
        written.append(f"{'quarter' if table == 'fin_quarterly' else 'year'} {filing.period_end}")
    return written


_KEY = ("instrument_id", "statement_type", "period_end")
# Needed to form a valid insert row (NOT NULL), but never changed on an exchange-filed row.
_PROTECTED = ("source", "fetched_at", "announcement_date", "fiscal_year", "extra")


def merge_upsert(
    session: Session, model: FinModel, rows: Iterable[Mapping[str, Any]], *, source: str
) -> int:
    """Upsert rows from a non-exchange ``source`` (e.g. a Screener upload). Periods an exchange
    filing already stored keep their values, ``source`` and ``announcement_date``: only their
    NULL columns are filled. Other periods are upserted as given. Returns rows written."""
    full: list[dict[str, Any]] = []
    fills: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        old = _existing(session, model, row["instrument_id"], row["statement_type"],
                        row["period_end"])  # fmt: skip
        if old is None or old.source != EXCHANGE_SOURCE or source == EXCHANGE_SOURCE:
            full.append(dict(row))
            continue
        fill_cols = sorted(
            k
            for k, v in row.items()
            if k not in _KEY and k not in _PROTECTED and v is not None
            and getattr(old, k, None) is None
        )  # fmt: skip
        if not fill_cols:
            continue
        base = {k: row[k] for k in (*_KEY, "source", "fiscal_year") if k in row}
        fills.setdefault(tuple(fill_cols), []).append({**base, **{c: row[c] for c in fill_cols}})
    written = upsert(session, model, full) if full else 0
    for cols, group in fills.items():
        written += upsert(session, model, group, update=list(cols))
    return written
