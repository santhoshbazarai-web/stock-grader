"""Reconciliation data (SPEC v0.2 §3.9): each source's figures for the latest periods, and the
``reconciliation_issues`` they produce. The comparison itself is pure
(``app.fundamentals.reconcile``).

Sources (``jobs.reconciliation.sources``; all in rupees):

- ``nse_xbrl``: the exchange-filed line items (latest version; summed quarters excluded), with
  EBITDA derived as in the canonical tables (PBT + interest + depreciation - other income).
  Its superseded versions and its other-basis figures are the clues for the cause.
- ``annual_report_pdf``: values read from annual reports that were accepted (automatically or
  by the owner); values waiting in the review queue are not compared.
- ``indianapi``: the Indian API's statements, re-mapped from its cached answers
  (``add_vendor``); its EPS only for periods after the latest split / bonus.
- ``yfinance`` (the fundamentals fallback) and ``market_lens`` (optional): fetched by the
  caller through the router and passed in as frames.

BSE XBRL is not ingested yet, so it is not a source here.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import ReconciliationConfig
from app.data.canonical import Table
from app.data.indianapi_parse import Mapped
from app.data.results_store import EXCHANGE_SOURCE
from app.data.xbrl import ItemValue, wide_record
from app.db.enums import IssueStatus, PeriodType, ReviewStatus, StatementType
from app.db.models import FinLineItem, PdfLineCandidate, ReconciliationIssue
from app.db.upsert import upsert
from app.fundamentals.reconcile import CRORE, Finding, Key, Reconciliation
from app.fundamentals.xbrl_map import XbrlMap

_GROUP = {PeriodType.QUARTER: "quarter", PeriodType.YEAR: "year", PeriodType.INSTANT: "year"}
_TABLE: dict[str, Table] = {"quarter": "fin_quarterly", "year": "fin_annual"}
_ACCEPTED = (ReviewStatus.AUTO_ACCEPTED, ReviewStatus.ACCEPTED, ReviewStatus.CORRECTED)


@dataclass
class Collected:
    basis: StatementType
    values: dict[Key, dict[str, float]] = field(default_factory=dict)
    other_basis: dict[Key, float] = field(default_factory=dict)
    earlier_versions: dict[Key, list[float]] = field(default_factory=dict)
    periods: list[tuple[date, str]] = field(default_factory=list)  # compared (end, type)

    def add(self, key: Key, source: str, value: float | None) -> None:
        if value is not None and not pd.isna(value):
            self.values.setdefault(key, {})[source] = float(value)


def _wide(items: Mapping[str, ItemValue], group: str, xmap: XbrlMap) -> dict[str, Any]:
    """Canonical record in rupees (wide_record works in crore), with the ``extra`` items (bank
    deposits, advances ...) flattened in. Per-share and share-count items stay as they are."""
    rec = wide_record(items, _TABLE[group], xmap)
    flat = {**{k: v for k, v in rec.items() if k != "extra"}, **(rec.get("extra") or {})}
    return {k: v * CRORE if _is_amount(k, xmap) else v for k, v in flat.items()
            if isinstance(v, int | float)}  # fmt: skip


def _is_amount(code: str, xmap: XbrlMap) -> bool:
    spec = xmap.items.get(code)
    if spec is not None:
        return spec.unit == "amount"
    return code not in ("shares_diluted_cr", "book_value_per_share")  # derived wide columns


def company_basis(session: Session, iid: int) -> StatementType:
    """Consolidated when the company files consolidated figures (rule 5), else standalone."""
    has = session.scalar(select(FinLineItem.id).where(
        FinLineItem.instrument_id == iid, FinLineItem.source == EXCHANGE_SOURCE,
        FinLineItem.basis == StatementType.CONSOLIDATED).limit(1))  # fmt: skip
    return StatementType.CONSOLIDATED if has is not None else StatementType.STANDALONE


def collect_filed(
    session: Session, iid: int, cfg: ReconciliationConfig, xmap: XbrlMap
) -> Collected:
    """The exchange XBRL and accepted annual-report figures for the latest ``years`` fiscal
    years and ``quarters`` quarters of the company's basis."""
    basis = company_basis(session, iid)
    out = Collected(basis)
    rows = session.scalars(
        select(FinLineItem).where(
            FinLineItem.instrument_id == iid, FinLineItem.source == EXCHANGE_SOURCE,
            FinLineItem.derived.is_(False),
            FinLineItem.period_type.in_(list(_GROUP)),
        ).order_by(FinLineItem.version)
    ).all()  # fmt: skip
    # (basis, end, group) → code → versions (oldest first)
    grouped: dict[tuple[StatementType, date, str], dict[str, list[FinLineItem]]] = {}
    for r in rows:
        grouped.setdefault((r.basis, r.period_end, _GROUP[r.period_type]), {}) \
            .setdefault(r.item_code, []).append(r)  # fmt: skip
    ends = {g: sorted({e for (b, e, gg) in grouped if b == basis and gg == g}, reverse=True)
            for g in ("year", "quarter")}  # fmt: skip
    out.periods = [(e, "year") for e in ends["year"][:cfg.years]] + \
        [(e, "quarter") for e in ends["quarter"][:cfg.quarters]]  # fmt: skip
    for end, group in out.periods:
        for b in (basis, StatementType.STANDALONE if basis is StatementType.CONSOLIDATED
                  else StatementType.CONSOLIDATED):  # fmt: skip
            items = grouped.get((b, end, group))
            if not items:
                continue
            latest = {c: ItemValue(v[-1].value_inr, v[-1].tag or "") for c, v in items.items()}
            wide = _wide(latest, group, xmap)
            for code in cfg.items:
                key = (end, group, code)
                if b is basis:
                    out.add(key, "nse_xbrl", wide.get(code))
                    earlier = [r.value_inr for r in items.get(code, [])[:-1]]
                    if earlier:
                        out.earlier_versions[key] = earlier
                elif code in wide:
                    out.other_basis[key] = wide[code]
    year_ends = [e for e, g in out.periods if g == "year"]
    if year_ends and "annual_report_pdf" in cfg.sources:
        cands = session.scalars(
            select(PdfLineCandidate).where(
                PdfLineCandidate.instrument_id == iid, PdfLineCandidate.basis == basis,
                PdfLineCandidate.period_end.in_(year_ends),
                PdfLineCandidate.status.in_(_ACCEPTED),
            ).order_by(PdfLineCandidate.annual_report_id)
        ).all()  # fmt: skip
        by_end: dict[date, dict[str, ItemValue]] = {}
        for c in cands:  # the latest report's reading wins
            v = c.corrected_value_inr if c.status is ReviewStatus.CORRECTED else c.value_inr
            if v is not None:
                by_end.setdefault(c.period_end, {})[c.item_code] = ItemValue(v, "pdf")
        for end, read in by_end.items():
            wide = _wide(read, "year", xmap)
            for code in cfg.items:
                out.add((end, "year", code), "annual_report_pdf", wide.get(code))
    return out


def add_wide_frame(out: Collected, frame: pd.DataFrame, source: str, group: str,
                   items: Iterable[str]) -> int:  # fmt: skip
    """A canonical wide frame (index = period end, ₹ crore) for the compared periods, e.g.
    yfinance's annual / quarterly statements. Returns the figures added."""
    wanted = {e for e, g in out.periods if g == group}
    n = 0
    for idx, rec in frame.iterrows():
        end = pd.Timestamp(str(idx)).date()
        if end not in wanted:
            continue
        for code in items:
            v = rec.get(code)
            if v is not None and not pd.isna(v):
                out.add((end, group, code), source, float(v) * CRORE)
                n += 1
    return n


def add_long_frame(out: Collected, frame: pd.DataFrame, source: str, items: Iterable[str]) -> int:
    """A long reference frame (``period_end, period_type, basis, item_code, value_inr``), e.g.
    Market Lens, for the compared periods of the company's basis."""
    wanted = set(out.periods)
    codes = set(items)
    n = 0
    for rec in frame.to_dict("records"):
        end = rec["period_end"]
        end = end if isinstance(end, date) else pd.Timestamp(end).date()
        if ((end, rec["period_type"]) in wanted and rec["basis"] == out.basis.value
                and rec["item_code"] in codes):  # fmt: skip
            out.add((end, rec["period_type"], rec["item_code"]), source, rec["value_inr"])
            n += 1
    return n


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(abs(a), abs(b), 1.0)


def save_findings(
    session: Session, iid: int, basis: StatementType, result: Reconciliation, now: datetime
) -> dict[str, int]:
    """Upsert findings as open issues (an issue the owner ignored stays ignored while its
    figures are unchanged) and resolve open issues whose figures now agree. Does not commit."""
    issues = session.scalars(
        select(ReconciliationIssue).where(ReconciliationIssue.instrument_id == iid)
    )
    existing = {(i.period_end, _GROUP[i.period_type], i.item_code, i.source): i for i in issues}
    counts = {"open": 0, "new": 0, "resolved": 0, "ignored": 0}
    rows = []
    found: set[tuple[date, str, str, str]] = set()
    for f in result.findings:
        k = (f.period_end, f.period_type, f.item_code, f.source)
        found.add(k)
        old = existing.get(k)
        if old is not None and old.status is IssueStatus.IGNORED \
                and _same(old.value_inr, f.value) and _same(old.reference_value_inr,
                                                            f.reference_value):  # fmt: skip
            old.checked_at = now
            counts["ignored"] += 1
            continue
        still_open = old is not None and old.status is IssueStatus.OPEN
        counts["open"] += 1
        counts["new"] += not still_open
        rows.append(_issue_row(iid, basis, f, now, old.detected_at if still_open and old
                               else now))  # fmt: skip
    if rows:
        upsert(session, ReconciliationIssue, rows)
    for key, src in result.compared:
        k = (*key, src)
        old = existing.get(k)
        if k not in found and old is not None and old.status is IssueStatus.OPEN:
            old.status, old.resolved_at, old.checked_at = IssueStatus.RESOLVED, now, now
            counts["resolved"] += 1
    return counts


def _issue_row(iid: int, basis: StatementType, f: Finding, now: datetime,
               detected: datetime) -> dict[str, Any]:  # fmt: skip
    return {
        "instrument_id": iid, "period_end": f.period_end,
        "period_type": PeriodType.QUARTER if f.period_type == "quarter" else PeriodType.YEAR,
        "basis": basis, "item_code": f.item_code, "source": f.source,
        "reference_source": f.reference_source, "reference_value_inr": f.reference_value,
        "value_inr": f.value, "diff_rel": f.diff_rel, "values": f.values, "cause": f.cause,
        "reasons": f.reasons, "status": IssueStatus.OPEN, "detected_at": detected,
        "checked_at": now, "resolved_at": None,
    }  # fmt: skip


def open_issues(session: Session, iid: int) -> list[ReconciliationIssue]:
    return list(session.scalars(
        select(ReconciliationIssue)
        .where(ReconciliationIssue.instrument_id == iid,
               ReconciliationIssue.status == IssueStatus.OPEN)
        .order_by(ReconciliationIssue.period_end.desc(), ReconciliationIssue.item_code)
    ))  # fmt: skip


def issue_text(issue: ReconciliationIssue) -> str:
    """One line for the report: what differs, and the likely cause when one was found."""
    what = issue.reasons[0] if issue.reasons else f"{issue.item_code} {issue.period_end}"
    return f"{what}; {issue.reasons[1]}" if issue.cause and len(issue.reasons) > 1 else what


def add_vendor(out: Collected, mapped: Mapped, items: Iterable[str], source: str,
               restated_before: date | None = None) -> int:  # fmt: skip
    """The Indian API's figures (re-mapped from its cached answers, so they are compared even
    where a filed figure has replaced them in fin_line_items). Its EPS is on today's share
    basis, so EPS of periods before the latest split / bonus (``restated_before``) is not
    compared with the as-filed figure."""
    wanted = set(out.periods)
    codes = set(items)
    n = 0
    for v in mapped.values:
        group = "quarter" if v.period_type == "quarter" else "year"
        if (v.period_end, group) not in wanted or v.item_code not in codes:
            continue
        if v.unit == "per_share" and restated_before is not None \
                and v.period_end < restated_before:  # fmt: skip
            continue
        out.add((v.period_end, group, v.item_code), source, v.value)
        n += 1
    return n
