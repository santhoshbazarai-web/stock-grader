"""Sector-specific valuation models (SPEC §5.6), selected by ``config/sectors.yaml``.
Banks, NBFCs and insurers never use FCFF DCF (AGENTS.md rule 10). Pure functions.

Banks / NBFCs
    justified P/B = (ROE - g) / (Ke - g)                   g = sector ``long_run_growth``
    value         = justified P/B x BVPS
    residual income: BV_0 = BVPS, BV_t = BV_{t-1} x (1 + ROE x (1 - payout))
                     RI_t = (ROE - Ke) x BV_{t-1}
                     value = BV_0 + sum RI_t/(1+Ke)^t + RI_{N+1}/(Ke - g)/(1+Ke)^N
Insurance      P/EV band or peer P/EV x EV per share; appraisal = EV + VNB x multiple
Cyclicals      EV = median EBITDA margin over ``normalise_years`` x current sales x EV/EBITDA
               band median; equity = EV - net debt - minority + non-op investments
Real estate    NAV x (1 - nav_discount)                 (NAV entered manually)
Holding cos    SOTP = sum(listed stakes at market) x (1 - holding_discount) + standalone value
"""

from dataclasses import dataclass, field

import pandas as pd

from app.fundamentals.metrics import by_year, column


@dataclass(frozen=True)
class ModelValue:
    value: float | None
    reasons: list[str] = field(default_factory=list)


# ───────────────────────── banks / NBFCs ─────────────────────────


def justified_pb(*, roe: float | None, ke: float, g: float, bvps: float | None) -> ModelValue:
    if roe is None or bvps is None or bvps <= 0:
        return ModelValue(None, ["justified P/B needs ROE and positive book value"])
    if ke <= g:
        return ModelValue(None, [f"Ke {ke:.2%} <= long-run growth {g:.2%}"])
    pb = (roe - g) / (ke - g)
    if pb <= 0:
        return ModelValue(None, [f"ROE {roe:.1%} <= growth {g:.1%}: justified P/B not positive"])
    return ModelValue(pb * bvps, [f"justified P/B {pb:.2f}x = (ROE {roe:.1%} - g)/(Ke - g)"])


def residual_income(
    *, bvps: float | None, roe: float | None, ke: float, g: float, payout: float, years: int
) -> ModelValue:
    if roe is None or bvps is None or bvps <= 0:
        return ModelValue(None, ["residual income needs ROE and positive book value"])
    if ke <= g:
        return ModelValue(None, [f"Ke {ke:.2%} <= growth {g:.2%}"])
    bv, pv = bvps, 0.0
    for t in range(1, years + 1):
        pv += (roe - ke) * bv / (1 + ke) ** t
        bv *= 1 + roe * (1 - payout)
    terminal = (roe - ke) * bv / (ke - g) / (1 + ke) ** years
    return ModelValue(bvps + pv + terminal, [f"residual income over {years}y at Ke {ke:.2%}"])


# ───────────────────────── insurance ─────────────────────────


def p_ev_value(ev_per_share: float | None, p_ev: float | None, label: str) -> ModelValue:
    if ev_per_share is None or p_ev is None or ev_per_share <= 0 or p_ev <= 0:
        return ModelValue(None, [f"{label}: needs embedded value per share and a P/EV"])
    return ModelValue(ev_per_share * p_ev, [f"{label}: {p_ev:.2f}x EV/share {ev_per_share:,.1f}"])


def insurance_appraisal(
    ev_per_share: float | None, vnb_per_share: float | None, vnb_multiple: float | None
) -> ModelValue:
    if None in (ev_per_share, vnb_per_share, vnb_multiple):
        return ModelValue(None, ["appraisal needs EV, VNB (manual inputs) and a VNB multiple"])
    value = ev_per_share + vnb_per_share * vnb_multiple  # type: ignore[operator]
    return ModelValue(value, ["appraisal value = EV + VNB x multiple"])


# ───────────────────────── cyclicals ─────────────────────────


def normalised_ev_ebitda(
    annual: pd.DataFrame,
    *,
    normalise_years: int,
    band_median_ev_ebitda: float | None,
    net_debt: float,
    minority_interest: float,
    non_op_investments: float,
    shares_cr: float,
) -> ModelValue:
    df = by_year(annual)
    if df.empty or band_median_ev_ebitda is None or shares_cr <= 0:
        return ModelValue(None, ["needs annual data, an EV/EBITDA band and a share count"])
    y = int(df.index.max())
    win = df.reindex(range(y - normalise_years + 1, y + 1))
    rev, ebitda = column(win, "revenue"), column(win, "ebitda")
    if rev.isna().any() or ebitda.isna().any() or (rev <= 0).any():
        return ModelValue(None, [f"needs revenue and EBITDA for all {normalise_years} years"])
    margin = float((ebitda / rev).median())
    mid_cycle = margin * float(rev.iloc[-1])
    ev = mid_cycle * band_median_ev_ebitda
    equity = ev - net_debt - minority_interest + non_op_investments
    reasons = [
        f"mid-cycle EBITDA {mid_cycle:,.0f} Cr ({normalise_years}y median margin "
        f"{margin:.1%}) x {band_median_ev_ebitda:.1f}x"
    ]
    return ModelValue(equity / shares_cr, reasons)


# ───────────────────────── real estate / holding companies ─────────────────────────


def nav_value(nav_per_share: float | None, discount: float) -> ModelValue:
    if nav_per_share is None or nav_per_share <= 0:
        return ModelValue(None, ["NAV per share is a manual input and is missing"])
    return ModelValue(nav_per_share * (1 - discount), [f"NAV less {discount:.0%} discount"])


def sotp_value(
    *,
    listed_holdings_value_cr: float | None,
    standalone_value_cr: float | None,
    holding_discount: float,
    net_debt: float,
    shares_cr: float,
) -> ModelValue:
    if listed_holdings_value_cr is None or standalone_value_cr is None or shares_cr <= 0:
        return ModelValue(None, ["SOTP needs listed stakes, standalone value and shares"])
    total = listed_holdings_value_cr * (1 - holding_discount) + standalone_value_cr - net_debt
    reason = f"SOTP: stakes {listed_holdings_value_cr:,.0f} Cr less {holding_discount:.0%}"
    return ModelValue(total / shares_cr, [reason])
