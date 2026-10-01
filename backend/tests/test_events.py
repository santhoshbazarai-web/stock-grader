"""Exchange event feeds (SPEC v0.2 §3.8, §10 events): pure parsers on the fixtures in
tests/fixtures/events, classification, and the NSE / BSE / Market Lens providers over mocked
HTTP (window splitting, paging, raw cache before parsing)."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import responses
from requests import PreparedRequest

from app.core.config import load_config
from app.data.events import (
    EVENT_COLUMNS,
    EventFormatError,
    classify,
    is_results_meeting,
    normalise_company,
    parse_bse_announcements,
    parse_date,
    parse_nse_announcements,
    parse_nse_board_meetings,
    parse_nse_deals,
    parse_nse_pit,
    parse_nse_pledges,
    parse_nse_results_feed,
    parse_nse_sast,
)
from app.data.providers.base import EventsProvider, ProviderError, ReferenceFinancialsProvider
from app.data.providers.bse import BseProvider
from app.data.providers.market_lens import (
    MarketLensFormatError,
    MarketLensProvider,
    build_market_lens_provider,
    parse_market_lens,
)
from app.data.providers.nse import NseProvider, NseSession, _windows
from app.data.raw_store import RawStore
from app.db.enums import EventKind
from tests.conftest import REPO_CONFIG_DIR
from tests.web_support import mock_warmup

CONFIG = load_config(REPO_CONFIG_DIR)
PCFG = CONFIG.providers
CLS = CONFIG.jobs.event_classification
FIX = Path(__file__).parent / "fixtures" / "events"
IST = ZoneInfo("Asia/Kolkata")
STAMP = lambda: datetime(2024, 6, 14, 12, 0, tzinfo=UTC)  # noqa: E731


def load(name: str) -> object:
    return json.loads((FIX / name).read_text())


def text(name: str) -> str:
    return (FIX / name).read_text()


# ───────────────────────── parsers ─────────────────────────


def test_nse_announcements() -> None:
    df = parse_nse_announcements(load("nse_announcements.json"))
    assert list(df.columns) == EVENT_COLUMNS
    assert len(df) == 3 and df.attrs["warnings"][0].startswith("announcement without")
    first = df.iloc[0]
    assert first["kind"] == "announcement" and first["source_id"] == "104511001"
    assert first["symbol"] == "DEMOIT" and first["isin"] == "INE9DEMO0001"
    assert first["title"] == "Resignation of Statutory Auditors"
    assert first["disseminated_at"] == datetime(2024, 6, 13, 18, 15, tzinfo=IST)
    assert first["event_date"] == date(2024, 6, 13)
    assert first["url"].startswith("https://nsearchives.nseindia.com/")
    assert df.iloc[1]["url"] is None  # plain-http links from the feed are dropped
    assert df.iloc[0]["data"] == {"industry": "Computers - Software"}


def test_nse_board_meetings_and_results_purpose() -> None:
    df = parse_nse_board_meetings(load("nse_board_meetings.json"))
    assert len(df) == 2 and len(df.attrs["warnings"]) == 1
    it = df.iloc[0]
    assert it["kind"] == "board_meeting" and it["event_date"] == date(2024, 6, 20)
    assert it["source_id"] == "DEMOIT:2024-06-20:Financial Results"
    assert it["title"] == "Board meeting: Financial Results"
    purposes = CONFIG.jobs.results_watch.results_purposes
    assert is_results_meeting(it["data"]["purpose"], purposes)
    assert not is_results_meeting(df.iloc[1]["data"]["purpose"], purposes)


def test_nse_results_feed() -> None:
    df = parse_nse_results_feed(load("nse_results.json"), PCFG.nse.results.xbrl_hosts)
    assert len(df) == 3 and df.attrs["warnings"][0].startswith("results filing without")
    cons, stand, other = (df.iloc[i] for i in range(3))
    assert cons["kind"] == "results" and cons["source_id"].endswith("DEMOIT_Q4_CONS.xml")
    assert cons["event_date"] == date(2024, 3, 31)
    assert cons["data"]["basis"] == "consolidated" and cons["data"]["period_start"] == "2024-01-01"
    assert "period ended 31 Mar 2024, consolidated" in cons["title"]
    # no XBRL document: keyed by symbol + period + basis; dissemination from exchdisstime
    assert stand["source_id"] == "DEMOIT:2024-03-31:standalone:Fourth Quarter"
    assert stand["url"] is None and stand["disseminated_at"].hour == 16
    assert other["url"] is None  # XBRL host not allowed


def test_nse_pledge_sast_pit() -> None:
    pledge = parse_nse_pledges(load("nse_pledge.json")).iloc[0]
    assert pledge["kind"] == "pledge" and pledge["symbol"] is None
    assert pledge["company"] == "Demo Bank Ltd."
    assert pledge["title"] == "Promoter pledge disclosure: 30% of promoter holding pledged"
    assert pledge["data"]["shares_pledged"] == 12_000_000
    assert classify(pledge["title"], pledge["detail"], CLS) == "pledge_invocation"

    sast = parse_nse_sast(load("nse_sast.json")).iloc[0]
    assert sast["title"] == "SAST reg. 29: Growth Fund LP, acquisition of 2,500,000 shares"
    assert sast["data"]["holding_after_pct"] == 5.1

    pit = parse_nse_pit(load("nse_pit.json"))
    assert len(pit) == 1 and pit.attrs["warnings"]
    row = pit.iloc[0]
    assert row["source_id"] == "5540021" and row["event_date"] == date(2024, 6, 11)
    assert row["title"] == ("Insider trade: R Kumar (Promoters) sell 150,000 shares, "
                            "₹21.00 cr")  # fmt: skip
    assert classify(row["title"], row["detail"], CLS) == "promoter_sale"


def test_nse_deals() -> None:
    bulk = parse_nse_deals(text("bulk.csv"), EventKind.BULK_DEAL)
    assert len(bulk) == 2
    assert bulk.iloc[0]["title"] == ("Bulk deal: ALPHA SECURITIES LLP bought 650,000 shares at "
                                     "₹1,412.35")  # fmt: skip
    assert bulk.iloc[1]["data"]["side"] == "SELL"
    assert bulk.iloc[0]["source_id"] != bulk.iloc[1]["source_id"]
    block = parse_nse_deals(text("block.csv"), EventKind.BLOCK_DEAL)
    assert block.iloc[0]["kind"] == "block_deal" and block.iloc[0]["data"]["shares"] == 2e6
    with pytest.raises(EventFormatError, match="without columns"):
        parse_nse_deals("a,b\n1,2\n", EventKind.BULK_DEAL)
    with pytest.raises(ValueError, match="not a deal kind"):
        parse_nse_deals(text("bulk.csv"), EventKind.PLEDGE)


def test_bse_announcements() -> None:
    df, total = parse_bse_announcements(
        load("bse_announcements.json"), results_categories=["Result"],
        attachment_url=PCFG.bse.attachment_url)  # fmt: skip
    assert total == 3 and len(df) == 2 and df.attrs["warnings"]
    result, meeting = df.iloc[0], df.iloc[1]
    assert result["kind"] == "results" and result["bse_code"] == "990001"
    assert result["url"] == f"{PCFG.bse.attachment_url}a1b2c3d4-0001.pdf"
    assert result["detail"] == "Audited results for Q4 FY24"
    assert result["disseminated_at"] == datetime(2024, 6, 14, 16, 10, 22, 113000, tzinfo=IST)
    assert meeting["kind"] == "announcement" and meeting["url"] is None
    with pytest.raises(EventFormatError):
        parse_bse_announcements({"x": 1}, results_categories=[], attachment_url="")


def test_unknown_shapes_raise() -> None:
    with pytest.raises(EventFormatError, match="unexpected payload"):
        parse_nse_announcements({"rows": []})
    with pytest.raises(EventFormatError):
        parse_nse_pit("<html>")


@pytest.mark.parametrize(
    ("title", "detail", "category"),
    [
        ("Resignation of Statutory Auditors", None, "auditor_resignation"),
        ("Cessation", "Cessation of M/s XYZ as Statutory Auditor", "auditor_resignation"),
        ("Credit Rating", "ICRA downgraded the rating to BBB", "credit_rating_downgrade"),
        ("Credit Rating", "CRISIL reaffirmed AA+", "credit_rating"),
        ("Default", "Delay in payment of interest on NCDs", "default"),
        ("Outcome of Board Meeting", "Financial Results for Q4", "results"),
        ("Resignation of Company Secretary", None, "kmp_change"),
        ("Press release", "New product launch", None),
    ],
)
def test_classify(title: str, detail: str | None, category: str | None) -> None:
    assert classify(title, detail, CLS) == category


def test_helpers() -> None:
    assert normalise_company("HDFC Bank Ltd.") == normalise_company("hdfc bank limited")
    assert normalise_company(None) is None
    assert parse_date("20240614") == date(2024, 6, 14)
    assert parse_date("14-Jun-2024 10:00:00") == date(2024, 6, 14)
    assert parse_date("-") is None
    assert _windows(date(2024, 6, 1), date(2024, 6, 14), 7) == [
        (date(2024, 6, 8), date(2024, 6, 14)),
        (date(2024, 6, 1), date(2024, 6, 7)),
    ]
    assert _windows(date(2024, 6, 14), date(2024, 6, 14), 7) == [
        (date(2024, 6, 14), date(2024, 6, 14))]  # fmt: skip


# ───────────────────────── providers ─────────────────────────


class Clock:
    def __call__(self) -> float:
        return 0.0


def nse(tmp_path: Path) -> NseProvider:
    return NseProvider(PCFG.nse, NseSession(PCFG.nse, clock=Clock()),
                       RawStore(tmp_path, clock=STAMP))  # fmt: skip


@responses.activate
def test_nse_events_windows_cache_and_parse(tmp_path: Path) -> None:
    base, cfg = PCFG.nse.base_url, PCFG.nse.events
    mock_warmup(PCFG.nse.browser)
    seen: list[str] = []

    def announcements(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        seen.append(request.url or "")
        return 200, {}, text("nse_announcements.json")

    responses.add_callback(responses.GET, f"{base}{cfg.announcements_path}",
                           callback=announcements)  # fmt: skip
    p = nse(tmp_path)
    assert isinstance(p, EventsProvider)
    df = p.events(EventKind.ANNOUNCEMENT, date(2024, 6, 1), date(2024, 6, 14))
    # two windows of at most 7 days, newest first; identical rows deduplicated
    assert len(seen) == 2 and "from_date=08-06-2024" in seen[0] and "to_date=07-06-2024" in seen[1]
    # the same rows came back for both windows: the first (newest) copy is kept
    assert len(df) == 3
    assert set(df["raw_path"]) == {"nse/2024/06/14/announcement_20240608_20240614.json"}
    assert (tmp_path / "nse/2024/06/14/announcement_20240601_20240607.json").is_file()


@responses.activate
def test_nse_results_feed_reads_each_period_and_deals_file(tmp_path: Path) -> None:
    base, cfg = PCFG.nse.base_url, PCFG.nse.events
    mock_warmup(PCFG.nse.browser)
    periods: list[str] = []

    def results(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        periods.append((request.url or "").split("period=")[1].split("&")[0])
        return 200, {}, text("nse_results.json")

    responses.add_callback(responses.GET, f"{base}{cfg.results_path}", callback=results)
    responses.add(responses.GET, f"{PCFG.nse.archives_url}{cfg.bulk_deals_path}",
                  body=text("bulk.csv"))  # fmt: skip
    p = nse(tmp_path)
    df = p.events(EventKind.RESULTS, date(2024, 6, 13), date(2024, 6, 14))
    assert sorted(periods) == ["Annual", "Quarterly"] and len(df) == 3
    deals = p.events(EventKind.BULK_DEAL, date(2024, 6, 13), date(2024, 6, 14))
    assert len(deals) == 2 and deals.iloc[0]["raw_path"] == "nse/2024/06/14/bulk_deal.csv"


@responses.activate
def test_nse_events_bad_shape_is_a_provider_error(tmp_path: Path) -> None:
    base = PCFG.nse.base_url
    mock_warmup(PCFG.nse.browser)
    responses.add(responses.GET, f"{base}{PCFG.nse.events.pit_path}", json={"rows": 1})
    with pytest.raises(ProviderError, match="insider_trade"):
        nse(tmp_path).events(EventKind.INSIDER_TRADE, date(2024, 6, 14), date(2024, 6, 14))
    # cached before parsing, so a parser fix can re-read it
    assert (tmp_path / "nse/2024/06/14/insider_trade_20240614_20240614.json").is_file()


@responses.activate
def test_bse_announcements_paging(tmp_path: Path) -> None:
    url = f"{PCFG.bse.api_url}{PCFG.bse.announcements_path}"
    page = load("bse_announcements.json")
    assert isinstance(page, dict)
    page["Table1"] = [{"ROWCNT": 5}]  # 3 rows per page, 5 in all → two pages
    calls: list[str] = []

    def serve(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        calls.append(request.url or "")
        n = len(calls)
        body = dict(page, Table=[dict(r, NEWSID=f"{r.get('NEWSID')}-{n}") for r in page["Table"]])
        return 200, {}, json.dumps(body)

    responses.add_callback(responses.GET, url, callback=serve)
    responses.add(responses.GET, PCFG.bse.browser.warmup_urls[0], body="<html/>")

    class Limiter:
        taken = 0

        def acquire(self, provider: object, *, timeout: float) -> None:
            Limiter.taken += 1

    p = BseProvider(PCFG.bse, raw_store=RawStore(tmp_path, clock=STAMP), limiter=Limiter())
    assert isinstance(p, EventsProvider)
    df = p.events(EventKind.ANNOUNCEMENT, date(2024, 6, 13), date(2024, 6, 14))
    assert len(calls) == 2 and "strPrevDate=20240613" in calls[0] and "pageno=2" in calls[1]
    assert Limiter.taken == 2  # warm-up + 2 pages; the router pays for the first request
    assert len(df) == 4 and set(df["kind"]) == {"results", "announcement"}
    assert (tmp_path / "bse/2024/06/14/announcements_20240613_20240614_p2.json").is_file()
    with pytest.raises(ProviderError, match="no pledge feed"):
        p.events(EventKind.PLEDGE, date(2024, 6, 13), date(2024, 6, 14))


def test_market_lens_parse_and_guard() -> None:
    cfg = PCFG.market_lens
    df = parse_market_lens(load("market_lens.json"), cfg)
    annual = df[df["period_type"] == "year"].set_index("item_code")["value_inr"]
    assert annual["revenue"] == pytest.approx(1000e7) and annual["pat"] == pytest.approx(120e7)
    assert "cfo" not in annual and "total_equity" not in annual  # null / "-": left out, not 0
    q = df[df["period_type"] == "quarter"].iloc[0]
    assert q["basis"] == "standalone" and q["period_end"] == date(2024, 3, 31)
    assert len(df) == 5  # the unrecognised period type and the dateless row are skipped
    with pytest.raises(MarketLensFormatError):
        parse_market_lens({"rows": []}, cfg)
    assert cfg.enabled is False and build_market_lens_provider(PCFG) is None
    on = PCFG.model_copy(update={"market_lens": cfg.model_copy(update={"enabled": True})})
    provider = build_market_lens_provider(on)
    assert isinstance(provider, MarketLensProvider)
    assert isinstance(provider, ReferenceFinancialsProvider)
