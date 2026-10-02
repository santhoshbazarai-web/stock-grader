"""Ten-year fundamentals and the shareholding trend for the report page charts (SPEC §9).
Pure function over canonical frames; ``load_history`` is the DB read.

Series per fiscal year: sales, EBITDA, PAT, CFO, FCF (CFO - net capex), ROCE and the cash
conversion cycle, with the same definitions as ``fundamentals.metrics``. Banks and insurers
(sector model bank / insurance) get NII, PAT, ROE, ROA, loans (advances), deposits, the
credit-deposit ratio, credit cost and BVPS instead (``fundamentals.banking``); ``series`` names
the ones that apply and ``years_available`` how many fiscal years the window really has.
A year without an input stays ``None`` (a gap in the chart), never 0. The window is the longest
CAGR window in ``scoring.fundamentals.cagr_years``.
"""

from datetime import date

import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import AppConfig, SectorModel
from app.db.models import FinAnnual, Instrument
from app.fundamentals.banking import bank_frame, bank_metrics
from app.fundamentals.metrics import annual_metrics, by_year, column
from app.reports.data import (
    load_financials,
    load_overrides,
    load_shareholding,
    load_shares_year_end,
    no_promoter_note,
)

GENERAL_SERIES = ("revenue", "ebitda", "pat", "cfo", "fcf", "roce", "ccc_days")
BANK_SERIES = ("nii", "pat", "roe", "roa", "advances", "deposits", "cd_ratio", "credit_cost",
               "bvps")  # fmt: skip


class YearPoint(BaseModel):
    fiscal_year: int
    revenue: float | None
    ebitda: float | None
    pat: float | None
    cfo: float | None
    fcf: float | None
    roce: float | None = Field(description="Fraction")
    ccc_days: float | None
    # banks / insurers (fractions for ratios, ₹ crore for amounts, ₹ for BVPS)
    nii: float | None = None
    roe: float | None = None
    roa: float | None = None
    advances: float | None = None
    deposits: float | None = None
    cd_ratio: float | None = None
    credit_cost: float | None = None
    bvps: float | None = None


class ShareholdingPoint(BaseModel):
    period_end: date
    promoter_pct: float | None
    fii_pct: float | None
    dii_pct: float | None
    mf_pct: float | None = None
    public_pct: float | None
    promoter_pledge_pct: float | None
    filing_date: date | None = None  # when the pattern was filed (point in time, rule 4)
    source: str | None = None  # where it came from: nse, bse, screener...
    pledge_source: str | None = None  # pattern, NSE pledge disclosure, no promoter


class FundamentalsHistory(BaseModel):
    symbol: str
    statement_type: str | None
    source: str | None
    years: list[YearPoint]
    shareholding: list[ShareholdingPoint]
    missing: list[str] = Field(description="Series with no value in any year of the window")
    model: str = Field("general", description="general | bank (banks and insurers)")
    series: list[str] = Field(default_factory=list, description="The series that apply")
    years_available: int = Field(0, description="Fiscal years in the window with any value")
    shareholding_note: str | None = Field(None, description="e.g. no identified promoter")


def _num(v: object) -> float | None:
    if v is None:
        return None
    f = float(v)  # type: ignore[arg-type]
    return None if pd.isna(f) else f


def _date(v: object) -> date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return pd.Timestamp(v).date()  # type: ignore[arg-type]


def fundamentals_history(
    symbol: str,
    annual: pd.DataFrame,
    shareholding: pd.DataFrame,
    config: AppConfig,
    *,
    statement_type: str | None = None,
    source: str | None = None,
    bank: bool = False,
    shares_year_end: dict[int, float] | None = None,
) -> FundamentalsHistory:
    n = max(config.scoring.fundamentals.cagr_years)
    points: list[YearPoint] = []
    if not annual.empty and bank:
        df = by_year(annual)
        bm = bank_metrics(annual)
        frame = by_year(bank_frame(annual))
        ye = shares_year_end or {}
        weighted = column(df, "shares_diluted_cr")
        for y in list(df.index)[-n:]:
            row = df.loc[y]
            sh = ye.get(int(y)) or _num(weighted.get(y))
            eq = _num(row.get("total_equity"))

            def pct(col: str, yr: int = y) -> float | None:
                v = _num(bm[col].get(yr))
                return v / 100 if v is not None else None

            points.append(
                YearPoint(
                    fiscal_year=int(y), revenue=None, ebitda=None, pat=_num(row.get("pat")),
                    cfo=None, fcf=None, roce=None, ccc_days=None,
                    nii=_num(bm["nii"].get(y)), roe=pct("roe_pct"), roa=pct("roa_pct"),
                    advances=_num(frame["advances"].get(y)) if "advances" in frame else None,
                    deposits=_num(frame["deposits"].get(y)) if "deposits" in frame else None,
                    cd_ratio=pct("cd_ratio_pct"), credit_cost=pct("credit_cost_pct"),
                    bvps=eq / sh if eq is not None and sh else None,
                )
            )  # fmt: skip
    elif not annual.empty:
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
    fields = BANK_SERIES if bank else GENERAL_SERIES
    missing = [f for f in fields if all(getattr(p, f) is None for p in points)]
    with_data = sum(any(getattr(p, f) is not None for f in fields) for p in points)
    shp = [
        ShareholdingPoint(
            period_end=idx.date(),
            filing_date=_date(r.get("filing_date")),
            source=str(r["source"]) if isinstance(r.get("source"), str) else None,
            pledge_source=(
                str(r["pledge_source"]) if isinstance(r.get("pledge_source"), str) else None
            ),
            **{
                c: _num(r.get(c))
                for c in (
                    "promoter_pct",
                    "fii_pct",
                    "dii_pct",
                    "mf_pct",
                    "public_pct",
                    "promoter_pledge_pct",
                )
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
        model="bank" if bank else "general",
        series=list(fields),
        years_available=with_data,
        shareholding_note=no_promoter_note(shareholding.sort_index())
        if len(shareholding)
        else None,
    )


def load_history(session: Session, symbol: str, config: AppConfig) -> FundamentalsHistory | None:
    sym = symbol.upper()
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == sym))
    if iid is None:
        return None
    annual, basis, source = load_financials(session, FinAnnual, iid, "fin_annual")
    shp, _ = load_shareholding(session, iid, config)
    sector = load_overrides(session, iid).sector or session.scalar(
        select(Instrument.sector).where(Instrument.id == iid)
    )
    model = config.sectors.for_sector(sector).model
    bank = model in (SectorModel.BANK, SectorModel.INSURANCE)
    return fundamentals_history(
        sym, annual, shp, config, statement_type=basis, source=source, bank=bank,
        shares_year_end=load_shares_year_end(session, iid, annual) if bank else None,
    )  # fmt: skip
