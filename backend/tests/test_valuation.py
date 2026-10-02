"""Valuation engine (SPEC §5) — worked examples with hand-computed numbers.

Tolerance (AGENTS.md): 2% for valuations, 0.5% for ratios. Most checks below are tighter
because the hand computations are exact.
"""

import math
import statistics
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from app.core.config import SectorConfig, load_config
from app.valuation.bands import band, band_prices, multiple_series, regression_band
from app.valuation.blend import Confidence, Zone, blend, classify_zone, fair_value
from app.valuation.dcf import (
    DcfInputs,
    base_inputs,
    beta_estimate,
    blume_beta,
    cost_of_equity,
    cost_of_equity_explained,
    default_g1,
    growth_path,
    run_dcf,
    run_scenarios,
    sensitivity_grid,
    size_premium,
    wacc,
)
from app.valuation.epv import epv, graham_number, normalised_ebit
from app.valuation.relative import relative_value
from app.valuation.reverse_dcf import reverse_dcf
from app.valuation.sector_models import (
    insurance_appraisal,
    justified_pb,
    nav_value,
    normalised_ev_ebitda,
    p_ev_value,
    residual_income,
    sotp_value,
)
from tests.conftest import REPO_CONFIG_DIR
from tests.fundamentals_fixture import annual_frame

CFG = load_config(REPO_CONFIG_DIR)
V = CFG.valuation
SECTORS = CFG.sectors.root


def val(x: float) -> object:  # valuations
    return pytest.approx(x, rel=1e-4)


# ───────────────────────── DCF worked example ─────────────────────────
#
# Revenue 1,000 Cr, EBIT margin 20%, tax 25%, D&A = capex = 5% of revenue, no working capital
# → FCFF_t = revenue_t x 0.20 x 0.75 = 0.15 x revenue_t.
# g1 = 10% for years 1-5, fading 9, 8, 7, 6, 5% in years 6-10; WACC 12%; g_T 5%.
#
#   t   g     revenue     FCFF      PV @12%
#   1  10%   1,100.00   165.0000   147.3214
#   2  10%   1,210.00   181.5000   144.6907
#   3  10%   1,331.00   199.6500   142.1069
#   4  10%   1,464.10   219.6150   139.5693
#   5  10%   1,610.51   241.5765   137.0770
#   6   9%   1,755.46   263.3184   133.4053
#   7   8%   1,895.89   284.3839   128.6408
#   8   7%   2,028.60   304.2907   122.8979
#   9   6%   2,150.32   322.5482   116.3141
#  10   5%   2,257.84   338.6756   109.0445
#                         sum PV   1,321.0679
#
# FCFF_11 = 2,257.8372 x 1.05 x 0.15 = 355.6094;  TV = 355.6094 / (0.12 - 0.05) = 5,080.1337
# PV(TV) = 5,080.1337 / 1.12^10 = 1,635.6671;  EV = 2,956.7350 (TV = 55.3% of EV)
# Equity = EV - net debt 100 + non-op investments 50 = 2,906.7350;  / 10 Cr shares = 290.6735

BASE = DcfInputs(
    revenue=1000.0,
    ebit_margin=0.20,
    tax_rate=0.25,
    da_pct=0.05,
    capex_pct=0.05,
    nwc_pct=0.0,
    g1=0.10,
    g_terminal=0.05,
    wacc=0.12,
    net_debt=100.0,
    minority_interest=0.0,
    non_op_investments=50.0,
    shares_cr=10.0,
)


def test_growth_path_fades_linearly() -> None:
    assert growth_path(0.10, 0.05, 5, 5) == pytest.approx(
        [0.10] * 5 + [0.09, 0.08, 0.07, 0.06, 0.05]
    )


def test_dcf_worked_example() -> None:
    r = run_dcf(BASE)
    assert r.fcff[0] == val(165.0) and r.fcff[-1] == val(338.6756)
    assert r.pv_explicit == val(1321.0679)
    assert r.pv_terminal == val(1635.6671)
    assert r.enterprise_value == val(2956.7350)
    assert r.equity_value == val(2906.7350)
    assert r.value_per_share == val(290.6735)
    assert r.terminal_share == pytest.approx(0.5532, abs=1e-4)


def test_working_capital_investment_reduces_fcff() -> None:
    # NWC 10% of revenue: year 1 ΔNWC = 0.10 x (1,100 - 1,000) = 10 → FCFF_1 = 165 - 10 = 155
    r = run_dcf(replace(BASE, nwc_pct=0.10))
    assert r.fcff[0] == val(155.0)
    assert r.value_per_share is not None and r.value_per_share < 290.6735


def test_dcf_undefined_when_wacc_not_above_terminal_growth() -> None:
    r = run_dcf(replace(BASE, wacc=0.05))
    assert r.value_per_share is None and "WACC" in r.reasons[0]


def test_scenarios_shift_growth_margin_and_wacc() -> None:
    # Config deltas: bear g1 -5%, margin -2%, WACC +1%; bull g1 +4%, margin +1.5%, WACC -0.5%
    # Same projection by hand → bear 172.1875 (g1 5%, 18%, 13%), bull 422.0344 (14%, 21.5%, 11.5%)
    s = run_scenarios(BASE, V)
    assert s["base"].value_per_share == val(290.6735)
    assert s["bear"].value_per_share == val(172.1875)
    assert s["bull"].value_per_share == val(422.0344)


def test_sensitivity_grid() -> None:
    # WACC 12% ± 2% in 0.5% steps (9 rows) x g_T 4-7% (4 columns). Hand-computed corners:
    # (10%, 4%) → 359.5144;  (14%, 7%) → 264.5533;  centre (12%, 5%) = base 290.6735
    g = sensitivity_grid(BASE, V)
    assert g.waccs == pytest.approx([0.10 + 0.005 * k for k in range(9)])
    assert g.terminal_growths == [0.04, 0.05, 0.06, 0.07]
    assert g.values[0][0] == val(359.5144)
    assert g.values[-1][-1] == val(264.5533)
    assert g.values[4][1] == val(290.6735)
    low = sensitivity_grid(replace(BASE, wacc=0.08), V)
    assert low.values[0][2] is None  # WACC 6% <= g_T 6%


# ───────────────────────── cost of capital ─────────────────────────


def test_wacc_worked_example() -> None:
    # Ke = 6.5% + 1.1 x 7% + size premium 0 (mcap 30,000 Cr) = 14.2%
    # Kd = 180 / 1,800 x (1 - 25%) = 7.5%;  weights 30,000/32,000 and 2,000/32,000
    # WACC = 0.9375 x 14.2% + 0.0625 x 7.5% = 13.78125%
    ke = cost_of_equity(1.1, 30000, V)
    assert ke == pytest.approx(0.142)
    w = wacc(
        ke=ke, market_cap_cr=30000, debt_cr=2000, interest_cr=180, avg_debt_cr=1800, tax_rate=0.25
    )
    assert w.kd_after_tax == pytest.approx(0.075)
    assert w.wacc == pytest.approx(0.1378125)


def test_size_premium_tiers() -> None:
    assert (size_premium(3000, V), size_premium(15000, V), size_premium(90000, V)) == (
        0.02,
        0.01,
        0.0,
    )


def test_wacc_debt_free_and_unknown_cost_of_debt() -> None:
    assert (
        wacc(
            ke=0.14,
            market_cap_cr=1000,
            debt_cr=0,
            interest_cr=None,
            avg_debt_cr=None,
            tax_rate=0.25,
        ).wacc
        == 0.14
    )
    with pytest.raises(ValueError, match="cost of debt"):
        wacc(
            ke=0.14, market_cap_cr=1000, debt_cr=50, interest_cr=None, avg_debt_cr=40, tax_rate=0.25
        )


def _weekly_pair(raw_beta: float) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(3)
    fridays = pd.date_range("2022-01-07", periods=110, freq="W-FRI")
    r_index = rng.normal(0.002, 0.02, len(fridays))
    index = pd.Series(100 * np.cumprod(1 + r_index), index=fridays)
    stock = pd.Series(50 * np.cumprod(1 + raw_beta * r_index), index=fridays)
    return stock, index


def test_blume_beta() -> None:
    # stock weekly return = 1.5 x index return exactly → raw beta 1.5
    # Blume: 0.67 x 1.5 + 0.33 = 1.335
    stock, index = _weekly_pair(1.5)
    assert blume_beta(stock, index, V) == pytest.approx(1.335, rel=1e-6)
    # raw 3.0 → 2.34, clamped to the configured cap 1.8
    stock, index = _weekly_pair(3.0)
    assert blume_beta(stock, index, V) == pytest.approx(V.beta.cap)


def test_beta_estimate_reports_the_clamp() -> None:
    # raw 0.3 → Blume 0.67 x 0.3 + 0.33 = 0.531, below the floor 0.6: clamped, and said so
    est = beta_estimate(*_weekly_pair(0.3), V)
    assert est is not None and est.clamped == "floor"
    assert est.value == V.beta.floor and est.unclamped == pytest.approx(0.531, rel=1e-6)
    top = beta_estimate(*_weekly_pair(3.0), V)
    assert top is not None and top.clamped == "cap" and top.unclamped == pytest.approx(2.34)
    mid = beta_estimate(*_weekly_pair(1.5), V)
    assert mid is not None and mid.clamped is None


def test_cost_of_equity_build_up() -> None:
    # Rf 6.5% + 0.8 x 7% + size premium (mcap 3,000 Cr ≤ 5,000 → 2%) = 14.1%
    ke, text = cost_of_equity_explained(0.8, 3000, V)
    assert ke == pytest.approx(0.141)
    assert text == "Ke 14.10% = Rf 6.50% + beta 0.80 x ERP 7.00% + size premium 2.00%"
    assert cost_of_equity(0.8, 3000, V) == pytest.approx(ke)
    big, _ = cost_of_equity_explained(1.0, 1_000_000, V)
    assert big == pytest.approx(0.135)  # 6.5% + 7%, no size premium


def test_blume_beta_needs_enough_history() -> None:
    stock, index = _weekly_pair(1.0)
    assert blume_beta(stock.iloc[:20], index.iloc[:20], V) is None


# ───────────────────────── inputs from the financials ─────────────────────────


def test_base_inputs_from_canonical_financials() -> None:
    # tests/fundamentals_fixture.py, window FY2022-FY2024 (margin_years = 3):
    #   EBIT margin  (0.16 + 0.16 + 270/1500 = 0.18) / 3            = 0.166667
    #   D&A %        (0.05 + 0.05 + 80/1500) / 3                     = 0.051111
    #   capex %      (0.08 + 0.08 + (120-20)/1500) / 3               = 0.075556
    #   tax          (45/150, 45/150, 78/260) = 30% each
    #   NWC %        (150 + 100 - 73) / 1500                         = 0.118
    #   net debt     100 - 60 = 40; non-op investments 40; shares 10.5 Cr
    b = base_inputs(annual_frame(), g1=0.12, wacc_rate=0.12, config=V)
    x = b.inputs
    assert x is not None
    assert (x.revenue, x.tax_rate, x.g_terminal) == (
        1500.0,
        pytest.approx(0.30),
        V.dcf.terminal_growth,
    )
    assert x.ebit_margin == pytest.approx(0.166667, rel=1e-5)
    assert x.da_pct == pytest.approx(0.051111, rel=1e-4)
    assert x.capex_pct == pytest.approx(0.075556, rel=1e-4)
    assert x.nwc_pct == pytest.approx(0.118)
    assert (x.net_debt, x.non_op_investments, x.shares_cr) == (40.0, 40.0, 10.5)
    assert b.assumed_nil == ["minority_interest_bs"]  # the fixture reports none
    assert any("data gap" in r for r in b.reasons)


def test_base_inputs_overrides_and_missing_data() -> None:
    b = base_inputs(
        annual_frame(),
        g1=0.12,
        wacc_rate=0.12,
        config=V,
        overrides={"g1": 0.2, "ebit_margin": 0.25},
    )
    assert b.inputs is not None and (b.inputs.g1, b.inputs.ebit_margin) == (0.2, 0.25)
    assert any("user overrides" in r for r in b.reasons)
    missing = base_inputs(
        annual_frame({2023: {"depreciation": None}}), g1=0.1, wacc_rate=0.12, config=V
    )
    assert missing.inputs is None and "depreciation (FY2023)" in missing.reasons[0]


def test_default_g1_is_capped() -> None:
    assert default_g1(0.30, SECTORS["default"], V) == V.dcf.g1_cap_by_default  # 25%
    capped = SECTORS["it_services"].model_copy(update={"g1_cap": 0.15})
    assert default_g1(0.30, capped, V) == 0.15
    assert default_g1(0.08, SECTORS["default"], V) == 0.08
    assert default_g1(None, SECTORS["default"], V) is None


# ───────────────────────── reverse DCF ─────────────────────────


def test_reverse_dcf_recovers_g1() -> None:
    # The worked example values the stock at 290.6735 with g1 = 10% → a price of 290.6735
    # must imply 10%. Historical 5-yr growth 8% → gap +2%.
    r = reverse_dcf(BASE, 290.6735, 0.08, V)
    assert r.implied_growth == pytest.approx(0.10, abs=1e-5)
    assert r.gap == pytest.approx(0.02, abs=1e-5)
    # Bear-scenario price with base WACC/margins implies less growth
    cheaper = run_dcf(replace(BASE, g1=0.04)).value_per_share
    assert reverse_dcf(BASE, cheaper, 0.08, V).implied_growth == pytest.approx(0.04, abs=1e-5)  # type: ignore[arg-type]


def test_reverse_dcf_outside_bracket() -> None:
    r = reverse_dcf(BASE, 1e6, None, V)
    assert r.implied_growth is None and "above" in r.reasons[0]
    assert reverse_dcf(BASE, 0.01, None, V).implied_growth is None


# ───────────────────────── bands ─────────────────────────


def _trading_days(start: str, end: str) -> pd.DatetimeIndex:
    return pd.bdate_range(start, end)


def test_pe_band_worked_example() -> None:
    # EPS (TTM) 10 known from 2019-01-01, 20 from 2021-06-01, -5 (loss) from 2023-01-02.
    # Price = PE x EPS with PE cycling 18, 20, 22 on profitable days → median 20.
    # Loss days are excluded; sigma is the sample std of the valid PEs.
    days = _trading_days("2019-01-01", "2023-06-30")
    pe_cycle = np.tile([18.0, 20.0, 22.0], len(days) // 3 + 1)[: len(days)]
    eps = pd.Series(
        [10.0, 20.0, -5.0], index=pd.to_datetime(["2019-01-01", "2021-06-01", "2023-01-02"])
    )
    eps_daily = eps.reindex(eps.index.union(days)).ffill().reindex(days)
    price = pd.Series(np.where(eps_daily > 0, pe_cycle * eps_daily, 50.0), index=days)

    b = band("pe", price, eps, lookback_years=5, min_observations=250)
    valid = pe_cycle[(eps_daily > 0).to_numpy()]
    assert b.observations == len(valid) and b.observations < len(days)
    assert b.median == pytest.approx(20.0)
    assert b.sigma == pytest.approx(statistics.stdev(valid.tolist()))
    assert b.levels[1] == pytest.approx(20.0 + b.sigma)  # type: ignore[operator]
    # Back to prices at current EPS 20: median → 400, -1 sigma → (20 - sigma) x 20
    prices = band_prices(b, 20.0)
    assert prices is not None and prices[0] == pytest.approx(400.0)
    assert prices[-1] == pytest.approx((20 - b.sigma) * 20)  # type: ignore[operator]
    assert band_prices(b, -5.0) is None  # no price when current earnings are negative


def test_ev_ebitda_band_uses_net_debt_offset() -> None:
    # EV/share = price + net debt/share (50); EBITDA/share 25; price 450 → (450+50)/25 = 20x
    days = _trading_days("2023-01-02", "2024-06-28")
    price = pd.Series(450.0, index=days)
    ebitda_ps = pd.Series([25.0], index=[days[0]])
    nd_ps = pd.Series([50.0], index=[days[0]])
    assert multiple_series(price, ebitda_ps, nd_ps).iloc[0] == pytest.approx(20.0)
    b = band("ev_ebitda", price, ebitda_ps, lookback_years=5, min_observations=250, offset=nd_ps)
    # sigma 0: every level is 20x → price = 20 x 30 - 50 = 550 at EBITDA/share 30
    assert band_prices(b, 30.0, current_offset=50.0) == {
        k: pytest.approx(550.0) for k in range(-2, 3)
    }


def test_band_needs_enough_observations() -> None:
    days = _trading_days("2024-01-01", "2024-03-29")
    b = band(
        "pb",
        pd.Series(100.0, index=days),
        pd.Series([50.0], index=[days[0]]),
        lookback_years=5,
        min_observations=250,
    )
    assert not b.ok and "only" in b.reasons[0]


# ───────────────────────── relative, EPV, Graham ─────────────────────────


def test_relative_worked_example() -> None:
    # Peers PE 18, 20, 22, 25, 30 → median 22. ROCE 30% vs peers 20%; growth 15% vs 12%.
    # adj = 22 x (1.5)^0.5 x (1.25)^0.5 = 22 x sqrt(1.875) = 30.1247x; x EPS 10 = 301.247
    r = relative_value(
        peer_multiples=[18, 20, 25, 30, 22],
        peer_quality=0.20,
        peer_growth=0.12,
        quality=0.30,
        growth=0.15,
        per_share_metric=10.0,
        config=V,
    )
    assert r.peer_median == 22.0
    assert r.adj_multiple == pytest.approx(22 * math.sqrt(1.875))
    assert r.value == pytest.approx(301.247, rel=1e-5)


def test_relative_needs_positive_bases() -> None:
    r = relative_value(
        peer_multiples=[20],
        peer_quality=0.2,
        peer_growth=0.1,
        quality=0.3,
        growth=-0.05,
        per_share_metric=10,
        config=V,
    )
    assert r.value is None and "growth" in r.reasons[0]
    assert (
        relative_value(
            peer_multiples=[-3],
            peer_quality=0.2,
            peer_growth=0.1,
            quality=0.3,
            growth=0.1,
            per_share_metric=10,
            config=V,
        ).value
        is None
    )


def test_epv_worked_example() -> None:
    # EBIT margins FY2020-24: 18%, 20%, 22%, 20%, 20% → mean 20% x latest revenue 1,500 = 300
    # EPV = 300 x 0.75 / 12% = 1,875 - net debt 100 + non-op 50 = 1,825 / 10 = 182.50
    df = annual_frame(
        {
            2020: {"ebit": 180.0},
            2021: {"ebit": 200.0},
            2022: {"ebit": 220.0},
            2023: {"ebit": 200.0},
            2024: {"ebit": 300.0},
        }
    )
    assert normalised_ebit(df, V.epv.normalise_years) == pytest.approx(300.0)
    r = epv(
        normalised_ebit_cr=300.0,
        tax_rate=0.25,
        wacc=0.12,
        net_debt=100,
        minority_interest=0,
        non_op_investments=50,
        shares_cr=10,
    )
    assert r.value == pytest.approx(182.5)


def test_graham_number() -> None:
    # sqrt(22.5 x EPS 20 x BVPS 100) = sqrt(45,000) = 212.132
    assert graham_number(20, 100, V).value == pytest.approx(212.1320, rel=1e-6)
    assert graham_number(-1, 100, V).value is None


# ───────────────────────── sector models ─────────────────────────


def test_justified_pb_worked_example() -> None:
    # (ROE 16% - g 10%) / (Ke 13.5% - g 10%) = 1.714286x x BVPS 200 = 342.857
    r = justified_pb(roe=0.16, ke=0.135, g=0.10, bvps=200)
    assert r.value == pytest.approx(342.8571, rel=1e-6)
    assert justified_pb(roe=0.08, ke=0.135, g=0.10, bvps=200).value is None  # ROE <= g


def test_two_stage_justified_pb_hdfcbank() -> None:
    from app.valuation.sector_models import justified_pb_grid, justified_pb_two_stage

    # HDFCBANK: ROE 13.7%, Ke 12.25%, BVPS 381, terminal g 5% (valuation.dcf.terminal_growth)
    # flat ROE and book growing at g: the single-stage formula, (13.7 - 5)/(12.25 - 5) = 1.20x
    flat = justified_pb_two_stage(roe=0.137, roe_norm=0.137, ke=0.1225, g=0.05, bvps=381,
                                  retention=None, years=5)  # fmt: skip
    assert flat.value == pytest.approx(381 * 0.087 / 0.0725, rel=1e-9)  # 457.20
    # one stage-1 year, ROE -> 15%, 72.8% retained:
    #   RI_1 = (15% - 12.25%) x 381 = 10.4775, PV 9.334076
    #   BV_1 = 381 x (1 + 15% x 72.8%) = 422.6052
    #   terminal = 2.75% x 422.6052 / 7.25% / 1.1225 = 142.804921
    #   value = 381 + 9.334076 + 142.804921 = 533.138997
    one = justified_pb_two_stage(roe=0.137, roe_norm=0.15, ke=0.1225, g=0.05, bvps=381,
                                 retention=0.728, years=1)  # fmt: skip
    assert one.value == pytest.approx(533.138997, rel=1e-8)
    # the old perpetual g = 10% (banks.long_run_growth) gave 1.64x = 626.5: far richer
    assert flat.value < justified_pb(roe=0.137, ke=0.1225, g=0.10, bvps=381).value  # type: ignore[operator]
    assert (
        justified_pb_two_stage(
            roe=0.137, roe_norm=0.137, ke=0.05, g=0.05, bvps=381, retention=None, years=5
        ).value
        is None
    )  # Ke <= g
    grid = justified_pb_grid(roe=0.137, roe_norm=0.137, ke=0.1225, g=0.05, bvps=381,
                             retention=None, years=5, roe_steps=[-0.02, 0.0, 0.02],
                             g_steps=[-0.01, 0.0, 0.01], ke_steps=[-0.01, 0.0, 0.01])  # fmt: skip
    assert len(grid) == 27
    base = next(c for c in grid if c["roe"] == 0.137 and c["g"] == 0.05 and c["ke"] == 0.1225)
    assert base["value"] == pytest.approx(457.2, rel=1e-9)
    hi_ke = next(c for c in grid if c["roe"] == 0.137 and c["g"] == 0.05
                 and c["ke"] == pytest.approx(0.1325))  # fmt: skip
    assert hi_ke["value"] < base["value"]  # type: ignore[operator]


def test_residual_income_worked_example() -> None:
    # BVPS 100, ROE 15%, Ke 12%, g 5%, payout 40% (BV grows 15% x 60% = 9%/yr), 3 years:
    #   t1  BV 100.00  RI 3.0000  PV 2.678571
    #   t2  BV 109.00  RI 3.2700  PV 2.606824
    #   t3  BV 118.81  RI 3.5643  PV 2.536998
    # BV_4 = 129.5029; terminal = 0.03 x 129.5029 / 0.07 / 1.12^3 = 39.504688
    # value = 100 + 7.822394 + 39.504688 = 147.327082
    r = residual_income(bvps=100, roe=0.15, ke=0.12, g=0.05, payout=0.4, years=3)
    assert r.value == pytest.approx(147.327082, rel=1e-7)


def test_insurance_nav_and_sotp() -> None:
    assert p_ev_value(400, 2.5, "band P/EV").value == 1000  # 2.5 x EV/share 400
    assert insurance_appraisal(400, 30, 15).value == 850  # 400 + 30 x 15
    assert nav_value(500, SECTORS["real_estate"].nav_discount or 0).value == 400  # 20% off
    # stakes 10,000 x (1 - 30%) + standalone 2,000 - net debt 1,000 = 8,000 / 10 Cr = 800
    assert (
        sotp_value(
            listed_holdings_value_cr=10000,
            standalone_value_cr=2000,
            holding_discount=0.30,
            net_debt=1000,
            shares_cr=10,
        ).value
        == 800
    )
    assert insurance_appraisal(400, None, 15).value is None


def test_cyclical_normalised_ev_ebitda() -> None:
    # EBITDA margins over 5 years 10, 25, 15, 30, 20% → median 20% x current sales 2,000
    # = 400 mid-cycle EBITDA x band median 6x = 2,400 EV - net debt 400 = 2,000 / 20 Cr = 100
    df = annual_frame()
    for year, margin in zip(range(2020, 2025), [0.10, 0.25, 0.15, 0.30, 0.20], strict=True):
        df.loc[pd.Timestamp(f"{year}-03-31"), ["revenue", "ebitda"]] = [2000.0, 2000 * margin]
    r = normalised_ev_ebitda(
        df,
        normalise_years=5,
        band_median_ev_ebitda=6.0,
        net_debt=400,
        minority_interest=0,
        non_op_investments=0,
        shares_cr=20,
    )
    assert r.value == pytest.approx(100.0)


def test_financial_sectors_have_no_dcf_weight() -> None:
    for name in ("banks", "nbfc", "insurance"):
        assert "dcf_base" not in (SECTORS[name].weights or {})


# ───────────────────────── blend ─────────────────────────
#
# Default sector weights: dcf_base 40%, band_pe 25%, band_ev_ebitda 15%, relative 20%.
# Method values 300, 250, 280, 270 → FV = 120 + 62.5 + 42 + 54 = 278.50
# Dispersion: mean 275, deviations ±25, ±5 → population sd sqrt(1300/4) = 18.028 → CV 6.6%
#   → high confidence (≤ 20%).
# Baseline = min(bear DCF 172.19, EPV 182.50, band -1 sigma 200) = 172.19
# Top band = max(bull DCF 422.03, band +1 sigma 380) = 422.03, capped at band +2 sigma 420
# Grade B → MoS 27.5%: discount below 278.50 x 0.725 = 201.9125; fair up to 278.50 x 1.10 = 306.35

METHODS = {"dcf_base": 300.0, "band_pe": 250.0, "band_ev_ebitda": 280.0, "relative": 270.0}
BAND_PRICES = {-2: 150.0, -1: 200.0, 0: 290.0, 1: 380.0, 2: 420.0}


def _blend(cmp: float, sector: SectorConfig | None = None, **kw: object):  # type: ignore[no-untyped-def]
    args = dict(
        method_values=METHODS,
        provisional_grade="B",
        bear_dcf=172.1875,
        bull_dcf=422.0344,
        epv=182.5,
        band_prices=BAND_PRICES,
    )
    args.update(kw)
    return blend(cmp=cmp, sector=sector or SECTORS["default"], config=V, **args)  # type: ignore[arg-type]


def test_blend_worked_example() -> None:
    v = _blend(250.0)
    assert v.fair_value == pytest.approx(278.5)
    assert v.baseline == pytest.approx(172.1875)
    assert v.top_band == pytest.approx(420.0)  # capped
    assert v.mos_pct == 0.275
    assert v.method_cv == pytest.approx(math.sqrt(325) / 275)
    assert v.confidence is Confidence.HIGH
    assert v.zone is Zone.FAIR
    assert any("baseline 172.2 from bear DCF" in r for r in v.reasons)


@pytest.mark.parametrize(
    ("cmp", "zone"),
    [
        (150.0, Zone.DEEP_DISCOUNT),  # < baseline 172.19
        (190.0, Zone.DISCOUNT),  # < 201.9125
        (201.9125, Zone.FAIR),  # boundary belongs to fair
        (306.35, Zone.FAIR),  # FV x 1.10 is still fair
        (350.0, Zone.PREMIUM),  # <= top band 420
        (420.0, Zone.PREMIUM),
        (450.0, Zone.EXTREME_PREMIUM),
    ],
)
def test_zones(cmp: float, zone: Zone) -> None:
    assert _blend(cmp).zone is zone


def test_missing_method_renormalises_weights() -> None:
    # band_ev_ebitda missing → (120 + 62.5 + 54) / 0.85 = 278.2353
    v = _blend(250.0, method_values={**METHODS, "band_ev_ebitda": None})
    assert v.fair_value == pytest.approx(236.5 / 0.85)
    line = next(ln for ln in v.methods if ln.name == "dcf_base")
    assert line.effective_weight == pytest.approx(0.40 / 0.85)
    assert any("renormalised" in r for r in v.reasons)


def test_too_little_weight_means_no_fair_value() -> None:
    # only relative (20%) available < 50% coverage → no FV, no zone
    v = _blend(250.0, method_values={"relative": 270.0})
    assert v.fair_value is None and v.zone is None
    assert v.confidence is Confidence.LOW


def test_asset_heavy_book_floor() -> None:
    # metals: asset_heavy → baseline = max(172.19, 0.8 x BVPS 300 = 240) = 240
    metals = SECTORS["metals"]
    methods = {"normalised_ev_ebitda": 300.0, "band_ev_ebitda": 280.0, "band_pb": 260.0}
    v = _blend(230.0, metals, method_values=methods, book_value_ps=300.0)
    assert v.baseline == pytest.approx(240.0)
    assert v.zone is Zone.DEEP_DISCOUNT
    assert not SECTORS["default"].asset_heavy
    assert _blend(230.0, book_value_ps=300.0).baseline == pytest.approx(172.1875)


def test_confidence_levels() -> None:
    wide = {"dcf_base": 400.0, "band_pe": 200.0, "band_ev_ebitda": 300.0, "relative": 300.0}
    # mean 300, deviations 100, -100, 0, 0 → sd sqrt(5000) = 70.71 → CV 23.6% → medium
    assert _blend(250.0, method_values=wide).confidence is Confidence.MEDIUM
    # 600, 150, 300, 250: mean 325, sd 167.9 → CV 51.7% → low (> 35%)
    very_wide = {"dcf_base": 600.0, "band_pe": 150.0, "band_ev_ebitda": 300.0, "relative": 250.0}
    assert _blend(250.0, method_values=very_wide).confidence is Confidence.LOW


def test_nav_sector_uses_single_model() -> None:
    fv, lines, _ = fair_value({}, SECTORS["real_estate"], V, single_model_value=400.0)
    assert fv == 400.0 and lines[0].name == "nav"


def test_classify_zone_without_baseline_or_top() -> None:
    # No baseline: nothing can be Deep Discount; no top band: nothing can be Extreme
    assert (
        classify_zone(
            10.0, baseline=None, fair_value=100, mos=0.2, top_band=None, fair_upper_mult=1.1
        )
        is Zone.DISCOUNT
    )
    assert (
        classify_zone(
            1000.0, baseline=None, fair_value=100, mos=0.2, top_band=None, fair_upper_mult=1.1
        )
        is Zone.PREMIUM
    )


# ───────────────────────── end to end on the fixture company ─────────────────────────


def test_fixture_company_end_to_end() -> None:
    """Financials → DCF inputs → scenarios → blend, to check the pieces fit together."""
    b = base_inputs(annual_frame(), g1=0.12, wacc_rate=0.12, config=V)
    assert b.inputs is not None
    scen = run_scenarios(b.inputs, V)
    values = {k: r.value_per_share for k, r in scen.items()}
    assert values["bear"] < values["base"] < values["bull"]  # type: ignore[operator]
    e = epv(
        normalised_ebit_cr=normalised_ebit(annual_frame(), V.epv.normalise_years),
        tax_rate=b.inputs.tax_rate,
        wacc=b.inputs.wacc,
        net_debt=b.inputs.net_debt,
        minority_interest=0,
        non_op_investments=b.inputs.non_op_investments,
        shares_cr=b.inputs.shares_cr,
    )
    v = blend(
        cmp=150.0,
        sector=SECTORS["default"],
        config=V,
        provisional_grade="A",
        method_values={"dcf_base": values["base"]},
        bear_dcf=values["bear"],
        bull_dcf=values["bull"],
        epv=e.value,
    )
    # DCF alone carries 40% < 50% coverage → no fair value, but baseline/top still computed
    assert v.fair_value is None and v.baseline is not None and v.top_band == values["bull"]


def test_market_cap_uses_the_latest_share_count_on_file() -> None:
    from app.reports.valuation_run import _latest_shares

    idx = pd.DatetimeIndex(["2024-03-31", "2025-03-31"])
    # the FY2025 row is a balance sheet only (no P&L, so no diluted share count)
    annual = pd.DataFrame({"shares_diluted_cr": [10.0, np.nan]}, index=idx)
    quarterly = pd.DataFrame({"shares_diluted_cr": [10.2, 20.4]},
                             index=pd.DatetimeIndex(["2024-06-30", "2025-09-30"]))  # fmt: skip
    assert _latest_shares(annual, quarterly) == (20.4, "quarter ended 2025-09-30")
    assert _latest_shares(annual, pd.DataFrame()) == (10.0, "year ended 2024-03-31")
    assert _latest_shares(pd.DataFrame(), pd.DataFrame()) == (None, "")


def test_deep_discount_is_never_shallower_than_discount() -> None:
    # a bank: no bear DCF / EPV, a narrow P/B band whose -1 sigma (260) sits above
    # FV x (1 - MoS) = 278.5 x 0.725 = 201.9. CMP 250 is 10% under FV: fair, not deep discount
    band = {-2: 240.0, -1: 260.0, 0: 280.0, 1: 300.0, 2: 320.0}
    v = _blend(250.0, bear_dcf=None, epv=None, band_prices=band)
    assert v.baseline == pytest.approx(260.0) and v.zone is Zone.FAIR
    assert any("deep discount starts at the threshold" in r for r in v.reasons)
    # below the MoS threshold it is deep (the floor is above it)
    assert _blend(190.0, bear_dcf=None, epv=None, band_prices=band).zone is Zone.DEEP_DISCOUNT


def test_band_after_a_structural_break_uses_only_the_post_break_window() -> None:
    days = pd.bdate_range("2020-01-01", "2024-12-31")
    price = pd.Series(100.0, index=days)
    brk = pd.Timestamp("2023-07-03")
    bvps = pd.Series({pd.Timestamp("2019-12-31"): 50.0, brk: 25.0})  # P/B 2x before, 4x after
    full = band("pb", price, bvps, lookback_years=5, min_observations=250)
    post = band("pb", price, bvps, lookback_years=5, min_observations=250, start_floor=brk)
    assert full.median == pytest.approx(2.0)  # mostly pre-merger days: misleading
    assert post.median == pytest.approx(4.0) and post.sigma == pytest.approx(0.0)
    assert post.observations == len(days[days >= brk])
    assert post.reasons == [f"pb 03 Jul 2023 to 31 Dec 2024: median 4.0x, sigma 0.0x over "
                            f"{post.observations} days"]  # fmt: skip
    short = band("pb", price, bvps, lookback_years=5, min_observations=500,
                 start_floor=pd.Timestamp("2024-06-03"))  # fmt: skip
    assert not short.ok and "03 Jun 2024 to 31 Dec 2024" in short.reasons[0]


def test_pb_vs_roe_regression_band() -> None:
    days = pd.bdate_range("2020-01-01", "2024-12-31")
    roe = pd.Series({pd.Timestamp(f"{y}-01-01"): r
                     for y, r in ((2020, 0.10), (2021, 0.12), (2022, 0.14), (2023, 0.16),
                                  (2024, 0.13))})  # fmt: skip
    bvps = pd.Series({pd.Timestamp("2019-12-31"): 50.0})
    step_roe = roe.reindex(roe.index.union(days)).ffill().reindex(days)
    price = (0.5 + 10.0 * step_roe) * 50.0  # P/B = 0.5 + 10 x ROE exactly
    b = regression_band(price, bvps, roe, roe_now=0.15, min_observations=250, min_distinct_roe=3)
    assert b.ok and b.median == pytest.approx(2.0) and b.sigma == pytest.approx(0.0, abs=1e-9)
    assert band_prices(b, 400.0) == pytest.approx({k: 800.0 for k in (-2, -1, 0, 1, 2)})
    assert "P/B = 0.50 + 10.00 x ROE" in b.reasons[0] and "full history" in b.reasons[0]
    # fitted only after 2023: two distinct ROE values are not enough
    late = regression_band(price, bvps, roe, roe_now=0.15, min_observations=250,
                           min_distinct_roe=3, start=pd.Timestamp("2023-01-01"))  # fmt: skip
    assert not late.ok and "2 distinct ROE values" in late.reasons[0]
    assert not regression_band(price, bvps, roe, roe_now=None, min_observations=250,
                               min_distinct_roe=3).ok  # fmt: skip
