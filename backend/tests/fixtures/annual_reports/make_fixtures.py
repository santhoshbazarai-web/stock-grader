"""Builds the synthetic annual-report PDFs used by tests/test_annual_report.py.

    uv run python tests/fixtures/annual_reports/make_fixtures.py

Layouts follow real Indian annual reports (a contents page, statements with a note column and
two dated value columns, a cash flow split over two pages, notes pages that repeat statement
labels), but every company and figure is made up. Hand-computed expected values are in the
tests. Needs reportlab (a dev dependency).
"""

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

HERE = Path(__file__).parent
W, H = A4
LABEL_X, NOTE_X, COL1_X, COL2_X = 50, 330, 450, 540  # value columns are right-aligned

Row = tuple[str, str, str, str] | tuple[str]  # (label, note, current, previous) or (heading,)


def _page(
    c: Canvas,
    title: list[str],
    header: tuple[str, str],
    unit: str,
    rows: list[Row],
    *,
    company: str = "ACME INDUSTRIES LIMITED",
    header_note: str = "Note",
) -> None:
    y = H - 50
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(W / 2, y, company)
    for line in title:
        y -= 16
        c.drawCentredString(W / 2, y, line)
    y -= 18
    c.setFont("Helvetica", 8)
    c.drawRightString(COL2_X, y, unit)
    y -= 16
    c.setFont("Helvetica-Bold", 8)
    c.drawString(LABEL_X, y, "Particulars")
    if header_note:
        c.drawCentredString(NOTE_X, y, header_note)
    for x, text in ((COL1_X, header[0]), (COL2_X, header[1])):
        c.drawRightString(x, y, text)
    y -= 18
    for row in rows:
        if len(row) == 1:
            c.setFont("Helvetica-Bold", 8)
            c.drawString(LABEL_X, y, row[0])
        else:
            label, note, cur, prev = row
            c.setFont("Helvetica", 8)
            c.drawString(LABEL_X + (10 if label.startswith("(") else 0), y, label)
            if note:
                c.drawCentredString(NOTE_X, y, note)
            if cur:
                c.drawRightString(COL1_X, y, cur)
            if prev:
                c.drawRightString(COL2_X, y, prev)
        y -= 14
    c.showPage()


def _text_page(c: Canvas, lines: list[str]) -> None:
    y = H - 60
    c.setFont("Helvetica", 10)
    for line in lines:
        c.drawString(60, y, line)
        y -= 16
    c.showPage()


STANDALONE_BS: list[Row] = [
    ("ASSETS",),
    ("Non-current assets",),
    ("(a) Property, Plant and Equipment", "3", "2,400.50", "2,200.00"),
    ("(b) Capital work-in-progress", "3", "150.25", "90.00"),
    ("(c) Other Intangible assets", "4", "49.50", "60.00"),
    ("(d) Financial Assets",),
    ("(i) Investments", "5", "800.00", "700.00"),
    ("(ii) Trade receivables", "6", "20.00", "15.00"),
    ("(e) Deferred tax assets (net)", "", "30.00", "25.00"),
    ("Total non-current assets", "", "3,450.25", "3,090.00"),
    ("Current assets",),
    ("(a) Inventories", "7", "600.00", "550.00"),
    ("(b) Financial Assets",),
    ("(i) Investments", "5", "250.00", "200.00"),
    ("(ii) Trade receivables", "6", "700.00", "640.00"),
    ("(iii) Cash and cash equivalents", "8", "180.00", "120.00"),
    ("(iv) Bank balances other than (iii) above", "8", "70.00", "50.00"),
    ("(c) Other current assets", "", "99.75", "80.00"),
    ("Total current assets", "", "1,899.75", "1,640.00"),
    ("Total Assets", "", "5,350.00", "4,730.00"),
    ("EQUITY AND LIABILITIES",),
    ("Equity",),
    ("(a) Equity Share capital", "9", "100.00", "100.00"),
    ("(b) Other Equity", "", "3,650.00", "3,100.00"),
    ("Total equity", "", "3,750.00", "3,200.00"),
    ("Liabilities",),
    ("Non-current liabilities",),
    ("(a) Financial Liabilities",),
    ("(i) Borrowings", "10", "500.00", "600.00"),
    ("(ii) Lease liabilities", "", "40.00", "45.00"),
    ("(b) Provisions", "", "60.00", "55.00"),
    ("Total non-current liabilities", "", "600.00", "700.00"),
    ("Current liabilities",),
    ("(a) Financial Liabilities",),
    ("(i) Borrowings", "10", "200.00", "150.00"),
    ("(ii) Trade payables", "11", "", ""),  # heading row: the two parts follow
    ("total outstanding dues of micro enterprises and small enterprises", "", "50.00", "40.00"),
    (
        "total outstanding dues of creditors other than micro enterprises and small enterprises",
        "",
        "450.00",
        "380.00",
    ),
    ("(iii) Other financial liabilities", "", "150.00", "130.00"),
    ("(b) Other current liabilities", "", "100.00", "90.00"),
    ("(c) Provisions", "", "50.00", "40.00"),
    ("Total current liabilities", "", "1,000.00", "830.00"),
    ("Total Equity and Liabilities", "", "5,350.00", "4,730.00"),
]

STANDALONE_CF_1: list[Row] = [
    ("A. Cash flow from operating activities",),
    ("Profit before tax", "", "900.00", "800.00"),
    ("Depreciation and amortisation expense", "", "150.00", "140.00"),
    ("Cash generated from operations", "", "1,050.00", "950.00"),
    ("Income taxes paid", "", "(230.00)", "(200.00)"),
    ("Net cash generated from operating activities (A)", "", "820.00", "750.00"),
    ("B. Cash flow from investing activities",),
    ("Purchase of property, plant and equipment", "", "(450.00)", "(380.00)"),
    ("Proceeds from sale of property, plant and equipment", "", "12.00", "8.00"),
    ("Purchase of investments", "", "(150.00)", "(100.00)"),
    ("Net cash used in investing activities (B)", "", "(588.00)", "(472.00)"),
]
STANDALONE_CF_2: list[Row] = [
    ("C. Cash flow from financing activities",),
    ("Proceeds from borrowings", "", "100.00", "50.00"),
    ("Repayment of borrowings", "", "(150.00)", "(120.00)"),
    ("Dividends paid", "", "(120.00)", "(100.00)"),
    ("Interest paid", "", "(60.00)", "(50.00)"),
    ("Net cash used in financing",),  # the label wraps onto the next line
    ("activities (C)", "", "(230.00)", "(220.00)"),
    ("Net increase in cash and cash equivalents (A+B+C)", "", "2.00", "58.00"),
    ("Cash and cash equivalents at the beginning of the year", "", "178.00", "120.00"),
    ("Cash and cash equivalents at the end of the year", "", "180.00", "178.00"),
]

CONSOLIDATED_BS: list[Row] = [
    ("ASSETS",),
    ("Non-current assets",),
    ("(a) Property, Plant and Equipment", "3", "3,000.00", "2,700.00"),
    ("(b) Capital work-in-progress", "3", "200.00", "100.00"),
    ("(c) Goodwill", "", "100.00", "100.00"),
    ("(d) Financial Assets",),
    ("(i) Investments", "5", "300.00", "250.00"),
    ("Total non-current assets", "", "3,600.00", "3,150.00"),
    ("Current assets",),
    ("(a) Inventories", "7", "800.00", "700.00"),
    ("(b) Financial Assets",),
    ("(i) Trade receivables", "6", "900.00", "800.00"),
    ("(ii) Cash and cash equivalents", "8", "300.00", "250.00"),
    ("(c) Other current assets", "", "100.00", "100.00"),
    ("Total current assets", "", "2,100.00", "1,850.00"),
    ("Total Assets", "", "5,700.00", "5,000.00"),
    ("EQUITY AND LIABILITIES",),
    ("Equity",),
    ("(a) Equity Share capital", "9", "100.00", "100.00"),
    ("(b) Other Equity", "", "3,800.00", "3,200.00"),
    ("Equity attributable to owners of the Company", "", "3,900.00", "3,300.00"),
    ("Non-controlling interests", "", "100.00", "80.00"),
    ("Total equity", "", "4,000.00", "3,380.00"),
    ("Liabilities",),
    ("Non-current liabilities",),
    ("(a) Financial Liabilities",),
    ("(i) Borrowings", "10", "700.00", "720.00"),
    ("Total non-current liabilities", "", "700.00", "720.00"),
    ("Current liabilities",),
    ("(a) Financial Liabilities",),
    ("(i) Borrowings", "10", "300.00", "250.00"),
    ("(ii) Trade payables", "11", "600.00", "550.00"),
    ("(b) Other current liabilities", "", "100.00", "100.00"),
    ("Total current liabilities", "", "1,000.00", "900.00"),
    ("Total Equity and Liabilities", "", "5,700.00", "5,000.00"),
]

CONSOLIDATED_CF: list[Row] = [
    ("A. Cash flow from operating activities",),
    ("Profit before tax", "", "1,100.00", "950.00"),
    ("Net cash generated from operating activities (A)", "", "1,000.00", "880.00"),
    ("B. Cash flow from investing activities",),
    ("Purchase of property, plant and equipment", "", "(600.00)", "(500.00)"),
    ("Proceeds from sale of property, plant and equipment", "", "20.00", "10.00"),
    ("Net cash used in investing activities (B)", "", "(580.00)", "(490.00)"),
    ("C. Cash flow from financing activities",),
    ("Dividends paid", "", "(150.00)", "(120.00)"),
    ("Repayment of borrowings", "", "(220.00)", "(200.00)"),
    ("Net cash used in financing activities (C)", "", "(370.00)", "(320.00)"),
    ("Net increase in cash and cash equivalents (A+B+C)", "", "50.00", "70.00"),
]


def acme_fy2024() -> None:
    c = Canvas(str(HERE / "acme_ar_fy2024.pdf"), pagesize=A4)
    _text_page(c, ["ACME INDUSTRIES LIMITED", "Annual Report 2023-24", "", "Growing responsibly"])
    _text_page(c, ["Contents", "Directors' Report ........ 12",
                   "Standalone Balance Sheet ........ 45",
                   "Standalone Statement of Cash Flows ........ 46",
                   "Consolidated Balance Sheet ........ 80",
                   "Consolidated Statement of Cash Flows ........ 81"])  # fmt: skip
    unit = "(₹ in crore)"
    bs_head = ("As at 31 March 2024", "As at 31 March 2023")
    cf_head = ("Year ended 31 March 2024", "Year ended 31 March 2023")
    _page(c, ["Standalone Balance Sheet as at 31 March 2024"], bs_head, unit, STANDALONE_BS)
    _page(c, ["Standalone Statement of Cash Flows for the year ended 31 March 2024"], cf_head,
          unit, STANDALONE_CF_1)  # fmt: skip
    _page(c, [], cf_head, unit, STANDALONE_CF_2, header_note="")  # continuation, no title
    _page(c, ["Notes to the Standalone Financial Statements for the year ended 31 March 2024",
              "10. Borrowings"], bs_head, unit,
          [("Term loans from banks", "", "500.00", "600.00"),
           ("Working capital loans", "", "200.00", "150.00"),
           ("Total borrowings", "", "700.00", "750.00")])  # fmt: skip
    _page(
        c,
        ["Consolidated Balance Sheet as at March 31, 2024"],
        ("As at March 31, 2024", "As at March 31, 2023"),
        unit,
        CONSOLIDATED_BS,
    )
    _page(
        c,
        ["Consolidated Statement of Cash Flows for the year ended March 31, 2024"],
        ("Year ended March 31, 2024", "Year ended March 31, 2023"),
        unit,
        CONSOLIDATED_CF,
    )
    c.save()


IGAAP_BS: list[Row] = [
    ("EQUITY AND LIABILITIES",),
    ("Shareholders' Funds",),
    ("(a) Share Capital", "1", "5,000.00", "5,000.00"),
    ("(b) Reserves and Surplus", "2", "45,000.00", "40,000.00"),
    ("Non-Current Liabilities",),
    ("(a) Long-term borrowings", "3", "12,000.00", "14,000.00"),
    ("(b) Deferred tax liabilities (Net)", "", "1,500.00", "1,400.00"),
    ("Current Liabilities",),
    ("(a) Short-term borrowings", "4", "3,000.00", "2,500.00"),
    ("(b) Trade payables", "5", "9,000.00", "8,000.00"),
    ("(c) Other current liabilities", "", "2,000.00", "1,800.00"),
    ("(d) Short-term provisions", "", "1,500.00", "1,300.00"),
    ("TOTAL", "", "79,000.00", "74,000.00"),
    ("ASSETS",),
    ("Non-current assets",),
    ("(a) Fixed assets",),
    ("(i) Tangible assets", "6", "30,000.00", "28,000.00"),
    ("(ii) Intangible assets", "6", "500.00", "600.00"),
    ("(iii) Capital work-in-progress", "", "2,500.00", "1,400.00"),
    ("(b) Non-current investments", "7", "8,000.00", "7,000.00"),
    ("(c) Long-term loans and advances", "", "3,000.00", "2,000.00"),
    ("Current assets",),
    ("(a) Current investments", "", "2,000.00", "3,000.00"),
    ("(b) Inventories", "", "12,000.00", "11,000.00"),
    ("(c) Trade receivables", "", "10,000.00", "9,500.00"),
    ("(d) Cash and bank balances", "", "6,000.00", "5,000.00"),
    ("(e) Short-term loans and advances", "", "5,000.00", "6,500.00"),
    ("TOTAL", "", "79,000.00", "74,000.00"),
]
IGAAP_CF: list[Row] = [
    ("A. Cash flow from operating activities",),
    ("Net profit before tax", "", "9,000.00", "8,000.00"),
    ("Net cash from operating activities (A)", "", "11,000.00", "9,000.00"),
    ("B. Cash flow from investing activities",),
    ("Purchase of fixed assets", "", "(5,600.00)", "(4,000.00)"),
    ("Sale of fixed assets", "", "100.00", "-"),
    ("Net cash used in investing activities (B)", "", "(5,500.00)", "(4,000.00)"),
    ("C. Cash flow from financing activities",),
    ("Repayment of long-term borrowings", "", "(2,000.00)", "(1,000.00)"),
    ("Dividend paid (including dividend distribution tax)", "", "(2,500.00)", "(2,300.00)"),
    ("Net cash used in financing activities (C)", "", "(4,500.00)", "(3,300.00)"),
    ("Net increase in cash and cash equivalents (A+B+C)", "", "1,000.00", "1,700.00"),
]


def acme_fy2012_igaap() -> None:
    c = Canvas(str(HERE / "acme_ar_fy2012_igaap.pdf"), pagesize=A4)
    _text_page(c, ["ACME INDUSTRIES LIMITED", "22nd Annual Report 2011-2012"])
    unit = "(Rs. in Lakhs)"
    _page(
        c,
        ["BALANCE SHEET AS AT 31ST MARCH, 2012"],
        ("As at 31.03.2012", "As at 31.03.2011"),
        unit,
        IGAAP_BS,
        header_note="Note No.",
    )
    _page(
        c,
        ["CASH FLOW STATEMENT FOR THE YEAR ENDED 31ST MARCH, 2012"],
        ("Year ended 31.03.2012", "Year ended 31.03.2011"),
        unit,
        IGAAP_CF,
        header_note="",
    )
    c.save()


def scanned() -> None:
    """An image-only report (no text layer), as scanned older reports are."""
    c = Canvas(str(HERE / "scanned_ar.pdf"), pagesize=A4)
    for _ in range(2):
        c.rect(50, 50, W - 100, H - 100, fill=1)
        c.showPage()
    c.save()


if __name__ == "__main__":
    acme_fy2024()
    acme_fy2012_igaap()
    scanned()
    print("written to", HERE)
