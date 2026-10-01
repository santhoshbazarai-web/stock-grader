"""Split/bonus adjustment (data/adjust.py). Hand-computed expectations in each test."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.data.adjust import adjust_prices, largest_overnight_gap


def bars(closes: dict[str, float], volume: int = 1000) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in closes])
    c = np.array(list(closes.values()), dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": volume}, index=idx
    )


def actions(*rows: tuple[str, str, float | None, float | None]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"ex_date": date.fromisoformat(d), "action_type": t, "ratio_old": o, "ratio_new": n}
         for d, t, o, n in rows],
        columns=["ex_date", "action_type", "ratio_old", "ratio_new"],
    )  # fmt: skip


def test_bonus_one_for_one_halves_earlier_prices() -> None:
    raw = bars({"2024-06-03": 1000, "2024-06-04": 1010, "2024-06-05": 505, "2024-06-06": 510})
    res = adjust_prices(raw, actions(("2024-06-05", "bonus", 1, 2)))
    adj = res.prices
    assert res.complete and res.warnings == []
    assert list(adj["adj_factor"]) == [0.5, 0.5, 1.0, 1.0]
    assert list(adj["adj_close"]) == [500, 505, 505, 510]
    assert adj["adj_high"].iloc[0] == pytest.approx(505.0)
    assert list(adj["adj_volume"]) == [2000, 2000, 1000, 1000]
    assert list(adj["close"]) == [1000, 1010, 505, 510]  # raw untouched


def test_face_value_split_ten_to_two() -> None:
    raw = bars({"2019-09-04": 2500, "2019-09-05": 502})
    adj = adjust_prices(raw, actions(("2019-09-05", "split", 1, 5))).prices
    assert list(adj["adj_close"]) == [500, 502]
    assert list(adj["adj_volume"]) == [5000, 1000]


def test_bonus_three_for_two() -> None:
    # 3 new for every 2 held: 2 shares → 5, factor 2/5
    adj = adjust_prices(bars({"2018-01-09": 1000, "2018-01-10": 401}),
                        actions(("2018-01-10", "bonus", 2, 5))).prices  # fmt: skip
    assert adj["adj_close"].iloc[0] == pytest.approx(400.0)


def test_actions_compound_backwards() -> None:
    raw = bars({"2017-01-02": 1000, "2018-01-02": 1000, "2019-01-02": 1000})
    acts = actions(("2017-06-01", "split", 1, 2), ("2018-06-01", "bonus", 1, 2))
    assert list(adjust_prices(raw, acts).prices["adj_factor"]) == [0.25, 0.5, 1.0]


def test_same_event_from_two_sources_counted_once() -> None:
    # NSE says "Bonus 1:1", yfinance says "split x2" for the same ex-date.
    raw = bars({"2024-06-04": 1000, "2024-06-05": 500})
    res = adjust_prices(raw, actions(("2024-06-05", "bonus", 1, 2), ("2024-06-05", "split", 1, 2)))
    assert list(res.prices["adj_close"]) == [500, 500]
    assert any("counted once" in w for w in res.warnings)


def test_missing_ratio_leaves_earlier_bars_null() -> None:
    raw = bars({"2016-02-29": 900, "2016-03-01": 450, "2020-01-01": 600, "2020-06-01": 300})
    acts = actions(("2016-03-01", "bonus", None, None), ("2020-03-02", "split", 1, 2))
    res = adjust_prices(raw, acts)
    adj = res.prices
    assert not res.complete and res.unadjustable_before == pd.Timestamp("2016-03-01")
    assert pd.isna(adj["adj_close"].iloc[0]) and pd.isna(adj["adj_volume"].iloc[0])
    assert list(adj["adj_close"].iloc[1:]) == [225, 300, 300]  # later bars still right
    assert any("no usable ratio" in w for w in res.warnings)


def test_dividends_and_rights_do_not_adjust() -> None:
    raw = bars({"2023-05-30": 1000, "2023-05-31": 976})
    acts = pd.DataFrame(
        [
            {"ex_date": date(2023, 5, 31), "action_type": "dividend", "ratio_old": None,
             "ratio_new": None},
            {"ex_date": date(2023, 5, 31), "action_type": "rights", "ratio_old": None,
             "ratio_new": None},
        ]
    )  # fmt: skip
    res = adjust_prices(raw, acts)
    assert list(res.prices["adj_factor"]) == [1.0, 1.0] and res.complete
    assert any("rights" in w for w in res.warnings)


def test_no_actions_is_identity() -> None:
    raw = bars({"2024-01-01": 100, "2024-01-02": 101})
    adj = adjust_prices(raw, pd.DataFrame(columns=["ex_date", "action_type"])).prices
    assert list(adj["adj_close"]) == [100, 101]


def test_known_bonus_infosys_2018_removes_ex_date_jump() -> None:
    """Real event: Infosys 1:1 bonus, record date 5 Sep 2018, ex-date 4 Sep 2018.

    The prices here are synthetic (this environment cannot download history); they follow the
    shape of the event: ~₹1,430 before, ~₹715 from the ex-date. Run
    ``python -m app.jobs verify-adjustment --symbol INFY`` after ``eod_prices`` to repeat
    this check on real stored data.
    """
    rng = np.random.default_rng(7)
    days = pd.bdate_range("2018-08-20", "2018-09-14")
    level = np.where(days < pd.Timestamp("2018-09-04"), 1430.0, 715.0)
    closes = level * (1 + rng.normal(0, 0.006, len(days)))
    raw = bars(dict(zip([d.date().isoformat() for d in days], closes, strict=True)))

    res = adjust_prices(raw, actions(("2018-09-04", "bonus", 1, 2)))

    assert largest_overnight_gap(raw["close"]) > 0.45  # the unadjusted ~50% drop
    assert largest_overnight_gap(res.prices["adj_close"]) < 0.03  # gone after adjustment
    before = res.prices.loc["2018-09-03"]
    assert before["adj_close"] == pytest.approx(before["close"] / 2)
