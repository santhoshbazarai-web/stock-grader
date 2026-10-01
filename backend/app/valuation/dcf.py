"""FCFF DCF (SPEC §5.1): two-stage growth, Gordon terminal value, WACC, Blume beta, scenarios
and a WACC x terminal-growth sensitivity grid. Pure functions.

Projection (revenue-driven, so the ``margin_delta`` of a scenario has something to act on):

    g_t      = g1                                         t = 1..N1
             = g1 + (g_T - g1) x (t - N1) / N2            t = N1+1..N1+N2   (linear fade to g_T)
    rev_t    = rev_{t-1} x (1 + g_t)
    FCFF_t   = rev_t x margin x (1 - tax) + rev_t x (da% - capex%) - nwc% x (rev_t - rev_{t-1})
    TV       = FCFF_{N+1} / (WACC - g_T)                  FCFF_{N+1} grows revenue by g_T
    EV       = sum FCFF_t / (1+WACC)^t + TV / (1+WACC)^N  (end-of-year discounting)
    equity   = EV - net debt - minority interest + non-operating investments
    value/sh = equity / diluted shares

WACC = E/(D+E) x Ke + D/(D+E) x Kd, Ke = Rf + beta x ERP + size premium,
Kd = interest / average debt x (1 - tax), weights at market value (E = market cap, D = book
debt as its market-value proxy). Beta: slope of weekly stock vs benchmark returns over the
lookback, Blume-adjusted 0.67 x beta + 0.33, clamped to [floor, cap] from config.
"""

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from app.core.config import SectorConfig, ValuationConfig
from app.fundamentals.metrics import by_year, capex

BLUME_WEIGHT = 0.67  # Blume (1975): adjusted beta = 0.67 x raw + 0.33 x 1
_WEEKLY = "W-FRI"


# ───────────────────────── cost of capital ─────────────────────────


@dataclass(frozen=True)
class BetaEstimate:
    value: float  # Blume-adjusted (if configured) and clamped to [floor, cap]
    unclamped: float  # before the clamp
    clamped: str | None  # "floor" | "cap" | None


def blume_beta(
    stock_close: pd.Series,
    index_close: pd.Series,
    config: ValuationConfig,
    *,
    as_of: pd.Timestamp | None = None,
) -> float | None:
    """Weekly-return regression slope over ``beta.lookback_years``, Blume-adjusted and clamped.
    ``None`` when fewer than half the expected weeks overlap."""
    est = beta_estimate(stock_close, index_close, config, as_of=as_of)
    return est.value if est is not None else None


def beta_estimate(
    stock_close: pd.Series,
    index_close: pd.Series,
    config: ValuationConfig,
    *,
    as_of: pd.Timestamp | None = None,
) -> BetaEstimate | None:
    """:func:`blume_beta` with whether the clamp bound (a clamped beta is a data gap)."""
    cfg = config.beta
    end = as_of or min(stock_close.index.max(), index_close.index.max())
    start = end - pd.DateOffset(years=cfg.lookback_years)
    weekly = pd.concat(
        [
            stock_close.loc[start:end].resample(_WEEKLY).last(),
            index_close.loc[start:end].resample(_WEEKLY).last(),
        ],
        axis=1,
    )
    returns = weekly.pct_change().dropna()
    if len(returns) < 26 * cfg.lookback_years:
        return None
    var = float(returns.iloc[:, 1].var())
    if var == 0:
        return None
    raw = float(returns.iloc[:, 0].cov(returns.iloc[:, 1])) / var
    beta = BLUME_WEIGHT * raw + (1 - BLUME_WEIGHT) if cfg.blume_adjust else raw
    clamped = "floor" if beta < cfg.floor else "cap" if beta > cfg.cap else None
    return BetaEstimate(float(min(max(beta, cfg.floor), cfg.cap)), float(beta), clamped)


def size_premium(market_cap_cr: float, config: ValuationConfig) -> float:
    for tier in config.size_premium:
        if tier.max_mcap is None or market_cap_cr <= tier.max_mcap:
            return tier.premium
    return config.size_premium[-1].premium  # pragma: no cover - last tier is open-ended


def cost_of_equity_explained(
    beta: float, market_cap_cr: float, config: ValuationConfig
) -> tuple[float, str]:
    """Ke and its build-up: ``Ke r% = Rf r% + beta b x ERP e% + size s%``."""
    sp = size_premium(market_cap_cr, config)
    ke = config.risk_free_rate + beta * config.equity_risk_premium + sp
    return ke, (f"Ke {ke:.2%} = Rf {config.risk_free_rate:.2%} + beta {beta:.2f} x ERP "
                f"{config.equity_risk_premium:.2%} + size premium {sp:.2%}")  # fmt: skip


def cost_of_equity(beta: float, market_cap_cr: float, config: ValuationConfig) -> float:
    return (
        config.risk_free_rate
        + beta * config.equity_risk_premium
        + size_premium(market_cap_cr, config)
    )


@dataclass(frozen=True)
class Wacc:
    wacc: float
    ke: float
    kd_after_tax: float | None
    equity_weight: float
    debt_weight: float


def wacc(
    *,
    ke: float,
    market_cap_cr: float,
    debt_cr: float,
    interest_cr: float | None,
    avg_debt_cr: float | None,
    tax_rate: float,
) -> Wacc:
    """Market-value weights. With no debt the WACC is Ke; with debt but no usable interest
    cost, Kd is unknown and a ValueError is raised (never assume a cost of debt)."""
    if debt_cr <= 0:
        return Wacc(ke, ke, None, 1.0, 0.0)
    if interest_cr is None or not avg_debt_cr or avg_debt_cr <= 0:
        raise ValueError("cost of debt unknown: interest or average debt missing")
    kd = interest_cr / avg_debt_cr * (1 - tax_rate)
    total = market_cap_cr + debt_cr
    we, wd = market_cap_cr / total, debt_cr / total
    return Wacc(we * ke + wd * kd, ke, kd, we, wd)


# ───────────────────────── projection ─────────────────────────


@dataclass(frozen=True)
class DcfInputs:
    revenue: float  # base-year revenue, ₹ Cr
    ebit_margin: float
    tax_rate: float
    da_pct: float  # D&A / revenue
    capex_pct: float  # capex / revenue
    nwc_pct: float  # net working capital / revenue (ΔNWC = nwc% x Δrevenue)
    g1: float
    g_terminal: float
    wacc: float
    net_debt: float
    minority_interest: float
    non_op_investments: float
    shares_cr: float
    stage1_years: int = 5
    stage2_years: int = 5


@dataclass(frozen=True)
class DcfResult:
    value_per_share: float | None
    equity_value: float | None
    enterprise_value: float | None
    pv_explicit: float | None
    pv_terminal: float | None
    growth_path: list[float] = field(default_factory=list)
    fcff: list[float] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def terminal_share(self) -> float | None:
        if self.pv_terminal is None or not self.enterprise_value:
            return None
        return self.pv_terminal / self.enterprise_value


def growth_path(g1: float, g_terminal: float, n1: int, n2: int) -> list[float]:
    fade = [g1 + (g_terminal - g1) * k / n2 for k in range(1, n2 + 1)]
    return [g1] * n1 + fade


def fcff_for(revenue: float, prev_revenue: float, x: DcfInputs) -> float:
    ebit = revenue * x.ebit_margin
    return (
        ebit * (1 - x.tax_rate)
        + revenue * (x.da_pct - x.capex_pct)
        - x.nwc_pct * (revenue - prev_revenue)
    )


def run_dcf(x: DcfInputs) -> DcfResult:
    if x.wacc <= x.g_terminal:
        reason = f"WACC {x.wacc:.2%} <= terminal growth {x.g_terminal:.2%}"
        return DcfResult(None, None, None, None, None, reasons=[reason])
    if x.shares_cr <= 0:
        return DcfResult(None, None, None, None, None, reasons=["no share count"])
    path = growth_path(x.g1, x.g_terminal, x.stage1_years, x.stage2_years)
    rev, flows, pv = x.revenue, [], 0.0
    for t, g in enumerate(path, start=1):
        nxt = rev * (1 + g)
        f = fcff_for(nxt, rev, x)
        flows.append(f)
        pv += f / (1 + x.wacc) ** t
        rev = nxt
    n = len(path)
    fcff_next = fcff_for(rev * (1 + x.g_terminal), rev, x)
    tv = fcff_next / (x.wacc - x.g_terminal)
    pv_tv = tv / (1 + x.wacc) ** n
    ev = pv + pv_tv
    equity = ev - x.net_debt - x.minority_interest + x.non_op_investments
    reasons = [
        f"g1 {x.g1:.1%} for {x.stage1_years}y fading to {x.g_terminal:.1%}; WACC {x.wacc:.2%}",
        f"terminal value is {pv_tv / ev:.0%} of EV" if ev > 0 else "enterprise value not positive",
    ]
    return DcfResult(equity / x.shares_cr, equity, ev, pv, pv_tv, path, flows, reasons)


# ───────────────────────── scenarios & sensitivity ─────────────────────────


def scenario_inputs(base: DcfInputs, config: ValuationConfig) -> dict[str, DcfInputs]:
    out = {}
    for name in ("bear", "base", "bull"):
        d = getattr(config.dcf.scenarios, name)
        out[name] = replace(
            base,
            g1=base.g1 + d.g1_delta,
            ebit_margin=base.ebit_margin + d.margin_delta,
            wacc=base.wacc + d.wacc_delta,
        )
    return out


def run_scenarios(base: DcfInputs, config: ValuationConfig) -> dict[str, DcfResult]:
    return {k: run_dcf(v) for k, v in scenario_inputs(base, config).items()}


@dataclass(frozen=True)
class SensitivityGrid:
    waccs: list[float]
    terminal_growths: list[float]
    values: list[list[float | None]]  # values[i][j] for waccs[i], terminal_growths[j]


def sensitivity_grid(base: DcfInputs, config: ValuationConfig) -> SensitivityGrid:
    s = config.dcf.sensitivity
    steps = round(s.wacc_range / s.wacc_step)
    waccs = [round(base.wacc + k * s.wacc_step, 10) for k in range(-steps, steps + 1)]
    growths = list(s.tg_values)
    values = [
        [run_dcf(replace(base, wacc=w, g_terminal=g)).value_per_share for g in growths]
        for w in waccs
    ]
    return SensitivityGrid(waccs, growths, values)


# ───────────────────────── inputs from financials ─────────────────────────


@dataclass(frozen=True)
class BaseInputs:
    inputs: DcfInputs | None
    reasons: list[str]
    # Inputs the filings did not provide that were taken as nil so a DCF is possible at all
    # (e.g. Screener exports carry no payables or minority interest). The caller records each
    # as a data gap (rule 1) and the report shows them.
    assumed_nil: list[str] = field(default_factory=list)


def default_g1(
    hist_revenue_cagr_5y: float | None, sector: SectorConfig, config: ValuationConfig
) -> float | None:
    """g1 default = min(5-yr revenue CAGR, sector cap) (SPEC §5.1)."""
    if hist_revenue_cagr_5y is None:
        return None
    cap = sector.g1_cap if sector.g1_cap is not None else config.dcf.g1_cap_by_default
    return min(hist_revenue_cagr_5y, cap)


def base_inputs(
    annual: pd.DataFrame,
    *,
    g1: float | None,
    wacc_rate: float,
    config: ValuationConfig,
    overrides: dict[str, float] | None = None,
) -> BaseInputs:
    """Build DCF inputs from the canonical annual frame. Operating ratios are averages over the
    last ``dcf.margin_years`` (all years required); balance-sheet items are the latest year.
    ``overrides`` (user assumptions: g1, ebit_margin, wacc, g_terminal, ...) win."""
    df = by_year(annual)
    if df.empty:
        return BaseInputs(None, ["no annual data"])
    y = int(df.index.max())
    years = list(range(y - config.dcf.margin_years + 1, y + 1))
    win = df.reindex(years)

    def col(name: str) -> pd.Series:
        return pd.to_numeric(win.get(name, pd.Series(np.nan, index=win.index)), errors="coerce")

    rev = col("revenue")
    needed = {
        "revenue": rev,
        "ebit": col("ebit"),
        "depreciation": col("depreciation"),
        "capex": capex(win),
        "cfo": col("cfo"),
    }
    missing = [f"{k} (FY{yy})" for k, s in needed.items() for yy in years if pd.isna(s.get(yy))]
    latest = df.loc[y]

    def last(name: str) -> float | None:
        v = latest.get(name)
        return None if v is None or pd.isna(v) else float(v)

    for name in ("total_debt", "cash_and_equivalents", "shares_diluted_cr"):
        if last(name) is None:
            missing.append(f"{name} (FY{y})")
    if missing:
        return BaseInputs(None, [f"missing {', '.join(missing)}"])
    assumed_nil: list[str] = []
    if (rev <= 0).any():
        return BaseInputs(None, ["non-positive revenue in margin window"])

    tax = (col("tax") / col("pbt")).where(col("pbt") > 0)
    tax_rate = float(tax.mean()) if tax.notna().all() and 0 <= tax.mean() <= 1 else None
    reasons = []
    if tax_rate is None:
        tax_rate = config.tax_rate_default
        reasons.append(f"effective tax rate undefined; using default {tax_rate:.1%}")
    nwc = [last("receivables"), last("inventory"), last("payables")]
    if any(v is None for v in nwc):
        nwc_pct = 0.0
        assumed_nil.append("working_capital")
        reasons.append("receivables/inventory/payables incomplete: ΔNWC assumed nil (data gap)")
    else:
        nwc_pct = (nwc[0] + nwc[1] - nwc[2]) / float(rev.iloc[-1])  # type: ignore[operator]
    if g1 is None and not (overrides and "g1" in overrides):
        return BaseInputs(None, ["no g1: 5-yr revenue CAGR unavailable and no override"])
    x = DcfInputs(
        revenue=float(rev.iloc[-1]),
        ebit_margin=float((col("ebit") / rev).mean()),
        tax_rate=tax_rate,
        da_pct=float((col("depreciation") / rev).mean()),
        capex_pct=float((needed["capex"] / rev).mean()),
        nwc_pct=nwc_pct,
        g1=g1 if g1 is not None else 0.0,
        g_terminal=config.dcf.terminal_growth,
        wacc=wacc_rate,
        net_debt=(last("total_debt") or 0.0) - (last("cash_and_equivalents") or 0.0),
        minority_interest=last("minority_interest_bs") or 0.0,
        non_op_investments=last("non_operating_investments") or 0.0,
        shares_cr=last("shares_diluted_cr") or 0.0,
        stage1_years=config.dcf.stage1_years,
        stage2_years=config.dcf.stage2_years,
    )
    for name, label in (
        ("minority_interest_bs", "minority interest"),
        ("non_operating_investments", "non-operating investments"),
    ):
        if last(name) is None:
            assumed_nil.append(name)
            reasons.append(f"{label} not reported: taken as nil (data gap)")
    if overrides:
        x = replace(x, **overrides)  # type: ignore[arg-type]
        reasons.append(f"user overrides: {', '.join(sorted(overrides))}")
    lo, hi = config.dcf.terminal_growth_bounds
    if not lo <= x.g_terminal <= hi:
        reasons.append(f"terminal growth {x.g_terminal:.1%} outside [{lo:.0%}, {hi:.0%}]")
    return BaseInputs(x, reasons, assumed_nil)
