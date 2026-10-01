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

import pandas as pd
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.core.config import NseResultsConfig, Provider
from app.data.canonical import Table, fields_for, fiscal_year
from app.data.xbrl import ItemValue, ResultsFiling, assemble_wide, has_bs, has_pl
from app.db.enums import LineStatement, PeriodType, StatementType
from app.db.models import FinAnnual, FinLineItem, FinQuarterly, PdfLineCandidate, ResultFiling
from app.db.upsert import upsert
from app.fundamentals.xbrl_map import XbrlMap, get_xbrl_map

EXCHANGE_SOURCE = Provider.NSE.value
# fin_line_items.source of values read from annual-report PDFs (SPEC §3.6 step 3). They only
# fill gaps: never stored where an exchange-filed figure exists, and dropped when one arrives.
PDF_SOURCE = "annual_report_pdf"
# Source of figures from the development offline exchange (app.devtools.offline_exchange): XBRL
# like an exchange filing, but synthetic, so it is labelled as such everywhere.
OFFLINE_SOURCE = Provider.OFFLINE.value

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
    source = {"upload": "upload_xbrl", OFFLINE_SOURCE: "offline_xbrl"}.get(
        filing_row.exchange, "nse_xbrl"
    )
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
                "annual_report_id": None,
                "confidence": None,
            }
    return _apply_versions(session, instrument_id, basis, observations, cfg)


def _apply_versions(
    session: Session,
    instrument_id: int,
    basis: StatementType,
    observations: dict[tuple[Any, ...], dict[str, Any]],
    cfg: NseResultsConfig,
) -> set[tuple[date, PeriodType]]:
    """Merge new observations (key → row) into each key's version timeline (see
    :func:`record_line_items`). A filed figure after a derived one is always a new version, so
    the latest version is filed even when it confirms the derived sum. An exchange-filed figure
    removes the key's annual-report PDF figures (gap fillers only); their review-queue rows are
    marked as no longer stored. Callers never pass PDF figures for keys with filed ones."""
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
    superseded: list[tuple[Any, ...]] = []
    for key, obs in observations.items():
        old = existing.get(key, [])
        timeline = [_as_dict(r) for r in old if not _replaced_by(r, obs)]
        if obs["source"] != PDF_SOURCE and not obs["derived"]:
            if any(r["source"] == PDF_SOURCE for r in timeline):
                superseded.append(key)
            timeline = [r for r in timeline if r["source"] != PDF_SOURCE]
        timeline.append(obs)
        timeline.sort(key=lambda r: (r["usable_from"] is None, r["usable_from"] or date.max,
                                     r["announced_at"] or _FAR, r["filing_id"] or 0))  # fmt: skip
        kept: list[dict[str, Any]] = []
        for r in timeline:
            confirms_derived = bool(kept) and kept[-1]["derived"] and not r["derived"]
            if kept and not confirms_derived and _same(kept[-1]["value_inr"], r["value_inr"],
                                                       r["unit"], cfg):  # fmt: skip
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
    if superseded:
        _mark_superseded(session, instrument_id, basis, superseded)
    return {(k[0], k[1]) for k in observations}


def _mark_superseded(
    session: Session, instrument_id: int, basis: StatementType, keys: list[tuple[Any, ...]]
) -> None:
    for period_end, _, statement, code in keys:
        session.execute(
            update(PdfLineCandidate)
            .where(
                PdfLineCandidate.instrument_id == instrument_id,
                PdfLineCandidate.basis == basis,
                PdfLineCandidate.period_end == period_end,
                PdfLineCandidate.statement == statement,
                PdfLineCandidate.item_code == code,
                PdfLineCandidate.stored.is_(True),
            )
            .values(stored=False, note="superseded by an exchange XBRL figure")
        )


def _replaced_by(r: FinLineItem, obs: Mapping[str, Any]) -> bool:
    """Re-parsing the same filing or annual report, or re-deriving a year, replaces its own
    earlier figure."""
    if obs["derived"]:
        return bool(r.derived)
    if r.derived:
        return False
    if obs["annual_report_id"] is not None:
        return bool(r.annual_report_id == obs["annual_report_id"])
    return r.annual_report_id is None and bool(r.filing_id == obs["filing_id"])


def _as_dict(r: FinLineItem) -> dict[str, Any]:
    cols = ("instrument_id", "isin", "period_end", "period_type", "statement", "basis",
            "item_code", "value_inr", "unit", "source", "filing_id", "announced_at",
            "usable_from", "derived", "tag", "map_version", "annual_report_id",
            "confidence")  # fmt: skip
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
    source: str | None = EXCHANGE_SOURCE,
    clear: frozenset[str] = frozenset(),
    fy_end_month: int | None = None,
) -> list[str]:
    """Rewrite fin_quarterly / fin_annual rows from the latest line-item versions for every
    period the filing touched (analysis uses the latest version). A quarter needs its P&L; a
    year row is written when the year's P&L is known, or its fiscal-year-end balance sheet
    (period ending in ``fy_end_month``: a bank's FY balance sheet with no full-year P&L parsed
    still gives book value), or updated (e.g. a restated balance sheet) when it exists.
    ``source=None`` keeps an existing row's source (annual-report
    values fill a row; they don't make it exchange-filed). ``clear``: columns set to NULL when
    the line items no longer give them (a rejected PDF value). Returns the periods written."""
    targets: set[tuple[date, Table]] = set()
    for end, ptype in touched:
        targets.add((end, "fin_quarterly" if ptype is PeriodType.QUARTER else "fin_annual"))
    written = []
    for end, table in sorted(targets, key=lambda t: (t[0], t[1] != "fin_quarterly")):
        model: FinModel = FinQuarterly if table == "fin_quarterly" else FinAnnual
        rec, first = _wide_from_items(latest_items(session, instrument_id, basis, end), table,
                                      xmap)  # fmt: skip
        old = _existing(session, model, instrument_id, basis, end)
        year_end_bs = (table == "fin_annual" and fy_end_month is not None
                       and end.month == fy_end_month and has_bs(rec))  # fmt: skip
        if not has_pl(rec) and not year_end_bs and old is None:
            continue
        row: dict[str, Any] = {
            "instrument_id": instrument_id,
            "statement_type": basis,
            "period_end": end,
            "source": source or (old.source if old is not None else PDF_SOURCE),
            "fetched_at": fetched_at,
            "announcement_date": _earliest(first, old.announcement_date if old else None),
            **{c: rec[c] for c in fields_for(table) if rec.get(c) is not None},
            **{c: None for c in clear if c in fields_for(table) and rec.get(c) is None},
        }
        extra = {**((old.extra if old else None) or {}), **(rec.get("extra") or {})}
        if extra:
            row["extra"] = extra
        if table == "fin_annual":
            row["fiscal_year"] = fiscal_year(datetime.combine(end, time()))
            year = latest_items(session, instrument_id, basis, end).get(PeriodType.YEAR, {})
            row["is_derived"] = any(
                r.derived for code, (r, _) in year.items() if code in ("revenue", "pat", "pbt")
            )
        upsert(session, model, [row])
        written.append(f"{'quarter' if table == 'fin_quarterly' else 'year'} {end}")
    return written


def _quarter_ends(fy_end: date) -> list[date]:
    """The four quarter ends of the fiscal year ending ``fy_end`` (month ends)."""
    stamp = pd.Timestamp(fy_end)
    return [(stamp - pd.offsets.MonthEnd(3 * k)).date() for k in range(4)]


def _fy_end_month(session: Session, instrument_id: int, cfg: NseResultsConfig) -> int:
    """The company's fiscal-year end month, from its filed (not derived) years."""
    month = session.scalar(
        select(func.extract("month", FinLineItem.period_end))
        .where(
            FinLineItem.instrument_id == instrument_id,
            FinLineItem.period_type == PeriodType.YEAR,
            FinLineItem.derived.is_(False),
        )
        .group_by(func.extract("month", FinLineItem.period_end))
        .order_by(func.count().desc())
        .limit(1)
    )
    return int(month) if month is not None else cfg.default_fy_end_month


def derive_years(
    session: Session,
    *,
    instrument_id: int,
    basis: StatementType,
    quarters: set[date],
    cfg: NseResultsConfig,
    xmap: XbrlMap,
) -> set[tuple[date, PeriodType]]:
    """SPEC §3.6 step 5: sum a fiscal year's P&L from its four quarters when no annual figures
    were filed for it (e.g. its Q4 filing is missing or failed).

    For each fiscal year containing one of ``quarters``: if all four quarters are stored and
    the year has no filed P&L, every P&L amount item reported in all four quarters is summed
    from their latest versions. It is stored as a year line item with ``derived = true``,
    source ``derived``, and ``usable_from`` = the day the last of those quarter figures became
    usable. Per-share items (EPS) are not summed; balances come from the Q4 quarter
    (``carry_to_year``). A later filed annual figure supersedes it as a new version."""
    month = _fy_end_month(session, instrument_id, cfg)
    years = set()
    for q in quarters:
        stamp = pd.Timestamp(q)
        year = stamp.year if stamp.month <= month else stamp.year + 1
        years.add((pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)).date())
    observations: dict[tuple[Any, ...], dict[str, Any]] = {}
    for fy_end in sorted(years):
        ends = _quarter_ends(fy_end)
        latest = {e: latest_items(session, instrument_id, basis, e) for e in ends}
        per_quarter = [latest[e].get(PeriodType.QUARTER, {}) for e in ends]
        filed_year = latest[fy_end].get(PeriodType.YEAR, {})
        if any(not q for q in per_quarter) or any(
            not r.derived and xmap.items[c].statement == "pl" for c, (r, _) in filed_year.items()
        ):
            continue
        for code, spec in xmap.items.items():
            if spec.statement != "pl" or spec.unit != "amount":
                continue
            rows = [q[code][0] for q in per_quarter if code in q]
            if len(rows) < 4:
                continue
            dates = [r.usable_from for r in rows]
            key = (fy_end, PeriodType.YEAR, LineStatement.PL, code)
            observations[key] = {
                "instrument_id": instrument_id,
                "isin": rows[0].isin,
                "period_end": fy_end,
                "period_type": PeriodType.YEAR,
                "statement": LineStatement.PL,
                "basis": basis,
                "item_code": code,
                "value_inr": float(sum(r.value_inr for r in rows)),
                "unit": spec.unit,
                "source": "derived",
                "filing_id": None,
                "announced_at": None,
                "usable_from": None if None in dates else max(d for d in dates if d),
                "derived": True,
                "tag": "sum of quarters " + ", ".join(e.isoformat() for e in reversed(ends)),
                "map_version": max((r.map_version or 0) for r in rows) or None,
                "annual_report_id": None,
                "confidence": None,
            }
    return _apply_versions(session, instrument_id, basis, observations, cfg)


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
    quarters = {end for end, ptype in touched if ptype is PeriodType.QUARTER}
    touched |= derive_years(session, instrument_id=instrument_id, basis=statement_type,
                            quarters=quarters, cfg=cfg, xmap=xmap)  # fmt: skip
    source = OFFLINE_SOURCE if filing_row.exchange == OFFLINE_SOURCE else EXCHANGE_SOURCE
    written = rebuild_wide(session, instrument_id=instrument_id, basis=statement_type,
                           touched=touched, xmap=xmap, fetched_at=fetched_at, source=source,
                           fy_end_month=_fy_end_month(session, instrument_id, cfg))  # fmt: skip
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
