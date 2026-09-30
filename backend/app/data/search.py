"""Symbol search (SPEC v0.2 §3.5): pg_trgm fuzzy matching over NSE symbols, company names, BSE
codes, ISINs, Fyers tickers and aliases (former symbols and names, BSE's symbol and name, the
owner's aliases).

Every searchable term scores max(similarity, word_similarity) against the query (0-1; a query
that is a whole word of a longer name, e.g. "hdfc bank" in "HDFC Bank Limited", scores 1).
Codes (symbol, BSE code, ISIN, Fyers ticker, a former or alias symbol) also match exactly.
Terms below ``providers.symbols.search.min_similarity`` are dropped; the GIN trigram indexes on
lower(term) serve the filter. Hits are grouped per company (its instrument, or the
master row of a BSE-only company) and ranked:

1. an exact code match,
2. then members of the universe index (``jobs.universe_index``, Nifty 500),
3. then everything else;

within each tier active companies first, then by score, NSE-listed before BSE-only.
"""

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import (
    ColumnElement,
    Double,
    Integer,
    cast,
    func,
    literal,
    null,
    or_,
    select,
    text,
    union_all,
)
from sqlalchemy.orm import Session

from app.core.config import SearchConfig
from app.db.enums import AliasKind, SymbolStatus
from app.db.models import IndexMembership, Instrument, Symbol, SymbolAlias

MatchKind = Literal[
    "symbol", "name", "isin", "bse_code", "fyers_symbol", "former_symbol", "former_name",
    "bse_symbol", "bse_name", "user",
]  # fmt: skip
_CODE_ALIASES = (AliasKind.FORMER_SYMBOL, AliasKind.BSE_SYMBOL, AliasKind.USER)


@dataclass(frozen=True)
class SearchHit:
    symbol: str | None  # NSE symbol; None for a BSE-only company
    name: str | None
    isin: str | None
    bse_code: str | None
    series: str | None
    sector: str | None
    industry: str | None
    is_index: bool
    in_universe_index: bool  # a current member of jobs.universe_index (Nifty 500)
    active: bool
    match: MatchKind  # what matched
    matched: str  # the term that matched
    exact: bool
    score: float


def _score(term: Any, q: str) -> ColumnElement[float]:
    low = func.lower(term)
    return func.greatest(func.similarity(low, q), func.word_similarity(q, low))


def _fuzzy(term: Any, q: str) -> ColumnElement[bool]:
    low = func.lower(term)
    return or_(low.op("%")(q), literal(q).op("<%")(low))


def _branch(
    base: Any, term: Any, kind: str | Any, *, exact: ColumnElement[bool] | None,
    fuzzy: bool, q: str, exact_in_filter: bool = True,
) -> Any:  # fmt: skip
    """One searchable column: (instrument_id, symbol_id, term, kind, exact, score). Every
    filter can use an index: the trigram GIN index for fuzzy terms, a btree for exact codes.
    ``exact_in_filter=False``: the fuzzy filter already finds exact matches (similarity 1), so
    exactness is only computed for the rows found."""
    is_exact = exact if exact is not None else literal(False)
    score = _score(term, q) if fuzzy else literal(0.0, Double)
    cond = [c for c in (exact if exact_in_filter else None, _fuzzy(term, q) if fuzzy else None)
            if c is not None]  # fmt: skip
    return base.add_columns(
        term.label("term"), (literal(kind) if isinstance(kind, str) else kind).label("kind"),
        is_exact.label("exact"), score.label("score"),
    ).where(term.is_not(None), or_(*cond))  # fmt: skip


def search(
    session: Session,
    query: str,
    *,
    cfg: SearchConfig,
    universe_index: str,
    limit: int,
    include_indices: bool = False,
) -> list[SearchHit]:
    raw = " ".join(query.split())
    q, code = raw.lower(), raw.upper().replace(" ", "")
    if not q:
        return []
    threshold = str(cfg.min_similarity)
    session.execute(
        text("SELECT set_config('pg_trgm.similarity_threshold', :t, true), "
             "set_config('pg_trgm.word_similarity_threshold', :t, true)"),
        {"t": threshold},
    )  # fmt: skip

    no_symbol = cast(null(), Integer).label("symbol_id")
    inst = select(Instrument.id.label("instrument_id"), no_symbol).where(
        Instrument.is_active.is_(True),
        *(() if include_indices else (Instrument.is_index.is_(False),)),
    )
    sym = select(Symbol.instrument_id.label("instrument_id"), Symbol.id.label("symbol_id"))
    ali = (
        select(Symbol.instrument_id.label("instrument_id"), Symbol.id.label("symbol_id"))
        .select_from(SymbolAlias)
        .join(Symbol, Symbol.id == SymbolAlias.symbol_id)
    )
    alias_exact = (func.upper(func.replace(SymbolAlias.alias, " ", "")) == code) & \
        SymbolAlias.kind.in_(_CODE_ALIASES)  # fmt: skip
    branches = [
        _branch(inst, Instrument.symbol, "symbol", exact=Instrument.symbol == code, fuzzy=True,
                q=q),
        _branch(inst, Instrument.name, "name", exact=None, fuzzy=True, q=q),
        _branch(inst, Instrument.isin, "isin", exact=Instrument.isin == code, fuzzy=False, q=q),
        _branch(sym, Symbol.name, "name", exact=None, fuzzy=True, q=q),
        _branch(sym, Symbol.bse_code, "bse_code", exact=Symbol.bse_code == code, fuzzy=False,
                q=q),
        _branch(sym, Symbol.isin, "isin", exact=Symbol.isin == code, fuzzy=False, q=q),
        _branch(sym, Symbol.fyers_symbol, "fyers_symbol", exact=Symbol.fyers_symbol == code,
                fuzzy=False, q=q),
        _branch(ali, SymbolAlias.alias, SymbolAlias.kind, exact=alias_exact, fuzzy=True, q=q,
                exact_in_filter=False),
    ]  # fmt: skip
    terms = union_all(*branches).subquery()
    # The planner under-costs the trigram operators and would scan whole tables (several
    # times slower than the GIN indexes once there are thousands of companies); every branch
    # above has an index, so sequential scans are off for this statement only.
    session.execute(text("SELECT set_config('enable_seqscan', 'off', true)"))
    rows = session.execute(
        select(terms).order_by(terms.c.exact.desc(), terms.c.score.desc())
        .limit(cfg.candidate_limit)
    ).all()  # fmt: skip
    session.execute(text("SELECT set_config('enable_seqscan', 'on', true)"))

    best: dict[tuple[str, int], Any] = {}
    for r in rows:
        if not r.exact and r.score < cfg.min_similarity:
            continue
        key = ("i", r.instrument_id) if r.instrument_id is not None else ("s", r.symbol_id)
        if key not in best or (r.exact, r.score) > (best[key].exact, best[key].score):
            best[key] = r
    return _hits(session, best, universe_index, include_indices)[:limit]


def _hits(
    session: Session, best: dict[tuple[str, int], Any], universe_index: str, include_indices: bool
) -> list[SearchHit]:
    inst_ids = [k[1] for k in best if k[0] == "i"]
    sym_ids = [k[1] for k in best if k[0] == "s"]
    instruments = {i.id: i for i in session.scalars(
        select(Instrument).where(Instrument.id.in_(inst_ids)))}  # fmt: skip
    masters = {s.instrument_id: s for s in session.scalars(
        select(Symbol).where(Symbol.instrument_id.in_(inst_ids)))}  # fmt: skip
    bse_only = {s.id: s for s in session.scalars(select(Symbol).where(Symbol.id.in_(sym_ids)))}
    members = set(session.scalars(
        select(IndexMembership.instrument_id).where(
            IndexMembership.index_name == universe_index,
            IndexMembership.effective_to.is_(None),
            IndexMembership.instrument_id.in_(inst_ids),
        )
    ))  # fmt: skip
    out = []
    for (kind, ident), r in best.items():
        if kind == "i":
            i = instruments.get(ident)
            if i is None or not i.is_active or (i.is_index and not include_indices):
                continue
            m = masters.get(i.id)
            out.append(SearchHit(
                i.symbol, i.name, i.isin, m.bse_code if m else None, i.series, i.sector,
                i.industry, i.is_index, i.id in members,
                m is None or m.status is SymbolStatus.ACTIVE, r.kind, r.term, r.exact,
                float(r.score),
            ))  # fmt: skip
        else:
            s = bse_only.get(ident)
            if s is None:
                continue
            out.append(SearchHit(
                s.nse_symbol, s.name, s.isin, s.bse_code, s.nse_series, None, None, False,
                False, s.status is SymbolStatus.ACTIVE, r.kind, r.term, r.exact,
                float(r.score),
            ))  # fmt: skip
    out.sort(key=lambda h: (not h.exact, not h.in_universe_index, not h.active, -h.score,
                            h.symbol is None, h.symbol or h.bse_code or ""))  # fmt: skip
    return out
