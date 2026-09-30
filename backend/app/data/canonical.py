"""Canonical fundamentals schema and the ONE mapping from every source onto it.

``CANONICAL_FIELDS`` is the single source of truth: for each canonical field (a column of
``fin_annual`` / ``fin_quarterly`` / ``shareholding``) it records the unit, which tables carry
it, and the exact label each source uses (for ``nse`` on the fin tables: the element names of
the exchange results XBRL; see ``XBRL_*`` below). Parsers never hard-code labels; they look them up
here. Add a source label here and every parser picks it up.

Units
- ``cr``   ₹ crore. Screener exports are already in crore; yfinance and XBRL report absolute
           rupees and are divided by 1e7.
- ``rs``   ₹ per share (EPS, book value per share); never scaled.
- ``cr_shares`` share count in crore (yfinance absolute counts are divided by 1e7).
- ``pct``  percentage points 0-100.
- ``count`` plain count.

Sign convention: every canonical *amount* is stated as a positive magnitude of what the
name says (``purchase_of_fixed_assets`` is the cash spent, ``dividends_paid`` the cash paid).
Sources that report outflows as negatives carry ``sign=-1`` on the label.

Labels are tried in order; the first one present with a non-null value wins. A field with no
label for a source is not available from that source and stays NULL (→ data gap, rule 1).
Fields marked ``derived`` for a source are computed in that source's parser from other
canonical fields; the formula is in ``derived`` so it is documented here too.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import pandas as pd

Unit = Literal["cr", "rs", "cr_shares", "pct", "count"]
Table = Literal["fin_annual", "fin_quarterly", "shareholding"]
Source = Literal["screener", "yfinance", "nse"]

ANNUAL: tuple[Table, ...] = ("fin_annual",)
BOTH: tuple[Table, ...] = ("fin_annual", "fin_quarterly")
SHP: tuple[Table, ...] = ("shareholding",)


@dataclass(frozen=True)
class Label:
    text: str
    sign: int = 1


@dataclass(frozen=True)
class FieldSpec:
    unit: Unit
    tables: tuple[Table, ...]
    description: str
    labels: Mapping[Source, tuple[Label, ...]] = field(default_factory=dict)
    derived: Mapping[Source, str] = field(default_factory=dict)


def _l(*texts: str, sign: int = 1) -> tuple[Label, ...]:
    return tuple(Label(t, sign) for t in texts)


# Screener "Data Sheet" labels: annual P&L / balance sheet / cash flow rows, and the
# "Quarters" block. yfinance labels: raw keys from Ticker.get_*(pretty=False).
CANONICAL_FIELDS: dict[str, FieldSpec] = {
    # ───────── P&L (annual + quarterly) ─────────
    "revenue": FieldSpec(
        "cr", BOTH, "Net sales / revenue from operations (banks: interest earned)",
        {"screener": _l("Sales"), "yfinance": _l("TotalRevenue", "OperatingRevenue"),
         "nse": _l("RevenueFromOperations", "InterestEarned")},
    ),
    "cogs": FieldSpec(
        "cr", BOTH, "Cost of goods sold (materials consumed)",
        {"yfinance": _l("CostOfRevenue")},
        {"screener": "Raw Material Cost - Change in Inventory (annual only)",
         "nse": "sum of the reported XBRL_SUMS['cogs'] lines (materials + purchases + "
                "change in inventories)"},
    ),
    "ebitda": FieldSpec(
        "cr", BOTH,
        "Operating profit before D&A, excluding other income (yfinance EBITDA may include it)",
        {"screener": _l("Operating Profit"), "yfinance": _l("EBITDA")},
        {"screener": "annual: pbt + interest + depreciation - other_income "
                     "(quarterly uses the 'Operating Profit' row)",
         "nse": "pbt + interest + depreciation - other_income"},
    ),
    "other_income": FieldSpec(
        "cr", BOTH, "Non-operating / other income",
        {"screener": _l("Other Income"),
         "yfinance": _l("OtherNonOperatingIncomeExpenses", "OtherIncomeExpense"),
         "nse": _l("OtherIncome")},
    ),
    "depreciation": FieldSpec(
        "cr", BOTH, "Depreciation and amortisation",
        {"screener": _l("Depreciation"), "yfinance": _l("ReconciledDepreciation"),
         "nse": _l("DepreciationDepletionAndAmortisationExpense")},
    ),
    "ebit": FieldSpec(
        "cr", BOTH, "Earnings before interest and tax (PBT + interest; includes other income)",
        {"yfinance": _l("EBIT")},
        {"screener": "pbt + interest", "nse": "pbt + interest"},
    ),
    "interest": FieldSpec(
        "cr", BOTH, "Finance cost (banks: interest expended)",
        {"screener": _l("Interest"), "yfinance": _l("InterestExpense"),
         "nse": _l("FinanceCosts", "InterestExpended")},
    ),
    "pbt": FieldSpec(
        "cr", BOTH, "Profit before tax",
        {"screener": _l("Profit before tax"), "yfinance": _l("PretaxIncome"),
         "nse": _l("ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax")},
    ),
    "tax": FieldSpec(
        "cr", BOTH, "Tax expense",
        {"screener": _l("Tax"), "yfinance": _l("TaxProvision"), "nse": _l("TaxExpense")},
        {"nse": "else the sum of XBRL_SUMS['tax'] (current + deferred)"},
    ),
    "pat": FieldSpec(
        "cr", BOTH, "Net profit attributable to shareholders",
        {"screener": _l("Net profit"),
         "yfinance": _l("NetIncomeCommonStockholders", "NetIncome"),
         "nse": _l("ProfitOrLossAttributableToOwnersOfParent", "ProfitLossForPeriod",
                   "NetProfitLossForThePeriod")},
    ),
    "sga": FieldSpec(
        "cr", ANNUAL, "Selling, general and administrative expenses (Beneish SGAI)",
        {"screener": _l("Selling and admin"), "yfinance": _l("SellingGeneralAndAdministration")},
    ),
    "minority_interest_pl": FieldSpec(
        "cr", BOTH, "Profit attributable to minority interests",
        {"yfinance": _l("MinorityInterests", sign=-1),
         "nse": _l("ProfitOrLossAttributableToNonControllingInterests")},
    ),
    "eps_diluted": FieldSpec(
        "rs", BOTH, "Diluted EPS (quarterly: for the quarter, not annualised)",
        {"yfinance": _l("DilutedEPS"),
         "nse": _l("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                   "DilutedEarningsLossPerShareFromContinuingOperations")},
        {"screener": "pat / shares_diluted_cr (annual only)"},
    ),
    "shares_diluted_cr": FieldSpec(
        "cr_shares", BOTH, "Diluted (bonus/split-adjusted) share count, crore",
        {"screener": _l("Adjusted Equity Shares in Cr"), "yfinance": _l("DilutedAverageShares")},
        {"nse": "pat / eps_diluted (the filing states no share count)"},
    ),
    # ───────── balance sheet (annual) ─────────
    "total_assets": FieldSpec(
        "cr", ANNUAL, "Total assets",
        {"screener": _l("Total"), "yfinance": _l("TotalAssets"), "nse": _l("Assets")},
    ),
    "current_assets": FieldSpec(
        "cr", ANNUAL, "Current assets (Piotroski current ratio, Altman working capital)",
        {"yfinance": _l("CurrentAssets"), "nse": _l("CurrentAssets")},
    ),
    "current_liabilities": FieldSpec(
        "cr", ANNUAL, "Current liabilities",
        {"yfinance": _l("CurrentLiabilities"), "nse": _l("CurrentLiabilities")},
    ),
    "total_equity": FieldSpec(
        "cr", ANNUAL, "Shareholders' equity (excluding minority interest)",
        {"yfinance": _l("StockholdersEquity"),
         "nse": _l("EquityAttributableToOwnersOfParent", "Equity")},
        {"screener": "Equity Share Capital + Reserves"},
    ),
    "retained_earnings": FieldSpec(
        "cr", ANNUAL, "Retained earnings (Screener/XBRL: reserves / other equity, which also "
                      "holds share premium)",
        {"screener": _l("Reserves"), "yfinance": _l("RetainedEarnings"),
         "nse": _l("OtherEquity")},
    ),
    "minority_interest_bs": FieldSpec(
        "cr", ANNUAL, "Minority interest (balance sheet)",
        {"yfinance": _l("MinorityInterest"), "nse": _l("NonControllingInterest")},
    ),
    "total_debt": FieldSpec(
        "cr", ANNUAL, "Total borrowings",
        {"screener": _l("Borrowings"), "yfinance": _l("TotalDebt")},
        {"nse": "sum of the reported XBRL_SUMS['total_debt'] lines (non-current + current)"},
    ),
    "cash_and_equivalents": FieldSpec(
        "cr", ANNUAL, "Cash and bank balances",
        {"screener": _l("Cash & Bank"), "yfinance": _l("CashAndCashEquivalents")},
        {"nse": "sum of the reported XBRL_SUMS['cash_and_equivalents'] lines"},
    ),
    "non_operating_investments": FieldSpec(
        "cr", ANNUAL, "Investments (treated as non-operating)",
        {"screener": _l("Investments"),
         "yfinance": _l("LongTermEquityInvestment", "InvestmentsAndAdvances")},
        {"nse": "sum of the reported XBRL_SUMS['non_operating_investments'] lines"},
    ),
    "receivables": FieldSpec(
        "cr", ANNUAL, "Trade receivables",
        {"screener": _l("Receivables"), "yfinance": _l("AccountsReceivable", "Receivables")},
        {"nse": "sum of the reported XBRL_SUMS['receivables'] lines"},
    ),
    "inventory": FieldSpec(
        "cr", ANNUAL, "Inventories",
        {"screener": _l("Inventory"), "yfinance": _l("Inventory"), "nse": _l("Inventories")},
    ),
    "payables": FieldSpec(
        "cr", ANNUAL, "Trade payables",
        {"yfinance": _l("AccountsPayable", "Payables"), "nse": _l("TradePayablesCurrent")},
    ),
    "net_block": FieldSpec(
        "cr", ANNUAL, "Net fixed assets",
        {"screener": _l("Net Block"), "yfinance": _l("NetPPE")},
        {"nse": "sum of the reported XBRL_SUMS['net_block'] lines (PPE + other intangibles)"},
    ),
    "book_value_per_share": FieldSpec(
        "rs", ANNUAL, "Book value per share", {},
        {"screener": "total_equity / shares_diluted_cr", "nse": "total_equity / shares_diluted_cr"},
    ),
    # ───────── cash flow (annual) ─────────
    "cfo": FieldSpec(
        "cr", ANNUAL, "Cash from operating activities",
        {"screener": _l("Cash from Operating Activity"), "yfinance": _l("OperatingCashFlow"),
         "nse": _l("CashFlowsFromUsedInOperatingActivities")},
    ),
    "purchase_of_fixed_assets": FieldSpec(
        "cr", ANNUAL, "Capex outflow (positive)",
        {"yfinance": _l("PurchaseOfPPE", sign=-1),
         "nse": _l("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities")},
    ),
    "sale_of_fixed_assets": FieldSpec(
        "cr", ANNUAL, "Proceeds from sale of fixed assets",
        {"yfinance": _l("SaleOfPPE"),
         "nse": _l("ProceedsFromSalesOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities")},
    ),
    "dividends_paid": FieldSpec(
        "cr", ANNUAL, "Dividends paid (Screener: dividend amount for the year)",
        {"screener": _l("Dividend Amount"),
         "yfinance": _l("CashDividendsPaid", "CommonStockDividendPaid", sign=-1),
         "nse": _l("DividendsPaidClassifiedAsFinancingActivities")},
    ),
    # ───────── shareholding (quarterly filings) ─────────
    "promoter_pct": FieldSpec(
        "pct", SHP, "Promoter & promoter group holding",
        {"screener": _l("Promoters"), "nse": _l("pr_and_prgrp")},
    ),
    "promoter_pledge_pct": FieldSpec("pct", SHP, "Share of promoter holding pledged", {}),
    "fii_pct": FieldSpec("pct", SHP, "Foreign institutional holding", {"screener": _l("FIIs")}),
    "dii_pct": FieldSpec("pct", SHP, "Domestic institutional holding", {"screener": _l("DIIs")}),
    "mf_pct": FieldSpec("pct", SHP, "Mutual fund holding", {}),
    "public_pct": FieldSpec(
        "pct", SHP, "Public / non-institutional holding",
        {"screener": _l("Public"), "nse": _l("public_val")},
    ),
    "num_shareholders": FieldSpec(
        "count", SHP, "Number of shareholders", {"screener": _l("No. of Shareholders")},
    ),
}  # fmt: skip

# yfinance reports absolute rupees / share counts.
YFINANCE_SCALE: dict[Unit, float] = {
    "cr": 1e-7,
    "cr_shares": 1e-7,
    "rs": 1.0,
    "pct": 1.0,
    "count": 1.0,
}


# ───────── NSE / BSE results XBRL (SEBI Ind AS results taxonomy, prefix ``in-bse-fin``) ─────────
# Element *local names*; the namespace (which changes with each taxonomy year) is ignored.
# ``CANONICAL_FIELDS[...].labels["nse"]`` lists the elements read directly. The tables below are
# for lines the filing splits up, the filing's own descriptive facts, and bank-only inputs.
# Check a real filing with ``python -m app.jobs xbrl-inspect <file.xml>`` and extend these.

# Canonical field → lines summed when the filing has no single total. At least one line must
# be reported; lines a filing does not tag are nil in its layout (they are not assumed zero
# when *every* line is missing — the field is then NULL).
XBRL_SUMS: dict[str, tuple[str, ...]] = {
    "cogs": ("CostOfMaterialsConsumed", "PurchasesOfStockInTrade",
             "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade"),
    "tax": ("CurrentTax", "DeferredTax"),
    "total_debt": ("BorrowingsNoncurrent", "BorrowingsCurrent"),
    "cash_and_equivalents": ("CashAndCashEquivalents",
                             "BankBalanceOtherThanCashAndCashEquivalents"),
    "non_operating_investments": ("NoncurrentInvestments", "CurrentInvestments"),
    "receivables": ("TradeReceivablesCurrent", "TradeReceivablesNoncurrent"),
    "net_block": ("PropertyPlantAndEquipment", "OtherIntangibleAssets"),
}  # fmt: skip

# Cash-flow lines canonicalised as positive magnitudes whatever sign the filer used.
XBRL_MAGNITUDES: frozenset[str] = frozenset(
    {"purchase_of_fixed_assets", "sale_of_fixed_assets", "dividends_paid"}
)

# Descriptive facts of a results filing (text / dates).
XBRL_INFO: dict[str, tuple[str, ...]] = {
    "company": ("NameOfTheCompany",),
    "symbol": ("Symbol", "NSESymbol"),
    "scrip_code": ("ScripCode", "BSEScripCode"),
    "isin": ("ISIN",),
    "period_start": ("DateOfStartOfReportingPeriod",),
    "period_end": ("DateOfEndOfReportingPeriod",),
    "fy_start": ("DateOfStartOfFinancialYear",),
    "nature": ("NatureOfReportStandaloneConsolidated",),
    "audited": ("WhetherResultsAreAuditedOrUnaudited",),
    "board_meeting": ("DateOfBoardMeetingWhenFinancialResultsWereApproved",
                      "DateOfBoardMeeting"),
}  # fmt: skip

# Bank results → ``extra`` keys read by app.fundamentals.banking (BANK_FIELDS). Presence of
# ``InterestEarned`` marks a bank-format filing.
XBRL_BANK_EXTRA: dict[str, tuple[str, ...]] = {
    "interest_earned": ("InterestEarned",),
    "interest_expended": ("InterestExpended",),
    "operating_expenses": ("OperatingExpenses",),
    "loan_loss_provisions": ("ProvisionsOtherThanTaxAndContingencies",),
    "gross_npa": ("GrossNonPerformingAssets", "GrossNpa"),
    "net_npa": ("NetNonPerformingAssets", "NetNpa"),
    "crar_pct": ("CapitalAdequacyRatioBaselIii", "CapitalAdequacyRatio"),
    "advances": ("Advances",),
    "investments": ("Investments",),
    "deposits": ("Deposits",),
}  # fmt: skip
XBRL_BANK_MARKER = "InterestEarned"
# Bank figures that are balances at period end (not flows): a Q4 filing often reports them only
# in the quarter's context, and they hold for the fiscal year that ends on the same day.
XBRL_BANK_STOCKS: frozenset[str] = frozenset(
    {"gross_npa", "net_npa", "crar_pct", "advances", "investments", "deposits"}
)
XBRL_BANK_PCT: frozenset[str] = frozenset({"crar_pct"})


def fields_for(table: Table) -> list[str]:
    return [name for name, spec in CANONICAL_FIELDS.items() if table in spec.tables]


def labels_for(source: Source, table: Table) -> dict[str, tuple[Label, ...]]:
    return {
        name: spec.labels[source]
        for name, spec in CANONICAL_FIELDS.items()
        if table in spec.tables and source in spec.labels
    }


def unavailable_from(source: Source, table: Table) -> list[str]:
    """Canonical fields this source can neither read nor derive (→ data gaps)."""
    return [
        name
        for name, spec in CANONICAL_FIELDS.items()
        if table in spec.tables and source not in spec.labels and source not in spec.derived
    ]


def pick(values: Mapping[str, Any], labels: tuple[Label, ...], scale: float = 1.0) -> float | None:
    """First label with a usable number, scaled and signed; ``None`` if none."""
    for label in labels:
        raw = values.get(label.text)
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        try:
            return float(raw) * label.sign * scale
        except (TypeError, ValueError):
            continue
    return None


def fiscal_year(period_end: pd.Timestamp | datetime) -> int:
    """Calendar year in which the fiscal year ends (Mar-2025 → 2025, Dec-2024 → 2024)."""
    return int(period_end.year)


def frame_to_rows(
    df: pd.DataFrame,
    table: Table,
    *,
    instrument_id: int,
    source: str,
    fetched_at: datetime,
    extra_cols: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Canonical frame (index = period end) → upsert dicts with every canonical column present
    and NaN turned into ``None`` (never 0)."""
    cols = fields_for(table)
    rows = []
    for period_end, rec in df.iterrows():
        row: dict[str, Any] = {
            "instrument_id": instrument_id,
            "period_end": pd.Timestamp(str(period_end)).date(),
            "source": source,
            "fetched_at": fetched_at,
            **(extra_cols or {}),
        }
        for col in cols:
            v = rec.get(col)
            row[col] = None if v is None or pd.isna(v) else v
        for col in ("announcement_date", "filing_date", "statement_type", "fiscal_year", "extra"):
            if col in rec.index:
                v = rec[col]
                row[col] = None if not isinstance(v, dict | str | int) and pd.isna(v) else v
        rows.append(row)
    return rows


def mapping_markdown() -> str:
    """Render ``CANONICAL_FIELDS`` as the table in docs/CANONICAL_FIELDS.md."""
    sources: tuple[Source, ...] = ("screener", "yfinance", "nse")

    def cell(spec: FieldSpec, source: Source) -> str:
        parts = [
            f"`{lbl.text}`" + (" (negated)" if lbl.sign < 0 else "")
            for lbl in spec.labels.get(source, ())
        ]
        if source in spec.derived:
            parts.append(f"derived: {spec.derived[source]}")
        return " / ".join(parts) or "—"

    lines = [
        "# Canonical fundamentals fields",
        "",
        "Generated from `backend/app/data/canonical.py` (`CANONICAL_FIELDS`) by",
        "`uv run python -m app.data.canonical > ../docs/CANONICAL_FIELDS.md`. Do not edit by hand.",
        "",
        "Units: `cr` ₹ crore, `rs` ₹ per share, `cr_shares` shares in crore, `pct` 0-100.",
        "Labels are tried left to right; `—` means the source cannot supply the field (it stays",
        "NULL and is recorded as a data gap). For NSE, fin-table labels are XBRL element names",
        "(namespace ignored); the line sums, bank extras and descriptive facts are the `XBRL_*`",
        "tables in `canonical.py`. Check a real filing with `python -m app.jobs xbrl-inspect`.",
        "",
        "| Field | Unit | Tables | Description | Screener (Data Sheet) | yfinance "
        "| NSE (results XBRL; shareholding API) |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, spec in CANONICAL_FIELDS.items():
        tables = ", ".join(spec.tables)
        row = [f"`{name}`", spec.unit, tables, spec.description]
        row += [cell(spec, s) for s in sources]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(mapping_markdown(), end="")
