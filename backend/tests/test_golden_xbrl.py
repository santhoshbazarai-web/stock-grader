"""Golden XBRL tests (P17): real filings of a bank, a manufacturer and an IT company, three
fiscal years each (one before 2017), against hand-checked figures in
tests/fixtures/golden_xbrl/expected.yaml. Entries whose file or figures are missing are
skipped with the reason; see that directory's README.md."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import load_config
from app.data.xbrl import parse_results
from tests.conftest import REPO_CONFIG_DIR

GOLDEN = Path(__file__).parent / "fixtures" / "golden_xbrl"
SPEC: dict[str, Any] = yaml.safe_load((GOLDEN / "expected.yaml").read_text())["companies"]
CFG = load_config(REPO_CONFIG_DIR).providers.nse.results
TOLERANCE = 0.005  # AGENTS.md rule 3: 0.5% for reported figures and ratios


def test_golden_set_covers_three_kinds_three_years_and_a_pre_2017_year() -> None:
    assert sorted(c["kind"] for c in SPEC.values()) == ["bank", "it", "manufacturer"]
    for symbol, company in SPEC.items():
        years = [f["fiscal_year"] for f in company["filings"]]
        assert len(set(years)) == 3, symbol
        assert min(years) < 2017, f"{symbol}: needs a pre-2017 (pre-Ind-AS) year"
        for f in company["filings"]:
            assert f["file"].startswith(f"{symbol}/"), f
            assert f["expected_crore"], f


CASES = [
    pytest.param(symbol, company, filing, id=f"{symbol}-FY{filing['fiscal_year']}")
    for symbol, company in SPEC.items()
    for filing in company["filings"]
]


@pytest.mark.parametrize(("symbol", "company", "filing"), CASES)
def test_golden_filing(symbol: str, company: dict[str, Any], filing: dict[str, Any]) -> None:
    path = GOLDEN / filing["file"]
    if not path.is_file():
        pytest.skip(f"{filing['file']} not downloaded yet (see golden_xbrl/README.md)")
    expected: dict[str, Any] = {**filing["expected_crore"],
                                **(filing.get("expected_extra_crore") or {})}  # fmt: skip
    missing = sorted(k for k, v in expected.items() if v is None)
    if missing:
        pytest.skip(f"{filing['file']}: figures not hand-checked yet: {', '.join(missing)}")
    assert filing.get("checked_against"), "record where the figures were checked"

    parsed = parse_results(path.read_bytes(), CFG)
    assert parsed.statement_type is not None and parsed.statement_type.value == company["basis"]
    year = parsed.annual
    assert year is not None, f"{filing['file']}: no fiscal-year context"
    assert year["fiscal_year"] == filing["fiscal_year"]
    extra = year.get("extra") or {}
    for field, want in expected.items():
        got = year.get(field) if field in filing["expected_crore"] else extra.get(field)
        if want == "not_in_xbrl":
            assert got is None, f"{field}: expected no value, parsed {got}"
            continue
        assert got is not None, f"{field}: not parsed (check xbrl_map.yaml with xbrl-inspect)"
        assert got == pytest.approx(float(want), rel=TOLERANCE), field


def test_harness_compares_filled_entries() -> None:
    """The harness itself, on the synthetic ACME filing (not a golden company)."""
    company = {"kind": "manufacturer", "basis": "consolidated"}
    filing: dict[str, Any] = {
        "fiscal_year": 2024,
        "file": "../xbrl/acme_q4fy24_consolidated.xml",
        "checked_against": "tests/test_xbrl.py hand computation",
        "expected_crore": {"revenue": 4800, "pat": 800, "cfo": 950, "sga": "not_in_xbrl"},
    }
    test_golden_filing("ACME", company, filing)  # passes
    wrong = {**filing, "expected_crore": {**filing["expected_crore"], "revenue": 4900}}
    with pytest.raises(AssertionError, match="revenue"):
        test_golden_filing("ACME", company, wrong)
    unchecked = {**filing, "checked_against": None}
    with pytest.raises(AssertionError, match="record where"):
        test_golden_filing("ACME", company, unchecked)
