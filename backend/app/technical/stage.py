"""Weinstein stage (SPEC §6) from the 30-week SMA slope, price position and volume.

    slope   = SMA_t / SMA_{t-k} - 1             k = stage_slope_weeks; flat if |slope| <= f
    prior   = SMA_{t-k} / SMA_{t-k-p} - 1       p = stage_prior_weeks
    Stage 2 (advancing)  rising SMA and close above it
    Stage 4 (declining)  falling SMA and close below it
    Stage 1 (basing)     otherwise, after a decline (prior slope falling), or a falling SMA with
                         price already above it (early base / reversal attempt)
    Stage 3 (topping)    otherwise, after an advance (prior slope rising), or a rising SMA with
                         price below it (distribution / failing trend)
    Flat SMA and flat prior: stage 1 if price is above the SMA, else stage 3.

Volume: a close above the SMA whose week volume >= breakout multiple x the average of the prior
``stage_volume_avg_weeks`` is reported as a volume-confirmed stage-2 breakout.
"""

from dataclasses import dataclass, field

import pandas as pd

from app.core.config import TechnicalConfig
from app.technical.bars import sma


@dataclass(frozen=True)
class StageResult:
    stage: int | None
    sma: float | None
    sma_slope: float | None
    prior_slope: float | None
    price_vs_sma: float | None
    breakout_volume_confirmed: bool = False
    reasons: list[str] = field(default_factory=list)


def weinstein_stage(weekly: pd.DataFrame, cfg: TechnicalConfig) -> StageResult:
    n, k, p = cfg.stage_sma_weeks, cfg.stage_slope_weeks, cfg.stage_prior_weeks
    need = n + k + p
    if len(weekly) < need:
        return StageResult(
            None,
            None,
            None,
            None,
            None,
            reasons=[f"stage needs {need} weekly bars, have {len(weekly)}"],
        )
    avg = sma(weekly["close"], n)
    now, before, earlier = avg.iloc[-1], avg.iloc[-1 - k], avg.iloc[-1 - k - p]
    slope, prior = now / before - 1, before / earlier - 1
    close = float(weekly["close"].iloc[-1])
    above = close > now
    flat = cfg.stage_flat_slope_pct
    rising, falling = slope > flat, slope < -flat

    if rising and above:
        stage, why = 2, "rising 30w SMA, price above"
    elif falling and not above:
        stage, why = 4, "falling 30w SMA, price below"
    elif falling and above:
        stage, why = 1, "SMA still falling but price reclaimed it (basing)"
    elif rising and not above:
        stage, why = 3, "SMA still rising but price lost it (topping)"
    elif prior < -flat:
        stage, why = 1, "SMA flattening after a decline (basing)"
    elif prior > flat:
        stage, why = 3, "SMA flattening after an advance (topping)"
    else:
        stage, why = (1, "flat SMA, price above") if above else (3, "flat SMA, price below")

    vol = weekly["volume"].astype(float)
    avg_vol = vol.iloc[-1 - cfg.stage_volume_avg_weeks : -1].mean()
    confirmed = bool(
        stage == 2 and avg_vol > 0 and vol.iloc[-1] >= cfg.stage_breakout_volume_mult * avg_vol
    )
    reasons = [
        f"stage {stage}: {why} (slope {slope:+.1%} over {k}w, price {close / now - 1:+.1%} vs SMA)"
    ]
    if confirmed:
        reasons.append("breakout week on high volume")
    return StageResult(
        stage, float(now), float(slope), float(prior), close / now - 1, confirmed, reasons
    )
