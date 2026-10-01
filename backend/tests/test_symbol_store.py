"""symbol_master job (SPEC v0.2 §3.5): ISIN join into symbols / instruments / aliases."""

import json
from datetime import date
from pathlib import Path

from sqlalchemy import select

from app.core.config import JobName
from app.data.symbol_master import parse_bse_scrips, parse_fyers_master
from app.db.enums import AliasKind, JobStatus, SymbolStatus
from app.db.models import Instrument, PriceDaily, Symbol, SymbolAlias
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, RunRecord, run_job
from tests.jobs_support import NOW, Env

FIX = Path(__file__).parent / "fixtures" / "symbols"
HDFC, INFY, BHARTI, ACME = "INE040A01034", "INE009A01021", "INE397D01024", "INE999Z01011"


def masters(env: Env, *, bse: bool = True, fyers: bool = True, equity: str | None = None) -> None:
    names = ("EQUITY_L.csv", "symbolchange.csv", "namechange.csv")
    env.nse.symbol_files = {n: (FIX / n).read_text() for n in names}
    if equity is not None:
        env.nse.symbol_files["EQUITY_L.csv"] = equity
    env.bse.scrips = parse_bse_scrips(json.loads((FIX / "bse_scrips.json").read_text())) \
        if bse else None  # fmt: skip
    env.prices.fyers_master = (parse_fyers_master((FIX / "NSE_CM.csv").read_text())
                               + parse_fyers_master((FIX / "BSE_CM.csv").read_text())) \
        if fyers else None  # fmt: skip


def run(env: Env) -> RunRecord:
    return run_job(REGISTRY[JobName.SYMBOL_MASTER], env.ctx, JobOptions())


def symbols(env: Env) -> dict[str, Symbol]:
    with env.session() as s:
        return {r.isin: r for r in s.scalars(select(Symbol))}


def aliases(env: Env, isin: str) -> set[tuple[str, str]]:
    with env.session() as s:
        rows = s.execute(select(SymbolAlias.kind, SymbolAlias.alias).join(Symbol)
                         .where(Symbol.isin == isin)).all()  # fmt: skip
    return {(k.value, a) for k, a in rows}


def test_join_into_symbols_and_instruments(env: Env) -> None:
    masters(env)
    rec = run(env)
    assert rec.status is JobStatus.SUCCESS and rec.outcome is not None
    d = rec.outcome.details
    assert (d["companies"], d["nse_listed"], d["bse_only"]) == (6, 5, 1)
    assert d["not_fetched"] == {}
    rows = symbols(env)
    hdfc = rows[HDFC]
    assert (hdfc.nse_symbol, hdfc.bse_code, hdfc.fyers_symbol, hdfc.status) == (
        "HDFCBANK", "500180", "NSE:HDFCBANK-EQ", SymbolStatus.ACTIVE)  # fmt: skip
    assert hdfc.sources == ["bse", "fyers", "nse"] and hdfc.last_seen == date(2024, 6, 14)
    with env.session() as s:
        inst = s.scalars(select(Instrument).where(Instrument.symbol == "HDFCBANK")).one()
        assert (inst.isin, inst.name, inst.series, inst.source) == (
            HDFC, "HDFC Bank Limited", "EQ", "nse")  # fmt: skip
        assert inst.listing_date == date(1995, 11, 8) and hdfc.instrument_id == inst.id
    assert rows[ACME].instrument_id is None and rows[ACME].bse_code == "590099"
    assert ("former_symbol", "INFOSYSTCH") in aliases(env, INFY)
    assert ("former_name", "Bharti Tele-Ventures Limited") in aliases(env, BHARTI)


def test_symbol_change_renames_the_instrument_and_keeps_history(env: Env) -> None:
    with env.session() as s:
        s.add(Instrument(symbol="INFOSYS", isin=INFY, source="nse", fetched_at=NOW))
        s.flush()
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == "INFOSYS"))
        s.add(PriceDaily(instrument_id=iid, date=date(2011, 1, 3), open=1, high=1, low=1,
                         close=1, volume=1, source="demo", fetched_at=NOW))  # fmt: skip
        s.commit()
    masters(env)
    rec = run(env)
    assert rec.outcome is not None and rec.outcome.details["renamed"] == ["INFOSYS → INFY"]
    with env.session() as s:
        inst = s.scalars(select(Instrument).where(Instrument.symbol == "INFY")).one()
        assert inst.id == iid and s.scalar(select(PriceDaily.instrument_id)) == iid
        assert s.scalar(select(Instrument).where(Instrument.symbol == "INFOSYS")) is None


def test_conflicting_instruments_are_reported_not_merged(env: Env) -> None:
    with env.session() as s:
        s.add(Instrument(symbol="INFOSYS", isin=INFY, source="nse", fetched_at=NOW))
        s.add(Instrument(symbol="INFY", source="watchlist", fetched_at=NOW))  # a bare row
        s.commit()
    masters(env)
    rec = run(env)
    assert rec.status is JobStatus.SUCCESS and rec.outcome is not None
    assert "INFOSYS is now INFY" in rec.outcome.details["conflicts"][0]


def test_missing_optional_masters_keep_their_columns(env: Env) -> None:
    masters(env)
    run(env)
    with env.session() as s:
        sid = s.scalar(select(Symbol.id).where(Symbol.isin == HDFC))
        s.add(SymbolAlias(symbol_id=sid, alias="HDFCB", kind=AliasKind.USER, source="user"))
        s.commit()
    masters(env, bse=False, fyers=False)
    env.nse.symbol_files.pop("namechange.csv")
    rec = run(env)
    assert rec.outcome is not None
    assert set(rec.outcome.details["not_fetched"]) == {"bse", "fyers", "name_changes"}
    rows = symbols(env)
    assert (rows[HDFC].bse_code, rows[HDFC].fyers_symbol) == ("500180", "NSE:HDFCBANK-EQ")
    assert set(rows[HDFC].sources) == {"nse", "bse", "fyers"}
    assert rows[ACME].status is SymbolStatus.ACTIVE  # BSE not read: nobody goes inactive
    assert ("former_name", "Bharti Tele-Ventures Limited") in aliases(env, BHARTI)  # kept
    assert ("user", "HDFCB") in aliases(env, HDFC)


def test_delisted_company_becomes_inactive_and_keeps_aliases(env: Env) -> None:
    masters(env)
    run(env)
    equity = "\n".join(line for line in (FIX / "EQUITY_L.csv").read_text().splitlines()
                       if not line.startswith("BHARTIARTL"))  # fmt: skip
    masters(env, equity=equity)
    env.bse.scrips = [b for b in (env.bse.scrips or []) if b.isin != BHARTI]
    rec = run(env)
    assert rec.outcome is not None and rec.outcome.details["inactive"] == 1
    assert symbols(env)[BHARTI].status is SymbolStatus.INACTIVE
    assert ("former_name", "Bharti Tele-Ventures Limited") in aliases(env, BHARTI)


def test_nse_list_is_required(env: Env) -> None:
    rec = run(env)  # no files at all
    assert rec.status is JobStatus.FAILED and "NSE equity list unavailable" in (rec.error or "")
