"""Structural breaks (SPEC §4 "Structural breaks"). Pure functions.

A merger, demerger or large acquisition (``config/structural_events.yaml``) makes the company
before and after different businesses. Aggregate growth over a window that contains the break's
fiscal year compares the two (HDFCBANK FY24: PAT +42% from absorbing HDFC Ltd, while earnings
per share grew 2%), so such windows are measured **per share** instead:

- sales / EBITDA growth → sales / EBITDA per year-end share;
- EPS growth → profit per year-end share (the weighted-average EPS straddles the issue date);
- book value growth → equity per year-end share.

Year-end shares are the reported share count at the fiscal-year end. Without it, the
weighted-average diluted share count (PAT / EPS) is used and the note says so; without either,
the metric is None with the reason. Every switch is listed in the notes for the report.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

import pandas as pd

from app.core.config import StructuralEvent
from app.fundamentals.metrics import Metric, by_year, cagr, column

# growth metric prefix → the aggregate field it grows
_AGGREGATE = {"sales": "revenue", "ebitda": "ebitda", "eps": "pat"}


def break_year(event: StructuralEvent, fy_end_month: int = 3) -> int:
    """The fiscal year the break falls in (1 Jul 2023 → FY2024 for a March year end)."""
    d = event.date
    return d.year + 1 if d.month > fy_end_month else d.year


def crossing(events: Iterable[StructuralEvent], start_fy: int, end_fy: int,
             fy_end_month: int = 3) -> StructuralEvent | None:  # fmt: skip
    """The first break inside a growth window from ``start_fy`` to ``end_fy`` (the base year
    is before the break, the end year at or after it)."""
    for e in events:
        if start_fy < break_year(e, fy_end_month) <= end_fy:
            return e
    return None


@dataclass(frozen=True)
class PerShare:
    frame: pd.DataFrame  # by fiscal year: sales_ps, ebitda_ps, eps, bvps
    basis: str  # "year-end shares" | "weighted-average diluted shares"


def per_share(annual: pd.DataFrame, shares_year_end: Mapping[int, float] | None) -> PerShare:
    """Per-share series by fiscal year (₹ per share; shares in crore as in fin_annual)."""
    df = by_year(annual)
    ye = pd.Series({int(k): float(v) for k, v in (shares_year_end or {}).items()}, dtype=float)
    ye = ye.reindex(df.index)
    weighted = column(df, "shares_diluted_cr")
    if ye.isna().all():
        shares, basis = weighted, "weighted-average diluted shares"
    elif ye.notna().all():
        shares, basis = ye, "year-end shares"
    else:  # older years without a reported count: the weighted count (PAT / EPS) stands in
        shares, basis = ye.fillna(weighted), "year-end shares; weighted where not reported"
    shares = shares.where(shares > 0)
    out = pd.DataFrame(index=df.index)
    out["sales_ps"] = column(df, "revenue") / shares
    out["ebitda_ps"] = column(df, "ebitda") / shares
    out["eps"] = column(df, "pat") / shares
    out["bvps"] = column(df, "total_equity") / shares
    return PerShare(out, basis)


def _ps_cagr(ps: PerShare, col: str, year: int, n: int) -> Metric:
    s = ps.frame[col] if col in ps.frame.columns else pd.Series(dtype=float)
    end, start = s.get(year), s.get(year - n)
    v = cagr(end, start, n)
    if v is None:
        return Metric(None, f"per-share {col} not positive (or missing) in FY{year - n} / FY{year}")
    return Metric(v, f"per share ({ps.basis})")


def adjust_growth(
    metrics: Mapping[str, Metric],
    annual: pd.DataFrame,
    events: list[StructuralEvent],
    *,
    cagr_years: Iterable[int],
    shares_year_end: Mapping[int, float] | None = None,
    year: int | None = None,
    fy_end_month: int = 3,
) -> tuple[dict[str, Metric], list[str]]:
    """Replace the sales / EBITDA / EPS CAGRs whose window crosses a break with per-share CAGRs.
    Returns the metrics and the notes (one per break that changed something)."""
    out = dict(metrics)
    if not events or annual.empty:
        return out, []
    y = int(year if year is not None else by_year(annual).index.max())
    ps = per_share(annual, shares_year_end)
    col = {"sales": "sales_ps", "ebitda": "ebitda_ps", "eps": "eps"}
    switched: dict[date, list[str]] = {}
    by_date = {e.date: e for e in events}
    for k in cagr_years:
        e = crossing(events, y - k, y, fy_end_month)
        if e is None:
            continue
        for prefix in _AGGREGATE:
            key = f"{prefix}_cagr_{k}y"
            if key in out:
                out[key] = _ps_cagr(ps, col[prefix], y, k)
                switched.setdefault(e.date, []).append(key)
    notes = [
        f"Structural break FY{break_year(by_date[d], fy_end_month)} ({by_date[d].kind}): "
        f"{by_date[d].description}. Growth across it is per share ({ps.basis}): "
        f"{', '.join(keys)}"
        for d, keys in sorted(switched.items())
    ]
    return out, notes


def yoy_growth(
    annual: pd.DataFrame,
    field: str,
    year: int,
    events: list[StructuralEvent],
    *,
    shares_year_end: Mapping[int, float] | None = None,
    fy_end_month: int = 3,
) -> tuple[float | None, str | None]:
    """Year-on-year growth of ``field`` (revenue, ebitda, pat, total_equity) in ``year``; per
    share when the year is a break year. Returns (growth, note when per share)."""
    e = crossing(events, year - 1, year, fy_end_month)
    if e is None:
        s = column(by_year(annual), field)
        now, prev = s.get(year), s.get(year - 1)
        if now is None or prev is None or pd.isna(now) or pd.isna(prev) or prev <= 0:
            return None, None
        return float(now / prev - 1), None
    ps = per_share(annual, shares_year_end)
    name = {"revenue": "sales_ps", "ebitda": "ebitda_ps", "pat": "eps",
            "total_equity": "bvps"}.get(field)  # fmt: skip
    if name is None:
        return None, f"{field} growth in break year FY{year} has no per-share measure"
    s = ps.frame[name]
    now, prev = s.get(year), s.get(year - 1)
    if now is None or prev is None or pd.isna(now) or pd.isna(prev) or prev <= 0:
        return None, f"{field} per share missing for FY{year - 1} / FY{year}"
    return float(now / prev - 1), f"FY{year} {field} growth per share ({ps.basis}): {e.kind}"
