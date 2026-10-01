"""Symbol master parsers and the ISIN join (SPEC v0.2 §3.5), on tests/fixtures/symbols."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import responses

from app.core.config import load_config
from app.data.providers.base import ProviderUnavailable
from app.data.providers.bse import BseProvider
from app.data.providers.fyers import fetch_fyers_masters
from app.data.providers.nse import NseProvider, NseSession
from app.data.raw_store import RawStore
from app.data.symbol_master import (
    MasterFormatError,
    NameChange,
    SymbolChange,
    current_symbol,
    join_masters,
    normalise_name,
    parse_bse_scrips,
    parse_fyers_master,
    parse_nse_equity_list,
    parse_nse_name_changes,
    parse_nse_symbol_changes,
)
from tests.conftest import REPO_CONFIG_DIR

FIX = Path(__file__).parent / "fixtures" / "symbols"


def text(name: str) -> str:
    return (FIX / name).read_text()


def test_nse_equity_list() -> None:
    rows = parse_nse_equity_list(text("EQUITY_L.csv"))
    assert [r.symbol for r in rows] == ["HDFCBANK", "BHARTIARTL", "INFY", "TCS", "HDFCLIFE"]
    hdfc = rows[0]
    assert (hdfc.name, hdfc.series, hdfc.isin, hdfc.face_value) == (
        "HDFC Bank Limited", "EQ", "INE040A01034", 1.0)  # fmt: skip
    assert hdfc.listing_date == date(1995, 11, 8)
    with pytest.raises(MasterFormatError, match="unexpected header"):
        parse_nse_equity_list("A,B\n1,2\n")


def test_change_files() -> None:
    sc = parse_nse_symbol_changes(text("symbolchange.csv"))
    assert sc[0] == SymbolChange("INFOSYSTCH", "INFOSYS", date(2008, 7, 2),
                                 "Infosys Technologies Limited")  # fmt: skip
    assert current_symbol("INFOSYSTCH", sc) == "INFY"
    nc = parse_nse_name_changes(text("namechange.csv"))
    assert nc[0] == NameChange("BHARTIARTL", "Bharti Tele-Ventures Limited",
                               "Bharti Airtel Limited", date(2006, 4, 24))  # fmt: skip
    # a file without a header is read positionally
    assert parse_nse_symbol_changes("Acme Ltd,OLDA,NEWA,01-Jan-2020\n")[0].new == "NEWA"
    assert parse_nse_name_changes("") == []


def test_bse_and_fyers() -> None:
    bse = parse_bse_scrips(json.loads(text("bse_scrips.json")))
    assert [b.code for b in bse] == ["500180", "532454", "500209", "532540", "590099", "590100"]
    assert bse[0].bse_id == "HDFCBANK" and bse[0].active and not bse[-1].active
    with pytest.raises(MasterFormatError, match="no usable rows"):
        parse_bse_scrips([{"foo": 1}])
    fy = parse_fyers_master(text("NSE_CM.csv")) + parse_fyers_master(text("BSE_CM.csv"))
    assert ("NSE:HDFCBANK-EQ", "INE040A01034") == (fy[0].ticker, fy[0].isin)
    assert fy[-1].ticker == "BSE:ACMERURAL-X"


def test_normalise_name() -> None:
    assert normalise_name("TATA CONSULTANCY SERVICES LTD.") == normalise_name(
        "Tata Consultancy Services Limited") == "tata consultancy services"  # fmt: skip
    assert normalise_name("Larsen & Toubro Ltd") == "larsen and toubro"


@pytest.fixture
def joined():  # type: ignore[no-untyped-def]
    return join_masters(
        parse_nse_equity_list(text("EQUITY_L.csv")),
        parse_bse_scrips(json.loads(text("bse_scrips.json"))),
        parse_fyers_master(text("NSE_CM.csv")) + parse_fyers_master(text("BSE_CM.csv")),
        parse_nse_symbol_changes(text("symbolchange.csv")),
        parse_nse_name_changes(text("namechange.csv")),
    )


def test_join_on_isin(joined) -> None:  # type: ignore[no-untyped-def]
    by_isin = {r.isin: r for r in joined.symbols}
    hdfc = by_isin["INE040A01034"]
    assert (hdfc.nse_symbol, hdfc.bse_code, hdfc.fyers_symbol) == (
        "HDFCBANK", "500180", "NSE:HDFCBANK-EQ")  # NSE ticker preferred over BSE's  # fmt: skip
    assert hdfc.sources == ["nse", "bse", "fyers"] and hdfc.name == "HDFC Bank Limited"
    acme = by_isin["INE999Z01011"]  # BSE only
    assert (acme.nse_symbol, acme.bse_code, acme.fyers_symbol) == (None, "590099",
                                                                   "BSE:ACMERURAL-X")  # fmt: skip
    assert "INE998Z01013" not in by_isin  # delisted on BSE, never on NSE: dropped
    assert by_isin["INE795G01014"].bse_code is None  # NSE only in these fixtures


def test_aliases(joined) -> None:  # type: ignore[no-untyped-def]
    got = {(a.isin, a.kind, a.alias, a.valid_until) for a in joined.aliases}
    infy, bharti = "INE009A01021", "INE397D01024"
    assert (infy, "former_symbol", "INFOSYSTCH", date(2008, 7, 2)) in got  # chain → INFY
    assert (infy, "former_symbol", "INFOSYS", date(2011, 11, 26)) in got
    assert (infy, "former_name", "Infosys Technologies Limited", date(2011, 6, 16)) in got
    assert (bharti, "former_name", "Bharti Tele-Ventures Limited", date(2006, 4, 24)) in got
    # same name in other words is not an alias; BSE's symbol equal to NSE's neither
    assert not any(a.isin == "INE467B01029" for a in joined.aliases)
    assert not any(a.kind == "bse_symbol" and a.isin == "INE040A01034" for a in joined.aliases)
    assert joined.warnings == ["1 symbol/name change(s) for symbols no longer listed"]


def test_missing_master_leaves_columns_out() -> None:
    out = join_masters(parse_nse_equity_list(text("EQUITY_L.csv")), None, None, [], [])
    assert all(r.bse_code is None and r.sources == ["nse"] for r in out.symbols)


# ───────────────────────── providers (HTTP via `responses`) ─────────────────────────

PCFG = load_config(REPO_CONFIG_DIR).providers


def STAMP() -> datetime:  # noqa: N802 - a constant-like clock
    return datetime(2024, 6, 14, 12, tzinfo=UTC)


@responses.activate
def test_nse_symbol_files_are_cached_then_parsed(tmp_path: Path) -> None:
    files = PCFG.nse.symbol_files
    base = PCFG.nse.archives_url
    for path, name in ((files.equity_list_path, "EQUITY_L.csv"),
                       (files.symbol_changes_path, "symbolchange.csv"),
                       (files.name_changes_path, "namechange.csv")):  # fmt: skip
        responses.add(responses.GET, f"{base}{path}", body=text(name))
    store = RawStore(tmp_path, clock=STAMP)
    p = NseProvider(PCFG.nse, NseSession(PCFG.nse), store)
    assert len(p.equity_list()) == 5
    assert p.symbol_changes()[1].new == "INFY"
    assert p.name_changes()[0].symbol == "BHARTIARTL"
    day = tmp_path / "nse" / "2024" / "06" / "14"
    assert sorted(f.name for f in day.iterdir()) == ["EQUITY_L.csv", "namechange.csv",
                                                     "symbolchange.csv"]  # fmt: skip


@responses.activate
def test_bse_scrip_master(tmp_path: Path) -> None:
    url = f"{PCFG.bse.api_url}{PCFG.bse.scrip_master_path}"
    responses.add(responses.GET, PCFG.bse.browser.warmup_urls[0], body="<html/>")
    responses.add(responses.GET, url, body=text("bse_scrips.json"))
    scrips = BseProvider(PCFG.bse, raw_store=RawStore(tmp_path, clock=STAMP)).scrip_master()
    assert scrips[0].code == "500180"
    api = responses.calls[1].request  # after the www.bseindia.com warm-up
    assert api.headers["Referer"] == PCFG.bse.referer
    assert (tmp_path / "bse/2024/06/14/scrip_master.json").is_file()
    # a challenge page or a 403 from every method: BSE is blocking (best-effort source)
    responses.replace(responses.GET, url, body="<html>blocked</html>")
    with pytest.raises(ProviderUnavailable, match="BSE is blocking automated access"):
        BseProvider(PCFG.bse).scrip_master()
    responses.replace(responses.GET, url, status=403)
    with pytest.raises(ProviderUnavailable, match="requests: HTTP 403"):
        BseProvider(PCFG.bse).scrip_master()


@responses.activate
def test_fyers_masters(tmp_path: Path) -> None:
    nse_url, bse_url = PCFG.symbols.fyers_masters
    responses.add(responses.GET, nse_url, body=text("NSE_CM.csv"))
    responses.add(responses.GET, bse_url, body=text("BSE_CM.csv"))
    throttled: list[int] = []
    got = fetch_fyers_masters([nse_url, bse_url], timeout_s=5, throttle=throttled.append,
                              raw_store=RawStore(tmp_path, clock=STAMP))  # fmt: skip
    assert len(got) == 5 and throttled == [0, 1]
    assert (tmp_path / "fyers/2024/06/14/BSE_CM.csv").is_file()
