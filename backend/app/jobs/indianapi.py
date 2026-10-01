"""fundamentals_indianapi: 10+ years of statements per stock from the Indian API, without NSE
(SPEC §3.2, §3.6). See ``app/data/indianapi_store.py`` for the cache / priority rules.

Per stock: if its cached answers are current (``refresh_due``) they are re-mapped (no call);
otherwise /stock is fetched under the candidate names until one answers for *our* company
(ISIN / NSE code), then the four /historical_stats tables with that name. Every answer is
stored before it is read. A wrong company, a 404, a 429 after retries, a missing key or an
exhausted budget is a data gap, never a crash and never stored as data. The run stops at the
budget limit; without ``--symbols`` / ``--all-universe`` it takes at most
``max_stocks_per_run`` stocks, never fetched or stalest first.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import func, select

from app.core.config import Dataset
from app.core.settings import config_dir_from_env
from app.data.gaps import GapRecord
from app.data.indianapi_parse import Mapped, map_statements, quarter_sums, verify_identity
from app.data.indianapi_store import (
    cached_answers,
    candidate_names,
    endpoint_key,
    refresh_due,
    remember_name,
    save_answer,
    save_differences,
    store_mapped,
)
from app.data.providers.base import ProviderError
from app.data.providers.indianapi import (
    NOT_CONFIGURED,
    BudgetExhausted,
    IndianApiClient,
    NotConfigured,
    Unauthorized,
    VendorNotFound,
    usage_text,
)
from app.db.models import Instrument, ResultFiling, VendorResponse
from app.fundamentals.indianapi_map import get_indianapi_map
from app.fundamentals.xbrl_map import get_xbrl_map
from app.jobs.common import ensure_instruments, universe
from app.jobs.runner import JobContext, JobOptions, JobOutcome

logger = logging.getLogger(__name__)
SOURCE = "indianapi"
WRONG_COMPANY = "vendor returned a different company"


class StopRun(Exception):
    """The budget is used up or the key is refused: no further stock is fetched."""


@dataclass
class SymbolOutcome:
    symbol: str
    status: str  # stored | cached | failed
    message: str
    calls: int = 0
    years: dict[str, int] = field(default_factory=dict)  # P&L / BS / CF → fiscal years
    model: str | None = None
    differences: int = 0
    gaps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _gap(ctx: JobContext, symbol: str, reason: str) -> None:
    ctx.gaps.record(GapRecord(Dataset.FIN_ANNUAL, symbol, reason, [SOURCE], "indianapi"))


def _results_since(session: Any, instrument_id: int) -> date | None:
    got = session.scalar(select(func.max(ResultFiling.announcement_date))
                         .where(ResultFiling.instrument_id == instrument_id))  # fmt: skip
    return got if isinstance(got, date) else None


def fetch_symbol(ctx: JobContext, symbol: str, *, user: bool = False,
                 force_cache: bool = False) -> SymbolOutcome:  # fmt: skip
    """Fetch (when due) and store one stock. ``force_cache``: never call the vendor."""
    cfg = ctx.config.providers.indianapi
    jcfg = ctx.config.jobs.fundamentals_indianapi
    amap = get_indianapi_map(config_dir_from_env())
    client: IndianApiClient | None = ctx.indianapi
    sym = symbol.strip().upper()
    out = SymbolOutcome(sym, "failed", "")
    session = ctx.session_factory()
    try:
        iid = ensure_instruments(session, [sym])[sym]
        inst = session.get(Instrument, iid)
        assert inst is not None
        cached = cached_answers(session, iid)
        stock_row = cached.get("/stock")
        due = refresh_due(stock_row.fetched_at if stock_row else None, now=ctx.clock(), cfg=cfg,
                          results_since=_results_since(session, iid), user=user)  # fmt: skip
        answers: dict[str, Any] = {k: r.payload for k, r in cached.items()}
        fetched = False
        if due.due and not force_cache:
            if client is None or not client.configured:
                msg = NOT_CONFIGURED if client is None or cfg.enabled else str(NotConfigured())
                out.gaps.append(msg)
                if not answers:
                    out.message = msg
                    _gap(ctx, sym, msg)
                    session.commit()
                    return out
            else:
                answers = _fetch(ctx, session, client, inst, out, jcfg.max_name_attempts,
                                 answers)  # fmt: skip
                fetched = True
        if "/stock" not in answers:
            out.message = "; ".join(out.gaps) or "no verified Indian API answer for this stock"
            session.commit()
            return out
        hists = {k: v for k, v in answers.items() if k != "/stock"}
        mapped = map_statements(answers["/stock"], hists, amap)
        _store(ctx, session, inst, mapped, amap.version, out)
        out.status = "stored" if fetched else "cached"
        out.message = (f"{_years_text(out.years)} ({mapped.model} model; "
                       f"{'fetched' if fetched else due.reason})")  # fmt: skip
        session.commit()
        return out
    except StopRun:
        session.commit()
        raise
    finally:
        session.close()


def _fetch(
    ctx: JobContext,
    session: Any,
    client: IndianApiClient,
    inst: Instrument,
    out: SymbolOutcome,
    max_names: int,
    cached: dict[str, Any],
) -> dict[str, Any]:
    cfg = ctx.config.providers.indianapi
    stock_ep, *hist_eps = cfg.calls_per_stock
    answers: dict[str, Any] = {}
    name = None
    wrong: list[str] = []
    unavailable = False
    names = candidate_names(session, inst, cfg, max_names)
    for cand in names:
        try:
            got = client.get(stock_ep, cand)
        except VendorNotFound as exc:
            out.calls += 1
            out.notes.append(str(exc))
            continue
        except (BudgetExhausted, Unauthorized) as exc:
            _gap(ctx, inst.symbol, str(exc))
            out.gaps.append(str(exc))
            raise StopRun(str(exc)) from exc
        except ProviderError as exc:  # network, 429 / 5xx after retries
            out.gaps.append(f"Indian API /stock unavailable: {exc}")
            _gap(ctx, inst.symbol, out.gaps[-1])
            unavailable = True
            break
        out.calls += got.attempts
        ident = verify_identity(got.payload, isin=inst.isin, symbol=inst.symbol)
        save_answer(session, ctx.raw_store, instrument_id=inst.id, symbol=inst.symbol,
                    ep=stock_ep, answer=got, identity_ok=ident.ok, note=ident.reason)  # fmt: skip
        if not ident.ok:
            wrong.append(f"{cand!r}: {ident.reason}")
            out.notes.append(f"name {cand!r}: {ident.reason}")
            continue
        name = cand
        answers["/stock"] = got.payload
        remember_name(session, inst.id, cand, ctx.clock())
        break
    if name is None:
        tried = ", ".join(repr(n) for n in names)
        if wrong:
            msg = (f"{WRONG_COMPANY} (not stored) for every name tried: {'; '.join(wrong)}. "
                   "Add the vendor's name to providers.indianapi.name_fallbacks")  # fmt: skip
        elif not unavailable:
            msg = (f"Indian API has no answer for {inst.symbol} under {tried}; add the "
                   "vendor's name to providers.indianapi.name_fallbacks")  # fmt: skip
        else:
            msg = ""
        if msg:
            out.gaps.append(msg)
            _gap(ctx, inst.symbol, msg)
        return cached
    for ep in hist_eps:
        try:
            got = client.get(ep, name)
        except (BudgetExhausted, Unauthorized) as exc:
            _gap(ctx, inst.symbol, str(exc))
            out.gaps.append(str(exc))
            raise StopRun(str(exc)) from exc
        except ProviderError as exc:
            out.notes.append(f"{endpoint_key(ep)}: {exc}")
            if endpoint_key(ep) in cached:
                answers[endpoint_key(ep)] = cached[endpoint_key(ep)]
            continue
        out.calls += got.attempts
        save_answer(session, ctx.raw_store, instrument_id=inst.id, symbol=inst.symbol, ep=ep,
                    answer=got)  # fmt: skip
        answers[endpoint_key(ep)] = got.payload
    return answers


def _store(ctx: JobContext, session: Any, inst: Instrument, mapped: Mapped, version: int,
           out: SymbolOutcome) -> None:  # fmt: skip
    amap = get_indianapi_map(config_dir_from_env())
    store_mapped(session, instrument_id=inst.id, isin=inst.isin, mapped=mapped,
                 map_version=version, xmap=get_xbrl_map(),
                 results_cfg=ctx.config.providers.nse.results, now=ctx.clock())  # fmt: skip
    out.model = mapped.model
    out.years = {"P&L": len(mapped.years("pl")), "BS": len(mapped.years("bs")),
                 "CF": len(mapped.years("cf"))}  # fmt: skip
    out.differences = save_differences(session, inst.id, mapped.differences, ctx.clock())
    out.notes += mapped.notes + [d.text() for d in mapped.differences]
    for g in mapped.gaps:
        out.gaps.append(g)
        _gap(ctx, inst.symbol, g)
    for s in quarter_sums(mapped, ("profit_after_tax",), amap.units.quarter_sum_tolerance):
        if not s.ok:
            q, a = s.quarters / 1e7, s.annual / 1e7
            msg = (f"Indian API FY{s.fiscal_year} quarters of {s.item_code} sum to {q:,.0f} "
                   f"crore, the year says {a:,.0f}")  # fmt: skip
            out.notes.append(msg)
            _gap(ctx, inst.symbol, msg)


def _years_text(years: dict[str, int]) -> str:
    return ", ".join(f"{k} {v} yr" for k, v in years.items()) or "no statements"


def _due_first(ctx: JobContext, symbols: list[str], limit: int) -> list[str]:
    """Never-fetched stocks first, then the stalest."""
    session = ctx.session_factory()
    try:
        last = dict(session.execute(
            select(Instrument.symbol, func.max(VendorResponse.fetched_at))
            .join(VendorResponse, VendorResponse.instrument_id == Instrument.id, isouter=True)
            .where(Instrument.symbol.in_(symbols))
            .group_by(Instrument.symbol)
        ).all())  # fmt: skip
    finally:
        session.close()
    order = sorted(symbols, key=lambda s: (last.get(s) is not None, last.get(s) or 0, s))
    return order[:limit]


def fundamentals_indianapi(ctx: JobContext, options: JobOptions) -> JobOutcome:
    jcfg = ctx.config.jobs.fundamentals_indianapi
    symbols = universe(ctx, options)
    if not options.symbols and not options.all_universe:
        symbols = _due_first(ctx, symbols, jcfg.max_stocks_per_run)
    results: list[SymbolOutcome] = []
    stopped = None
    for sym in symbols:
        try:
            results.append(fetch_symbol(ctx, sym, user=options.force))
        except StopRun as exc:
            stopped = str(exc)
            break
    calls = sum(r.calls for r in results)
    usage = None
    used = ctx.indianapi.used_this_month() if ctx.indianapi is not None else None
    if used is not None:
        usage = usage_text(used, ctx.config.providers.indianapi.monthly_request_budget)
    details: dict[str, Any] = {
        "symbols": len(symbols),
        "stored": {r.symbol: r.message for r in results if r.status == "stored"},
        "cached": {r.symbol: r.message for r in results if r.status == "cached"},
        "failed": {r.symbol: r.message for r in results if r.status == "failed"},
        "calls": calls,
        "usage": usage,
        "stopped": stopped,
        "differences": {r.symbol: r.differences for r in results if r.differences},
    }
    return JobOutcome(sum(r.status != "failed" for r in results), details)
