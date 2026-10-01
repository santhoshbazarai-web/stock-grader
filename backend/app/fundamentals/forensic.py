"""Forensic scores (SPEC §4): Piotroski F, Beneish M, Altman Z''. Pure functions.

All three compare fiscal year ``t`` with ``t-1`` (Piotroski and Beneish) or use year ``t``
(Altman), from a canonical ``fin_annual`` frame. The model coefficients below *define* the
published models and are kept with the formulas; the thresholds used to *judge* the results
(Beneish flag, Altman zones) come from ``scoring.forensic`` in config.

A score is ``None`` when any input is missing, with the missing inputs listed — a partial
Piotroski score or an M-score with a variable dropped would not be comparable.

Input approximations (canonical fields available from Indian filings):
- Piotroski leverage uses total debt / total assets (long-term debt is not split out).
- Beneish LVGI uses (current liabilities + total debt) / total assets as in the original, so
  short-term borrowings sit in both terms; being a year-on-year ratio, most of that cancels.
- Beneish AQI treats ``non_operating_investments`` as "securities" and ``net_block`` as PP&E.
- Altman X4 uses book equity / (total assets - total equity), so minority interest counts
  as a liability (conservative).
"""

from dataclasses import dataclass, field

import pandas as pd

from app.core.config import ForensicConfig
from app.fundamentals.metrics import by_year

# ───────────────────────── model definitions ─────────────────────────

# Beneish (1999), 8-variable model.
BENEISH_INTERCEPT = -4.84
BENEISH_COEF = {
    "dsri": 0.920,
    "gmi": 0.528,
    "aqi": 0.404,
    "sgi": 0.892,
    "depi": 0.115,
    "sgai": -0.172,
    "tata": 4.679,
    "lvgi": -0.327,
}
# Altman (1995/2005) Z'' for non-manufacturers / emerging markets (without the 3.25 constant
# of the "EM score"; zones in config are for this form).
ALTMAN_COEF = {"x1": 6.56, "x2": 3.26, "x3": 6.72, "x4": 1.05}


@dataclass(frozen=True)
class ScoreResult:
    value: float | None
    components: dict[str, float | bool | None] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    flag: str | None = None  # interpretation, e.g. "manipulation risk", "distress"
    reasons: list[str] = field(default_factory=list)


class _Year:
    """Accessor for one fiscal year's canonical values (``None`` when missing)."""

    def __init__(self, df: pd.DataFrame, year: int, missing: list[str]) -> None:
        self._row = df.loc[year] if year in df.index else None
        self._year = year
        self._missing = missing

    def __call__(self, name: str) -> float | None:
        if self._row is None or name not in self._row.index or pd.isna(self._row[name]):
            tag = f"{name} (FY{self._year})"
            if tag not in self._missing:
                self._missing.append(tag)
            return None
        return float(self._row[name])


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _years(annual: pd.DataFrame, year: int | None) -> tuple[pd.DataFrame, int]:
    df = by_year(annual)
    if df.empty:
        raise ValueError("no annual data")
    return df, int(year if year is not None else df.index.max())


# ───────────────────────── Piotroski ─────────────────────────


def piotroski(annual: pd.DataFrame, year: int | None = None) -> ScoreResult:
    """Piotroski F-score (0-9) for fiscal ``year`` vs the year before; ROA and asset turnover use
    opening total assets as in the original paper (so year t-2 assets are needed too)."""
    df, y = _years(annual, year)
    missing: list[str] = []
    cur, prev, prev2 = _Year(df, y, missing), _Year(df, y - 1, missing), _Year(df, y - 2, missing)

    pat = cur("pat")
    roa = _div(pat, prev("total_assets"))
    roa_prev = _div(prev("pat"), prev2("total_assets"))
    cfo = cur("cfo")
    lev = _div(cur("total_debt"), cur("total_assets"))
    lev_prev = _div(prev("total_debt"), prev("total_assets"))
    cr = _div(cur("current_assets"), cur("current_liabilities"))
    cr_prev = _div(prev("current_assets"), prev("current_liabilities"))
    shares, shares_prev = cur("shares_diluted_cr"), prev("shares_diluted_cr")

    def gm(get: _Year) -> float | None:
        rev, cogs = get("revenue"), get("cogs")
        return None if rev is None or cogs is None or rev <= 0 else (rev - cogs) / rev

    gm_cur, gm_prev = gm(cur), gm(prev)
    turn = _div(cur("revenue"), prev("total_assets"))
    turn_prev = _div(prev("revenue"), prev2("total_assets"))

    tests: dict[str, float | bool | None] = {
        "roa_positive": None if roa is None else roa > 0,
        "cfo_positive": None if cfo is None else cfo > 0,
        "roa_improving": None if roa is None or roa_prev is None else roa > roa_prev,
        "cfo_exceeds_pat": None if cfo is None or pat is None else cfo > pat,
        "leverage_falling": None if lev is None or lev_prev is None else lev < lev_prev,
        "current_ratio_rising": None if cr is None or cr_prev is None else cr > cr_prev,
        "no_dilution": None if shares is None or shares_prev is None else shares <= shares_prev,
        "gross_margin_rising": None if gm_cur is None or gm_prev is None else gm_cur > gm_prev,
        "asset_turnover_rising": (None if turn is None or turn_prev is None else turn > turn_prev),
    }
    if any(v is None for v in tests.values()):
        undetermined = [k for k, v in tests.items() if v is None]
        return ScoreResult(
            None, tests, missing, reasons=[f"undetermined tests: {', '.join(undetermined)}"]
        )
    score = float(sum(bool(v) for v in tests.values()))
    passed = [k for k, v in tests.items() if v]
    return ScoreResult(score, tests, [], reasons=[f"F={score:.0f}/9; passed: {', '.join(passed)}"])


# ───────────────────────── Beneish ─────────────────────────


def beneish(annual: pd.DataFrame, config: ForensicConfig, year: int | None = None) -> ScoreResult:
    """Beneish M-score for fiscal ``year`` vs the year before. Above
    ``config.beneish_flag_above`` → flagged as a manipulation risk."""
    df, y = _years(annual, year)
    missing: list[str] = []
    t, p = _Year(df, y, missing), _Year(df, y - 1, missing)

    def gm(get: _Year) -> float | None:
        rev, cogs = get("revenue"), get("cogs")
        return None if rev is None or cogs is None or rev == 0 else (rev - cogs) / rev

    def hard_assets_share(get: _Year) -> float | None:
        ta = get("total_assets")
        parts = [get("current_assets"), get("net_block"), get("non_operating_investments")]
        if ta is None or ta == 0 or any(x is None for x in parts):
            return None
        return 1 - sum(x for x in parts if x is not None) / ta

    def dep_rate(get: _Year) -> float | None:
        dep, ppe = get("depreciation"), get("net_block")
        return None if dep is None or ppe is None else _div(dep, dep + ppe)

    def leverage(get: _Year) -> float | None:
        cl, debt, ta = get("current_liabilities"), get("total_debt"), get("total_assets")
        return None if cl is None or debt is None else _div(cl + debt, ta)

    idx = {
        "dsri": _div(_div(t("receivables"), t("revenue")), _div(p("receivables"), p("revenue"))),
        "gmi": _div(gm(p), gm(t)),
        "aqi": _div(hard_assets_share(t), hard_assets_share(p)),
        "sgi": _div(t("revenue"), p("revenue")),
        "depi": _div(dep_rate(p), dep_rate(t)),
        "sgai": _div(_div(t("sga"), t("revenue")), _div(p("sga"), p("revenue"))),
        "tata": None,
        "lvgi": _div(leverage(t), leverage(p)),
    }
    pat, cfo = t("pat"), t("cfo")
    idx["tata"] = None if pat is None or cfo is None else _div(pat - cfo, t("total_assets"))
    components: dict[str, float | bool | None] = dict(idx)
    if any(v is None for v in idx.values()):
        return ScoreResult(None, components, missing, reasons=["incomplete Beneish inputs"])
    m = BENEISH_INTERCEPT + sum(BENEISH_COEF[k] * v for k, v in idx.items() if v is not None)
    flagged = m > config.beneish_flag_above
    return ScoreResult(
        m,
        components,
        [],
        "manipulation risk" if flagged else None,
        [f"M={m:.2f} {'>' if flagged else '<='} {config.beneish_flag_above} flag threshold"],
    )


# ───────────────────────── Altman ─────────────────────────


def altman_z2(
    annual: pd.DataFrame,
    config: ForensicConfig,
    year: int | None = None,
    *,
    is_financial: bool = False,
) -> ScoreResult:
    """Altman Z'' (emerging-markets / non-manufacturer form). Not applicable to banks, NBFCs
    and insurers (SPEC §4): pass ``is_financial=True`` to get an explained ``None``."""
    if is_financial:
        return ScoreResult(None, reasons=["Altman Z'' not applicable to financials"])
    df, y = _years(annual, year)
    missing: list[str] = []
    t = _Year(df, y, missing)
    ta, equity = t("total_assets"), t("total_equity")
    ca, cl = t("current_assets"), t("current_liabilities")
    liabilities = None if ta is None or equity is None else ta - equity
    x = {
        "x1": None if ca is None or cl is None else _div(ca - cl, ta),
        "x2": _div(t("retained_earnings"), ta),
        "x3": _div(t("ebit"), ta),
        "x4": _div(equity, liabilities) if liabilities and liabilities > 0 else None,
    }
    if liabilities is not None and liabilities <= 0:
        missing.append("total liabilities not positive")
    components: dict[str, float | bool | None] = dict(x)
    if any(v is None for v in x.values()):
        return ScoreResult(None, components, missing, reasons=["incomplete Altman inputs"])
    z = sum(ALTMAN_COEF[k] * v for k, v in x.items() if v is not None)
    if z > config.altman_safe_above:
        zone = "safe"
    elif z < config.altman_distress_below:
        zone = "distress"
    else:
        zone = "grey"
    return ScoreResult(z, components, [], zone, [f"Z''={z:.2f} ({zone} zone)"])
