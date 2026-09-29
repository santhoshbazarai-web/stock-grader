# Canonical fundamentals fields

Generated from `backend/app/data/canonical.py` (`CANONICAL_FIELDS`) by
`uv run python -m app.data.canonical > ../docs/CANONICAL_FIELDS.md`. Do not edit by hand.

Units: `cr` ₹ crore, `rs` ₹ per share, `cr_shares` shares in crore, `pct` 0-100.
Labels are tried left to right; `—` means the source cannot supply the field (it stays
NULL and is recorded as a data gap).

| Field | Unit | Tables | Description | Screener (Data Sheet) | yfinance | NSE |
|---|---|---|---|---|---|---|
| `revenue` | cr | fin_annual, fin_quarterly | Net sales / revenue from operations | `Sales` | `TotalRevenue` / `OperatingRevenue` | — |
| `cogs` | cr | fin_annual, fin_quarterly | Cost of goods sold (materials consumed) | derived: Raw Material Cost - Change in Inventory (annual only) | `CostOfRevenue` | — |
| `ebitda` | cr | fin_annual, fin_quarterly | Operating profit before D&A, excluding other income (yfinance EBITDA may include it) | `Operating Profit` / derived: annual: pbt + interest + depreciation - other_income (quarterly uses the 'Operating Profit' row) | `EBITDA` | — |
| `other_income` | cr | fin_annual, fin_quarterly | Non-operating / other income | `Other Income` | `OtherNonOperatingIncomeExpenses` / `OtherIncomeExpense` | — |
| `depreciation` | cr | fin_annual, fin_quarterly | Depreciation and amortisation | `Depreciation` | `ReconciledDepreciation` | — |
| `ebit` | cr | fin_annual, fin_quarterly | Earnings before interest and tax (PBT + interest; includes other income) | derived: pbt + interest | `EBIT` | — |
| `interest` | cr | fin_annual, fin_quarterly | Finance cost | `Interest` | `InterestExpense` | — |
| `pbt` | cr | fin_annual, fin_quarterly | Profit before tax | `Profit before tax` | `PretaxIncome` | — |
| `tax` | cr | fin_annual, fin_quarterly | Tax expense | `Tax` | `TaxProvision` | — |
| `pat` | cr | fin_annual, fin_quarterly | Net profit attributable to shareholders | `Net profit` | `NetIncomeCommonStockholders` / `NetIncome` | — |
| `minority_interest_pl` | cr | fin_annual, fin_quarterly | Profit attributable to minority interests | — | `MinorityInterests` (negated) | — |
| `eps_diluted` | rs | fin_annual, fin_quarterly | Diluted EPS | derived: pat / shares_diluted_cr (annual only) | `DilutedEPS` | — |
| `shares_diluted_cr` | cr_shares | fin_annual, fin_quarterly | Diluted (bonus/split-adjusted) share count, crore | `Adjusted Equity Shares in Cr` | `DilutedAverageShares` | — |
| `total_assets` | cr | fin_annual | Total assets | `Total` | `TotalAssets` | — |
| `current_liabilities` | cr | fin_annual | Current liabilities | — | `CurrentLiabilities` | — |
| `total_equity` | cr | fin_annual | Shareholders' equity (excluding minority interest) | derived: Equity Share Capital + Reserves | `StockholdersEquity` | — |
| `minority_interest_bs` | cr | fin_annual | Minority interest (balance sheet) | — | `MinorityInterest` | — |
| `total_debt` | cr | fin_annual | Total borrowings | `Borrowings` | `TotalDebt` | — |
| `cash_and_equivalents` | cr | fin_annual | Cash and bank balances | `Cash & Bank` | `CashAndCashEquivalents` | — |
| `non_operating_investments` | cr | fin_annual | Investments (treated as non-operating) | `Investments` | `LongTermEquityInvestment` / `InvestmentsAndAdvances` | — |
| `receivables` | cr | fin_annual | Trade receivables | `Receivables` | `AccountsReceivable` / `Receivables` | — |
| `inventory` | cr | fin_annual | Inventories | `Inventory` | `Inventory` | — |
| `payables` | cr | fin_annual | Trade payables | — | `AccountsPayable` / `Payables` | — |
| `net_block` | cr | fin_annual | Net fixed assets | `Net Block` | `NetPPE` | — |
| `book_value_per_share` | rs | fin_annual | Book value per share | derived: total_equity / shares_diluted_cr | — | — |
| `cfo` | cr | fin_annual | Cash from operating activities | `Cash from Operating Activity` | `OperatingCashFlow` | — |
| `purchase_of_fixed_assets` | cr | fin_annual | Capex outflow (positive) | — | `PurchaseOfPPE` (negated) | — |
| `sale_of_fixed_assets` | cr | fin_annual | Proceeds from sale of fixed assets | — | `SaleOfPPE` | — |
| `dividends_paid` | cr | fin_annual | Dividends paid (Screener: dividend amount for the year) | `Dividend Amount` | `CashDividendsPaid` (negated) / `CommonStockDividendPaid` (negated) | — |
| `promoter_pct` | pct | shareholding | Promoter & promoter group holding | `Promoters` | — | `pr_and_prgrp` |
| `promoter_pledge_pct` | pct | shareholding | Share of promoter holding pledged | — | — | — |
| `fii_pct` | pct | shareholding | Foreign institutional holding | `FIIs` | — | — |
| `dii_pct` | pct | shareholding | Domestic institutional holding | `DIIs` | — | — |
| `mf_pct` | pct | shareholding | Mutual fund holding | — | — | — |
| `public_pct` | pct | shareholding | Public / non-institutional holding | `Public` | — | `public_val` |
| `num_shareholders` | count | shareholding | Number of shareholders | `No. of Shareholders` | — | — |
