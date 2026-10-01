"""Synthetic but internally consistent companies — for tests and the local demo (never real data).

``seed_company`` stores an instrument with ~3.5 years of adjusted prices, ten fiscal years of
consolidated annual statements (FY ends 31 March, announced mid-May), twelve quarters and two
shareholding filings. Revenue grows ``growth`` a year at a 20% EBIT margin; the share price is
scaled to trade around ``pe`` x EPS so the bands and the DCF land in a sensible range.
"""

from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import StatementType
from app.db.models import FinAnnual, FinQuarterly, Instrument, PriceDaily, Shareholding
from app.db.upsert import upsert

SHARES_CR = 10.0


def synthetic_daily(n_days: int = 900, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2021-01-01", periods=n_days)
    close = 100 * np.cumprod(1 + rng.normal(0.0008, 0.018, n_days))
    spread = close * rng.uniform(0.005, 0.03, n_days)
    return pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.005, n_days)),
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": rng.integers(10_000, 50_000, n_days).astype(float),
        },
        index=days,
    )


def ensure_instrument(
    db: Session, symbol: str, sector: str | None, *, is_index: bool = False, name: str | None = None
) -> int:
    upsert(
        db,
        Instrument,
        [
            {
                "symbol": symbol,
                "name": name or f"{symbol} Ltd",
                "sector": sector,
                "source": "nse",
                "is_index": is_index,
            }
        ],
    )
    iid = db.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
    if iid is None:  # pragma: no cover - upsert just wrote it
        raise LookupError(symbol)
    return iid


def store_prices(
    db: Session, iid: int, daily: pd.DataFrame, *, adjusted: bool = True, source: str = "fyers"
) -> None:
    ohlcv = daily[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float).tolist()
    rows = []
    for d, (o, h, lo, c, v) in zip(pd.DatetimeIndex(daily.index), ohlcv, strict=True):
        adj = adjusted
        rows.append(
            {
                "instrument_id": iid,
                "date": d.date(),
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": int(v),
                "adj_factor": 1.0 if adj else None,
                "adj_open": o if adj else None,
                "adj_high": h if adj else None,
                "adj_low": lo if adj else None,
                "adj_close": c if adj else None,
                "adj_volume": int(v) if adj else None,
                "source": source,
            }
        )
    upsert(db, PriceDaily, rows)


def seed_index(
    db: Session, symbol: str = "NIFTY500", seed: int = 5, *, source: str = "fyers"
) -> None:
    daily = synthetic_daily(seed=seed)
    store_prices(db, ensure_instrument(db, symbol, None, is_index=True), daily * 100, source=source)


def annual_rows(
    iid: int, growth: float, *, first_fy: int = 2015, years: int = 10
) -> list[dict[str, Any]]:
    rows = []
    for i in range(years):
        fy = first_fy + i
        rev = 1000.0 * (1 + growth) ** i
        ebit = 0.20 * rev
        dep = 0.03 * rev
        interest = 5.0
        pbt = ebit - interest
        tax = 0.25 * pbt
        pat = pbt - tax
        equity = 1500.0 + 300.0 * i
        rows.append(
            {
                "instrument_id": iid,
                "statement_type": StatementType.CONSOLIDATED,
                "period_end": date(fy, 3, 31),
                "fiscal_year": fy,
                "announcement_date": date(fy, 5, 15),
                "revenue": rev,
                "cogs": 0.55 * rev,
                "ebitda": ebit + dep,
                "other_income": 0.05 * pbt,
                "depreciation": dep,
                "ebit": ebit,
                "interest": interest,
                "pbt": pbt,
                "tax": tax,
                "pat": pat,
                "sga": 0.12 * rev,
                "minority_interest_pl": 0.0,
                "eps_diluted": pat / SHARES_CR,
                "shares_diluted_cr": SHARES_CR,
                "total_assets": equity + 600.0,
                "current_assets": 0.45 * rev,
                "current_liabilities": 0.20 * rev,
                "total_equity": equity,
                "retained_earnings": equity - 100.0,
                "minority_interest_bs": 0.0,
                "total_debt": 50.0,
                "cash_and_equivalents": 200.0,
                "non_operating_investments": 100.0,
                "receivables": 0.15 * rev,
                "inventory": 0.05 * rev,
                "payables": 0.08 * rev,
                "net_block": 0.4 * rev,
                "book_value_per_share": equity / SHARES_CR,
                "cfo": 0.9 * (pat + dep),
                "purchase_of_fixed_assets": 0.04 * rev,
                "sale_of_fixed_assets": 0.0,
                "dividends_paid": 0.3 * pat,
                "extra": None,
                "source": "screener",
            }
        )
    return rows


def quarterly_rows(
    iid: int, annual: list[dict[str, Any]], *, quarters: int = 12
) -> list[dict[str, Any]]:
    """Quarters of the last three fiscal years; each quarter is a quarter of the year's P&L
    with a small intra-year ramp so YoY growth is visible."""
    rows = []
    for a in annual[-(quarters // 4) :]:
        fy = a["fiscal_year"]
        for k, (m, d) in enumerate(((6, 30), (9, 30), (12, 31), (3, 31))):
            year = fy - 1 if m != 3 else fy
            share = (0.23, 0.245, 0.255, 0.27)[k]
            rows.append(
                {
                    "instrument_id": iid,
                    "statement_type": StatementType.CONSOLIDATED,
                    "period_end": date(year, m, d),
                    "announcement_date": date(year + (1 if m == 12 else 0), (m % 12) + 1, 20)
                    if m != 3
                    else date(fy, 5, 15),
                    **{
                        f: (a[f] * share if a[f] is not None else None)
                        for f in (
                            "revenue",
                            "cogs",
                            "ebitda",
                            "other_income",
                            "depreciation",
                            "ebit",
                            "interest",
                            "pbt",
                            "tax",
                            "pat",
                            "eps_diluted",
                        )
                    },
                    "minority_interest_pl": 0.0,
                    "shares_diluted_cr": SHARES_CR,
                    "extra": None,
                    "source": "screener",
                }
            )
    return rows


def _shareholding_path(
    n: int, pledge: tuple[float, float]
) -> list[tuple[date, float, float, float, float]]:
    """``n`` quarter-ends up to 2024-03-31: FII and DII each +0.5 pp a quarter; the pledge is
    ``pledge[0]`` until the last quarter, which has ``pledge[1]``."""
    ends = pd.date_range(end="2024-03-31", periods=n, freq="QE")
    return [
        (
            e.date(),
            55.0,
            pledge[1] if k == n - 1 else pledge[0],
            20.5 - 0.5 * (n - 1 - k),
            12.5 - 0.5 * (n - 1 - k),
        )
        for k, e in enumerate(ends)
    ]


def seed_company(
    db: Session,
    symbol: str = "SYNTH",
    *,
    sector: str | None = "it_services",
    growth: float = 0.12,
    pe: float = 25.0,
    seed: int = 11,
    pledge: tuple[float, float] = (0.0, 0.0),
    shp_quarters: int = 2,
    name: str | None = None,
    price_source: str = "fyers",
) -> int:
    iid = ensure_instrument(db, symbol, sector, name=name)
    annual = annual_rows(iid, growth)
    upsert(db, FinAnnual, annual)
    upsert(db, FinQuarterly, quarterly_rows(iid, annual))
    upsert(
        db,
        Shareholding,
        [
            {
                "instrument_id": iid,
                "period_end": pe_,
                "filing_date": pe_ + timedelta(days=21),  # SEBI: within 21 days
                "promoter_pct": promoter,
                "promoter_pledge_pct": pl,
                "fii_pct": fii,
                "dii_pct": dii,
                "mf_pct": 8.0,
                "public_pct": 100 - promoter - fii - dii,
                "num_shareholders": 100_000,
                "source": "nse",
            }
            for pe_, promoter, pl, fii, dii in _shareholding_path(shp_quarters, pledge)
        ],
    )
    daily = synthetic_daily(seed=seed)
    eps = annual[-1]["eps_diluted"]
    scale = pe * eps / float(daily["close"].iloc[-1])
    scaled = daily.copy()
    for c in ("open", "high", "low", "close"):
        scaled[c] = daily[c] * scale
    store_prices(db, iid, scaled, source=price_source)
    return iid


def eps_now() -> float:
    a = annual_rows(0, 0.12)[-1]
    return float(a["eps_diluted"])
