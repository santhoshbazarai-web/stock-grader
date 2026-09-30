"""symbol_master: the ISIN-joined symbol master and its aliases (SPEC v0.2 §3.5)."""

from typing import Any

from app.core.config import Dataset, Provider
from app.data.providers.base import (
    BseScripMasterProvider,
    FyersSymbolMasterProvider,
    NseSymbolFilesProvider,
)
from app.data.symbol_master import join_masters
from app.data.symbol_store import store_master
from app.jobs.runner import JobContext, JobOptions, JobOutcome


def symbol_master(ctx: JobContext, options: JobOptions) -> JobOutcome:
    """Read NSE ``EQUITY_L`` (required), NSE's symbol- and name-change files, the BSE scrip
    master and the Fyers symbol masters (each optional), join them on ISIN and store them
    (``app.data.symbol_store``). Every file is cached raw before parsing."""
    del options
    router = ctx.router

    def fetch(provider: Provider, protocol: type, call: Any) -> Any:
        return router.fetch(Dataset.SYMBOL_MASTER, protocol, call, symbol=None,
                            providers=[provider], record_gap=False)  # fmt: skip

    nse = fetch(Provider.NSE, NseSymbolFilesProvider, lambda p: p.equity_list())
    if nse.data is None:
        raise RuntimeError(f"NSE equity list unavailable: {'; '.join(nse.reasons)}")
    got, missing = {"nse"}, {}
    parts: dict[str, Any] = {}
    for key, provider, protocol, call in (
        ("symbol_changes", Provider.NSE, NseSymbolFilesProvider, lambda p: p.symbol_changes()),
        ("name_changes", Provider.NSE, NseSymbolFilesProvider, lambda p: p.name_changes()),
        ("bse", Provider.BSE, BseScripMasterProvider, lambda p: p.scrip_master()),
        ("fyers", Provider.FYERS, FyersSymbolMasterProvider, lambda p: p.symbol_master()),
    ):
        res = fetch(provider, protocol, call)
        if res.data is None:
            missing[key] = "; ".join(res.reasons)[:300]
        else:
            parts[key] = res.data
            got.add(key)
    join = join_masters(nse.data, parts.get("bse"), parts.get("fyers"),
                        parts.get("symbol_changes", []), parts.get("name_changes", []))  # fmt: skip
    session = ctx.session_factory()
    try:
        summary = store_master(session, join, fetched=got, today=ctx.today(), now=ctx.now())
        session.commit()
    finally:
        session.close()
    details: dict[str, Any] = {
        "companies": len(join.symbols),
        "nse_listed": sum(1 for s in join.symbols if s.nse_symbol),
        "bse_only": sum(1 for s in join.symbols if not s.nse_symbol),
        "aliases": summary.aliases,
        "renamed": summary.renamed,
        "conflicts": summary.conflicts,
        "inactive": summary.inactive,
        "not_fetched": missing,
        "warnings": join.warnings,
    }
    return JobOutcome(summary.symbols, details)
