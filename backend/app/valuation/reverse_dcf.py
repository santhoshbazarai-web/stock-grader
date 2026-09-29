"""Reverse DCF (SPEC §5.2): the stage-1 growth g1 the market price implies, holding WACC,
terminal growth and operating ratios at base. Pure function.

Solves run_dcf(inputs with g1 = g).value_per_share - CMP = 0 with ``scipy.optimize.brentq``
over ``valuation.dcf.reverse_growth_bracket``. Value rises monotonically with g1 whenever a
growth year adds positive FCFF, so a root is unique when it exists. If the price sits outside
the values at the bracket ends, there is no implied growth in range and the result says which
side.
"""

from dataclasses import dataclass, field, replace

from scipy.optimize import brentq

from app.core.config import ValuationConfig
from app.valuation.dcf import DcfInputs, run_dcf


@dataclass(frozen=True)
class ReverseDcf:
    implied_growth: float | None
    hist_growth: float | None
    gap: float | None  # implied - historical: > 0 means the price asks for faster growth
    reasons: list[str] = field(default_factory=list)


def _value(base: DcfInputs, g1: float) -> float:
    v = run_dcf(replace(base, g1=g1)).value_per_share
    if v is None:
        raise ValueError("DCF undefined")
    return v


def reverse_dcf(
    base: DcfInputs, cmp: float, hist_growth_5y: float | None, config: ValuationConfig
) -> ReverseDcf:
    lo, hi = config.dcf.reverse_growth_bracket
    try:
        v_lo, v_hi = _value(base, lo), _value(base, hi)
    except ValueError:
        return ReverseDcf(None, hist_growth_5y, None, ["base DCF undefined (WACC <= g_T?)"])
    if cmp < v_lo:
        reason = f"price below the value even at g1 = {lo:.0%}"
        return ReverseDcf(None, hist_growth_5y, None, [reason])
    if cmp > v_hi:
        reason = f"price above the value even at g1 = {hi:.0%}"
        return ReverseDcf(None, hist_growth_5y, None, [reason])
    implied = float(brentq(lambda g: _value(base, g) - cmp, lo, hi, xtol=1e-7))
    gap = implied - hist_growth_5y if hist_growth_5y is not None else None
    reasons = [f"price implies {implied:.1%} growth for {base.stage1_years} years"]
    if gap is not None:
        word = "above" if gap > 0 else "at or below"
        reasons.append(f"{word} the 5-yr historical {hist_growth_5y:.1%} (gap {gap:+.1%})")
    else:
        reasons.append("historical growth unavailable: no gap")
    return ReverseDcf(implied, hist_growth_5y, gap, reasons)
