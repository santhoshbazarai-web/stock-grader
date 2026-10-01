"""Shared scoring types and the piecewise-linear score map (SPEC §7.2). Pure functions."""

import math
from enum import StrEnum

import numpy as np

from app.core.config import PiecewiseLinearMap


class Grade(StrEnum):
    """Best first. Values match the config keys (``grade_cutoffs``, ``mos_by_grade``)."""

    A_PLUS = "A_plus"
    A = "A"
    B = "B"
    C = "C"
    D = "D"

    @property
    def label(self) -> str:
        return "A+" if self is Grade.A_PLUS else self.value

    @property
    def rank(self) -> int:
        """0 = best (A+) … 4 = worst (D)."""
        return list(Grade).index(self)


def worse(a: Grade, b: Grade) -> Grade:
    return a if a.rank >= b.rank else b


class Pillar(StrEnum):
    QUALITY = "quality"
    GROWTH = "growth"
    VALUATION = "valuation"
    HEALTH = "health"
    GOVERNANCE = "governance"
    TECHNICAL = "technical"


def map_score(points: PiecewiseLinearMap, x: float | None) -> float | None:
    """Linear between the map's points, clamped at the ends (±inf clamps too).

    ``None`` / NaN in → ``None`` out: a missing metric is never scored.
    """
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    xs = [p[0] for p in points.root]
    ys = [p[1] for p in points.root]
    if xs[0] > xs[-1]:  # decreasing map (lower metric scores higher): np.interp wants ascending
        xs, ys = xs[::-1], ys[::-1]
    return float(np.interp(x, xs, ys))
