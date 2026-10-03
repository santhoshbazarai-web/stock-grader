# ruff: noqa: E501
"""Durability proxy (scoring.durability): table-driven cases, missing inputs, config checks."""

from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.devtools.synthetic import seed_company, seed_index
from app.reports.service import refresh_report
from app.scoring.durability import DurabilityInputs, durability
from tests.api_support import app_client
from tests.conftest import REPO_CONFIG_DIR

CFG = load_config(REPO_CONFIG_DIR)
D = CFG.scoring.durability
YEARS = list(range(2016, 2026))


def s(values: list[float | None], years: list[int] | None = None) -> pd.Series:
    return pd.Series(values, index=years or YEARS[-len(values) :], dtype=float)


def grow(start: float, rate: float, n: int = 10) -> list[float]:
    return [start * (1 + rate) ** i for i in range(n)]


COMPOUNDER = DurabilityInputs(
    is_bank=False, trend_years=3, cost_of_equity=0.12,
    roe=s([0.20, 0.21, 0.22, 0.21, 0.23, 0.24, 0.23, 0.22, 0.23, 0.24]),
    roce=s([0.26, 0.27, 0.27, 0.28, 0.28, 0.29, 0.28, 0.27, 0.28, 0.29]),
    opm=s([0.24, 0.25, 0.24, 0.25, 0.25, 0.26, 0.25, 0.24, 0.25, 0.26]),
    pat=s(grow(100, 0.15)),
    debt_to_equity=s([0.15, 0.14, 0.12, 0.10, 0.10, 0.08, 0.07, 0.06, 0.05, 0.05]),
)  # fmt: skip
CYCLICAL = DurabilityInputs(
    is_bank=False, trend_years=3, cost_of_equity=0.12,
    roe=s([0.30, -0.02, 0.28, 0.01, 0.32, -0.05, 0.25, 0.02, 0.30, 0.04]),
    roce=s([0.28, 0.02, 0.26, 0.03, 0.30, -0.01, 0.24, 0.04, 0.27, 0.05]),
    opm=s([0.30, 0.04, 0.28, 0.05, 0.31, 0.02, 0.27, 0.05, 0.29, 0.06]),
    pat=s([100, 10, 95, 5, 120, -10, 90, 8, 110, 12]),
    debt_to_equity=s([0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4]),
)  # fmt: skip
TURNAROUND = DurabilityInputs(
    is_bank=False, trend_years=3, cost_of_equity=0.12,
    roe=s([-0.08, -0.05, -0.02, 0.0, 0.02, 0.05, 0.12, 0.18, 0.21, 0.23]),
    roce=s([-0.04, -0.02, 0.0, 0.01, 0.03, 0.06, 0.10, 0.15, 0.19, 0.22]),
    opm=s([0.00, 0.01, 0.02, 0.02, 0.04, 0.07, 0.11, 0.15, 0.18, 0.20]),
    pat=s([-50, -30, -10, 0, 5, 20, 60, 110, 160, 210]),
    debt_to_equity=s([2.0, 2.2, 2.0, 1.8, 1.5, 1.2, 0.9, 0.6, 0.4, 0.3]),
)  # fmt: skip
BANK = DurabilityInputs(
    is_bank=True, trend_years=3, cost_of_equity=0.13,
    roe=s([0.15, 0.16, 0.16, 0.17, 0.16, 0.17, 0.16, 0.17, 0.16, 0.17]),
    roa=s([0.017, 0.018, 0.017, 0.018, 0.018, 0.017, 0.018, 0.018, 0.017, 0.018]),
    pat=s(grow(100, 0.17)),
    deposits=s(grow(1000, 0.15, 11), [*YEARS, 2026]),
    advances=s(grow(800, 0.16, 11), [*YEARS, 2026]),
)  # fmt: skip


def with_(base: DurabilityInputs, **changes: Any) -> DurabilityInputs:
    return DurabilityInputs(**{**base.__dict__, **changes})


CASES = [
    ("strong compounder", COMPOUNDER, "strong", "high", {"leverage_trend": True, "roe_above_ke": True}),
    ("cyclical", CYCLICAL, "weak", "high", {"margin_stability": False, "roe_stability": False,
                                            "leverage_trend": False, "roe_above_ke": False}),
    ("turnaround", TURNAROUND, "weak", "high", {"profit_growth": True, "leverage_trend": True,
                                                "roe_median": False, "roe_stability": False}),
    ("bank", BANK, "strong", "high", {"roa_stability": True, "deposit_growth": True,
                                      "loan_growth": True, "roe_above_ke": True}),
    ("bank without deposit / loan history", with_(BANK, deposits=None, advances=None), "strong",
     "medium", {"deposit_growth": None, "loan_growth": None, "roa_median": True}),
    ("compounder without cost of equity", with_(COMPOUNDER, cost_of_equity=None), "strong", "high",
     {"roe_above_ke": None}),
]  # fmt: skip


@pytest.mark.parametrize(
    ("name", "inp", "rating", "confidence", "expected"), CASES, ids=[c[0] for c in CASES]
)
def test_ratings(name: str, inp: DurabilityInputs, rating: str, confidence: str,
                 expected: dict[str, bool | None]) -> None:  # fmt: skip
    d = durability(inp, D)
    assert d.rating == rating
    assert d.confidence == confidence
    got = {t.key: t.passed for t in d.tests}
    for key, want in expected.items():
        assert got[key] is want, (key, got[key])
    assert "not an analyst moat rating" in d.label
    assert len(d.reasons) == len(d.tests) + 1


def test_missing_inputs_lower_confidence_and_never_count_as_fails() -> None:
    thin = with_(COMPOUNDER, roce=None, opm=None, debt_to_equity=None)
    full, part = durability(COMPOUNDER, D), durability(thin, D)
    assert part.rating == "strong" and part.score == full.score == 1.0
    assert [t.passed for t in part.tests if t.key in ("roce_median", "margin_stability", "leverage_trend")] == [None] * 3  # fmt: skip
    assert part.confidence == "low"  # 4 of 7 tests answer: coverage 0.57 is below confidence_medium
    assert all("needs" in t.detail for t in part.tests if t.passed is None and t.key != "roe_above_ke")  # fmt: skip


def test_too_little_history_gives_no_rating() -> None:
    short = with_(COMPOUNDER, roe=s([0.2] * 3), roce=s([0.2] * 3), opm=s([0.2] * 3),
                  pat=s([1, 2, 3]), debt_to_equity=s([0.1] * 3))  # fmt: skip
    d = durability(short, D)
    assert d.rating is None and d.score is None and d.confidence is None
    assert all(t.passed is None for t in d.tests) and "no rating" in d.reasons[-1]


def test_infinite_and_nan_values_are_ignored() -> None:
    inp = with_(COMPOUNDER, debt_to_equity=s([float("inf"), None, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]))  # fmt: skip
    t = next(x for x in durability(inp, D).tests if x.key == "leverage_trend")
    assert t.passed is True


def test_thresholds_come_from_config() -> None:
    strict = D.model_copy(update={"roe_median_min": 0.30})
    t = next(x for x in durability(COMPOUNDER, strict).tests if x.key == "roe_median")
    assert t.passed is False


def test_config_validation() -> None:
    raw = CFG.scoring.durability.model_dump()
    with pytest.raises(ValueError, match="weak_max_score"):
        type(D).model_validate({**raw, "weak_max_score": 0.9})
    with pytest.raises(ValueError, match="min_years"):
        type(D).model_validate({**raw, "min_years": 11})


def test_report_carries_durability_and_screener_field(db: Session) -> None:
    seed_index(db)
    seed_company(db, "DUR", seed=21)
    refresh_report(db, "DUR", CFG)
    for c in app_client(db):
        assert isinstance(c, TestClient)
        r = c.get("/api/stocks/DUR/report").json()
        d = r["durability"]
        assert "not an analyst moat rating" in d["label"]
        assert d["rating"] in ("strong", "moderate", "weak", None)
        assert len(d["tests"]) == 7 and all("passed" in t for t in d["tests"])
        fields = {f["key"] for f in c.get("/api/screener/fields").json()["fields"]}
        assert "durability" in fields
        rows = c.get("/api/screener/scan").json()["rows"]
        assert "durability" in rows[0]
        m = c.get("/api/valuation-map").json()
        assert all("durability" in row for row in m["rows"])
        break
