"""Data depth (SPEC §7.3): how many fiscal years of P&L the report stands on. Pure.

- ``full``: at least ``data_depth.full_min_years`` (8) years: the grade as usual.
- ``provisional``: at least ``provisional_min_years`` (3): the grade is labelled provisional
  and its confidence is reduced (``grade_confidence``), the valuation confidence one level lower.
- ``technical_only``: fewer: the fundamentals pillars and valuation have too little history;
  the technical analysis stands, the grade is provisional with reduced confidence.

A P&L year is a fiscal year with revenue or PAT (summed-quarter years included).
"""

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from app.core.config import DataDepthConfig

Level = Literal["technical_only", "provisional", "full"]


@dataclass(frozen=True)
class DataDepth:
    level: Level
    pl_years: int
    reason: str

    @property
    def full(self) -> bool:
        return self.level == "full"


def pl_years(annual: pd.DataFrame) -> int:
    cols = [c for c in ("revenue", "pat") if c in annual.columns]
    if annual.empty or not cols:
        return 0
    has = annual[cols].apply(pd.to_numeric, errors="coerce").notna().any(axis=1)
    if "fiscal_year" in annual.columns:
        return int(annual.loc[has, "fiscal_year"].nunique())
    return int(has.sum())


def data_depth(annual: pd.DataFrame, cfg: DataDepthConfig) -> DataDepth:
    n = pl_years(annual)
    if n >= cfg.full_min_years:
        return DataDepth("full", n, f"{n} fiscal years of P&L (full needs {cfg.full_min_years})")
    if n >= cfg.provisional_min_years:
        why = (f"only {n} fiscal years of P&L (full needs {cfg.full_min_years}): grade "
               "provisional, reduced confidence")  # fmt: skip
        return DataDepth("provisional", n, why)
    why = (f"only {n} fiscal year(s) of P&L (provisional needs {cfg.provisional_min_years}, "
           f"full {cfg.full_min_years}): technical analysis only; grade provisional, reduced "
           "confidence")  # fmt: skip
    return DataDepth("technical_only", n, why)
