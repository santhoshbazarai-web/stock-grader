"""Risk (SPEC §6): invalidation level and reward:risk. Pure functions.

invalidation = support low - ``invalidation_atr_buffer`` x weekly ATR
R:R to X     = (X - entry) / (entry - invalidation)
entry        = min(CMP, buy-zone high): buy now if already inside the zone, else at its top
"""


def invalidation(support_low: float, atr: float, buffer: float) -> float:
    return support_low - buffer * atr


def reward_risk(target: float | None, entry: float, stop: float) -> float | None:
    if target is None or entry <= stop:
        return None
    return (target - entry) / (entry - stop)
