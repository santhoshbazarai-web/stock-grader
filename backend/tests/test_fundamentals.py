"""Unit tests for fundamentals/ against the hand-computed company in fundamentals_fixture.py.
Tolerance: 0.5% relative for ratios (AGENTS.md)."""

import math

import numpy as np
import pandas as pd
import pytest

from app.core.config import load_config
from app.fundamentals.banking import bank_metrics, bank_summary
from app.fundamentals.forensic import altman_z2, beneish, piotroski
from app.fundamentals.metrics import annual_metrics, by_year, cagr, summary_metrics, ttm
from tests.conftest import REPO_CONFIG_DIR
from tests.fundamentals_fixture import annual_frame, bank_annual_frame

CFG = load_config(REPO_CONFIG_DIR)
FUND = CFG.scoring.fundamentals
FORENSIC = CFG.scoring.forensic
TAX = CFG.valuation.tax_rate_default


def approx(x: float) -> object:
    return pytest.approx(x, rel=0.005)


@pytest.fixture
def m24() -> pd.Series:
    return annual_metrics(annual_frame(), tax_rate_fallback=TAX, days=365).loc[2024]


# ───────────────────────── annual metrics (FY2024) ─────────────────────────


def test_roce_uses_average_capital_employed(m24: pd.Series) -> None:
    # CE = TA - CL: FY23 1000-200 = 800, FY24 1200-200 = 1000 → avg 900; EBIT 270 / 900
    assert m24["roce"] == approx(0.30)


def test_roe_uses_average_equity(m24: pd.Series) -> None:
    assert m24["roe"] == approx(182 / 650)  # equity avg (600 + 700) / 2


def test_roic(m24: pd.Series) -> None:
    # t = 78/260 = 0.30 → NOPAT = 270 x 0.7 = 189
    # IC = equity + debt - cash - non-op inv: FY23 600+100-50-50 = 600, FY24 700+100-60-40 = 700
    assert m24["roic"] == approx(189 / 650)
    assert m24["roic_tax_rate_source"] == "effective"


def test_margins_cash_conversion_and_fcf(m24: pd.Series) -> None:
    assert m24["opm"] == approx(330 / 1500)
    assert m24["cfo_to_ebitda"] == approx(264 / 330)
    assert m24["cfo_to_pat"] == approx(264 / 182)
    assert m24["capex"] == approx(120 - 20)
    assert m24["fcf"] == approx(264 - 100)
    assert m24["capex_intensity"] == approx(100 / 264)
    assert m24["other_income_share"] == approx(20 / 260)


def test_leverage(m24: pd.Series) -> None:
    assert m24["debt_to_equity"] == approx(100 / 700)
    assert m24["net_debt_to_ebitda"] == approx((100 - 60) / 330)
    assert m24["interest_coverage"] == approx(270 / 10)


def test_working_capital_days(m24: pd.Series) -> None:
    assert m24["debtor_days"] == approx(150 / 1500 * 365)  # 36.5
    assert m24["inventory_days"] == approx(100 / 730 * 365)  # 50
    assert m24["payable_days"] == approx(73 / 730 * 365)  # 36.5
    assert m24["ccc_days"] == approx(36.5 + 50 - 36.5)


def test_accruals_ratio(m24: pd.Series) -> None:
    assert m24["accruals_ratio"] == approx((182 - 264) / 1100)  # avg TA (1000 + 1200) / 2


def test_first_year_has_no_opening_balance() -> None:
    m = annual_metrics(annual_frame(), tax_rate_fallback=TAX, days=365)
    assert math.isnan(m.loc[2019, "roce"]) and math.isnan(m.loc[2019, "roe"])
    assert m.loc[2019, "opm"] == approx(0.2)  # single-year metrics still work


def test_missing_year_is_not_bridged() -> None:
    df = annual_frame().drop(pd.Timestamp("2022-03-31"))
    m = annual_metrics(df, tax_rate_fallback=TAX, days=365)
    assert 2022 in m.index and math.isnan(m.loc[2022, "opm"])
    assert math.isnan(m.loc[2023, "roce"])  # opening (FY22) balance unknown


def test_non_positive_denominators() -> None:
    df = annual_frame({2024: {"total_equity": -50.0, "interest": 0.0, "pbt": -10.0}})
    m = annual_metrics(df, tax_rate_fallback=TAX, days=365).loc[2024]
    assert math.isnan(m["debt_to_equity"])
    assert m["interest_coverage"] == np.inf  # debt-free, positive EBIT
    assert math.isnan(m["other_income_share"])
    assert m["roic_tax_rate_source"] == "fallback"  # loss year: effective rate undefined


def test_missing_values_stay_missing() -> None:
    m = annual_metrics(
        annual_frame({2024: {"payables": None}}), tax_rate_fallback=TAX, days=365
    ).loc[2024]
    assert math.isnan(m["payable_days"]) and math.isnan(m["ccc_days"])
    assert m["debtor_days"] == approx(36.5)


# ───────────────────────── summary (windows, CAGR, TTM) ─────────────────────────


def summary(df: pd.DataFrame | None = None, quarterly: pd.DataFrame | None = None):  # type: ignore[no-untyped-def]
    return summary_metrics(
        annual_frame() if df is None else df, quarterly, FUND, tax_rate_fallback=TAX
    )


def test_five_year_averages() -> None:
    s = summary()
    assert s["roce_5y_avg"].value == approx((0.2 * 4 + 0.3) / 5)  # FY20-23 160/800 = 0.2
    assert s["roe_5y_avg"].value == approx((105 / 600 * 4 + 182 / 650) / 5)
    assert s["opm_5y_avg"].value == approx((0.2 * 4 + 0.22) / 5)


def test_cumulative_ratios() -> None:
    s = summary()
    assert s["cfo_to_ebitda_5y"].value == approx((160 * 4 + 264) / (200 * 4 + 330))  # 0.8
    assert s["cfo_to_pat_5y"].value == approx(904 / (105 * 4 + 182))
    assert s["fcf_conversion_5y"].value == approx((80 * 4 + 164) / 602)
    assert s["capex_intensity_5y"].value == approx((80 * 4 + 100) / 904)


def test_cagr_and_dilution() -> None:
    s = summary()
    assert s["sales_cagr_3y"].value == approx(1.5 ** (1 / 3) - 1)
    assert s["sales_cagr_5y"].value == approx(1.5**0.2 - 1)
    assert s["eps_cagr_5y"].value == approx((17.5 / 10.5) ** 0.2 - 1)
    assert s["dilution_5y"].value == approx(1.05**0.2 - 1)
    ten = s["sales_cagr_10y"]
    assert ten.value is None and "FY2014" in (ten.reason or "")


def test_cagr_undefined_for_non_positive_ends() -> None:
    assert cagr(100.0, -5.0, 3) is None and cagr(-1.0, 5.0, 3) is None
    assert cagr(121.0, 100.0, 2) == approx(0.1)


def test_window_with_missing_input_names_it() -> None:
    s = summary(annual_frame({2021: {"current_liabilities": None}}))
    roce = s["roce_5y_avg"]
    assert roce.value is None
    assert "current_liabilities (FY2021)" in (roce.reason or "")
    assert s["roce_latest"].value == approx(0.30)  # latest year unaffected


def _quarters(values: list[float], ends: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {"revenue": values, "ebitda": [v * 0.2 for v in values], "pat": [10.0] * len(values),
         "eps_diluted": [1.0] * len(values)},
        index=pd.DatetimeIndex(pd.to_datetime(ends)),
    )  # fmt: skip


def test_ttm() -> None:
    q = _quarters([100, 110, 120, 130], ["2023-06-30", "2023-09-30", "2023-12-31", "2024-03-31"])
    t = ttm(q)
    assert t["revenue"] == 460 and t["eps_diluted"] == 4 and t["ebit"] is None
    s = summary(quarterly=q)
    assert s["revenue_ttm"].value == 460 and s["opm_ttm"].value == approx(0.2)


def test_ttm_needs_four_consecutive_quarters() -> None:
    gap = _quarters([1, 1, 1, 1], ["2023-03-31", "2023-09-30", "2023-12-31", "2024-03-31"])
    assert ttm(gap)["revenue"] is None
    assert ttm(_quarters([1, 1, 1], ["2023-09-30", "2023-12-31", "2024-03-31"]))["revenue"] is None


def test_by_year_prefers_fiscal_year_column() -> None:
    df = annual_frame()
    df.index = pd.DatetimeIndex([pd.Timestamp(f"{y}-12-31") for y in range(2018, 2024)])
    assert list(by_year(df).index) == list(range(2019, 2025))


# ───────────────────────── forensic ─────────────────────────


def test_piotroski() -> None:
    # FY24 vs FY23: ROA 182/1000 > 0; CFO > 0; ROA up vs 105/1000; CFO 264 > PAT 182;
    # leverage 100/1200 < 100/1000; current ratio 500/200 > 400/200; shares 10.5 > 10 (FAIL);
    # gross margin 770/1500 > 0.5; asset turnover 1500/1000 > 1000/1000 → 8
    r = piotroski(annual_frame())
    assert r.value == 8
    assert r.components["no_dilution"] is False
    assert sum(bool(v) for v in r.components.values()) == 8


def test_piotroski_incomplete() -> None:
    r = piotroski(annual_frame({2024: {"current_assets": None}}))
    assert r.value is None and "current_assets (FY2024)" in r.missing
    assert r.components["current_ratio_rising"] is None


def test_beneish_hand_computed() -> None:
    dsri = (150 / 1500) / (100 / 1000)
    gmi = 0.5 / (770 / 1500)
    aqi = (1 - (500 + 500 + 40) / 1200) / (1 - (400 + 400 + 50) / 1000)
    sgi = 1.5
    depi = (50 / 450) / (80 / 580)
    sgai = (140 / 1500) / (100 / 1000)
    tata = (182 - 264) / 1200
    lvgi = (300 / 1200) / (300 / 1000)
    m = (-4.84 + 0.92 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi + 0.115 * depi
         - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)  # fmt: skip
    r = beneish(annual_frame(), FORENSIC)
    assert r.value == approx(m) and r.value == approx(-2.3687)
    assert r.components["aqi"] == approx(aqi)
    assert r.flag is None  # -2.37 <= -2.22


def test_beneish_flags_above_threshold() -> None:
    # Receivables ballooning: DSRI = (450/1500)/(0.1) = 3 → M = -2.3687 + 0.92 x 2 = -0.53
    r = beneish(annual_frame({2024: {"receivables": 450.0}}), FORENSIC)
    assert r.value == approx(-2.3687 + 0.92 * 2)
    assert r.flag == "manipulation risk"


def test_altman_z2() -> None:
    # X1 = (500-200)/1200, X2 = 600/1200, X3 = 270/1200, X4 = 700/(1200-700)
    z = 6.56 * 0.25 + 3.26 * 0.5 + 6.72 * 0.225 + 1.05 * 1.4  # 6.252
    r = altman_z2(annual_frame(), FORENSIC)
    assert r.value == approx(z) and r.flag == "safe"


def test_altman_distress_zone_and_financials() -> None:
    r = altman_z2(annual_frame({2024: {"ebit": -100.0, "retained_earnings": -300.0}}), FORENSIC)
    # 1.64 + 3.26 x -0.25 + 6.72 x (-100/1200) + 1.47 = 1.735 → grey
    assert r.value == approx(1.64 - 0.815 - 0.56 + 1.47) and r.flag == "grey"
    assert altman_z2(annual_frame(), FORENSIC, is_financial=True).value is None


# ───────────────────────── banking ─────────────────────────


def test_bank_metrics_hand_computed() -> None:
    b = bank_metrics(bank_annual_frame()).loc[2024]
    assert b["nim_pct"] == approx(500 / 10500 * 100)  # NII 1200-700; IEA avg (10000+11000)/2
    assert b["casa_pct"] == approx(42.0)
    assert b["gnpa_pct"] == approx(2.5)
    assert b["nnpa_pct"] == approx(0.8)
    assert b["pcr_pct"] == approx((230 - 72) / 230 * 100)
    assert b["credit_cost_pct"] == approx(85 / 8500 * 100)
    assert b["car_pct"] == approx(18.1)
    assert b["cost_to_income_pct"] == approx(270 / 600 * 100)
    assert b["roa_pct"] == approx(180 / 11500 * 100)
    assert b["roe_pct"] == approx(180 / 1100 * 100)
    assert b["loan_growth_pct"] == approx(12.5)


def test_bank_summary_reports_gaps() -> None:
    df = bank_annual_frame()
    df.at[pd.Timestamp("2024-03-31"), "extra"] = {**df.iloc[1]["extra"], "casa_deposits": None}
    s = bank_summary(df)
    assert s["casa_pct"].value is None and "casa_deposits (FY2024)" in (s["casa_pct"].reason or "")
    assert s["gnpa_pct"].value == approx(2.5)


def test_reason_names_only_the_years_a_metric_needs() -> None:
    s = summary(annual_frame({2023: {"payables": None, "current_liabilities": None}}))
    assert s["ccc_days"].value is not None  # single-year metric: FY2023 irrelevant
    roce = s["roce_latest"]
    assert roce.value is None and roce.reason == "missing current_liabilities (FY2023)"
