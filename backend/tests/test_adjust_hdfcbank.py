"""HDFC Bank's split (₹2 → ₹1, ex 19 Sep 2019) and 1:1 bonus (ex 27 Aug 2025): parsing NSE's
corporate-actions subjects, and adjusting raw prices exactly once, whether the actions come
from one source or two (yfinance reports a bonus as a split, sometimes a day apart), and
whether the raw prices are unadjusted or already adjusted by their source.

Prices here are synthetic (a flat ₹1,000 series halved at each ex-date, not HDFC Bank's
quotes); the action fixture is tests/fixtures/nse/corporate_actions_hdfcbank.json."""

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.core.config import Provider, load_config
from app.data.adjust import adjust_prices, restate_per_share
from app.data.gaps import InMemoryGapRecorder
from app.data.providers.nse import parse_corporate_actions
from app.data.router import DataRouter, Outcome
from tests.conftest import REPO_CONFIG_DIR

CFG = load_config(REPO_CONFIG_DIR).providers
ADJ = CFG.adjustment
FIX = Path(__file__).parent / "fixtures" / "nse"
SPLIT, BONUS = pd.Timestamp("2019-09-19"), pd.Timestamp("2025-08-27")


def nse_actions() -> pd.DataFrame:
    df, warnings = parse_corporate_actions(
        json.loads((FIX / "corporate_actions_hdfcbank.json").read_text())
    )
    assert warnings == ["skipped 'Annual General Meeting': no ex-date"]
    return df


def raw_prices(*, unadjusted: bool = True) -> pd.DataFrame:
    """Business days 2019-06 .. 2025-10, flat at ₹1,000 in today's share terms. Unadjusted:
    ₹4,000 before the split, ₹2,000 between split and bonus (each action halves the price)."""
    days = pd.bdate_range("2019-06-03", "2025-10-31")
    close = pd.Series(1000.0, index=days)
    if unadjusted:
        close[days < BONUS] *= 2
        close[days < SPLIT] *= 2
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                         "volume": 1000})  # fmt: skip


def test_nse_subjects_give_the_split_and_bonus_ratios() -> None:
    df = nse_actions()
    rows = {r["action_type"]: r for r in df.to_dict("records")}
    split, bonus = rows["split"], rows["bonus"]
    assert split["ex_date"] == date(2019, 9, 19) and (split["ratio_old"], split["ratio_new"]) == (
        1,
        2,
    )
    assert split["record_date"] == date(2019, 9, 20)
    assert bonus["ex_date"] == date(2025, 8, 27) and (bonus["ratio_old"], bonus["ratio_new"]) == (
        1,
        2,
    )


def test_unadjusted_prices_are_adjusted_once_with_no_gap() -> None:
    res = adjust_prices(raw_prices(), nse_actions(), ADJ)
    adj = res.prices["adj_close"]
    assert np.allclose(adj, 1000.0)  # continuous: no jump at either ex-date
    f = res.prices["adj_factor"]
    assert (
        f[f.index < SPLIT].iloc[-1] == 0.25
        and f[(f.index >= SPLIT) & (f.index < BONUS)].iloc[0] == 0.5
    )
    assert not [w for w in res.warnings if "missing or doubled" in w]


def test_the_same_event_from_two_sources_a_day_apart_counts_once() -> None:
    yf = pd.DataFrame([  # yfinance: both as "split x2", ex-dates a day off NSE's
        {"ex_date": date(2019, 9, 20), "action_type": "split", "ratio_old": 1.0, "ratio_new": 2.0},
        {"ex_date": date(2025, 8, 26), "action_type": "split", "ratio_old": 1.0, "ratio_new": 2.0},
    ])  # fmt: skip
    both = pd.concat([nse_actions()[["ex_date", "action_type", "ratio_old", "ratio_new"]], yf])
    res = adjust_prices(raw_prices(), both, ADJ)
    assert np.allclose(res.prices["adj_close"], 1000.0)
    dups = [w for w in res.warnings if "duplicates" in w]
    assert len(dups) == 2
    # the ex-date the raw prices confirm is kept (NSE's), the other one dropped
    assert "split on 2019-09-20 duplicates the one on 2019-09-19" in dups[0]
    assert "on 2025-08-26 duplicates the one on 2025-08-27" in dups[1]
    # without the window (the old rule: same ex-date only) both were applied: a doubled jump
    doubled = adjust_prices(raw_prices(), both)
    assert not np.allclose(doubled.prices["adj_close"], 1000.0)


def test_prices_already_adjusted_by_the_source_are_not_adjusted_again() -> None:
    res = adjust_prices(raw_prices(unadjusted=False), nse_actions(), ADJ)
    assert np.allclose(res.prices["adj_close"], 1000.0)
    assert sum("already adjusted by the source" in w for w in res.warnings) == 2
    # the old behaviour (no detection): pre-2019 prices quartered, two upward jumps
    old = adjust_prices(raw_prices(unadjusted=False), nse_actions())
    assert old.prices["adj_close"].iloc[0] == pytest.approx(250.0)


def test_a_missing_action_is_reported_as_an_abnormal_gap() -> None:
    res = adjust_prices(raw_prices(), nse_actions().iloc[[0]], ADJ)  # the split only
    gaps = [w for w in res.warnings if "may be missing or doubled" in w]
    assert gaps == ["adjusted close moves 50% on 2025-08-27: a split/bonus may be missing or "
                    "doubled"]  # fmt: skip


class NoLimit:
    def acquire(self, provider: Provider, *, timeout: float) -> None:
        pass


class EmptyCa:
    def __init__(self, name: Provider) -> None:
        self.name = name

    def corporate_actions(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        return pd.DataFrame(columns=["ex_date", "action_type"])


def test_no_actions_anywhere_is_an_answer_not_a_gap() -> None:
    gaps = InMemoryGapRecorder()
    router = DataRouter({p.name: p for p in (EmptyCa(Provider.NSE), EmptyCa(Provider.YFINANCE))},
                        CFG, limiter=NoLimit(), gaps=gaps)  # fmt: skip
    res = router.corporate_actions("HDFCBANK", date(2026, 9, 1), date(2026, 10, 1))
    assert res.data is not None and res.data.empty and res.source is Provider.NSE
    assert [a.outcome for a in res.attempts] == [Outcome.EMPTY, Outcome.EMPTY]
    assert gaps.open == {}


def test_per_share_figures_are_restated_to_todays_share_basis() -> None:
    # synthetic per-share figures; announcement dates decide which basis each row is on
    fin = pd.DataFrame(
        {
            "eps_diluted": [80.0, 40.0, 20.0, 10.0],
            "book_value_per_share": [400.0, 200.0, 100.0, 50.0],
            "shares_diluted_cr": [270.0, 540.0, 1080.0, 2160.0],
            "pat": [21600.0, 21600.0, 21600.0, 21600.0],
            "announcement_date": [date(2019, 4, 20), date(2020, 4, 18), date(2025, 7, 19),
                                  date(2025, 10, 18)],
        },
        index=pd.DatetimeIndex(["2019-03-31", "2020-03-31", "2025-06-30", "2025-09-30"],
                               name="period_end"),
    )  # fmt: skip
    out, notes = restate_per_share(fin, nse_actions(), ADJ)
    # FY2019 (before the split and the bonus) x4; FY2020 and Jun-2025 (before the bonus) x2;
    # Sep-2025, announced after the bonus, was already on the new basis
    assert out["eps_diluted"].tolist() == [20.0, 20.0, 10.0, 10.0]
    assert out["book_value_per_share"].tolist() == [100.0, 100.0, 50.0, 50.0]
    assert out["shares_diluted_cr"].tolist() == [1080.0, 1080.0, 2160.0, 2160.0]
    assert out["pat"].tolist() == fin["pat"].tolist()  # amounts untouched
    assert notes == [
        "per-share figures known before 2019-09-19 restated for the split x2 on 2019-09-19",
        "per-share figures known before 2025-08-27 restated for the bonus x2 on 2025-08-27",
    ]
    # no announcement date: the period end decides
    out2, _ = restate_per_share(fin.drop(columns="announcement_date"), nse_actions(), ADJ)
    assert out2["eps_diluted"].tolist() == [20.0, 20.0, 10.0, 10.0]


def test_rows_already_on_todays_share_basis_are_not_restated() -> None:
    """The Indian API reports HDFC Bank's FY2015 EPS as 10.66 on PAT 10,703 crore: ~1,004 crore
    shares, i.e. already after the 2019 split and 2025 bonus. Restating it would quarter it."""
    idx = pd.DatetimeIndex(["2015-03-31", "2026-03-31"], name="period_end")
    fin = pd.DataFrame({"eps_diluted": [10.66, 49.39]}, index=idx)
    out, notes = restate_per_share(fin, nse_actions(), ADJ, current={date(2015, 3, 31)})
    assert out["eps_diluted"].tolist() == [10.66, 49.39] and notes == []
    # the same row from a filing (as reported then) is restated: x4 shares, EPS / 4
    filed, _ = restate_per_share(fin, nse_actions(), ADJ)
    assert filed["eps_diluted"].tolist() == [pytest.approx(2.665), 49.39]
