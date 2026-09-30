"""Writing exchange results (XBRL): versioned fin_line_items, and fin_quarterly / fin_annual
rebuilt from their latest versions, merged with other sources.

Line items (SPEC v0.2 §3.6): every figure a filing reports, for its own period and its
comparatives, in rupees, with the filing, the tag that matched and when it became public. A
later, different figure for the same period is a new ``version`` (a restatement), never an
overwrite. See :func:`record_line_items`.

Wide rows, per (instrument, statement type, period end), are rebuilt from the latest versions:

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
from datetime import UTC, date, datetime, time
from typing import Any, cast

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.core.config import NseResultsConfig, Provider
from app.data.canonical import Table, fields_for, fiscal_year
from app.data.xbrl import ItemValue, ResultsFiling, assemble_wide, has_pl
from app.db.enums import LineStatement, PeriodType, StatementType
from app.db.models import FinAnnual, FinLineItem, FinQuarterly, ResultFiling
from app.db.upsert import upsert
from app.fundamentals.xbrl_map import XbrlMap, get_xbrl_map

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


# ───────────────────────── fin_line_items (long format, versioned) ─────────────────────────

_ITEM_KEY = ("period_end", "period_type", "statement", "item_code")
_FAR = datetime.max.replace(tzinfo=UTC)


def _same(a: float, b: float, unit: str, cfg: NseResultsConfig) -> bool:
    """Equal within filing rounding: a comparative re-stating the same number (rounded to
    lakhs in one filing, crores in the next) is not a restatement."""
    slack = cfg.restatement_tolerance_rel * max(abs(a), abs(b))
    if unit == "amount":
        slack = max(slack, cfg.restatement_tolerance_inr)
    return abs(a - b) <= max(slack, 1e-9)


def record_line_items(
    session: Session,
    *,
    instrument_id: int,
    basis: StatementType,
    filing: ResultsFiling,
    filing_row: ResultFiling,
    usable_from: date | None,
    cfg: NseResultsConfig,
    xmap: XbrlMap,
) -> set[tuple[date, PeriodType]]:
    """Store every period the filing reports (current and comparative) in fin_line_items.

    Per (period, statement, basis, item): the figures ever filed are ordered by when they
    became public (``usable_from``; download order does not matter, a backfill runs newest
    first). A figure equal to the previous one (within :func:`_same`) adds nothing; a different
    one is a restatement and becomes the next ``version``. Re-parsing the same filing replaces
    its own figure instead of adding one. Returns the (period_end, period_type) touched."""
    source = "upload_xbrl" if filing_row.exchange == "upload" else "nse_xbrl"
    observations: dict[tuple[Any, ...], dict[str, Any]] = {}
    for period in filing.period_items:
        for code, item in period.items.items():
            spec = xmap.items[code]
            key = (period.end, PeriodType(period.period_type), LineStatement(spec.statement), code)
            observations[key] = {
                "instrument_id": instrument_id,
                "isin": filing.isin,
                "period_end": period.end,
                "period_type": PeriodType(period.period_type),
                "statement": LineStatement(spec.statement),
                "basis": basis,
                "item_code": code,
                "value_inr": item.value,
                "unit": spec.unit,
                "source": source,
                "filing_id": filing_row.id,
                "announced_at": filing_row.disseminated_at,
                "usable_from": usable_from,
                "derived": False,
                "tag": item.tag[:512],
                "map_version": filing.map_version,
            }
    if not observations:
        return set()
    ends = {k[0] for k in observations}
    existing: dict[tuple[Any, ...], list[FinLineItem]] = {}
    for row in session.scalars(
        select(FinLineItem).where(
            FinLineItem.instrument_id == instrument_id,
            FinLineItem.basis == basis,
            FinLineItem.period_end.in_(ends),
        )
    ):
        existing.setdefault(tuple(getattr(row, k) for k in _ITEM_KEY), []).append(row)

    stale: list[int] = []
    fresh: list[dict[str, Any]] = []
    for key, obs in observations.items():
        old = existing.get(key, [])
        timeline = [_as_dict(r) for r in old if r.filing_id != obs["filing_id"] or r.derived]
        timeline.append(obs)
        timeline.sort(key=lambda r: (r["usable_from"] is None, r["usable_from"] or date.max,
                                     r["announced_at"] or _FAR, r["filing_id"] or 0))  # fmt: skip
        kept: list[dict[str, Any]] = []
        for r in timeline:
            if kept and _same(kept[-1]["value_inr"], r["value_inr"], r["unit"], cfg):
                continue
            kept.append(r)
        for version, r in enumerate(kept, start=1):
            r["version"] = version
        before = sorted((r.version, r.value_inr, r.filing_id) for r in old)
        after = [(r["version"], r["value_inr"], r["filing_id"]) for r in kept]
        if before != after:
            stale += [r.id for r in old]
            fresh += kept
    if stale:
        session.execute(delete(FinLineItem).where(FinLineItem.id.in_(stale)))
    if fresh:
        session.execute(insert(FinLineItem), fresh)
    return {(k[0], k[1]) for k in observations}


def _as_dict(r: FinLineItem) -> dict[str, Any]:
    cols = ("instrument_id", "isin", "period_end", "period_type", "statement", "basis",
            "item_code", "value_inr", "unit", "source", "filing_id", "announced_at",
            "usable_from", "derived", "tag", "map_version")  # fmt: skip
    return {c: getattr(r, c) for c in cols}


def latest_items(
    session: Session, instrument_id: int, basis: StatementType, period_end: date
) -> dict[PeriodType, dict[str, tuple[FinLineItem, date | None]]]:
    """Per period type: item_code → (latest version, the date its first version was usable)."""
    rows = session.scalars(
        select(FinLineItem)
        .where(
            FinLineItem.instrument_id == instrument_id,
            FinLineItem.basis == basis,
            FinLineItem.period_end == period_end,
        )
        .order_by(FinLineItem.version)
    ).all()
    out: dict[PeriodType, dict[str, tuple[FinLineItem, date | None]]] = {}
    for r in rows:
        per = out.setdefault(r.period_type, {})
        first = per[r.item_code][1] if r.item_code in per else r.usable_from
        per[r.item_code] = (r, first)
    return out


def _wide_from_items(
    items: dict[PeriodType, dict[str, tuple[FinLineItem, date | None]]],
    table: Table,
    xmap: XbrlMap,
) -> tuple[dict[str, Any], date | None]:
    """The wide record from latest versions, and the date the period was first public."""
    by_type = {
        ptype.value: {code: ItemValue(r.value_inr, r.tag or "") for code, (r, _) in per.items()}
        for ptype, per in items.items()
    }
    rec = assemble_wide(by_type, table, xmap)
    relevant = (PeriodType.QUARTER,) if table == "fin_quarterly" else tuple(PeriodType)
    firsts = [f for t in relevant for _, f in items.get(t, {}).values() if f is not None]
    return rec, min(firsts, default=None)


def rebuild_wide(
    session: Session,
    *,
    instrument_id: int,
    basis: StatementType,
    touched: set[tuple[date, PeriodType]],
    xmap: XbrlMap,
    fetched_at: datetime,
) -> list[str]:
    """Rewrite fin_quarterly / fin_annual rows from the latest line-item versions for every
    period the filing touched (analysis uses the latest version). A quarter needs its P&L; a
    year row is written when the year's P&L is known, or updated (e.g. a restated balance
    sheet) when it already exists. Returns the periods written."""
    targets: set[tuple[date, Table]] = set()
    for end, ptype in touched:
        targets.add((end, "fin_quarterly" if ptype is PeriodType.QUARTER else "fin_annual"))
    written = []
    for end, table in sorted(targets, key=lambda t: (t[0], t[1] != "fin_quarterly")):
        model: FinModel = FinQuarterly if table == "fin_quarterly" else FinAnnual
        rec, first = _wide_from_items(latest_items(session, instrument_id, basis, end), table,
                                      xmap)  # fmt: skip
        old = _existing(session, model, instrument_id, basis, end)
        if not has_pl(rec) and old is None:
            continue
        row: dict[str, Any] = {
            "instrument_id": instrument_id,
            "statement_type": basis,
            "period_end": end,
            "source": EXCHANGE_SOURCE,
            "fetched_at": fetched_at,
            "announcement_date": _earliest(first, old.announcement_date if old else None),
            **{c: rec[c] for c in fields_for(table) if rec.get(c) is not None},
        }
        extra = {**((old.extra if old else None) or {}), **(rec.get("extra") or {})}
        if extra:
            row["extra"] = extra
        if table == "fin_annual":
            row["fiscal_year"] = fiscal_year(datetime.combine(end, time()))
        upsert(session, model, [row])
        written.append(f"{'quarter' if table == 'fin_quarterly' else 'year'} {end}")
    return written


def store_filing(
    session: Session,
    *,
    instrument_id: int,
    filing: ResultsFiling,
    filing_row: ResultFiling,
    statement_type: StatementType,
    announcement: date | None,
    fetched_at: datetime,
    cfg: NseResultsConfig,
    xmap: XbrlMap | None = None,
) -> list[str]:
    """Record the filing's line items (every period, versioned), then rebuild the wide rows of
    every period it touched. Returns the filing's own periods written ("quarter 2024-03-31",
    "year 2024-03-31"). Does not commit."""
    xmap = xmap or get_xbrl_map()
    touched = record_line_items(
        session, instrument_id=instrument_id, basis=statement_type, filing=filing,
        filing_row=filing_row, usable_from=announcement, cfg=cfg, xmap=xmap,
    )  # fmt: skip
    written = rebuild_wide(session, instrument_id=instrument_id, basis=statement_type,
                           touched=touched, xmap=xmap, fetched_at=fetched_at)  # fmt: skip
    own = {f"quarter {filing.period_end}", f"year {filing.period_end}"}
    return [p for p in written if p in own]


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
