"""Data depth levels (app/fundamentals/depth.py, SPEC §7.3)."""

import pandas as pd
import pytest

from app.core.config import DataDepthConfig, get_config
from app.fundamentals.depth import data_depth, pl_years


def frame(years: int, *, empty_pl: int = 0) -> pd.DataFrame:
    ends = pd.DatetimeIndex([f"{2026 - i}-03-31" for i in range(years)][::-1])
    rev = [None] * empty_pl + [100.0] * (years - empty_pl)
    return pd.DataFrame({"fiscal_year": ends.year, "revenue": rev, "pat": rev}, index=ends)


def test_config() -> None:
    cfg = get_config().scoring.data_depth
    assert (cfg.provisional_min_years, cfg.full_min_years) == (3, 8)
    with pytest.raises(ValueError, match="provisional_min_years"):
        DataDepthConfig(provisional_min_years=8, full_min_years=8, valuation_steps_down=1)


@pytest.mark.parametrize(("years", "level"), [
    (0, "technical_only"), (2, "technical_only"), (3, "provisional"), (7, "provisional"),
    (8, "full"), (12, "full"),
])  # fmt: skip
def test_levels(years: int, level: str) -> None:
    d = data_depth(frame(years), get_config().scoring.data_depth)
    assert (d.level, d.pl_years) == (level, years)
    assert d.full == (level == "full")


def test_years_without_a_pl_do_not_count() -> None:
    # 9 rows, 2 of them with no revenue / PAT (a balance sheet only): 7 → provisional
    f = frame(9, empty_pl=2)
    assert pl_years(f) == 7
    d = data_depth(f, get_config().scoring.data_depth)
    assert d.level == "provisional"
    assert d.reason == ("only 7 fiscal years of P&L (full needs 8): grade provisional, reduced "
                        "confidence")  # fmt: skip
    assert pl_years(pd.DataFrame()) == 0
