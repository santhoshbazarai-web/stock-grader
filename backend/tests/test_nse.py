"""NSE provider on saved sample files (tests/fixtures/nse) served via ``responses``."""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import responses
from requests import PreparedRequest

from app.core.config import NseConfig, Provider, load_config
from app.data.providers.base import (
    ConstituentsProvider,
    CorporateActionsProvider,
    DeliveryProvider,
    ProviderError,
    ProviderUnavailable,
    ResultsFilingsProvider,
    ShareholdingProvider,
    SurveillanceProvider,
)
from app.data.providers.nse import (
    BROWSER_HEADERS,
    NseProvider,
    NseSession,
    build_nse_provider,
    parse_ca_subject,
    parse_results_index,
)
from app.db.enums import CorporateActionType
from tests.conftest import REPO_CONFIG_DIR

FIX = Path(__file__).parent / "fixtures" / "nse"
CFG: NseConfig = load_config(REPO_CONFIG_DIR).providers.nse
HOME = f"{CFG.base_url}/"
BHAV = f"{CFG.archives_url}/products/content/sec_bhavdata_full_28032024.csv"
BAN = f"{CFG.archives_url}/content/fo/fo_secban.csv"
ASM = f"{CFG.base_url}/api/reportASM"
GSM = f"{CFG.base_url}/api/reportGSM"
CA = f"{CFG.base_url}/api/corporates-corporateActions"
SHP = f"{CFG.base_url}/api/corporate-share-holdings-master"
N500 = f"{CFG.niftyindices_url}/IndexConstituent/ind_nifty500list.csv"
RESULTS = f"{CFG.base_url}{CFG.results.index_path}"


def text(name: str) -> str:
    return (FIX / name).read_text()


@dataclass
class Log:
    urls: list[str] = field(default_factory=list)
    cookies: list[str | None] = field(default_factory=list)
    agents: list[str | None] = field(default_factory=list)


@pytest.fixture
def http() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def log(http: responses.RequestsMock) -> Log:
    """Homepage sets a cookie; every request is logged with its Cookie/User-Agent headers."""
    log = Log()

    def record(request: PreparedRequest) -> None:
        log.urls.append((request.url or "").split("?")[0])
        log.cookies.append(request.headers.get("Cookie"))
        log.agents.append(request.headers.get("User-Agent"))

    def home(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        record(request)
        return 200, {"Set-Cookie": "nsit=abc123; Path=/; Domain=www.nseindia.com"}, "<html/>"

    def serve(body: str, content_type: str) -> Any:
        def cb(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
            record(request)
            return 200, {"Content-Type": content_type}, body

        return cb

    http.add_callback(responses.GET, HOME, callback=home)
    for url, name, ctype in (
        (BHAV, "sec_bhavdata_full_28032024.csv", "text/csv"),
        (BAN, "fo_secban.csv", "text/csv"),
        (N500, "ind_nifty500list.csv", "text/csv"),
        (ASM, "reportASM.json", "application/json"),
        (GSM, "reportGSM.json", "application/json"),
        (CA, "corporate_actions_sampleind.json", "application/json"),
        (SHP, "shareholding_master_sampleind.json", "application/json"),
    ):
        http.add_callback(responses.GET, url, callback=serve(text(name), ctype))
    return log


@dataclass
class CountingLimiter:
    calls: list[Provider] = field(default_factory=list)

    def acquire(self, provider: Provider, *, timeout: float) -> None:
        self.calls.append(provider)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def nse(limiter: CountingLimiter | None = None, clock: Clock | None = None) -> NseProvider:
    session = NseSession(CFG, limiter=limiter, clock=clock or Clock())
    return NseProvider(CFG, session)


def test_protocols() -> None:
    p = nse()
    for proto in (
        DeliveryProvider,
        ConstituentsProvider,
        SurveillanceProvider,
        CorporateActionsProvider,
        ShareholdingProvider,
        ResultsFilingsProvider,
    ):
        assert isinstance(p, proto)


# ───────────── session etiquette ─────────────


def test_api_calls_warm_up_homepage_first_and_send_cookies(log: Log) -> None:
    nse().shareholding("SAMPLEIND")
    assert log.urls == [HOME, SHP]
    assert log.cookies[1] is not None and "nsit=abc123" in log.cookies[1]
    assert all(a == BROWSER_HEADERS["User-Agent"] for a in log.agents)


def test_cookies_reused_until_ttl_then_refreshed(log: Log) -> None:
    clock = Clock()
    p = nse(clock=clock)
    p.shareholding("SAMPLEIND")
    p.shareholding("SAMPLEIND")
    assert log.urls == [HOME, SHP, SHP]
    clock.t = CFG.cookie_ttl_s + 1
    p.shareholding("SAMPLEIND")
    assert log.urls == [HOME, SHP, SHP, HOME, SHP]


def test_archives_do_not_need_warm_up(log: Log) -> None:
    nse().delivery(date(2024, 3, 28))
    assert log.urls == [BHAV]


def test_forbidden_refreshes_cookies_once(http: responses.RequestsMock) -> None:
    http.add(responses.GET, HOME, body="<html/>")
    http.add(responses.GET, SHP, status=403)
    http.add(responses.GET, SHP, json=json.loads(text("shareholding_master_sampleind.json")))
    df = nse().shareholding("SAMPLEIND")
    assert len(df) == 3
    assert [c.request.url.split("?")[0] for c in http.calls] == [HOME, SHP, HOME, SHP]


def test_still_forbidden_is_transient_error(http: responses.RequestsMock) -> None:
    http.add(responses.GET, HOME, body="<html/>")
    http.add(responses.GET, SHP, status=403)
    with pytest.raises(ProviderError, match="403") as info:
        nse().shareholding("SAMPLEIND")
    assert not isinstance(info.value, ProviderUnavailable)


def test_html_instead_of_json_is_an_error(http: responses.RequestsMock) -> None:
    http.add(responses.GET, HOME, body="<html/>")
    http.add(responses.GET, SHP, body="<html>Access Denied</html>")
    with pytest.raises(ProviderError, match="not JSON"):
        nse().shareholding("SAMPLEIND")


def test_every_request_after_the_first_is_rate_limited(log: Log) -> None:
    limiter = CountingLimiter()
    nse(limiter=limiter).surveillance()  # home + ASM + GSM + ban = 4 requests
    assert limiter.calls == [Provider.NSE] * 3


def test_network_error_is_transient(http: responses.RequestsMock) -> None:
    with pytest.raises(ProviderError, match="ConnectionError"):
        nse().delivery(date(2024, 3, 28))


# ───────────── delivery (bhavcopy) ─────────────


def test_delivery_parses_padded_bhavcopy(log: Log) -> None:
    df = nse().delivery(date(2024, 3, 28))
    assert list(df["symbol"]) == ["20MICRONS", "SAMPLEIND", "M&M", "SMALLCO", "NEWLIST"]  # EQ/BE
    row = df.set_index("symbol").loc["SAMPLEIND"]
    assert row["date"] == date(2024, 3, 28)
    assert row["traded_qty"] == 1250000 and row["deliverable_qty"] == 687500
    assert row["delivery_pct"] == 55.0
    assert row["traded_value_cr"] == pytest.approx(181.09)  # 18109 lakh
    newlist = df.set_index("symbol").loc["NEWLIST"]
    assert pd.isna(newlist["delivery_pct"]) and pd.isna(newlist["deliverable_qty"])
    assert df["deliverable_qty"].dtype == "Int64"  # stays integer despite the gap


def test_delivery_holiday_is_empty_with_warning(http: responses.RequestsMock) -> None:
    http.add(responses.GET, BHAV.replace("28032024", "29032024"), status=404)
    df = nse().delivery(date(2024, 3, 29))
    assert df.empty and "holiday" in df.attrs["warnings"][0]


def test_delivery_rejects_unknown_header(http: responses.RequestsMock) -> None:
    http.add(responses.GET, BHAV, body="SYMBOL,CLOSE\nABC,1\n")
    with pytest.raises(ProviderError, match="bhavcopy header"):
        nse().delivery(date(2024, 3, 28))


# ───────────── constituents ─────────────


def test_index_constituents(log: Log) -> None:
    df = nse().index_constituents("NIFTY 500")
    assert list(df.columns) == ["symbol", "name", "industry", "series", "isin"]
    assert "M&M" in set(df["symbol"]) and len(df) == 5
    assert df.set_index("symbol").loc["TCS", "isin"] == "INE467B01029"


def test_unknown_index_unavailable() -> None:
    with pytest.raises(ProviderUnavailable):
        nse().index_constituents("NIFTYMICRO")


# ───────────── surveillance ─────────────


def test_surveillance_combines_asm_gsm_and_fo_ban(log: Log) -> None:
    df = nse().surveillance()
    got = set(df.itertuples(index=False, name=None))
    assert got == {
        ("ABCLTD", "asm_lt", "Stage II"),
        ("XYZLTD", "asm_lt", "Stage I"),
        ("PQRLTD", "asm_st", "Stage I"),
        ("DEFLTD", "gsm", "Stage 0"),
        ("GHILTD", "gsm", "Stage IV"),
        ("ABFRL", "fno_ban", None),
        ("BALRAMCHIN", "fno_ban", None),
        ("M&M-TEST", "fno_ban", None),
    }


def test_empty_fo_ban_list(http: responses.RequestsMock, log: Log) -> None:
    http.replace(responses.GET, BAN, body=text("fo_secban_nil.csv"))
    df = nse().surveillance()
    assert "fno_ban" not in set(df["list_name"])


def test_surveillance_fails_whole_call_if_a_list_is_missing(http: responses.RequestsMock) -> None:
    http.add(responses.GET, HOME, body="<html/>")
    http.add(responses.GET, ASM, json=json.loads(text("reportASM.json")))
    http.add(responses.GET, GSM, status=404)
    with pytest.raises(ProviderError):
        nse().surveillance()


def test_unrecognised_asm_shape(http: responses.RequestsMock) -> None:
    http.add(responses.GET, HOME, body="<html/>")
    http.add(responses.GET, ASM, json={"rows": []})
    with pytest.raises(ProviderError, match="unexpected ASM payload"):
        nse().surveillance()
    assert GSM not in [c.request.url for c in http.calls]  # failed fast


# ───────────── corporate actions ─────────────


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Bonus 1:1", (CorporateActionType.BONUS, 1.0, 2.0, None)),
        ("Bonus 3:2", (CorporateActionType.BONUS, 2.0, 5.0, None)),
        (
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
            (CorporateActionType.SPLIT, 1.0, 5.0, None),
        ),
        (
            "Face Value Split (Sub-Division) - From Re 1/- Per Share To Re 0.50/- Per Share",
            (CorporateActionType.SPLIT, 1.0, 2.0, None),
        ),
        ("Interim Dividend - Rs.9.50 Per Share", (CorporateActionType.DIVIDEND, None, None, 9.5)),
        (
            "Final Dividend - Rs 24 Per Share And Special Dividend - Rs 18 Per Share",
            (CorporateActionType.DIVIDEND, None, None, 42.0),
        ),
        ("Rights 1:5 @ Premium Rs 90/-", (CorporateActionType.RIGHTS, None, None, None)),
        ("Annual General Meeting", (CorporateActionType.OTHER, None, None, None)),
        ("Bonus Issue (ratio to be announced)", (CorporateActionType.BONUS, None, None, None)),
    ],
)
def test_parse_ca_subject(subject: str, expected: tuple[Any, ...]) -> None:
    assert parse_ca_subject(subject) == expected


def test_corporate_actions(log: Log, http: responses.RequestsMock) -> None:
    df = nse().corporate_actions("sampleind", date(2014, 1, 1), date(2024, 3, 31))
    by_date = {(r["ex_date"], r["action_type"]): r for r in df.to_dict("records")}

    final_and_special = by_date[(date(2023, 5, 31), "dividend")]  # two rows → one, summed
    assert final_and_special["dividend_per_share"] == 42.0
    assert "Special Dividend" in final_and_special["description"]
    bonus = by_date[(date(2022, 6, 15), "bonus")]
    assert (bonus["ratio_old"], bonus["ratio_new"], bonus["record_date"]) == (
        1.0, 2.0, date(2022, 6, 16),
    )  # fmt: skip
    assert by_date[(date(2019, 9, 5), "split")]["ratio_new"] == 5.0
    assert list(df["ex_date"]) == sorted(df["ex_date"])
    warnings = df.attrs["warnings"]
    assert any("no ex-date" in w for w in warnings)
    assert any("could not parse ratio" in w for w in warnings)
    q = next(c.request.url for c in http.calls if c.request.url.startswith(CA))
    assert "symbol=SAMPLEIND" in q and "from_date=01-01-2014" in q and "to_date=31-03-2024" in q


# ───────────── shareholding ─────────────


def test_shareholding_maps_canonical_fields(log: Log) -> None:
    df = nse().shareholding("SAMPLEIND")
    assert list(df.index) == [pd.Timestamp(d) for d in ("2023-09-30", "2023-12-31", "2024-03-31")]
    latest = df.loc["2024-03-31"]
    assert latest["promoter_pct"] == 54.9 and latest["public_pct"] == 45.1
    assert latest["filing_date"] == date(2024, 4, 19)
    assert df.loc["2023-09-30", "filing_date"] is None  # "-" → missing, not a guess
    assert "FII" in df.attrs["warnings"][0]


# ───────────── results filings (XBRL) ─────────────


def test_parse_results_index() -> None:
    payload = json.loads(text("financial_results_acme.json"))
    df, warnings = parse_results_index(payload, CFG.results.xbrl_hosts)
    assert len(df) == 2
    cons, stand = df.to_dict("records")
    assert cons["url"].endswith("10052024040200_WEB.xml")
    assert (cons["period_start"], cons["period_end"]) == (date(2024, 1, 1), date(2024, 3, 31))
    assert (cons["statement_type"], cons["audited"], cons["is_bank"]) == (
        "consolidated", True, False,
    )  # fmt: skip
    # broadCastDate wins over exchdisstime / filingDate; NSE times are IST
    assert cons["disseminated_at"].isoformat() == "2024-05-10T16:05:30+05:30"
    assert stand["statement_type"] == "standalone"  # "Non-Consolidated"
    assert any("no XBRL document for 01-Oct-2023" in w for w in warnings)
    assert any("evil.example.com" in w for w in warnings)  # not an https NSE host


def test_results_filings_requests_every_period_and_dedupes(http: responses.RequestsMock) -> None:
    body = text("financial_results_acme.json")
    http.add(responses.GET, HOME, body="<html/>")
    seen: list[str] = []

    def cb(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        seen.append((request.url or "").split("period=")[1])
        return 200, {"Content-Type": "application/json"}, body

    http.add_callback(responses.GET, RESULTS, callback=cb)
    limiter = CountingLimiter()
    df = nse(limiter).results_filings("acme")
    assert seen == CFG.results.periods and len(df) == 2  # same filings under both: deduped
    assert "symbol=ACME" in (http.calls[1].request.url or "")
    # the router pays for the first request; the session for the rest (homepage + 2nd period)
    assert len(limiter.calls) == len(CFG.results.periods)


def test_results_document_guards(http: responses.RequestsMock) -> None:
    ok = "https://nsearchives.nseindia.com/corporate/xbrl/A.xml"
    big = "https://nsearchives.nseindia.com/corporate/xbrl/BIG.xml"
    http.add(responses.GET, ok, body=b"<xbrl/>")
    http.add(responses.GET, big, body=b"x" * (CFG.results.max_xbrl_bytes + 1))
    http.add(responses.GET, "https://nsearchives.nseindia.com/corporate/xbrl/GONE.xml", status=404)
    p = nse()
    assert p.results_document(ok) == b"<xbrl/>"
    for url, message in (
        (big, "larger than"),
        ("https://nsearchives.nseindia.com/corporate/xbrl/GONE.xml", "not found"),
        ("https://evil.example.com/A.xml", "not on an allowed host"),
        ("http://nsearchives.nseindia.com/corporate/xbrl/A.xml", "not on an allowed host"),
    ):
        with pytest.raises(ProviderUnavailable, match=message):
            p.results_document(url)


def test_factory() -> None:
    assert isinstance(build_nse_provider(load_config(REPO_CONFIG_DIR).providers, None), NseProvider)
