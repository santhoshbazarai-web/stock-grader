"""Fundamental metrics (SPEC §4). Pure functions over canonical frames.

Inputs are canonical ``fin_annual`` / ``fin_quarterly`` frames (index = period end, columns as
in ``app.data.canonical``), in ₹ crore. Outputs are fractions (0.18 = 18%) except the ``*_days``
metrics (days) and per-share values (₹).

Conventions (see also SPEC §4 "Implementation notes"):
- Rows are keyed by fiscal year; "opening" means the previous fiscal year's closing balance.
  A missing year makes every metric that needs it NaN — never a stale or default value.
- A ratio is NaN when its denominator is not positive (negative equity, loss-making PBT...),
  because the ratio is not meaningful there. The one exception is interest coverage with zero
  interest and positive EBIT: +inf (debt-free), which score maps clamp to their best score.
- Multi-year figures (averages, cumulative ratios, CAGRs) need every year in the window;
  fewer years give ``None`` with the missing inputs named in ``Metric.reason``.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd

from app.core.config import FundamentalsConfig

# ───────────────────────── plumbing ─────────────────────────


@dataclass(frozen=True)
class Metric:
    value: float | None
    reason: str | None = None  # why the value is None, or a caveat

    @property
    def ok(self) -> bool:
        return self.value is not None


def by_year(frame: pd.DataFrame) -> pd.DataFrame:
    """Canonical frame → one row per fiscal year, consecutive (gaps become NaN rows)."""
    if frame.empty:
        return frame.copy()
    df = frame.copy()
    if "fiscal_year" in df.columns and df["fiscal_year"].notna().all():
        years = [int(v) for v in df["fiscal_year"]]
    else:
        years = [int(v) for v in pd.DatetimeIndex(df.index).year]
    df.index = pd.Index(years, name="fiscal_year")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.reindex(range(int(df.index.min()), int(df.index.max()) + 1))


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce").astype(float)
    return pd.Series(np.nan, index=df.index, dtype=float)


def ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    """num / den where den > 0, else NaN."""
    return num / den.where(den > 0)


def average(s: pd.Series) -> pd.Series:
    """Average of opening (previous year) and closing balance."""
    return (s + s.shift(1)) / 2


def cagr(end: float | None, start: float | None, years: int) -> float | None:
    if end is None or start is None or pd.isna(end) or pd.isna(start):
        return None
    if start <= 0 or end <= 0 or years <= 0:
        return None
    return float((end / start) ** (1 / years) - 1)


# ───────────────────────── annual metrics ─────────────────────────


def capex(df: pd.DataFrame) -> pd.Series:
    """Capex = purchase of fixed assets - sale of fixed assets (SPEC §4)."""
    return _col(df, "purchase_of_fixed_assets") - _col(df, "sale_of_fixed_assets")


def effective_tax_rate(df: pd.DataFrame) -> pd.Series:
    """tax / PBT where PBT > 0 and the rate lies in [0, 1]; NaN otherwise."""
    rate = ratio(_col(df, "tax"), _col(df, "pbt"))
    return rate.where((rate >= 0) & (rate <= 1))


def annual_metrics(annual: pd.DataFrame, *, tax_rate_fallback: float, days: int) -> pd.DataFrame:
    """Per-year metrics, indexed by fiscal year.

    ``tax_rate_fallback`` (``valuation.tax_rate_default``) replaces the effective tax rate in
    NOPAT only when that rate is undefined (loss years, credits) — it is a modelling choice
    reported in ``roic_tax_rate_source``, not a stand-in for missing data.
    """
    df = by_year(annual)
    out = pd.DataFrame(index=df.index)
    ta, cl = _col(df, "total_assets"), _col(df, "current_liabilities")
    equity, debt = _col(df, "total_equity"), _col(df, "total_debt")
    cash, inv_nonop = _col(df, "cash_and_equivalents"), _col(df, "non_operating_investments")
    revenue, cogs = _col(df, "revenue"), _col(df, "cogs")
    ebitda, ebit, pat = _col(df, "ebitda"), _col(df, "ebit"), _col(df, "pat")
    cfo, interest = _col(df, "cfo"), _col(df, "interest")

    out["capital_employed"] = ta - cl
    out["roce"] = ratio(ebit, average(out["capital_employed"]))
    out["roe"] = ratio(pat, average(equity))

    eff = effective_tax_rate(df)
    rate = eff.fillna(tax_rate_fallback).where(ebit.notna())
    out["roic_tax_rate_source"] = np.where(eff.notna(), "effective", "fallback")
    invested = equity + debt - cash - inv_nonop
    out["invested_capital"] = invested
    out["roic"] = ratio(ebit * (1 - rate), average(invested))

    out["opm"] = ratio(ebitda, revenue)
    out["cfo_to_ebitda"] = ratio(cfo, ebitda)
    out["cfo_to_pat"] = ratio(cfo, pat)
    out["capex"] = capex(df)
    out["fcf"] = cfo - out["capex"]
    out["other_income_share"] = ratio(_col(df, "other_income"), _col(df, "pbt"))
    out["debt_to_equity"] = ratio(debt, equity)
    out["net_debt"] = debt - cash
    out["net_debt_to_ebitda"] = ratio(out["net_debt"], ebitda)
    icr = ratio(ebit, interest)
    debt_free = (interest == 0) & (ebit > 0)
    out["interest_coverage"] = icr.mask(debt_free, np.inf)
    out["debtor_days"] = ratio(_col(df, "receivables"), revenue) * days
    out["inventory_days"] = ratio(_col(df, "inventory"), cogs) * days
    out["payable_days"] = ratio(_col(df, "payables"), cogs) * days
    out["ccc_days"] = out["debtor_days"] + out["inventory_days"] - out["payable_days"]
    out["capex_intensity"] = ratio(out["capex"], cfo)
    out["accruals_ratio"] = ratio(pat - cfo, average(ta))
    return out


# ───────────────────────── TTM ─────────────────────────

TTM_FIELDS = ("revenue", "ebitda", "other_income", "depreciation", "ebit", "interest", "pbt",
              "tax", "pat", "eps_diluted")  # fmt: skip


def ttm(quarterly: pd.DataFrame) -> dict[str, float | None]:
    """Sum of the last four *consecutive* quarters per flow item; ``None`` if four consecutive
    quarters with that item are not available."""
    out: dict[str, float | None] = dict.fromkeys(TTM_FIELDS)
    if quarterly is None or len(quarterly) < 4:
        return out
    q = quarterly.sort_index().iloc[-4:]
    ends = pd.DatetimeIndex(q.index).to_period("Q")
    if not all((b - a).n == 1 for a, b in pairwise(ends)):
        return out
    for name in TTM_FIELDS:
        if name in q.columns:
            values = pd.to_numeric(q[name], errors="coerce")
            if values.notna().all():
                out[name] = float(values.sum())
    return out


# ───────────────────────── summary ─────────────────────────

_INPUTS: dict[str, tuple[str, ...]] = {
    "roce": ("ebit", "total_assets", "current_liabilities"),
    "roe": ("pat", "total_equity"),
    "roic": ("ebit", "total_equity", "total_debt", "cash_and_equivalents",
             "non_operating_investments"),
    "opm": ("ebitda", "revenue"),
    "cfo_to_ebitda": ("cfo", "ebitda"),
    "cfo_to_pat": ("cfo", "pat"),
    "fcf": ("cfo", "purchase_of_fixed_assets", "sale_of_fixed_assets"),
    "other_income_share": ("other_income", "pbt"),
    "debt_to_equity": ("total_debt", "total_equity"),
    "net_debt_to_ebitda": ("total_debt", "cash_and_equivalents", "ebitda"),
    "interest_coverage": ("ebit", "interest"),
    "debtor_days": ("receivables", "revenue"),
    "inventory_days": ("inventory", "cogs"),
    "payable_days": ("payables", "cogs"),
    "ccc_days": ("receivables", "revenue", "inventory", "cogs", "payables"),
    "capex_intensity": ("purchase_of_fixed_assets", "sale_of_fixed_assets", "cfo"),
    "accruals_ratio": ("pat", "cfo", "total_assets"),
}  # fmt: skip


def _missing(df: pd.DataFrame, fields: Iterable[str], years: Iterable[int]) -> str:
    gaps = []
    for f in fields:
        col = _col(df, f)
        absent = [y for y in years if y not in col.index or pd.isna(col.get(y))]
        if absent:
            gaps.append(f"{f} ({', '.join(f'FY{y}' for y in absent)})")
    return "missing " + "; ".join(gaps) if gaps else "not meaningful (non-positive denominator)"


# Metrics built on an average of opening and closing balances need the previous year too.
_USES_OPENING = frozenset({"roce", "roe", "roic", "accruals_ratio"})


def _latest(df: pd.DataFrame, m: pd.DataFrame, name: str, year: int) -> Metric:
    v = m[name].get(year)
    if v is None or pd.isna(v):
        years = [year - 1, year] if name in _USES_OPENING else [year]
        return Metric(None, _missing(df, _INPUTS[name], years))
    return Metric(float(v))


def _window_mean(df: pd.DataFrame, m: pd.DataFrame, name: str, year: int, n: int) -> Metric:
    years = list(range(year - n + 1, year + 1))
    values = m[name].reindex(years)
    if values.isna().any():
        return Metric(None, _missing(df, _INPUTS[name], [year - n, *years]))
    return Metric(float(values.mean()))


def _cumulative(df: pd.DataFrame, num: pd.Series, den: pd.Series, fields: tuple[str, ...],
                year: int, n: int) -> Metric:  # fmt: skip
    years = list(range(year - n + 1, year + 1))
    a, b = num.reindex(years), den.reindex(years)
    if a.isna().any() or b.isna().any():
        return Metric(None, _missing(df, fields, years))
    total = float(b.sum())
    if total <= 0:
        return Metric(None, "not meaningful (non-positive denominator)")
    return Metric(float(a.sum()) / total)


def _cagr(df: pd.DataFrame, field: str, year: int, n: int) -> Metric:
    s = _col(df, field)
    end, start = s.get(year), s.get(year - n)
    v = cagr(end, start, n)
    if v is None:
        if end is None or start is None or pd.isna(end) or pd.isna(start):
            return Metric(None, _missing(df, (field,), [year - n, year]))
        return Metric(None, f"{field} not positive at both ends")
    return Metric(v)


def summary_metrics(
    annual: pd.DataFrame,
    quarterly: pd.DataFrame | None,
    params: FundamentalsConfig,
    *,
    tax_rate_fallback: float,
    year: int | None = None,
) -> dict[str, Metric]:
    """Headline metrics as of fiscal ``year`` (default: the latest year in ``annual``)."""
    df = by_year(annual)
    if df.empty:
        return {}
    y = int(year if year is not None else df.index.max())
    m = annual_metrics(annual, tax_rate_fallback=tax_rate_fallback, days=params.days_in_year)
    n = params.cumulative_years
    out: dict[str, Metric] = {}

    for name in ("roce", "roe", "roic", "opm", "cfo_to_ebitda", "cfo_to_pat", "other_income_share",
                 "debt_to_equity", "net_debt_to_ebitda", "interest_coverage", "debtor_days",
                 "inventory_days", "payable_days", "ccc_days", "accruals_ratio"):  # fmt: skip
        out[f"{name}_latest" if name in ("roce", "roe", "roic", "opm") else name] = _latest(
            df, m, name, y
        )
    for name in ("roce", "roe", "opm"):
        out[f"{name}_{n}y_avg"] = _window_mean(df, m, name, y, n)

    cfo, ebitda, pat = _col(df, "cfo"), _col(df, "ebitda"), _col(df, "pat")
    out[f"cfo_to_ebitda_{n}y"] = _cumulative(df, cfo, ebitda, ("cfo", "ebitda"), y, n)
    out[f"cfo_to_pat_{n}y"] = _cumulative(df, cfo, pat, ("cfo", "pat"), y, n)
    out[f"fcf_conversion_{n}y"] = _cumulative(
        df, m["fcf"], pat, ("cfo", "purchase_of_fixed_assets", "sale_of_fixed_assets", "pat"), y, n
    )
    out[f"capex_intensity_{n}y"] = _cumulative(
        df, m["capex"], cfo, _INPUTS["capex_intensity"], y, n
    )
    for k in params.cagr_years:
        out[f"sales_cagr_{k}y"] = _cagr(df, "revenue", y, k)
        out[f"ebitda_cagr_{k}y"] = _cagr(df, "ebitda", y, k)
        out[f"eps_cagr_{k}y"] = _cagr(df, "eps_diluted", y, k)
    out[f"dilution_{params.dilution_years}y"] = _cagr(
        df, "shares_diluted_cr", y, params.dilution_years
    )

    t = ttm(quarterly) if quarterly is not None else dict.fromkeys(TTM_FIELDS)
    for name in ("revenue", "ebitda", "pat", "eps_diluted"):
        v = t.get(name)
        out[f"{name}_ttm"] = Metric(v, None if v is not None else "needs 4 consecutive quarters")
    rev, ebd = t.get("revenue"), t.get("ebitda")
    out["opm_ttm"] = (
        Metric(ebd / rev) if rev and ebd is not None and rev > 0
        else Metric(None, "needs TTM revenue and EBITDA")
    )  # fmt: skip
    return out
