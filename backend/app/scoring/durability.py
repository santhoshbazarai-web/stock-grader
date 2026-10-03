"""Durability: a PROXY for how durable a business's returns look, computed only from reported
data. It is not an analyst moat rating and says nothing about competitive position.

Tests (``scoring.durability`` in config/scoring.yaml), each pass / fail / no data:

    general   median ROE, median ROCE, years ROE beat the cost of equity, stability (std-dev)
              of the operating margin and of ROE, share of years PAT grew, leverage trend
    banks     median ROE, years ROE beat the cost of equity, ROE stability, median ROA, ROA
              stability, share of PAT-growth years, deposit and loan growth consistency

A test without enough history has ``passed = None``: it lowers the confidence and is left out
of the score, never counted as a fail (rule 1). The rating is Strong / Moderate / Weak from the
share of tests with data that pass; with too few tests answering there is no rating at all.
Pure function over per-fiscal-year series.
"""

# ruff: noqa: E501
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from app.core.config import DurabilityConfig

Rating = Literal["strong", "moderate", "weak"]
Confidence = Literal["high", "medium", "low"]
LABEL = "Durability (proxy from reported data, not an analyst moat rating)"


@dataclass
class DurabilityInputs:
    """Per-fiscal-year series indexed by year (fractions for ratios); ``None`` = not available."""

    is_bank: bool
    trend_years: int
    cost_of_equity: float | None = None
    roe: pd.Series | None = None
    roce: pd.Series | None = None
    opm: pd.Series | None = None
    pat: pd.Series | None = None
    debt_to_equity: pd.Series | None = None
    roa: pd.Series | None = None
    deposits: pd.Series | None = None
    advances: pd.Series | None = None


@dataclass
class DurabilityTest:
    key: str
    label: str
    passed: bool | None
    detail: str


@dataclass
class Durability:
    rating: Rating | None
    score: float | None
    confidence: Confidence | None
    tests: list[DurabilityTest]
    reasons: list[str] = field(default_factory=list)
    label: str = LABEL


def _clean(s: pd.Series | None, n: int) -> pd.Series:
    if s is None:
        return pd.Series(dtype=float)
    return s.replace([np.inf, -np.inf], np.nan).dropna().sort_index().iloc[-n:]


def _need(name: str, s: pd.Series, cfg: DurabilityConfig) -> str | None:
    if len(s) < cfg.min_years:
        return f"needs {cfg.min_years} years of {name}; {len(s)} stored"
    return None


def _test(key: str, label: str, passed: bool | None, detail: str) -> DurabilityTest:
    return DurabilityTest(key, label, passed, detail)


def _median(key: str, label: str, name: str, s: pd.Series | None, floor: float,
            cfg: DurabilityConfig) -> DurabilityTest:  # fmt: skip
    c = _clean(s, cfg.window_years)
    if (why := _need(name, c, cfg)) is not None:
        return _test(key, label, None, why)
    m = float(c.median())
    return _test(key, label, m >= floor, f"median {m:.1%} over {len(c)} years (needs {floor:.0%})")


def _stability(key: str, label: str, name: str, s: pd.Series | None, ceiling: float,
               cfg: DurabilityConfig) -> DurabilityTest:  # fmt: skip
    c = _clean(s, cfg.window_years)
    if (why := _need(name, c, cfg)) is not None:
        return _test(key, label, None, why)
    sd = float(c.std(ddof=0))
    return _test(
        key, label, sd <= ceiling, f"std-dev {sd:.1%} over {len(c)} years (max {ceiling:.1%})"
    )


def _above_ke(inp: DurabilityInputs, cfg: DurabilityConfig) -> DurabilityTest:
    label = "Years ROE above cost of equity"
    if inp.cost_of_equity is None:
        return _test("roe_above_ke", label, None, "no cost of equity in this report")
    c = _clean(inp.roe, cfg.window_years)
    if (why := _need("ROE", c, cfg)) is not None:
        return _test("roe_above_ke", label, None, why)
    n = int((c > inp.cost_of_equity).sum())
    share = n / len(c)
    return _test("roe_above_ke", label, share >= cfg.years_above_ke_min_share,
                 f"{n} of {len(c)} years above {inp.cost_of_equity:.1%} (needs {cfg.years_above_ke_min_share:.0%})")  # fmt: skip


def _profit_growth(inp: DurabilityInputs, cfg: DurabilityConfig) -> DurabilityTest:
    label = "Years of profit growth"
    c = _clean(inp.pat, cfg.window_years + 1)
    if (why := _need("PAT", c, cfg)) is not None:
        return _test("profit_growth", label, None, why)
    up = int((c.diff().dropna() > 0).sum())
    steps = len(c) - 1
    return _test("profit_growth", label, up / steps >= cfg.profit_growth_min_share,
                 f"PAT grew in {up} of {steps} years (needs {cfg.profit_growth_min_share:.0%})")  # fmt: skip


def _leverage(inp: DurabilityInputs, cfg: DurabilityConfig) -> DurabilityTest:
    label = "Leverage trend"
    c = _clean(inp.debt_to_equity, cfg.window_years)
    if len(c) < inp.trend_years + 1:
        return _test("leverage_trend", label, None,
                     f"needs {inp.trend_years + 1} years of debt/equity; {len(c)} stored")  # fmt: skip
    now, then = float(c.iloc[-1]), float(c.iloc[-1 - inp.trend_years])
    ok = now - then <= cfg.leverage_increase_max and now <= cfg.leverage_max
    return _test("leverage_trend", label, ok,
                 f"debt/equity {then:.2f} to {now:.2f} over {inp.trend_years} years "
                 f"(max rise {cfg.leverage_increase_max:.2f}, max level {cfg.leverage_max:.2f})")  # fmt: skip


def _growth_consistency(key: str, label: str, name: str, level: pd.Series | None,
                        cfg: DurabilityConfig) -> DurabilityTest:  # fmt: skip
    c = _clean(level, cfg.window_years + 1)
    if (why := _need(name, c, cfg)) is not None:
        return _test(key, label, None, why)
    g = (c / c.shift(1) - 1).dropna()
    ok = int((g >= cfg.growth_consistency_min).sum())
    return _test(key, label, ok / len(g) >= cfg.growth_consistency_min_share,
                 f"grew at least {cfg.growth_consistency_min:.0%} in {ok} of {len(g)} years "
                 f"(needs {cfg.growth_consistency_min_share:.0%} of years)")  # fmt: skip


def durability(inp: DurabilityInputs, cfg: DurabilityConfig) -> Durability:
    if inp.is_bank:
        tests = [
            _median("roe_median", "Median ROE", "ROE", inp.roe, cfg.roe_median_min, cfg),
            _above_ke(inp, cfg),
            _stability("roe_stability", "ROE stability", "ROE", inp.roe, cfg.roe_std_max, cfg),
            _median("roa_median", "Median ROA", "ROA", inp.roa, cfg.bank_roa_median_min, cfg),
            _stability("roa_stability", "ROA stability", "ROA", inp.roa, cfg.bank_roa_std_max, cfg),
            _profit_growth(inp, cfg),
            _growth_consistency("deposit_growth", "Deposit growth consistency", "deposits", inp.deposits, cfg),
            _growth_consistency("loan_growth", "Loan growth consistency", "loans", inp.advances, cfg),
        ]  # fmt: skip
    else:
        tests = [
            _median("roe_median", "Median ROE", "ROE", inp.roe, cfg.roe_median_min, cfg),
            _median("roce_median", "Median ROCE", "ROCE", inp.roce, cfg.roce_median_min, cfg),
            _above_ke(inp, cfg),
            _stability("margin_stability", "Operating-margin stability", "operating margin", inp.opm, cfg.margin_std_max, cfg),
            _stability("roe_stability", "ROE stability", "ROE", inp.roe, cfg.roe_std_max, cfg),
            _profit_growth(inp, cfg),
            _leverage(inp, cfg),
        ]  # fmt: skip
    have = [t for t in tests if t.passed is not None]
    coverage = len(have) / len(tests)
    reasons = [f"{'pass' if t.passed else 'fail' if t.passed is False else 'no data'}: {t.label}: {t.detail}" for t in tests]  # fmt: skip
    if coverage < cfg.min_coverage:
        reasons.append(f"only {len(have)} of {len(tests)} tests have data: no rating")
        return Durability(None, None, None, tests, reasons)
    score = sum(1 for t in have if t.passed) / len(have)
    rating: Rating = "strong" if score >= cfg.strong_min_score else "weak" if score < cfg.weak_max_score else "moderate"  # fmt: skip
    conf: Confidence = "high" if coverage >= cfg.confidence_high else "medium" if coverage >= cfg.confidence_medium else "low"  # fmt: skip
    reasons.append(f"{sum(1 for t in have if t.passed)} of {len(have)} tests with data pass; {len(have)} of {len(tests)} tests had data (confidence {conf})")  # fmt: skip
    return Durability(rating, score, conf, tests, reasons)
