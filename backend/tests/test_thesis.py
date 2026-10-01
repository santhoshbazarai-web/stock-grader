"""LLM thesis, pure part (SPEC §8a): fact sheet, prompt and the no-new-facts check."""

import json
from pathlib import Path

import pytest

from app.core.config import load_config
from app.reports.dto import StockReport
from app.reports.thesis import (
    Fact,
    FactSheet,
    build_prompt,
    check,
    clean_draft,
    fact_sheet,
    fmt,
    retry_prompt,
)
from tests.conftest import REPO_CONFIG_DIR

CFG = load_config(REPO_CONFIG_DIR).jobs.thesis
FIXTURE = Path(__file__).parent / "fixtures" / "thesis" / "DEMOIT_report.json"


@pytest.fixture(scope="module")
def report() -> StockReport:
    return StockReport.model_validate(json.loads(FIXTURE.read_text()))


GOOD = (
    "Demoit Ltd trades at ₹1,030.53, above its top band of ₹966.26 and well over the fair "
    "value of ₹871.01, which puts it in the Extreme Premium zone. Quality is solid, with a "
    "quality score of 71 out of 100, ROCE of 13.4% and almost no debt (0.01x debt to equity), "
    "and the stock is in Stage 2 with an up trend. Growth is only moderate: sales grew 12.0% a "
    "year over five years while the price implies 15.3%. That gap, and a valuation score of 28 "
    "out of 100, give grade B and the action Book Profits. The main risk is that the price "
    "already assumes faster growth than the company has delivered."
)


def test_fact_sheet_states_the_report_in_display_units(report: StockReport) -> None:
    sheet = fact_sheet(report, CFG)
    facts = dict((f.label, f.text) for f in sheet.facts)
    assert facts["Current price (CMP)"] == "₹1,030.53"
    assert facts["Fair value"] == "₹871.01"
    assert facts["Grade"] == "B" and facts["Action"] == "Book Profits"
    assert facts["Zone"] == "Extreme Premium"
    assert facts["ROCE (latest year)"] == "13.4%"  # fraction → percent
    assert facts["Revenue (trailing 12 months)"] == "₹2,773 crore"
    assert facts["Earned premium"] == "4 of 5 conditions met"
    assert sum(f.label == "Reason" for f in sheet.facts) == CFG.max_reasons
    # missing values are left out, never written as 0
    assert "Relative-strength percentile" not in facts


def test_digest_follows_the_facts(report: StockReport) -> None:
    a = fact_sheet(report, CFG)
    assert a.digest() == fact_sheet(report, CFG).digest()
    moved = report.model_copy(update={"cmp": report.cmp * 1.01})
    assert fact_sheet(moved, CFG).digest() != a.digest()


def test_units() -> None:
    assert fmt(0.1234, "pct") == "12.3%"
    assert fmt(110.923, "x") == "110.92x"
    assert fmt(34.8, "days") == "35 days"
    assert fmt(1234567.0, "inr") == "₹1,234,567.00"
    assert fmt(8.0, "count") == "8"


def test_a_faithful_paragraph_passes(report: StockReport) -> None:
    result = check(GOOD, fact_sheet(report, CFG), CFG)
    assert result.ok, result.problems


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("₹871.01", "₹925.00"), "numbers not in the facts: 925.00"),
        (("13.4%", "18%"), "numbers not in the facts: 18"),
        (("The main risk", "In 3 years the main risk"), "numbers not in the facts: 3"),
        (("Book Profits.", "Book Profits, with a target price near the top band."),
         "forbidden phrases: target price"),
        (("give grade B", "give grade C"), "the text names grade C"),
        (("and the action Book Profits", "but it is a Strong Buy"), "names Strong Buy"),
        (("Extreme Premium zone", "Deep Discount zone"), "names Deep Discount"),
        (("trades at", "on 2025-01-15 traded at"), "dates not in the facts: 2025-01-15"),
    ],
)  # fmt: skip
def test_drafts_that_add_or_change_facts_are_rejected(
    report: StockReport, change: tuple[str, str], problem: str
) -> None:
    assert change[0] in GOOD
    result = check(GOOD.replace(*change), fact_sheet(report, CFG), CFG)
    assert not result.ok
    assert any(problem in p for p in result.problems), result.problems


def test_rounding_and_separators_are_tolerated(report: StockReport) -> None:
    sheet = fact_sheet(report, CFG)
    for variant in ("₹1,031", "₹1030.5", "₹1,030", "Rs 1030.53"):
        text = GOOD.replace("₹1,030.53", variant)
        assert check(text, sheet, CFG).ok, variant
    # Indian grouping (1,23,456) and the report date are fine
    sheet = FactSheet("X", (Fact("Revenue", "₹123,456 crore"), Fact("Report date", "2024-06-13")),
                      "B", "hold", "fair")  # fmt: skip
    words = " ".join(["word"] * CFG.min_words)
    assert check(f"Revenue was ₹1,23,456 crore on 2024-06-13. {words}", sheet, CFG).ok


def test_grade_named_by_the_reasons_is_allowed(report: StockReport) -> None:
    # the reasons mention the provisional grade A, so citing it is not a contradiction
    text = GOOD.replace("give grade B", "lower the provisional grade A to grade B")
    assert check(text, fact_sheet(report, CFG), CFG).ok


def test_length_limits(report: StockReport) -> None:
    sheet = fact_sheet(report, CFG)
    assert "write" in check("Too short.", sheet, CFG).problems[0]
    long = " ".join([GOOD] * 3)
    assert not check(long, sheet, CFG).ok


def test_prompt_and_retry(report: StockReport) -> None:
    sheet = fact_sheet(report, CFG)
    prompt = build_prompt(sheet, CFG)
    assert sheet.block() in prompt and f"{CFG.min_words} to {CFG.max_words} words" in prompt
    again = retry_prompt(sheet, CFG, "draft text", ["numbers not in the facts: 9"])
    assert again.startswith(prompt) and "numbers not in the facts: 9" in again
    assert "draft text" in again


def test_clean_draft() -> None:
    assert clean_draft('  "One\nparagraph  here."\n') == "One paragraph here."
