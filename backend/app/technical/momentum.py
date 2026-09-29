"""Momentum (SPEC §6): weekly RSI, distance from the 200-DMA as a z-score, 52-week-high
proximity. Pure functions.

    RSI            Wilder RSI(``rsi_period``) of weekly closes
    DMA z-score    (close - SMA_200) / stdev(close over the same 200 days)      (daily closes)
    52w proximity  close / max(weekly high over ``high_52w_weeks``) - 1         (<= 0)
"""

from dataclasses import dataclass

import pandas as pd

from app.core.config import TechnicalConfig
from app.technical.bars import rsi


@dataclass(frozen=True)
class Momentum:
    rsi: float | None
    dma_z: float | None
    high_52w: float | None
    from_52w_high: float | None  # 0 at the high, -0.1 = 10% below


def momentum(weekly: pd.DataFrame, daily_close: pd.Series, cfg: TechnicalConfig) -> Momentum:
    r = rsi(weekly["close"], cfg.rsi_period)
    rsi_last = None if r.empty or pd.isna(r.iloc[-1]) else float(r.iloc[-1])
    z = None
    if len(daily_close) >= cfg.dma_days:
        win = daily_close.iloc[-cfg.dma_days :].astype(float)
        sd = float(win.std(ddof=1))
        if sd > 0:
            z = (float(win.iloc[-1]) - float(win.mean())) / sd
    high = None
    prox = None
    if len(weekly) >= cfg.high_52w_weeks:
        high = float(weekly["high"].iloc[-cfg.high_52w_weeks :].max())
        prox = float(weekly["close"].iloc[-1]) / high - 1
    return Momentum(rsi_last, z, high, prox)
