"""Hand-computable company for the fundamentals tests (₹ crore).

FY2019-FY2023 are identical "base" years; FY2024 differs. Every expected value in
test_fundamentals.py is derived from these numbers in a comment next to the assertion.
"""

import pandas as pd

BASE = {
    "revenue": 1000.0, "cogs": 500.0, "ebitda": 200.0, "other_income": 10.0,
    "depreciation": 50.0, "ebit": 160.0, "interest": 10.0, "pbt": 150.0, "tax": 45.0,
    "pat": 105.0, "eps_diluted": 10.5, "shares_diluted_cr": 10.0, "sga": 100.0,
    "total_assets": 1000.0, "current_assets": 400.0, "current_liabilities": 200.0,
    "total_equity": 600.0, "retained_earnings": 500.0, "total_debt": 100.0,
    "cash_and_equivalents": 50.0, "non_operating_investments": 50.0, "receivables": 100.0,
    "inventory": 60.0, "payables": 50.0, "net_block": 400.0, "cfo": 160.0,
    "purchase_of_fixed_assets": 80.0, "sale_of_fixed_assets": 0.0,
}  # fmt: skip

FY2024 = {
    "revenue": 1500.0, "cogs": 730.0, "ebitda": 330.0, "other_income": 20.0,
    "depreciation": 80.0, "ebit": 270.0, "interest": 10.0, "pbt": 260.0, "tax": 78.0,
    "pat": 182.0, "eps_diluted": 17.5, "shares_diluted_cr": 10.5, "sga": 140.0,
    "total_assets": 1200.0, "current_assets": 500.0, "current_liabilities": 200.0,
    "total_equity": 700.0, "retained_earnings": 600.0, "total_debt": 100.0,
    "cash_and_equivalents": 60.0, "non_operating_investments": 40.0, "receivables": 150.0,
    "inventory": 100.0, "payables": 73.0, "net_block": 500.0, "cfo": 264.0,
    "purchase_of_fixed_assets": 120.0, "sale_of_fixed_assets": 20.0,
}  # fmt: skip


def annual_frame(overrides: dict[int, dict[str, float | None]] | None = None) -> pd.DataFrame:
    rows = {}
    for year in range(2019, 2025):
        rec = dict(FY2024 if year == 2024 else BASE)
        rec.update((overrides or {}).get(year, {}))
        rec["fiscal_year"] = year
        rows[pd.Timestamp(f"{year}-03-31")] = rec
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "period_end"
    return df


BANK_2023 = {
    "interest_earned": 1000.0, "interest_expended": 600.0, "advances": 8000.0,
    "gross_advances": 8200.0, "investments": 2000.0, "deposits": 9000.0,
    "casa_deposits": 3600.0, "gross_npa": 250.0, "net_npa": 80.0,
    "loan_loss_provisions": 60.0, "operating_expenses": 250.0, "crar_pct": 17.5,
}  # fmt: skip
BANK_2024 = {
    "interest_earned": 1200.0, "interest_expended": 700.0, "advances": 9000.0,
    "gross_advances": 9200.0, "investments": 2000.0, "deposits": 10000.0,
    "casa_deposits": 4200.0, "gross_npa": 230.0, "net_npa": 72.0,
    "loan_loss_provisions": 85.0, "operating_expenses": 270.0, "crar_pct": 18.1,
}  # fmt: skip


def bank_annual_frame() -> pd.DataFrame:
    rows = {
        pd.Timestamp("2023-03-31"): {"fiscal_year": 2023, "pat": 150.0, "other_income": 100.0,
                                     "total_assets": 11000.0, "total_equity": 1000.0,
                                     "extra": BANK_2023},
        pd.Timestamp("2024-03-31"): {"fiscal_year": 2024, "pat": 180.0, "other_income": 100.0,
                                     "total_assets": 12000.0, "total_equity": 1200.0,
                                     "extra": BANK_2024},
    }  # fmt: skip
    return pd.DataFrame.from_dict(rows, orient="index")
