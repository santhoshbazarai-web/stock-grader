"""Indian API /stock cross-checks and events (app/data/indianapi_checks.py) on the recorded
fixtures: keyMetrics vs our derived values, the bonus / split list vs ours, board meetings and
the analyst consensus (informational)."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.core.settings import config_dir_from_env
from app.data.indianapi_checks import (
    OurAction,
    analyst_consensus,
    board_meetings,
    corporate_action_checks,
    key_metric_checks,
    vendor_corporate_actions,
)
from app.data.indianapi_parse import map_statements
from app.db.enums import CorporateActionType
from app.fundamentals.indianapi_map import IndianApiMap, get_indianapi_map

FIX = Path(__file__).parent / "fixtures" / "indianapi"
BONUS, SPLIT = CorporateActionType.BONUS, CorporateActionType.SPLIT


@pytest.fixture(scope="module")
def amap() -> IndianApiMap:
    return get_indianapi_map(config_dir_from_env())


def stock(name: str) -> Any:
    return json.loads((FIX / f"{name}_stock.json").read_text())


def checks(name: str, amap: IndianApiMap) -> dict[str, Any]:
    st = stock(name)
    price = float(st["currentPrice"]["NSE"])
    return {c.name: c for c in key_metric_checks(st, map_statements(st, {}, amap), amap, price)}


def test_hdfcbank_key_metrics_agree_with_ours(amap: IndianApiMap) -> None:
    got = checks("hdfcbank", amap)
    # BVPS = TotalEquity 586,059.47 cr / 1,539.34 cr shares = 380.72 (vendor 380.72)
    assert got["book_value_per_share"].ours == pytest.approx(380.72, rel=0.005)
    assert got["book_value_per_share"].ok is True
    # market cap = 721.20 x 1,539.34 cr shares = 1,110,172 cr (vendor 1,109,000)
    assert got["market_cap_cr"].ours == pytest.approx(1_110_172, rel=0.005)
    # ROA on the profit including minority interest, over average total assets: 1.70%
    assert got["roa_pct"].ours == pytest.approx(1.70, rel=0.02)
    assert all(c.ok for c in got.values())
    assert "(FY2026)" in got["price_to_book"].text


def test_itc_book_value_difference_is_listed(amap: IndianApiMap) -> None:
    got = checks("itc", amap)
    bv = got["book_value_per_share"]
    # vendor 54.46 vs 72,507.30 cr / 1,252.95 cr shares = 57.87: 5.9% apart
    assert bv.ok is False and bv.rel == pytest.approx(0.059, abs=0.002)
    assert "above 5%" in bv.text
    assert got["roa_pct"].ok is True


def test_missing_key_metrics_are_not_compared(amap: IndianApiMap) -> None:
    got = checks("tcs", amap)  # TCS's /stock carries no keyMetrics
    assert all(c.ok is None and "vendor gives none" in c.text for c in got.values())


def test_no_price_means_no_price_ratios(amap: IndianApiMap) -> None:
    st = stock("hdfcbank")
    got = {c.name: c for c in key_metric_checks(st, map_statements(st, {}, amap), amap, None)}
    assert got["price_to_book"].ok is None and got["market_cap_cr"].ok is None
    assert got["book_value_per_share"].ok is True


def test_vendor_bonus_and_split_are_parsed() -> None:
    hdfc, since = vendor_corporate_actions(stock("hdfcbank"))
    assert [(a.ex_date, a.action_type, a.ratio_old, a.ratio_new) for a in hdfc] == [
        (date(2025, 8, 26), BONUS, 1.0, 2.0)]  # 1:1 bonus → 2 shares for 1  # fmt: skip
    assert since == date(2021, 6, 29)  # the oldest dividend: the lists cover 2021 on
    steel, _ = vendor_corporate_actions(stock("tatasteel"))
    assert [(a.ex_date, a.action_type, a.ratio_new) for a in steel] == [
        (date(2022, 7, 28), SPLIT, 10.0)]  # face value Rs 10 → Re 1  # fmt: skip


def test_corporate_action_cross_check() -> None:
    vendor, since = vendor_corporate_actions(stock("hdfcbank"))
    # ours agrees (ex-date one day apart, inside the window): nothing to report; the 2019
    # split predates the vendor's lists, so it is not expected there
    ours = [OurAction(date(2025, 8, 27), BONUS, 1, 2), OurAction(date(2019, 9, 19), SPLIT, 1, 2)]
    assert corporate_action_checks(vendor, since, ours, 3) == []
    missing = corporate_action_checks(vendor, since, [], 3)
    assert len(missing) == 1 and "bonus ex 2025-08-26" in missing[0]
    assert "not in our corporate actions" in missing[0]
    wrong = corporate_action_checks(vendor, since, [OurAction(date(2025, 8, 26), BONUS, 1, 3)], 3)
    assert wrong == ["bonus ex 2025-08-26: Indian API ratio 2x, ours 3x"]
    extra = corporate_action_checks([], since, [OurAction(date(2024, 1, 5), SPLIT, 1, 5)], 3)
    assert extra == ["our split ex 2024-01-05 is not in the Indian API list (which covers "
                     "2021-06-29 on)"]  # fmt: skip


def test_board_meetings_become_events() -> None:
    df = board_meetings(stock("hdfcbank"), symbol="HDFCBANK", isin="INE040A01034")
    assert len(df) == 26 and set(df["kind"]) == {"board_meeting"}
    first = df.iloc[0]
    assert first["event_date"] == date(2026, 10, 17)
    assert first["title"] == "Board meeting: Quarterly Results"
    assert first["data"] == {"purpose": "Quarterly Results", "source": "indianapi"}
    assert first["source_id"] == "HDFCBANK:2026-10-17:Quarterly Results"


def test_analyst_consensus_is_informational() -> None:
    got = analyst_consensus(stock("hdfcbank"))
    assert got == {"recommendations": 40, "mean_rating": 1.525,
                   "scale": "1 Strong Buy … 5 Strong Sell",
                   "ratings": {"Strong Sell": 0, "Sell": 0, "Hold": 2, "Buy": 17,
                               "Strong Buy": 21}}  # fmt: skip
    assert analyst_consensus({"recosBar": {"isDataPresent": False}}) is None
    assert analyst_consensus({}) is None
