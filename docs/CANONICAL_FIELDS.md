# Canonical fundamentals fields

Generated from `backend/app/data/canonical.py` (`CANONICAL_FIELDS`) by
`uv run python -m app.data.canonical > ../docs/CANONICAL_FIELDS.md`. Do not edit by hand.

Units: `cr` ₹ crore, `rs` ₹ per share, `cr_shares` shares in crore, `pct` 0-100.
Labels are tried left to right; `—` means the source cannot supply the field (it stays
NULL and is recorded as a data gap). For NSE, fin-table labels are XBRL element names
(namespace ignored); the line sums, bank extras and descriptive facts are the `XBRL_*`
tables in `canonical.py`. Check a real filing with `python -m app.jobs xbrl-inspect`.

| Field | Unit | Tables | Description | Screener (Data Sheet) | yfinance | NSE (results XBRL; shareholding API) |
|---|---|---|---|---|---|---|
| `revenue` | cr | fin_annual, fin_quarterly | Net sales / revenue from operations (banks: interest earned) | `Sales` | `TotalRevenue` / `OperatingRevenue` | `RevenueFromOperations` / `InterestEarned` |
| `cogs` | cr | fin_annual, fin_quarterly | Cost of goods sold (materials consumed) | derived: Raw Material Cost - Change in Inventory (annual only) | `CostOfRevenue` | derived: sum of the reported XBRL_SUMS['cogs'] lines (materials + purchases + change in inventories) |
| `ebitda` | cr | fin_annual, fin_quarterly | Operating profit before D&A, excluding other income (yfinance EBITDA may include it) | `Operating Profit` / derived: annual: pbt + interest + depreciation - other_income (quarterly uses the 'Operating Profit' row) | `EBITDA` | derived: pbt + interest + depreciation - other_income |
| `other_income` | cr | fin_annual, fin_quarterly | Non-operating / other income | `Other Income` | `OtherNonOperatingIncomeExpenses` / `OtherIncomeExpense` | `OtherIncome` |
| `depreciation` | cr | fin_annual, fin_quarterly | Depreciation and amortisation | `Depreciation` | `ReconciledDepreciation` | `DepreciationDepletionAndAmortisationExpense` |
| `ebit` | cr | fin_annual, fin_quarterly | Earnings before interest and tax (PBT + interest; includes other income) | derived: pbt + interest | `EBIT` | derived: pbt + interest |
| `interest` | cr | fin_annual, fin_quarterly | Finance cost (banks: interest expended) | `Interest` | `InterestExpense` | `FinanceCosts` / `InterestExpended` |
| `pbt` | cr | fin_annual, fin_quarterly | Profit before tax | `Profit before tax` | `PretaxIncome` | `ProfitBeforeTax` / `ProfitLossFromOrdinaryActivitiesBeforeTax` |
| `tax` | cr | fin_annual, fin_quarterly | Tax expense | `Tax` | `TaxProvision` | `TaxExpense` / derived: else the sum of XBRL_SUMS['tax'] (current + deferred) |
| `pat` | cr | fin_annual, fin_quarterly | Net profit attributable to shareholders | `Net profit` | `NetIncomeCommonStockholders` / `NetIncome` | `ProfitOrLossAttributableToOwnersOfParent` / `ProfitLossForPeriod` / `NetProfitLossForThePeriod` |
| `sga` | cr | fin_annual | Selling, general and administrative expenses (Beneish SGAI) | `Selling and admin` | `SellingGeneralAndAdministration` | — |
| `minority_interest_pl` | cr | fin_annual, fin_quarterly | Profit attributable to minority interests | — | `MinorityInterests` (negated) | `ProfitOrLossAttributableToNonControllingInterests` |
| `eps_diluted` | rs | fin_annual, fin_quarterly | Diluted EPS (quarterly: for the quarter, not annualised) | derived: pat / shares_diluted_cr (annual only) | `DilutedEPS` | `DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations` / `DilutedEarningsLossPerShareFromContinuingOperations` |
| `shares_diluted_cr` | cr_shares | fin_annual, fin_quarterly | Diluted (bonus/split-adjusted) share count, crore | `Adjusted Equity Shares in Cr` | `DilutedAverageShares` | derived: pat / eps_diluted (the filing states no share count) |
| `total_assets` | cr | fin_annual | Total assets | `Total` | `TotalAssets` | `Assets` |
| `current_assets` | cr | fin_annual | Current assets (Piotroski current ratio, Altman working capital) | — | `CurrentAssets` | `CurrentAssets` |
| `current_liabilities` | cr | fin_annual | Current liabilities | — | `CurrentLiabilities` | `CurrentLiabilities` |
| `total_equity` | cr | fin_annual | Shareholders' equity (excluding minority interest) | derived: Equity Share Capital + Reserves | `StockholdersEquity` | `EquityAttributableToOwnersOfParent` / `Equity` |
| `retained_earnings` | cr | fin_annual | Retained earnings (Screener/XBRL: reserves / other equity, which also holds share premium) | `Reserves` | `RetainedEarnings` | `OtherEquity` |
| `minority_interest_bs` | cr | fin_annual | Minority interest (balance sheet) | — | `MinorityInterest` | `NonControllingInterest` |
| `total_debt` | cr | fin_annual | Total borrowings | `Borrowings` | `TotalDebt` | derived: sum of the reported XBRL_SUMS['total_debt'] lines (non-current + current) |
| `cash_and_equivalents` | cr | fin_annual | Cash and bank balances | `Cash & Bank` | `CashAndCashEquivalents` | derived: sum of the reported XBRL_SUMS['cash_and_equivalents'] lines |
| `non_operating_investments` | cr | fin_annual | Investments (treated as non-operating) | `Investments` | `LongTermEquityInvestment` / `InvestmentsAndAdvances` | derived: sum of the reported XBRL_SUMS['non_operating_investments'] lines |
| `receivables` | cr | fin_annual | Trade receivables | `Receivables` | `AccountsReceivable` / `Receivables` | derived: sum of the reported XBRL_SUMS['receivables'] lines |
| `inventory` | cr | fin_annual | Inventories | `Inventory` | `Inventory` | `Inventories` |
| `payables` | cr | fin_annual | Trade payables | — | `AccountsPayable` / `Payables` | `TradePayablesCurrent` |
| `net_block` | cr | fin_annual | Net fixed assets | `Net Block` | `NetPPE` | derived: sum of the reported XBRL_SUMS['net_block'] lines (PPE + other intangibles) |
| `book_value_per_share` | rs | fin_annual | Book value per share | derived: total_equity / shares_diluted_cr | — | derived: total_equity / shares_diluted_cr |
| `cfo` | cr | fin_annual | Cash from operating activities | `Cash from Operating Activity` | `OperatingCashFlow` | `CashFlowsFromUsedInOperatingActivities` |
| `purchase_of_fixed_assets` | cr | fin_annual | Capex outflow (positive) | — | `PurchaseOfPPE` (negated) | `PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities` |
| `sale_of_fixed_assets` | cr | fin_annual | Proceeds from sale of fixed assets | — | `SaleOfPPE` | `ProceedsFromSalesOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities` |
| `dividends_paid` | cr | fin_annual | Dividends paid (Screener: dividend amount for the year) | `Dividend Amount` | `CashDividendsPaid` (negated) / `CommonStockDividendPaid` (negated) | `DividendsPaidClassifiedAsFinancingActivities` |
| `promoter_pct` | pct | shareholding | Promoter & promoter group holding | `Promoters` | — | `pr_and_prgrp` |
| `promoter_pledge_pct` | pct | shareholding | Share of promoter holding pledged | — | — | — |
| `fii_pct` | pct | shareholding | Foreign institutional holding | `FIIs` | — | — |
| `dii_pct` | pct | shareholding | Domestic institutional holding | `DIIs` | — | — |
| `mf_pct` | pct | shareholding | Mutual fund holding | — | — | — |
| `public_pct` | pct | shareholding | Public / non-institutional holding | `Public` | — | `public_val` |
| `num_shareholders` | count | shareholding | Number of shareholders | `No. of Shareholders` | — | — |
