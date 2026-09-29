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
- Loan growth= advances / previous advances - 1
"""

from typing import Any

import pandas as pd

from app.fundamentals.metrics import Metric, _col, average, by_year, ratio

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
)


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
    reported_nii = _col(df, "net_interest_income")
    nii = reported_nii.fillna(_col(df, "interest_earned") - _col(df, "interest_expended"))
    advances, gross_npa = _col(df, "advances"), _col(df, "gross_npa")
    earning = advances + _col(df, "investments")
    out["nii"] = nii
    out["nim_pct"] = ratio(nii, average(earning)) * 100
    out["casa_pct"] = ratio(_col(df, "casa_deposits"), _col(df, "deposits")) * 100
    out["gnpa_pct"] = ratio(gross_npa, _col(df, "gross_advances")) * 100
    out["nnpa_pct"] = ratio(_col(df, "net_npa"), advances) * 100
    out["pcr_pct"] = ratio(gross_npa - _col(df, "net_npa"), gross_npa) * 100
    out["credit_cost_pct"] = ratio(_col(df, "loan_loss_provisions"), average(advances)) * 100
    out["car_pct"] = _col(df, "crar_pct")
    income = nii + _col(df, "other_income")
    out["cost_to_income_pct"] = ratio(_col(df, "operating_expenses"), income) * 100
    out["roa_pct"] = ratio(_col(df, "pat"), average(_col(df, "total_assets"))) * 100
    out["roe_pct"] = ratio(_col(df, "pat"), average(_col(df, "total_equity"))) * 100
    out["loan_growth_pct"] = (ratio(advances, advances.shift(1)) - 1) * 100
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
