"""Golden stocks (SPEC §13): every expected number in tests/fixtures/golden/<SYMBOL>.json is
recomputed from that file's inputs. Tolerance (AGENTS.md): 0.5% for ratios, 2% for valuations.
See tests/fixtures/golden/README.md for how to fill a file."""

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from jsonschema import Draft202012Validator

from app.core.config import load_config
from app.data.canonical import fields_for
from app.fundamentals.banking import BANK_FIELDS, bank_summary
from app.fundamentals.forensic import altman_z2, beneish, piotroski
from app.fundamentals.metrics import summary_metrics
from app.valuation.dcf import DcfInputs, run_dcf
from tests.conftest import REPO_CONFIG_DIR

GOLDEN = Path(__file__).parent / "fixtures" / "golden"
SCHEMA = json.loads((GOLDEN / "schema.json").read_text())
FILES = sorted(p for p in GOLDEN.glob("*.json") if p.name != "schema.json")
CFG = load_config(REPO_CONFIG_DIR)
RATIO_TOL = 0.005
VALUATION_TOL = 0.02
FINANCIAL = {"private_bank", "nbfc", "insurer"}
MODEL_TYPES = set(SCHEMA["properties"]["model_type"]["enum"]) - {"example"}


def load(path: Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(path.read_text())
    return doc


def frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("period_end")), name="period_end")
    return df.sort_index()


def close(actual: float, expected: float, tol: float) -> bool:
    if expected == 0:
        return abs(actual) < 1e-9
    return abs(actual - expected) <= tol * abs(expected)


def ids(paths: list[Path]) -> list[str]:
    return [p.stem for p in paths]


# ───────────────────────── schema ─────────────────────────


def test_schema_tracks_canonical_fields() -> None:
    item = SCHEMA["properties"]["inputs"]["properties"]["annual"]["items"]
    assert set(item["properties"]) == {"period_end", "fiscal_year", "extra",
                                       *fields_for("fin_annual")}  # fmt: skip
    assert set(item["properties"]["extra"]["properties"]) == set(BANK_FIELDS)
    q = SCHEMA["properties"]["inputs"]["properties"]["quarterly"]["items"]["properties"]
    assert set(q) == {"period_end", *fields_for("fin_quarterly")}


def test_schema_rejects_typos_and_missing_required_metrics() -> None:
    doc = load(GOLDEN / "EXAMPLE.json")
    doc["model_type"] = "fmcg"
    doc["inputs"]["annual"][0]["revenu"] = 1.0
    del doc["expected"]["metrics"]["ccc_days"]
    errors = [e.message for e in Draft202012Validator(SCHEMA).iter_errors(doc)]
    assert any("revenu" in e for e in errors)
    assert any("ccc_days" in e for e in errors)


@pytest.mark.parametrize("path", FILES, ids=ids(FILES))
def test_file_is_valid(path: Path) -> None:
    doc = load(path)
    validator = Draft202012Validator(SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)
    assert doc["sector"] in CFG.sectors.root, f"unknown sector {doc['sector']!r}"
    years = [r["fiscal_year"] for r in doc["inputs"]["annual"]]
    assert years == sorted(years) and len(set(years)) == len(years), "annual rows out of order"


# ───────────────────────── recomputation ─────────────────────────


def golden_failures(doc: dict[str, Any]) -> list[str]:
    """Every expected number that the code does not reproduce, with the reason."""
    exp = doc["expected"]
    annual = frame(doc["inputs"]["annual"])
    quarterly = frame(doc["inputs"]["quarterly"]) if doc["inputs"].get("quarterly") else None
    year = exp.get("as_of_fiscal_year")
    failures = []

    if exp.get("metrics"):
        got = summary_metrics(
            annual, quarterly, CFG.scoring.fundamentals,
            tax_rate_fallback=CFG.valuation.tax_rate_default, year=year,
        )  # fmt: skip
        for name, expected in exp["metrics"].items():
            metric = got.get(name)
            if metric is None:
                failures.append(f"{name}: not a metric (available: {sorted(got)})")
            elif metric.value is None:
                failures.append(f"{name}: not computable — {metric.reason}")
            elif not close(metric.value, expected, RATIO_TOL):
                failures.append(f"{name}: got {metric.value:.6g}, expected {expected}")

    if exp.get("bank_metrics"):
        got_bank = bank_summary(annual, year)
        for name, expected in exp["bank_metrics"].items():
            metric = got_bank.get(name)
            if metric is None or metric.value is None:
                reason = metric.reason if metric else "not a bank metric"
                failures.append(f"{name}: {reason}")
            elif not close(metric.value, expected, RATIO_TOL):
                failures.append(f"{name}: got {metric.value:.6g}, expected {expected}")

    forensic = exp.get("forensic", {})
    fcfg = CFG.scoring.forensic
    checks = {
        "piotroski": lambda: piotroski(annual, year),
        "beneish_m": lambda: beneish(annual, fcfg, year),
        "altman_z2": lambda: altman_z2(
            annual, fcfg, year, is_financial=doc["model_type"] in FINANCIAL
        ),
    }
    for name, expected in forensic.items():
        result = checks[name]()
        if result.value is None:
            failures.append(f"{name}: not computable — missing {result.missing or result.reasons}")
        elif name == "piotroski" and result.value != expected:
            failures.append(f"piotroski: got {result.value:.0f}, expected {expected}")
        elif name != "piotroski" and not close(result.value, expected, RATIO_TOL):
            failures.append(f"{name}: got {result.value:.4f}, expected {expected}")
    return failures


@pytest.mark.parametrize("path", FILES, ids=ids(FILES))
def test_metrics_match(path: Path) -> None:
    failures = golden_failures(load(path))
    assert not failures, f"{path.name}:\n  " + "\n  ".join(failures)


def test_runner_catches_wrong_numbers() -> None:
    doc = load(GOLDEN / "EXAMPLE.json")
    doc["expected"]["metrics"]["roce_5y_avg"] *= 1.01  # 1% off: outside 0.5%
    doc["expected"]["metrics"]["debt_to_equity"] *= 1.004  # 0.4% off: inside
    doc["expected"]["metrics"]["roce_5yr_avg"] = 0.2  # typo'd name
    doc["expected"]["forensic"]["piotroski"] = 9
    failures = golden_failures(doc)
    assert len(failures) == 3
    assert any(f.startswith("roce_5y_avg: got 0.22") for f in failures)
    assert any("roce_5yr_avg: not a metric" in f for f in failures)
    assert any(f.startswith("piotroski: got 8") for f in failures)


def test_runner_reports_uncomputable_metrics() -> None:
    doc = load(GOLDEN / "EXAMPLE.json")
    for row in doc["inputs"]["annual"]:
        row.pop("payables")
    [failure] = golden_failures(doc)
    assert failure.startswith("ccc_days: not computable") and "payables (FY2024)" in failure


@pytest.mark.parametrize("path", FILES, ids=ids(FILES))
def test_valuation_matches(path: Path) -> None:
    valuation = load(path)["expected"].get("valuation")
    if not valuation:
        pytest.skip("no valuation expectations in this file")
    if dcf := valuation.get("dcf"):
        result = run_dcf(DcfInputs(**dcf["inputs"]))
        assert result.value_per_share is not None, result.reasons
        assert close(result.value_per_share, dcf["value_per_share"], VALUATION_TOL), (
            f"DCF: got {result.value_per_share:.2f}, expected {dcf['value_per_share']}"
        )
    if "pe_band" in valuation:
        pytest.skip("PE band check needs stored price history (runs once prices are loaded)")


def test_golden_set_complete() -> None:
    present = {load(p)["model_type"] for p in FILES if not load(p).get("is_example")}
    missing = sorted(MODEL_TYPES - present)
    if missing:
        pytest.skip(f"golden stocks still to add for: {', '.join(missing)}")
