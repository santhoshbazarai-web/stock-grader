"""Bank / NBFC metrics (SPEC §4). Pure functions; results in percent (0-100), matching the
``scoring.bank_maps`` units.

Bank-specific inputs live in ``fin_annual.extra`` under the keys in :data:`BANK_FIELDS`
(₹ crore unless noted); ``pat``, ``other_income``, ``total_assets`` and ``total_equity`` come
from the regular canonical columns. Use :func:`bank_frame` to lift ``extra`` into columns.

Definitions:
- NII        = interest_earned - interest_expended (or ``net_interest_income`` if reported)
- NIM        = NII / average(advances + investments)           (interest-earning assets proxy)
- CASA       = casa_deposits / deposits
- GNPA       = gross_npa / gross_advances;  NNPA = net_npa / advances
- PCR        = (gross_npa - net_npa) / gross_npa               (excluding technical write-offs)
- Credit cost= loan_loss_provisions / average(advances)
- CAR/CRAR   = reported ``crar_pct`` (risk-weighted assets are not in the statements)
- Cost/income= operating_expenses / (NII + other_income)
- RoA        = pat / average(total_assets);  RoE = pat / average(total_equity)
- Loan growth= advances / previous advances - 1;  deposit growth likewise
- CD ratio   = advances / deposits
- Equity/assets = total_equity / total_assets
- Payout     = dividends_paid / pat

Proxies (``PROXIES``): every metric derived here from statement lines rather than reported by
the bank is a *proxy* (e.g. the bank's own NIM uses average interest-earning assets; ours uses
advances + investments). GNPA, NNPA, CAR and CASA are the reported figures. The report labels
proxies, and the health pillar uses a proxy only in place of a missing reported metric
(``scoring.pillars.health``).

Per-share (``bank_per_share``): BVPS = equity / year-end shares, P/B = price / BVPS, and EPS
growth per share = growth of PAT per year-end share (weighted-average EPS when no year-end
count), so mergers do not inflate it (SPEC §4 structural breaks).
"""

from collections.abc import Mapping
from typing import Any

import pandas as pd

from app.fundamentals.metrics import Metric, average, by_year, column, ratio

BANK_FIELDS: dict[str, str] = {
    "interest_earned": "Interest earned (₹ Cr)",
    "interest_expended": "Interest expended (₹ Cr)",
    "net_interest_income": "Net interest income, if reported directly (₹ Cr)",
    "advances": "Net advances / loan book (₹ Cr)",
    "gross_advances": "Gross advances (₹ Cr)",
    "investments": "Investments (₹ Cr)",
    "deposits": "Total deposits (₹ Cr; NBFCs: leave empty)",
    "casa_deposits": "Current + savings account deposits (₹ Cr)",
    "gross_npa": "Gross non-performing assets (₹ Cr)",
    "net_npa": "Net non-performing assets (₹ Cr)",
    "loan_loss_provisions": "Provisions for NPAs / credit losses in the P&L (₹ Cr)",
    "operating_expenses": "Operating expenses (₹ Cr)",
    "crar_pct": "Capital adequacy ratio as reported (%)",
}

BANK_METRICS = (
    "nim_pct",
    "casa_pct",
    "gnpa_pct",
    "nnpa_pct",
    "pcr_pct",
    "credit_cost_pct",
    "car_pct",
    "cost_to_income_pct",
    "roa_pct",
    "roe_pct",
    "loan_growth_pct",
    "deposit_growth_pct",
    "cd_ratio_pct",
    "equity_to_assets_pct",
    "payout_pct",
)

# metric → what it is a proxy for / how it is derived (not reported by the bank)
PROXIES: dict[str, str] = {
    "nim_pct": "NII / average(advances + investments): earning-assets proxy",
    "credit_cost_pct": "loan-loss provisions / average advances",
    "cost_to_income_pct": "operating expenses / (NII + other income)",
    "roa_pct": "PAT / average total assets",
    "roe_pct": "PAT / average equity",
    "loan_growth_pct": "advances / previous advances - 1",
    "deposit_growth_pct": "deposits / previous deposits - 1",
    "cd_ratio_pct": "advances / deposits",
    "equity_to_assets_pct": "equity / total assets: capital proxy (CAR not reported)",
    "payout_pct": "dividends paid / PAT",
    "bvps": "equity / year-end shares",
    "pb": "price / BVPS",
    "eps_growth_ps_pct": "growth of PAT per year-end share",
}


def bank_frame(annual: pd.DataFrame) -> pd.DataFrame:
    """Canonical annual frame with the ``extra`` dict expanded into :data:`BANK_FIELDS` columns."""
    df = annual.copy()
    extras = df["extra"] if "extra" in df.columns else pd.Series([None] * len(df), index=df.index)
    for key in BANK_FIELDS:
        if key not in df.columns:
            df[key] = [(e or {}).get(key) if isinstance(e, dict) else None for e in extras]
    return df


def bank_metrics(annual: pd.DataFrame) -> pd.DataFrame:
    """Per-fiscal-year bank metrics (percent), from a frame prepared by :func:`bank_frame`."""
    df = by_year(bank_frame(annual))
    out = pd.DataFrame(index=df.index)
    reported_nii = column(df, "net_interest_income")
    nii = reported_nii.fillna(column(df, "interest_earned") - column(df, "interest_expended"))
    advances, gross_npa = column(df, "advances"), column(df, "gross_npa")
    earning = advances + column(df, "investments")
    out["nii"] = nii
    out["nim_pct"] = ratio(nii, average(earning)) * 100
    out["casa_pct"] = ratio(column(df, "casa_deposits"), column(df, "deposits")) * 100
    out["gnpa_pct"] = ratio(gross_npa, column(df, "gross_advances")) * 100
    out["nnpa_pct"] = ratio(column(df, "net_npa"), advances) * 100
    out["pcr_pct"] = ratio(gross_npa - column(df, "net_npa"), gross_npa) * 100
    out["credit_cost_pct"] = ratio(column(df, "loan_loss_provisions"), average(advances)) * 100
    out["car_pct"] = column(df, "crar_pct")
    income = nii + column(df, "other_income")
    out["cost_to_income_pct"] = ratio(column(df, "operating_expenses"), income) * 100
    out["roa_pct"] = ratio(column(df, "pat"), average(column(df, "total_assets"))) * 100
    out["roe_pct"] = ratio(column(df, "pat"), average(column(df, "total_equity"))) * 100
    out["loan_growth_pct"] = (ratio(advances, advances.shift(1)) - 1) * 100
    deposits = column(df, "deposits")
    out["deposit_growth_pct"] = (ratio(deposits, deposits.shift(1)) - 1) * 100
    out["cd_ratio_pct"] = ratio(advances, deposits) * 100
    equity, assets = column(df, "total_equity"), column(df, "total_assets")
    out["equity_to_assets_pct"] = ratio(equity, assets) * 100
    out["payout_pct"] = ratio(column(df, "dividends_paid"), column(df, "pat")) * 100
    return out


_BANK_INPUTS: dict[str, tuple[str, ...]] = {
    "nim_pct": ("interest_earned", "interest_expended", "advances", "investments"),
    "casa_pct": ("casa_deposits", "deposits"),
    "gnpa_pct": ("gross_npa", "gross_advances"),
    "nnpa_pct": ("net_npa", "advances"),
    "pcr_pct": ("gross_npa", "net_npa"),
    "credit_cost_pct": ("loan_loss_provisions", "advances"),
    "car_pct": ("crar_pct",),
    "cost_to_income_pct": ("operating_expenses", "interest_earned", "interest_expended",
                           "other_income"),
    "roa_pct": ("pat", "total_assets"),
    "roe_pct": ("pat", "total_equity"),
    "loan_growth_pct": ("advances",),
    "deposit_growth_pct": ("deposits",),
    "cd_ratio_pct": ("advances", "deposits"),
    "equity_to_assets_pct": ("total_equity", "total_assets"),
    "payout_pct": ("dividends_paid", "pat"),
}  # fmt: skip


def bank_summary(annual: pd.DataFrame, year: int | None = None) -> dict[str, Metric]:
    """Bank metrics for fiscal ``year`` (default latest) with reasons for any gaps."""
    frame = bank_frame(annual)
    df = by_year(frame)
    if df.empty:
        return {}
    y = int(year if year is not None else df.index.max())
    m = bank_metrics(annual)
    out: dict[str, Metric] = {}
    for name in BANK_METRICS:
        v: Any = m[name].get(y)
        if v is None or pd.isna(v):
            needed = _BANK_INPUTS[name]
            absent = [f for f in needed if f not in df.columns or pd.isna(df[f].get(y))]
            reason = (
                "missing " + ", ".join(f"{f} (FY{y})" for f in absent)
                if absent
                else "needs the previous year / non-positive denominator"
            )
            out[name] = Metric(None, reason)
        else:
            out[name] = Metric(float(v))
    return out


def bank_per_share(
    annual: pd.DataFrame,
    *,
    price: float | None,
    shares_year_end: Mapping[int, float] | None = None,
    year: int | None = None,
) -> dict[str, Metric]:
    """BVPS, P/B and EPS growth per share for fiscal ``year`` (default latest); all proxies.
    Shares in crore; equity and PAT in ₹ crore."""
    df = by_year(annual)
    if df.empty:
        return {}
    y = int(year if year is not None else df.index.max())
    ye = {int(k): float(v) for k, v in (shares_year_end or {}).items() if v and v > 0}

    def shares(fy: int) -> tuple[float | None, str]:
        if fy in ye:
            return ye[fy], "year-end shares"
        w = column(df, "shares_diluted_cr").get(fy)
        if w is None or pd.isna(w) or w <= 0:
            return None, "weighted-average diluted shares"
        return float(w), "weighted-average diluted shares"

    out: dict[str, Metric] = {}
    sh, basis = shares(y)
    eq = column(df, "total_equity").get(y)
    if sh is None or eq is None or pd.isna(eq):
        out["bvps"] = Metric(None, f"missing total_equity or shares (FY{y})")
    else:
        out["bvps"] = Metric(float(eq) / sh, f"proxy: equity / {basis}")
    bvps = out["bvps"].value
    if price is None or not bvps or bvps <= 0:
        out["pb"] = Metric(None, "needs a price and a positive BVPS")
    else:
        out["pb"] = Metric(price / bvps, "proxy: price / BVPS")
    pat = column(df, "pat")
    sh0, _ = shares(y - 1)
    now, prev = pat.get(y), pat.get(y - 1)
    if sh is None or sh0 is None or now is None or prev is None or pd.isna(now) \
            or pd.isna(prev) or prev <= 0:  # fmt: skip
        out["eps_growth_ps_pct"] = Metric(None, f"needs PAT and shares for FY{y - 1} and FY{y}")
    else:
        g = (float(now) / sh) / (float(prev) / sh0) - 1
        out["eps_growth_ps_pct"] = Metric(g * 100, f"proxy: PAT per {basis}")
    return out
