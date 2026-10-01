"""Ten-year fundamentals and the shareholding trend for the report page charts (SPEC §9).
Pure function over canonical frames; ``load_history`` is the DB read.

Series per fiscal year: sales, EBITDA, PAT, CFO, FCF (CFO - net capex), ROCE and the cash
conversion cycle, with the same definitions as ``fundamentals.metrics``. A year without an
input stays ``None`` (a gap in the chart), never 0. The window is the longest CAGR window in
``scoring.fundamentals.cagr_years``.
"""

from datetime import date

import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import AppConfig
from app.db.models import FinAnnual, Instrument
from app.fundamentals.metrics import annual_metrics, by_year
from app.reports.data import load_financials, load_shareholding


class YearPoint(BaseModel):
    fiscal_year: int
    revenue: float | None
    ebitda: float | None
    pat: float | None
    cfo: float | None
    fcf: float | None
    roce: float | None = Field(description="Fraction")
    ccc_days: float | None


class ShareholdingPoint(BaseModel):
    period_end: date
    promoter_pct: float | None
    fii_pct: float | None
    dii_pct: float | None
    public_pct: float | None
    promoter_pledge_pct: float | None


class FundamentalsHistory(BaseModel):
    symbol: str
    statement_type: str | None
    source: str | None
    years: list[YearPoint]
    shareholding: list[ShareholdingPoint]
    missing: list[str] = Field(description="Series with no value in any year of the window")


def _num(v: object) -> float | None:
    if v is None:
        return None
    f = float(v)  # type: ignore[arg-type]
    return None if pd.isna(f) else f


def fundamentals_history(
    symbol: str,
    annual: pd.DataFrame,
    shareholding: pd.DataFrame,
    config: AppConfig,
    *,
    statement_type: str | None = None,
    source: str | None = None,
) -> FundamentalsHistory:
    n = max(config.scoring.fundamentals.cagr_years)
    points: list[YearPoint] = []
    if not annual.empty:
        df = by_year(annual)
        m = annual_metrics(
            annual,
            tax_rate_fallback=config.valuation.tax_rate_default,
            days=config.scoring.fundamentals.days_in_year,
        )
        for y in list(df.index)[-n:]:
            row = df.loc[y]
            points.append(
                YearPoint(
                    fiscal_year=int(y),
                    revenue=_num(row.get("revenue")),
                    ebitda=_num(row.get("ebitda")),
                    pat=_num(row.get("pat")),
                    cfo=_num(row.get("cfo")),
                    fcf=_num(m["fcf"].get(y)) if "fcf" in m else None,
                    roce=_num(m["roce"].get(y)) if "roce" in m else None,
                    ccc_days=_num(m["ccc_days"].get(y)) if "ccc_days" in m else None,
                )
            )
    fields = ("revenue", "ebitda", "pat", "cfo", "fcf", "roce", "ccc_days")
    missing = [f for f in fields if all(getattr(p, f) is None for p in points)]
    shp = [
        ShareholdingPoint(
            period_end=idx.date(),
            **{
                c: _num(r.get(c))
                for c in ("promoter_pct", "fii_pct", "dii_pct", "public_pct", "promoter_pledge_pct")
            },
        )
        for idx, (_, r) in zip(
            pd.DatetimeIndex(shareholding.sort_index().index),
            shareholding.sort_index().iterrows(),
            strict=True,
        )
    ]
    return FundamentalsHistory(
        symbol=symbol,
        statement_type=statement_type,
        source=source,
        years=points,
        shareholding=shp,
        missing=missing,
    )


def load_history(session: Session, symbol: str, config: AppConfig) -> FundamentalsHistory | None:
    sym = symbol.upper()
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == sym))
    if iid is None:
        return None
    annual, basis, source = load_financials(session, FinAnnual, iid, "fin_annual")
    shp, _ = load_shareholding(session, iid)
    return fundamentals_history(sym, annual, shp, config, statement_type=basis, source=source)
