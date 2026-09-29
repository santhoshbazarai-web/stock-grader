"""Earnings power value and Graham number (SPEC §5.5). Pure functions.

normalised EBIT = mean(EBIT / revenue over ``epv.normalise_years``) x latest revenue
EPV (equity)    = normalised EBIT x (1 - tax) / WACC - net debt - minority + non-op investments
Graham number   = sqrt(graham_multiplier x EPS x BVPS)          (22.5 = 15 PE x 1.5 PB)
"""

import math
from dataclasses import dataclass, field

import pandas as pd

from app.core.config import ValuationConfig
from app.fundamentals.metrics import by_year, column


@dataclass(frozen=True)
class SimpleValue:
    value: float | None
    reasons: list[str] = field(default_factory=list)


def normalised_ebit(annual: pd.DataFrame, years: int) -> float | None:
    df = by_year(annual)
    if df.empty:
        return None
    y = int(df.index.max())
    win = df.reindex(range(y - years + 1, y + 1))
    rev, ebit = column(win, "revenue"), column(win, "ebit")
    if rev.isna().any() or ebit.isna().any() or (rev <= 0).any():
        return None
    return float((ebit / rev).mean() * rev.iloc[-1])


def epv(
    *,
    normalised_ebit_cr: float | None,
    tax_rate: float,
    wacc: float,
    net_debt: float,
    minority_interest: float,
    non_op_investments: float,
    shares_cr: float,
) -> SimpleValue:
    if normalised_ebit_cr is None:
        return SimpleValue(None, ["normalised EBIT unavailable (needs the full window)"])
    if wacc <= 0 or shares_cr <= 0:
        return SimpleValue(None, ["WACC or share count not positive"])
    enterprise = normalised_ebit_cr * (1 - tax_rate) / wacc
    equity = enterprise - net_debt - minority_interest + non_op_investments
    return SimpleValue(
        equity / shares_cr,
        [f"EPV: normalised EBIT {normalised_ebit_cr:,.0f} Cr capitalised at {wacc:.2%}"],
    )


def graham_number(eps: float | None, bvps: float | None, config: ValuationConfig) -> SimpleValue:
    if eps is None or bvps is None or eps <= 0 or bvps <= 0:
        return SimpleValue(None, ["Graham number needs positive EPS and book value"])
    return SimpleValue(math.sqrt(config.graham_multiplier * eps * bvps))
