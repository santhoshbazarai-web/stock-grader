"""Indian API: raw-response cache, vendor names, refresh policy and storage of mapped values
(SPEC §3.2, §3.6). Mapping itself is pure (``indianapi_parse``); this module writes.

- **Raw first.** Every answer is stored (``vendor_responses``: endpoint, params without the
  key, a hash of them, status, payload) and saved under ``data/raw/indianapi/yyyy/mm/dd/``
  before anything is read from it. Analysis reads our tables only.
- **Names.** The vendor looks stocks up by name: the name that last gave a verified answer
  (``vendor_names``), then the symbol master's name (with and without "Limited"), the NSE
  symbol, then ``indianapi.name_fallbacks``; at most ``max_name_attempts`` per stock.
- **Gap filler under filed figures.** A value is written to fin_line_items only where no
  exchange-filed (or annual-report) figure exists for that period and item; one written
  earlier is removed when a filed figure arrives (``results_store._apply_versions``). Rows
  carry ``source='indianapi'`` and ``vendor_reclassified=true``.
"""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.core.config import (
    EventClassificationConfig,
    IndianApiConfig,
    IndianApiEndpoint,
    NseResultsConfig,
    Provider,
)
from app.data.event_store import store_events
from app.data.indianapi_checks import EXCHANGE, MetricCheck, OurAction
from app.data.indianapi_parse import Difference, Mapped, VendorValue
from app.data.providers.indianapi import VendorResponse as Answer
from app.data.raw_store import RawStore
from app.data.results_store import (
    EXCHANGE_SOURCE,
    OFFLINE_SOURCE,
    PDF_SOURCE,
    VENDOR_SOURCE,
    _fy_end_month,
    rebuild_wide,
)
from app.db.enums import (
    CorporateActionType,
    IssueStatus,
    LineStatement,
    PeriodType,
    StatementType,
)
from app.db.models import (
    CorporateAction,
    FinLineItem,
    Instrument,
    ReconciliationIssue,
    Shareholding,
    VendorName,
    VendorResponse,
)
from app.db.upsert import upsert
from app.fundamentals.xbrl_map import XbrlMap

PROVIDER = Provider.INDIANAPI.value
# fields the reconciliation compares; vendor-internal differences on these are open issues
RECONCILED_ITEMS = frozenset({"revenue", "pat", "profit_after_tax", "total_equity", "deposits",
                              "advances", "eps_diluted"})  # fmt: skip
KEY_METRICS_SOURCE = "indianapi_keymetrics"
_LIMITED = re.compile(r"\s+(limited|ltd\.?)$", re.IGNORECASE)


def endpoint_key(ep: IndianApiEndpoint) -> str:
    """``/stock`` or the stats name: how cached answers are looked up."""
    return ep.params.get("stats", ep.path)


def params_hash(params: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()


# ───────────────────────── raw cache ─────────────────────────


def save_answer(
    session: Session,
    raw: RawStore | None,
    *,
    instrument_id: int | None,
    symbol: str,
    ep: IndianApiEndpoint,
    answer: Answer,
    identity_ok: bool | None = None,
    note: str | None = None,
) -> VendorResponse:
    """Store the answer before it is read (table + raw file). The params never hold the key."""
    h = params_hash(answer.params)
    raw_path = None
    if raw is not None:
        name = f"{endpoint_key(ep).strip('/')}_{symbol}_{h[:12]}.json"
        raw_path = raw.relative(raw.save(PROVIDER, name, answer.content))
    row = VendorResponse(provider=PROVIDER, instrument_id=instrument_id, symbol=symbol,
                         endpoint=endpoint_key(ep), params=answer.params, params_hash=h,
                         status=answer.status, fetched_at=answer.fetched_at,
                         payload=answer.payload, raw_path=raw_path, identity_ok=identity_ok,
                         note=note)  # fmt: skip
    session.add(row)
    session.flush()
    return row


def cached_answers(session: Session, instrument_id: int) -> dict[str, VendorResponse]:
    """The latest stored answer per endpoint key for the stock; /stock only if its identity
    was verified, historical stats only if fetched with a verified name."""
    rows = session.scalars(
        select(VendorResponse)
        .where(VendorResponse.provider == PROVIDER, VendorResponse.instrument_id == instrument_id)
        .order_by(VendorResponse.fetched_at.desc(), VendorResponse.id.desc())
    ).all()
    out: dict[str, VendorResponse] = {}
    for r in rows:
        if r.endpoint in out or r.payload is None:
            continue
        if r.endpoint == "/stock" and r.identity_ok is not True:
            continue
        if r.endpoint != "/stock" and r.identity_ok is False:
            continue
        out[r.endpoint] = r
    return out


# ───────────────────────── names ─────────────────────────


def candidate_names(session: Session, inst: Instrument, cfg: IndianApiConfig,
                    limit: int) -> list[str]:  # fmt: skip
    cached = session.scalar(select(VendorName.vendor_name).where(
        VendorName.provider == PROVIDER, VendorName.instrument_id == inst.id))  # fmt: skip
    names: list[str] = []
    for n in (cached, _LIMITED.sub("", inst.name or "").strip() or None, inst.name, inst.symbol,
              *cfg.name_fallbacks.get(inst.symbol, [])):  # fmt: skip
        if n and n.strip() and n.strip().lower() not in {x.lower() for x in names}:
            names.append(n.strip())
    return names[:limit]


def remember_name(session: Session, instrument_id: int, name: str, now: datetime) -> None:
    upsert(session, VendorName, [{"provider": PROVIDER, "instrument_id": instrument_id,
                                  "vendor_name": name, "verified_at": now}])  # fmt: skip


# ───────────────────────── refresh policy ─────────────────────────


@dataclass(frozen=True)
class Refresh:
    due: bool
    reason: str


def refresh_due(last: datetime | None, *, now: datetime, cfg: IndianApiConfig,
                results_since: date | None = None, user: bool = False) -> Refresh:  # fmt: skip
    """Fetch again only when (a) results were announced after the last fetch, (b) the cache
    is older than ``refresh_days``, or (c) the user asked and it is older than
    ``user_refresh_min_age_hours``. Never fetched: due."""
    if last is None:
        return Refresh(True, "not fetched yet")
    age = now - last
    if results_since is not None and results_since > last.date():
        return Refresh(True, f"results announced {results_since}, after the last fetch")
    if age > timedelta(days=cfg.refresh_days):
        return Refresh(True, f"cache older than {cfg.refresh_days} days")
    if user and age > timedelta(hours=cfg.user_refresh_min_age_hours):
        return Refresh(True, "refresh requested")
    if user:
        return Refresh(False, f"fetched {age.total_seconds() / 3600:.0f} h ago (refreshes after "
                       f"{cfg.user_refresh_min_age_hours:g} h): cache used")  # fmt: skip
    return Refresh(False, f"cache from {last.date()} is current")


# ───────────────────────── storing mapped values ─────────────────────────


@dataclass
class Stored:
    touched: set[tuple[date, PeriodType]] = field(default_factory=set)
    written: int = 0
    kept_filed: int = 0  # keys an exchange / annual-report figure already covers
    periods: list[str] = field(default_factory=list)


def record_vendor_values(session: Session, *, instrument_id: int, isin: str | None,
                         values: Iterable[VendorValue], map_version: int) -> Stored:  # fmt: skip
    out = Stored()
    vals = list(values)
    ends = {v.period_end for v in vals}
    existing: dict[tuple[Any, ...], list[FinLineItem]] = {}
    if ends:
        for r in session.scalars(select(FinLineItem).where(
                FinLineItem.instrument_id == instrument_id,
                FinLineItem.basis == StatementType.CONSOLIDATED,
                FinLineItem.period_end.in_(ends))):  # fmt: skip
            key = (r.period_end, r.period_type, r.statement, r.item_code)
            existing.setdefault(key, []).append(r)
    stale: list[int] = []
    fresh: list[dict[str, Any]] = []
    for v in vals:
        ptype, stmt = PeriodType(v.period_type), LineStatement(v.statement)
        key = (v.period_end, ptype, stmt, v.item_code)
        rows = existing.get(key, [])
        if any(r.source != VENDOR_SOURCE for r in rows):
            out.kept_filed += 1
            continue
        if len(rows) == 1 and rows[0].value_inr == v.value and rows[0].tag == v.origin:
            continue
        stale += [r.id for r in rows]
        fresh.append({
            "instrument_id": instrument_id, "isin": isin, "period_end": v.period_end,
            "period_type": ptype, "statement": stmt, "basis": StatementType.CONSOLIDATED,
            "item_code": v.item_code, "value_inr": v.value, "unit": v.unit, "version": 1,
            "source": VENDOR_SOURCE, "filing_id": None, "announced_at": None,
            "usable_from": None, "derived": False, "vendor_reclassified": True,
            "tag": f"indianapi {v.origin}"[:512], "map_version": map_version,
            "annual_report_id": None, "confidence": None,
        })  # fmt: skip
        out.touched.add((v.period_end, ptype))
    if stale:
        session.execute(delete(FinLineItem).where(FinLineItem.id.in_(stale)))
    if fresh:
        session.execute(insert(FinLineItem), fresh)
    out.written = len(fresh)
    return out


def store_mapped(session: Session, *, instrument_id: int, isin: str | None, mapped: Mapped,
                 map_version: int, xmap: XbrlMap, results_cfg: NseResultsConfig,
                 now: datetime) -> Stored:  # fmt: skip
    """Write the mapped values and rebuild the wide rows they touch. A wide row of a better
    source (exchange, offline exchange, annual report) keeps its source label."""
    st = record_vendor_values(session, instrument_id=instrument_id, isin=isin,
                              values=mapped.values, map_version=map_version)  # fmt: skip
    if st.touched:
        st.periods = rebuild_wide(
            session, instrument_id=instrument_id, basis=StatementType.CONSOLIDATED,
            touched=st.touched, xmap=xmap, fetched_at=now, source=VENDOR_SOURCE,
            fy_end_month=_fy_end_month(session, instrument_id, results_cfg),
            keep_sources=frozenset({EXCHANGE_SOURCE, OFFLINE_SOURCE, PDF_SOURCE}),
        )  # fmt: skip
    return st


def save_differences(session: Session, instrument_id: int, diffs: Iterable[Difference],
                     now: datetime) -> int:  # fmt: skip
    """/stock vs /historical_stats differences on the reconciled fields → open issues (the
    report's data-quality panel; they lower the valuation confidence like any other)."""
    rows = []
    for d in diffs:
        if d.item_code not in RECONCILED_ITEMS:
            continue
        kept_hist = d.kept == "quarter_results"
        rows.append({
            "instrument_id": instrument_id, "period_end": d.period_end,
            "period_type": PeriodType(d.period_type), "basis": StatementType.CONSOLIDATED,
            "item_code": d.item_code, "source": "indianapi_hist",
            "reference_source": "indianapi_stock",
            "reference_value_inr": d.hist if kept_hist else d.stock,
            "value_inr": d.stock if kept_hist else d.hist, "diff_rel": d.rel,
            "values": {"indianapi_stock": d.stock, "indianapi_hist": d.hist},
            "cause": "vendor_internal", "reasons": [d.text()], "status": IssueStatus.OPEN,
            "detected_at": now, "checked_at": now, "resolved_at": None,
        })  # fmt: skip
    if rows:
        upsert(session, ReconciliationIssue, rows,
               update=["reference_value_inr", "value_inr", "diff_rel", "values", "reasons",
                       "checked_at"])  # fmt: skip
    return len(rows)


def save_key_metric_checks(session: Session, instrument_id: int, checks: Iterable[MetricCheck],
                           period_end: date | None, now: datetime) -> int:  # fmt: skip
    """keyMetrics that differ from ours → open issues (``cause='key_metric'``; the values are
    the metric itself, not rupees); one that agrees again resolves its open issue."""
    if period_end is None:
        return 0
    rows, agree = [], []
    for c in checks:
        code = f"km_{c.name}"
        if c.ok is True:
            agree.append(code)
        if c.ok is not False or c.vendor is None or c.ours is None or c.rel is None:
            continue
        rows.append({
            "instrument_id": instrument_id, "period_end": period_end,
            "period_type": PeriodType.YEAR, "basis": StatementType.CONSOLIDATED,
            "item_code": code, "source": KEY_METRICS_SOURCE, "reference_source": "derived",
            "reference_value_inr": c.ours, "value_inr": c.vendor, "diff_rel": c.rel,
            "values": {KEY_METRICS_SOURCE: c.vendor, "derived": c.ours},
            "cause": "key_metric", "reasons": [c.text], "status": IssueStatus.OPEN,
            "detected_at": now, "checked_at": now, "resolved_at": None,
        })  # fmt: skip
    if rows:
        upsert(session, ReconciliationIssue, rows,
               update=["reference_value_inr", "value_inr", "diff_rel", "values", "reasons",
                       "checked_at"])  # fmt: skip
    if agree:
        for issue in session.scalars(select(ReconciliationIssue).where(
                ReconciliationIssue.instrument_id == instrument_id,
                ReconciliationIssue.source == KEY_METRICS_SOURCE,
                ReconciliationIssue.item_code.in_(agree),
                ReconciliationIssue.status == IssueStatus.OPEN)):  # fmt: skip
            issue.status, issue.resolved_at, issue.checked_at = IssueStatus.RESOLVED, now, now
    return len(rows)


def our_split_bonus(session: Session, instrument_id: int) -> list[OurAction]:
    return [OurAction(a.ex_date, a.action_type, a.ratio_old, a.ratio_new)
            for a in session.scalars(select(CorporateAction).where(
                CorporateAction.instrument_id == instrument_id,
                CorporateAction.action_type.in_([CorporateActionType.SPLIT,
                                                 CorporateActionType.BONUS])))]  # fmt: skip


def save_board_meetings(session: Session, frame: pd.DataFrame, cfg: EventClassificationConfig,
                        now: datetime) -> int:  # fmt: skip
    """The vendor's board-meeting calendar as events (``exchange='indianapi'``): the results
    watcher's board calendar reads them like NSE's."""
    if frame.empty:
        return 0
    return store_events(session, frame, exchange=EXCHANGE, cfg=cfg, now=now).rows


VENDOR_SHP_SOURCE = "indianapi"


def save_vendor_shareholding(session: Session, instrument_id: int, rows: list[dict[str, Any]],
                             now: datetime) -> int:  # fmt: skip
    """Vendor shareholding (``indianapi_checks.vendor_shareholding``) per quarter. A quarter
    with no row is inserted (source ``indianapi``). A quarter filed by the exchange keeps its
    promoter figure; when it lacks FII / DII (NSE gives only promoter vs public), the vendor's
    FII, DII (incl. MF) and public (excluding institutions) are added and the source names both.
    Returns the number of quarters written."""
    have = {r.period_end: r for r in session.scalars(
        select(Shareholding).where(Shareholding.instrument_id == instrument_id))}  # fmt: skip
    written = 0
    for v in rows:
        cur = have.get(v["period_end"])
        if cur is None or cur.source == VENDOR_SHP_SOURCE:
            upsert(session, Shareholding, [{
                "instrument_id": instrument_id, "period_end": v["period_end"],
                "filing_date": None, "promoter_pct": v["promoter_pct"],
                "promoter_pledge_pct": cur.promoter_pledge_pct if cur is not None else None,
                "fii_pct": v["fii_pct"], "dii_pct": v["dii_pct"], "mf_pct": None,
                "public_pct": v["public_pct"], "source": VENDOR_SHP_SOURCE, "fetched_at": now,
            }])  # fmt: skip
            written += 1
        elif cur.fii_pct is None and cur.dii_pct is None and v["fii_pct"] is not None:
            cur.fii_pct, cur.dii_pct, cur.public_pct = v["fii_pct"], v["dii_pct"], v["public_pct"]
            if cur.promoter_pct is None:
                cur.promoter_pct = v["promoter_pct"]
            cur.source = f"{cur.source}+{VENDOR_SHP_SOURCE}"[:32]
            written += 1
    return written
