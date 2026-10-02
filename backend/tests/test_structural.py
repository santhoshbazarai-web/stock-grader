"""Structural breaks (app/fundamentals/structural.py, SPEC §4): HDFCBANK's 1 Jul 2023 merger
with HDFC Ltd on the Indian API fixtures. FY24 net profit grew 42% because HDFC Ltd's
profit was added; earnings per year-end share grew 2%."""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.core.config import StructuralEvent, get_config
from app.core.settings import config_dir_from_env
from app.data.indianapi_parse import SHARES_PER_CRORE, map_statements
from app.fundamentals.indianapi_map import get_indianapi_map
from app.fundamentals.metrics import Metric
from app.fundamentals.structural import adjust_growth, break_year, crossing, yoy_growth

FIX = Path(__file__).parent / "fixtures" / "indianapi"
MERGER = StructuralEvent(date=date(2023, 7, 1), kind="merger", description="HDFC Ltd merged")
CR = 1e7


@pytest.fixture(scope="module")
def hdfc() -> tuple[pd.DataFrame, dict[int, float]]:
    """Canonical annual frame (₹ crore) and year-end shares (crore) from the fixtures."""
    stock = json.loads((FIX / "hdfcbank_stock.json").read_text())
    hists = {
        s: json.loads((FIX / f"hdfcbank_{f}.json").read_text())
        for s, f in (("yoy_results", "pl"), ("balancesheet", "bs"), ("cashflow", "cf"))
    }
    m = map_statements(stock, hists, get_indianapi_map(config_dir_from_env()))  # fmt: skip
    rows, shares = [], {}
    for fy in range(2016, 2027):
        end = date(fy, 3, 31)
        pat, eps = m.get("pat", end, "year"), m.get("eps_diluted", end, "year")
        rows.append({
            "period_end": pd.Timestamp(end), "fiscal_year": fy,
            "revenue": (m.get("revenue", end, "year") or float("nan")) / CR,
            "pat": (pat or float("nan")) / CR,
            "eps_diluted": eps,
            "shares_diluted_cr": pat / CR / eps if pat and eps else None,
            "total_equity": (m.get("total_equity", end, "instant") or float("nan")) / CR,
        })  # fmt: skip
        so = m.get("shares_outstanding", end, "instant")
        if so:
            shares[fy] = so / SHARES_PER_CRORE
    return pd.DataFrame(rows).set_index("period_end"), shares


def test_the_merger_is_in_the_config() -> None:
    [e] = get_config().structural_events.for_symbol("hdfcbank")
    assert (e.date, e.kind) == (date(2023, 7, 1), "merger")
    assert "₹558 cr → ₹760 cr" in e.description
    assert break_year(e) == 2024
    assert crossing([e], 2023, 2024) is e and crossing([e], 2024, 2026) is None
    assert crossing([e], 2019, 2024) is e and crossing([e], 2016, 2023) is None


HdfcData = tuple[pd.DataFrame, dict[int, float]]


def test_fy24_profit_up_42_percent_but_eps_up_2(hdfc: HdfcData) -> None:
    annual, shares = hdfc
    # the vendor's own history: Net Profit 46,149 → 65,446 cr (+42%); EPS 41.22 → 42.16 (+2%)
    hist = json.loads((FIX / "hdfcbank_pl.json").read_text())
    np_ = hist["Net Profit"]
    assert np_["Mar 2024"] / np_["Mar 2023"] - 1 == pytest.approx(0.418, abs=0.002)
    eps_hist = hist["EPS in Rs"]
    assert eps_hist["Mar 2024"] / eps_hist["Mar 2023"] - 1 == pytest.approx(0.023, abs=0.002)
    # ours, without the break: aggregate owners' PAT 45,997 → 64,062 cr (+39%)
    agg, note = yoy_growth(annual, "pat", 2024, [])
    assert agg == pytest.approx(0.393, abs=0.002) and note is None
    # with the break: PAT per year-end share 45,997 / 1,115.95 → 64,062 / 1,519.38 (+2.3%)
    ps, note = yoy_growth(annual, "pat", 2024, [MERGER], shares_year_end=shares)
    assert ps == pytest.approx(eps_hist["Mar 2024"] / eps_hist["Mar 2023"] - 1, abs=0.002)
    assert note == (
        "FY2024 pat growth per share (year-end shares; weighted where not reported): merger"
    )
    # revenue: +66% in aggregate, +22% per share
    rev, _ = yoy_growth(annual, "revenue", 2024, [])
    rev_ps, _ = yoy_growth(annual, "revenue", 2024, [MERGER], shares_year_end=shares)
    assert rev == pytest.approx(0.661, abs=0.002) and rev_ps == pytest.approx(0.220, abs=0.003)
    # a year after the break is compared normally
    assert yoy_growth(annual, "pat", 2025, [MERGER], shares_year_end=shares)[1] is None


def test_cagr_windows_across_the_break_are_per_share(
    hdfc: tuple[pd.DataFrame, dict[int, float]],
) -> None:
    annual, shares = hdfc
    metrics = {f"{p}_cagr_{k}y": Metric(9.9) for p in ("sales", "ebitda", "eps") for k in (1, 3)}
    out, notes = adjust_growth(metrics, annual, [MERGER], cagr_years=[1, 3],
                               shares_year_end=shares, year=2026)  # fmt: skip
    assert out["sales_cagr_1y"] == Metric(9.9)  # FY25 → FY26: after the break, unchanged
    # FY23 → FY26 crosses FY24: sales per year-end share 1,530.1 → 2,264.7 (3 years)
    s23 = annual.loc[pd.Timestamp("2023-03-31"), "revenue"] / shares[2023]
    s26 = annual.loc[pd.Timestamp("2026-03-31"), "revenue"] / shares[2026]
    assert out["sales_cagr_3y"].value == pytest.approx((s26 / s23) ** (1 / 3) - 1, rel=1e-9)
    assert out["sales_cagr_3y"].reason == (
        "per share (year-end shares; weighted where not reported)"
    )
    assert out["eps_cagr_3y"].value is not None and out["eps_cagr_3y"].value < 0.08
    assert out["ebitda_cagr_3y"].value is None  # the bank has no EBITDA: still None, reason
    assert notes == [
        "Structural break FY2024 (merger): HDFC Ltd merged. Growth across it is per "
        "share (year-end shares; weighted where not reported): sales_cagr_3y, "
        "ebitda_cagr_3y, eps_cagr_3y"
    ]


def test_without_year_end_shares_the_weighted_count_is_used(
    hdfc: tuple[pd.DataFrame, dict[int, float]],
) -> None:
    annual, _ = hdfc
    ps, note = yoy_growth(annual, "pat", 2024, [MERGER])
    # weighted-average diluted shares straddle the July issue: EPS 41.13 → 45.00 (+9.4%)
    assert ps == pytest.approx(45.00 / 41.13 - 1, abs=0.002)
    assert note == "FY2024 pat growth per share (weighted-average diluted shares): merger"


def test_no_events_change_nothing(hdfc: tuple[pd.DataFrame, dict[int, float]]) -> None:
    annual, shares = hdfc
    metrics = {"sales_cagr_3y": Metric(0.1)}
    assert adjust_growth(metrics, annual, [], cagr_years=[3], shares_year_end=shares) == (
        metrics, [])  # fmt: skip


def test_year_end_shares_missing_for_old_years_use_the_weighted_count(
    hdfc: HdfcData,
) -> None:
    annual, shares = hdfc
    recent = {fy: v for fy, v in shares.items() if fy >= 2020}  # older years not reported
    metrics = {"sales_cagr_10y": Metric(9.9)}
    out, _ = adjust_growth(metrics, annual, [MERGER], cagr_years=[10],
                               shares_year_end=recent, year=2026)  # fmt: skip
    assert out["sales_cagr_10y"].value is not None
    assert out["sales_cagr_10y"].reason == (
        "per share (year-end shares; weighted where not reported)"
    )


def test_uncomputed_per_share_cagr_says_which_years_are_missing(hdfc: HdfcData) -> None:
    annual, shares = hdfc
    recent = annual[annual["fiscal_year"] >= 2019]  # FY2016 not on file
    out, _ = adjust_growth({"sales_cagr_10y": Metric(9.9)}, recent, [MERGER], cagr_years=[10],
                           shares_year_end=shares, year=2026)  # fmt: skip
    m = out["sales_cagr_10y"]
    assert m.value is None
    assert m.reason == "per-share sales_ps missing for FY2016 (on file: FY2019-FY2026)"
