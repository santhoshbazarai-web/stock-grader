"""Symbol search (SPEC v0.2 §3.5): pg_trgm over symbols, names, codes and aliases, on the
symbol master built from tests/fixtures/symbols by the symbol_master job."""

import json
import statistics
import time
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from app.core.config import JobName
from app.data.search import SearchHit, search
from app.data.symbol_master import parse_bse_scrips, parse_fyers_master
from app.db.enums import AliasKind, SymbolStatus
from app.db.models import IndexMembership, Instrument, Symbol, SymbolAlias
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, run_job
from tests.jobs_support import NOW, Env

FIX = Path(__file__).parent / "fixtures" / "symbols"


@pytest.fixture
def master(env: Env) -> Env:
    names = ("EQUITY_L.csv", "symbolchange.csv", "namechange.csv")
    env.nse.symbol_files = {n: (FIX / n).read_text() for n in names}
    env.bse.scrips = parse_bse_scrips(json.loads((FIX / "bse_scrips.json").read_text()))
    env.prices.fyers_master = parse_fyers_master((FIX / "NSE_CM.csv").read_text())
    run_job(REGISTRY[JobName.SYMBOL_MASTER], env.ctx, JobOptions())
    return env


def find(env: Env, q: str, limit: int = 10, **kw: bool) -> list[SearchHit]:
    with env.session() as s:
        cfg = env.ctx.config
        return search(s, q, cfg=cfg.providers.symbols.search,
                      universe_index=cfg.jobs.universe_index, limit=limit, **kw)  # fmt: skip


def member(env: Env, symbol: str) -> None:
    with env.session() as s:
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
        index = env.ctx.config.jobs.universe_index
        since = date(2020, 1, 1)
        s.add(IndexMembership(index_name=index, instrument_id=iid, effective_from=since,
                              source="nse", fetched_at=NOW))  # fmt: skip
        s.commit()


# ───────────── the four ways to name HDFC Bank / Bharti Airtel ─────────────


def test_company_name(master: Env) -> None:
    top = find(master, "hdfc bank")[0]
    # typed with a space, it is also the symbol HDFCBANK: an exact code match
    assert (top.symbol, top.match, top.exact) == ("HDFCBANK", "symbol", True)
    top = find(master, "hdfc bank limited")[0]
    assert (top.symbol, top.match, top.exact) == ("HDFCBANK", "name", False)
    assert top.score == pytest.approx(1.0)
    assert find(master, "bharti airtel")[0].symbol == "BHARTIARTL"


def test_nse_symbol(master: Env) -> None:
    top = find(master, "HDFCBANK")[0]
    assert (top.symbol, top.match, top.exact) == ("HDFCBANK", "symbol", True)
    assert find(master, "hdfcbank")[0].symbol == "HDFCBANK"  # case does not matter


def test_bse_code(master: Env) -> None:
    hits = find(master, "500180")
    assert [(h.symbol, h.match, h.exact, h.bse_code) for h in hits] == [
        ("HDFCBANK", "bse_code", True, "500180")]  # fmt: skip


def test_old_company_name(master: Env) -> None:
    top = find(master, "Bharti Tele-Ventures")[0]
    assert (top.symbol, top.match) == ("BHARTIARTL", "former_name")
    assert top.matched == "Bharti Tele-Ventures Limited" and top.name == "Bharti Airtel Limited"
    infy = find(master, "infosys technologies")[0]
    assert (infy.symbol, infy.match) == ("INFY", "former_name")


# ───────────── other codes, typos, ranking ─────────────


def test_former_symbol_isin_and_fyers_ticker(master: Env) -> None:
    assert [(h.symbol, h.match) for h in find(master, "INFOSYSTCH")][:1] == [
        ("INFY", "former_symbol")]  # fmt: skip
    assert find(master, "INE009A01021")[0].symbol == "INFY"
    assert find(master, "NSE:INFY-EQ")[0].symbol == "INFY"


def test_typos_still_match(master: Env) -> None:
    assert find(master, "hdfc bnak")[0].symbol == "HDFCBANK"
    assert find(master, "tata consultancy")[0].symbol == "TCS"
    assert find(master, "zzzz qqqq") == []


def test_bse_only_after_nse_listed_on_a_tie(master: Env) -> None:
    with master.session() as s:
        s.add(Symbol(isin="INE997Z01015", name="HDFC Bank Limited", bse_code="590200",
                     status=SymbolStatus.ACTIVE, sources=["bse"]))  # fmt: skip
        s.commit()
    hits = find(master, "hdfc bank limited")  # both names score 1.0
    assert [(h.symbol, h.score) for h in hits[:2]] == [("HDFCBANK", 1.0), (None, 1.0)]


def test_bse_only_company(master: Env) -> None:
    top = find(master, "acme rural")[0]
    assert (top.symbol, top.bse_code, top.name) == (None, "590099", "Acme Rural Traders Ltd")
    assert find(master, "590099")[0].exact


def test_exact_first_then_nifty500_then_similarity(master: Env) -> None:
    member(master, "HDFCLIFE")
    hits = [h.symbol for h in find(master, "hdfc")]
    assert hits[:2] == ["HDFCLIFE", "HDFCBANK"]  # the Nifty 500 member first
    assert find(master, "hdfc")[0].in_universe_index
    assert find(master, "HDFCBANK")[0].symbol == "HDFCBANK"  # exact beats it


def test_inactive_after_active(master: Env) -> None:
    with master.session() as s:
        s.execute(Symbol.__table__.update().where(Symbol.nse_symbol == "HDFCBANK")
                  .values(status=SymbolStatus.INACTIVE))  # fmt: skip
        s.commit()
    hits = find(master, "hdfc")
    assert [h.symbol for h in hits][-1] == "HDFCBANK" and not hits[-1].active


def test_user_alias(master: Env) -> None:
    with master.session() as s:
        sid = s.scalar(select(Symbol.id).where(Symbol.nse_symbol == "HDFCBANK"))
        s.add(SymbolAlias(symbol_id=sid, alias="HDB", kind=AliasKind.USER, source="user"))
        s.commit()
    top = find(master, "hdb")[0]
    assert (top.symbol, top.match, top.exact) == ("HDFCBANK", "user", True)


def test_indices_only_on_request(master: Env) -> None:
    with master.session() as s:
        s.add(Instrument(symbol="NIFTYBANK", name="Nifty Bank", is_index=True, source="nse",
                         fetched_at=NOW))  # fmt: skip
        s.commit()
    assert "NIFTYBANK" not in [h.symbol for h in find(master, "nifty bank")]
    assert find(master, "nifty bank", include_indices=True)[0].symbol == "NIFTYBANK"


def test_under_100ms_on_a_full_size_master(master: Env) -> None:
    """SPEC §3.5: the endpoint returns within 100 ms. ~5,000 companies with aliases, like
    NSE + BSE; median of repeated queries, after one warm-up."""
    words = [
        "alpha",
        "bharat",
        "capital",
        "delta",
        "engineering",
        "finance",
        "global",
        "hind",
        "india",
        "jyoti",
        "kiran",
        "lakshmi",
        "metals",
        "national",
        "orient",
        "power",
    ]
    with master.session() as s:
        s.execute(
            insert(Instrument),
            [
                {
                    "symbol": f"SYN{i:05d}",
                    "name": f"{words[i % 16].title()} {words[i // 16 % 16]} {i} Limited",
                    "source": "test",
                    "fetched_at": NOW,
                }
                for i in range(5000)
            ],
        )
        ids = dict(
            s.execute(
                select(Instrument.symbol, Instrument.id).where(Instrument.symbol.like("SYN%"))
            ).all()
        )
        s.execute(
            insert(Symbol),
            [
                {
                    "isin": f"INE{i:07d}A1",
                    "name": f"Syn {i} Ltd",
                    "nse_symbol": f"SYN{i:05d}",
                    "instrument_id": ids[f"SYN{i:05d}"],
                    "bse_code": str(700000 + i),
                    "status": SymbolStatus.ACTIVE,
                    "sources": ["nse"],
                }
                for i in range(5000)
            ],
        )
        sids = dict(s.execute(select(Symbol.nse_symbol, Symbol.id)).all())
        s.execute(
            insert(SymbolAlias),
            [
                {
                    "symbol_id": sids[f"SYN{i:05d}"],
                    "alias": f"Old {words[i % 7]} {i} Industries",
                    "kind": AliasKind.FORMER_NAME,
                    "source": "nse",
                }
                for i in range(5000)
            ],
        )
        s.commit()  # fmt: skip
    queries = [
        "hdfc bank",
        "HDFCBANK",
        "500180",
        "bharti tele ventures",
        "global india",
        "old orient",
        "capital finance 42",
        "SYN01234",
    ]
    find(master, "warm up")
    timings = []
    for q in queries * 3:
        t0 = time.perf_counter()
        find(master, q)
        timings.append(time.perf_counter() - t0)
    assert statistics.median(timings) < 0.1, sorted(timings)
    assert find(master, "hdfc bank")[0].symbol == "HDFCBANK"  # still found among 5,000
