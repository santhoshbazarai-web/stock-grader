"""Point-in-time step series with several periods known on one day (the HDFCBANK pipeline
failure "cannot reindex on an axis with duplicate labels")."""

import pandas as pd

from app.reports.valuation_run import known_series
from app.valuation.bands import multiple_series


def ts(*days: str) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(list(days))


def test_same_day_keeps_the_latest_period() -> None:
    # FY25 (comparative) and FY26 filed together on 18 Apr 2026; FY24 known in 2024
    s = known_series([10.0, 11.0, 12.0], ts("2024-03-31", "2025-03-31", "2026-03-31"),
                     ts("2024-05-20", "2026-04-18", "2026-04-18"))  # fmt: skip
    assert s.to_dict() == {pd.Timestamp("2024-05-20"): 10.0, pd.Timestamp("2026-04-18"): 12.0}


def test_an_older_period_known_later_never_replaces_a_newer_one() -> None:
    s = known_series([12.0, 11.0], ts("2026-03-31", "2025-03-31"),
                     ts("2026-04-18", "2026-06-01"))  # fmt: skip
    assert s.to_dict() == {pd.Timestamp("2026-04-18"): 12.0}
    assert known_series([], ts(), ts()).empty


def test_step_tolerates_duplicate_dates() -> None:
    price = pd.Series([100.0, 110.0], index=ts("2026-04-20", "2026-04-21"))
    den = pd.Series([5.0, 10.0], index=ts("2026-04-18", "2026-04-18"))
    assert multiple_series(price, den).tolist() == [10.0, 11.0]
