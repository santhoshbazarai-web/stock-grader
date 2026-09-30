"""Annual-report PDF extraction (SPEC §3.6 step 3) against the synthetic reports in
tests/fixtures/annual_reports (built by make_fixtures.py; figures there are hand-made so that
every total and cross-check adds up)."""

from datetime import date
from pathlib import Path

import pytest

from app.core.config import load_config
from app.data.annual_report import (
    AnnualReportError,
    AnnualReportExtraction,
    ExtractedValue,
    Grid,
    Row,
    _grid_from_cells,
    _statement_values,
    extract_annual_report,
    find_dates,
    page_title,
    parse_amount,
    unit_factor,
)
from app.fundamentals.pdf_labels import (
    PdfLabels,
    PdfLabelsError,
    get_pdf_labels,
    load_pdf_labels,
    normalise,
)
from app.fundamentals.xbrl_map import get_xbrl_map
from tests.conftest import REPO_CONFIG_DIR

FIX = Path(__file__).parent / "fixtures" / "annual_reports"
NSE = load_config(REPO_CONFIG_DIR).providers.nse
CFG = NSE.annual_reports
LEVELS = NSE.results.rounding_levels
LABELS = get_pdf_labels()
XMAP = get_xbrl_map()
CRORE, LAKH = 1e7, 1e5


def _extract(name: str, fy: date | None = None) -> AnnualReportExtraction:
    return extract_annual_report((FIX / name).read_bytes(), cfg=CFG, rounding_levels=LEVELS,
                                 labels=LABELS, xmap=XMAP, fiscal_year_end=fy)  # fmt: skip


def _values(
    out: AnnualReportExtraction, basis: str, statement: str, period_end: date
) -> dict[str, ExtractedValue]:
    return {v.item_code: v for v in out.values
            if (v.basis, v.statement, v.period_end) == (basis, statement, period_end)}  # fmt: skip


# ───────────────────────── small parsers ─────────────────────────


@pytest.mark.parametrize(
    ("text", "want"),
    [("1,23,456.78", 123456.78), ("2,400.50", 2400.5), ("(450.00)", -450.0), ("-12.5", -12.5),
     ("—", 0.0), ("-", 0.0), ("100", 100.0), ("Note", None), ("3(a)", None), ("", None),
     ("12.5%", None), ("2023-24", None)],
)  # fmt: skip
def test_parse_amount(text: str, want: float | None) -> None:
    assert parse_amount(text) == want


@pytest.mark.parametrize(
    ("text", "want"),
    [("As at 31 March 2024", date(2024, 3, 31)), ("As at 31st March, 2012", date(2012, 3, 31)),
     ("As at March 31, 2024", date(2024, 3, 31)), ("Year ended 31.03.2011", date(2011, 3, 31)),
     ("31-Mar-24", date(2024, 3, 31)), ("As at 30/06/2023", date(2023, 6, 30))],
)  # fmt: skip
def test_find_dates(text: str, want: date) -> None:
    assert [d for _, _, d in find_dates(text)] == [want]


def test_find_dates_several_and_invalid() -> None:
    assert [d for _, _, d in find_dates("31 March 2024 31 March 2023")] == [
        date(2024, 3, 31), date(2023, 3, 31)]  # fmt: skip
    assert find_dates("31.13.2024 and 2023-24") == []


def test_unit_factor() -> None:
    assert unit_factor(["(₹ in crore)"], LEVELS) == (CRORE, "(₹ in crore)")
    assert unit_factor(["Rs. in Lakhs"], LEVELS)[0] == LAKH
    assert unit_factor(["All amounts in Indian Rupees million"], LEVELS)[0] == 1e6
    assert unit_factor(["Particulars"], LEVELS) == (None, None)
    assert unit_factor(["₹ in crore", "Rs. in lakhs"], LEVELS) == (None, None)  # conflicting


def test_normalise() -> None:
    assert normalise("(iv) Bank balances other than (iii) above") == \
        "bank balances other than above"  # fmt: skip
    assert normalise("Shareholders\u2019 Funds") == "shareholders funds"
    assert normalise("Net increase in cash (A+B+C)") == "net increase in cash"
    assert normalise("Other Equity [Refer Note 14]") == "other equity"
    assert normalise("B. Cash flow from investing activities") == \
        "cash flow from investing activities"  # fmt: skip


def test_page_title() -> None:
    assert page_title("ACME LTD\nConsolidated Balance Sheet as at 31 March 2024\n", LABELS, 8) \
        == ("bs", "consolidated", "Consolidated Balance Sheet as at 31 March 2024")  # fmt: skip
    assert page_title("BALANCE SHEET AS AT 31ST MARCH, 2012", LABELS, 8)[:2] == \
        ("bs", "standalone")  # type: ignore[index]  # fmt: skip
    assert page_title("Notes to the Standalone Balance Sheet", LABELS, 8) is None
    assert page_title("Statement of Cash Flows", LABELS, 8)[0] == "cf"  # type: ignore[index]
    many = "\n".join(["line"] * 8 + ["Balance Sheet"])
    assert page_title(many, LABELS, 8) is None  # title too far down


# ───────────────────────── Ind AS report (₹ crore) ─────────────────────────

# Hand-computed from make_fixtures.py (₹ crore): sum items add the rows named.
STANDALONE_BS_2024 = {
    "total_assets": 5350.00, "current_assets": 1899.75, "current_liabilities": 1000.00,
    "total_equity": 3750.00, "equity_share_capital": 100.00, "retained_earnings": 3650.00,
    "inventory": 600.00, "capital_work_in_progress": 150.25,
    "total_debt": 500.00 + 200.00,  # non-current + current borrowings
    "cash_and_equivalents": 180.00 + 70.00,  # cash + other bank balances
    "non_operating_investments": 800.00 + 250.00,
    "receivables": 20.00 + 700.00,
    "payables": 50.00 + 450.00,  # MSME + others
    "net_block": 2400.50 + 49.50,  # PPE + other intangibles
}  # fmt: skip
STANDALONE_BS_2023 = {
    "total_assets": 4730.00, "total_equity": 3200.00, "total_debt": 600.00 + 150.00,
    "net_block": 2200.00 + 60.00, "payables": 40.00 + 380.00, "receivables": 15.00 + 640.00,
}  # fmt: skip
CONSOLIDATED_BS_2024 = {
    "total_assets": 5700.00, "total_equity": 3900.00,  # owners' equity, not incl. NCI
    "minority_interest_bs": 100.00, "total_debt": 700.00 + 300.00, "payables": 600.00,
    "non_operating_investments": 300.00, "net_block": 3000.00,
}  # fmt: skip
STANDALONE_CF = {  # year ended 31 Mar 2024; outflows stored as magnitudes
    date(2024, 3, 31): {"cfo": 820.00, "purchase_of_fixed_assets": 450.00,
                        "sale_of_fixed_assets": 12.00, "dividends_paid": 120.00},
    date(2023, 3, 31): {"cfo": 750.00, "purchase_of_fixed_assets": 380.00,
                        "sale_of_fixed_assets": 8.00, "dividends_paid": 100.00},
}  # fmt: skip


@pytest.fixture(scope="module")
def fy2024() -> AnnualReportExtraction:
    return _extract("acme_ar_fy2024.pdf")


def test_ind_as_statements_located(fy2024: AnnualReportExtraction) -> None:
    found = {(s.statement, s.basis): s for s in fy2024.statements}
    assert set(found) == {("bs", "standalone"), ("cf", "standalone"), ("bs", "consolidated"),
                          ("cf", "consolidated")}  # fmt: skip
    # contents page (2) and notes page (6) are not statements; the cash flow runs over 2 pages
    assert found["bs", "standalone"].pages == [3]
    assert found["cf", "standalone"].pages == [4, 5]
    assert found["bs", "consolidated"].pages == [7]
    assert found["cf", "consolidated"].pages == [8]
    for s in found.values():
        assert s.column_dates == [date(2024, 3, 31), date(2023, 3, 31)]
        assert s.checks == ["passed", "passed"]
        assert s.method == "pdfplumber"
    assert fy2024.warnings == []
    assert fy2024.labels_version == LABELS.version and fy2024.page_count == 8


@pytest.mark.parametrize(
    ("basis", "period_end", "expected"),
    [("standalone", date(2024, 3, 31), STANDALONE_BS_2024),
     ("standalone", date(2023, 3, 31), STANDALONE_BS_2023),
     ("consolidated", date(2024, 3, 31), CONSOLIDATED_BS_2024)],
)  # fmt: skip
def test_ind_as_balance_sheet(
    fy2024: AnnualReportExtraction, basis: str, period_end: date, expected: dict[str, float]
) -> None:
    got = _values(fy2024, basis, "bs", period_end)
    for code, crore in expected.items():
        v = got[code]
        assert v.value_inr == pytest.approx(crore * CRORE, rel=1e-9), code
        assert v.period_type == "instant"
        assert v.confidence >= CFG.confidence.auto_accept, (code, v.reasons)
    if basis == "standalone":
        assert "minority_interest_bs" not in got


def test_ind_as_cash_flow(fy2024: AnnualReportExtraction) -> None:
    for period_end, expected in STANDALONE_CF.items():
        got = _values(fy2024, "standalone", "cf", period_end)
        assert {k: got[k].value_inr for k in expected} == pytest.approx(
            {k: v * CRORE for k, v in expected.items()})  # fmt: skip
        assert got["purchase_of_fixed_assets"].raw_value == -expected["purchase_of_fixed_assets"]
        assert got["dividends_paid"].pages == [5]  # from the continuation page
        assert all(v.period_type == "year" for v in got.values())


def test_reasons_explain_each_value(fy2024: AnnualReportExtraction) -> None:
    debt = _values(fy2024, "standalone", "bs", date(2024, 3, 31))["total_debt"]
    assert debt.raw_label == "(i) Borrowings + (i) Borrowings"
    text = " | ".join(debt.reasons)
    assert "sum of 2 rows" in text and "cross-check passed" in text
    assert "column dated 31 Mar 2024" in text and "x1e+07" in text


# ───────────────────────── Indian GAAP report (Rs. lakhs) ─────────────────────────


@pytest.fixture(scope="module")
def fy2012() -> AnnualReportExtraction:
    return _extract("acme_ar_fy2012_igaap.pdf", fy=date(2012, 3, 31))


def test_igaap_balance_sheet_in_lakhs(fy2012: AnnualReportExtraction) -> None:
    got = _values(fy2012, "standalone", "bs", date(2012, 3, 31))
    expected = {"total_assets": 79000, "equity_share_capital": 5000, "retained_earnings": 45000,
                "total_debt": 12000 + 3000, "payables": 9000, "net_block": 30000 + 500,
                "non_operating_investments": 8000 + 2000, "inventory": 12000,
                "receivables": 10000, "cash_and_equivalents": 6000,
                "capital_work_in_progress": 2500, "total_equity": 5000 + 45000}  # fmt: skip
    assert {k: got[k].value_inr for k in expected} == pytest.approx(
        {k: v * LAKH for k, v in expected.items()})  # fmt: skip
    # no "Total current assets" row in this layout: a gap, not a guess
    assert "current_assets" not in got and "current_liabilities" not in got
    # a bare "TOTAL" is a weaker label → review queue
    assert got["total_assets"].confidence == pytest.approx(0.85)
    assert got["total_assets"].confidence < CFG.confidence.auto_accept
    # no total equity row: share capital + reserves, marked down
    assert got["total_equity"].confidence == pytest.approx(CFG.confidence.fallback_sum_factor)
    assert "sum of equity_share_capital + retained_earnings" in got["total_equity"].reasons[0]


def test_igaap_cash_flow_with_nil_dash(fy2012: AnnualReportExtraction) -> None:
    cur = _values(fy2012, "standalone", "cf", date(2012, 3, 31))
    prev = _values(fy2012, "standalone", "cf", date(2011, 3, 31))
    assert cur["cfo"].value_inr == pytest.approx(11000 * LAKH)
    assert cur["dividends_paid"].value_inr == pytest.approx(2500 * LAKH)
    assert prev["sale_of_fixed_assets"].value_inr == 0.0  # printed "-": nil, not missing
    assert all(s.checks == ["passed", "passed"] for s in fy2012.statements)


# ───────────────────────── confidence factors ─────────────────────────


def _grid(rows: list[tuple[str, list[float | None]]], dates: list[date | None],
          unit: float | None = CRORE) -> Grid:  # fmt: skip
    return Grid(1, "pdfplumber", [Row(label, values, 1) for label, values in rows], dates, unit,
                "(₹ in crore)" if unit else None)  # fmt: skip


def _run(
    grid: Grid,
    statement: str = "bs",
    fy: date | None = None,
    method: str = "pdfplumber",
    labels: PdfLabels = LABELS,
) -> AnnualReportExtraction:
    out = AnnualReportExtraction([], [], [], 1, 1)
    _statement_values(out, [grid], statement, "standalone", method, labels, XMAP,  # type: ignore[arg-type]
                      CFG.confidence, CFG.extraction.min_label_score, fy)  # fmt: skip
    return out


BS_ROWS: list[tuple[str, list[float | None]]] = [
    ("Current assets", [None]),
    ("Inventories", [50.0]),
    ("Total assets", [100.0]),
    ("Total equity and liabilities", [100.0]),
]


def test_failed_cross_check_marks_the_column_down() -> None:
    ok = {v.item_code: v for v in _run(_grid(BS_ROWS, [date(2024, 3, 31)])).values}
    bad_rows = [*BS_ROWS[:3], ("Total equity and liabilities", [90.0])]
    out = _run(_grid(bad_rows, [date(2024, 3, 31)]))
    bad = {v.item_code: v for v in out.values}
    assert out.statements[0].checks == ["failed"]
    assert bad["inventory"].confidence == pytest.approx(
        ok["inventory"].confidence * CFG.confidence.check_failed_factor)  # fmt: skip
    assert any("≠" in r for r in bad["inventory"].reasons)


def test_no_cross_check_rows() -> None:
    out = _run(_grid(BS_ROWS[:2], [date(2024, 3, 31)]))
    assert out.statements[0].checks == ["none"]
    assert out.values[0].confidence == pytest.approx(CFG.confidence.no_check_factor)


def test_undated_columns_use_the_report_year() -> None:
    out = _run(_grid([(lbl, v * 2 if v != [None] else v) for lbl, v in BS_ROWS], [None, None]),
               fy=date(2015, 3, 31))  # fmt: skip
    ends = sorted({v.period_end for v in out.values})
    assert ends == [date(2014, 3, 31), date(2015, 3, 31)]
    assert all(v.confidence == pytest.approx(CFG.confidence.no_header_dates_factor)
               for v in out.values)  # fmt: skip
    # without the report year the column cannot be dated: skipped with a warning
    out = _run(_grid(BS_ROWS, [None]))
    assert out.values == [] and "no date" in out.warnings[0]


def test_camelot_factor_and_unmatched_rows() -> None:
    rows: list[tuple[str, list[float | None]]] = [("Cash and bank", [10.0])]
    assert _run(_grid(rows, [date(2024, 3, 31)])).values == []  # below min_label_score
    rows = [("Current assets", [None]), ("Inventories", [10.0])]
    v = _run(_grid(rows, [date(2024, 3, 31)]), method="camelot").values[0]
    assert v.confidence == pytest.approx(CFG.confidence.camelot_factor
                                         * CFG.confidence.no_check_factor)  # fmt: skip
    assert "read by the camelot fallback" in v.reasons


def test_ambiguous_label(tmp_path: Path) -> None:
    labels = load_pdf_labels(
        _write(
            tmp_path,
            BASE
            + """items:
  inventory: {statement: bs, labels: [inventories]}
  receivables: {statement: bs, labels: [inventory]}
""",
        )
    )
    rows: list[tuple[str, list[float | None]]] = [("Inventori", [10.0])]
    v = _run(_grid(rows, [date(2024, 3, 31)]), labels=labels).values[0]
    assert v.item_code == "inventory"  # 90% vs 88.9%: within ambiguity_margin
    assert v.confidence == pytest.approx(0.9 * CFG.confidence.ambiguous_factor
                                         * CFG.confidence.no_check_factor)  # fmt: skip
    assert "ambiguous: also close to receivables" in v.reasons


def test_ignore_labels_win() -> None:
    # "Total non-current assets" is closer to its ignore entry than to "total current assets"
    rows: list[tuple[str, list[float | None]]] = [("Total non-current assets", [10.0])]
    assert _run(_grid(rows, [date(2024, 3, 31)])).values == []


def test_sections_restrict_rows() -> None:
    rows: list[tuple[str, list[float | None]]] = [
        ("Non-current liabilities", [None]), ("Borrowings", [40.0]),
        ("Current liabilities", [None]), ("Borrowings", [10.0]), ("Trade payables", [5.0]),
        ("Current assets", [None]), ("Borrowings", [99.0]),  # not a liability section: ignored
    ]  # fmt: skip
    got = {v.item_code: v for v in _run(_grid(rows, [date(2024, 3, 31)])).values}
    assert got["total_debt"].value_inr == pytest.approx(50 * CRORE)
    assert got["payables"].value_inr == pytest.approx(5 * CRORE)


def test_no_unit_leaves_value_empty() -> None:
    out = _run(_grid(BS_ROWS, [date(2024, 3, 31)], unit=None))
    assert all(v.value_inr is None for v in out.values)
    assert all(v.raw_value is not None for v in out.values)
    assert "no unit stated" in out.warnings[0]


# ───────────────────────── documents that cannot be read ─────────────────────────


def test_scanned_report_is_refused() -> None:
    with pytest.raises(AnnualReportError, match="no text layer"):
        _extract("scanned_ar.pdf")


def test_not_a_pdf() -> None:
    with pytest.raises(AnnualReportError, match="not a PDF"):
        extract_annual_report(b"<html>", cfg=CFG, rounding_levels=LEVELS, labels=LABELS,
                              xmap=XMAP)  # fmt: skip


def test_too_many_pages() -> None:
    small = CFG.model_copy(update={"extraction": CFG.extraction.model_copy(
        update={"max_pages": 2})})  # fmt: skip
    with pytest.raises(AnnualReportError, match="max_pages"):
        extract_annual_report((FIX / "acme_ar_fy2024.pdf").read_bytes(), cfg=small,
                              rounding_levels=LEVELS, labels=LABELS, xmap=XMAP)  # fmt: skip


# ───────────────────────── camelot fallback ─────────────────────────


def test_grid_from_cells() -> None:
    cells = [
        ["Balance Sheet", "", "", ""],
        ["(₹ in crore)", "", "", ""],
        ["Particulars", "Note", "As at 31 March 2024", "As at 31 March 2023"],
        ["Current assets", "", "", ""],
        ["Inventories", "7", "600.00", "550.00"],
        ["Trade receivables", "6", "700.00", "640.00"],
        ["Total assets", "", "1,300.00", "1,190.00"],
    ]
    grid = _grid_from_cells(cells, 3, CFG.extraction, LEVELS)
    assert grid.method == "camelot"
    assert grid.column_dates == [date(2024, 3, 31), date(2023, 3, 31)]
    assert grid.unit == CRORE
    data = [r for r in grid.rows if any(v is not None for v in r.values)]
    assert [(r.label, r.values) for r in data] == [
        ("Inventories", [600.0, 550.0]), ("Trade receivables", [700.0, 640.0]),
        ("Total assets", [1300.0, 1190.0])]  # fmt: skip


def test_camelot_reads_a_statement_page() -> None:
    pytest.importorskip("camelot")
    from app.data.annual_report import _camelot_grid, _mapped_count

    grid = _camelot_grid((FIX / "acme_ar_fy2024.pdf").read_bytes(), 3, CFG.extraction, LEVELS)
    assert grid is not None and grid.method == "camelot"
    assert grid.column_dates == [date(2024, 3, 31), date(2023, 3, 31)]
    assert _mapped_count([grid], "bs", LABELS, CFG.extraction.min_label_score) >= 10


# ───────────────────────── the label dictionary ─────────────────────────


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "labels.yaml"
    p.write_text(text)
    return p


BASE = """
version: 1
headings: {bs: [balance sheet], cf: [cash flow statement], consolidated: [consolidated]}
sections: {current_assets: [current assets]}
checks: {}
"""


def test_labels_file_is_valid() -> None:
    assert isinstance(LABELS, PdfLabels)
    assert set(LABELS.items) <= set(XMAP.items)


@pytest.mark.parametrize(
    ("items", "match"),
    [("items: {inventory: {statement: bs, labels: [Inventories]}}", "not normalised"),
     ("items: {inventory: {statement: bs, labels: [inventories], sections: [nowhere]}}",
      "unknown sections"),
     ("items: {eps_basic: {statement: bs, labels: [eps]}}", "not a bs amount item"),
     ("items: {cfo: {statement: bs, labels: [cfo]}}", "not a bs amount item"),
     ("items: {total_equity: {statement: bs, labels: [total equity], fallback_sum: [nope]}}",
      "fallback_sum")],
)  # fmt: skip
def test_invalid_labels_file(tmp_path: Path, items: str, match: str) -> None:
    with pytest.raises(PdfLabelsError, match=match):
        load_pdf_labels(_write(tmp_path, BASE + items))
