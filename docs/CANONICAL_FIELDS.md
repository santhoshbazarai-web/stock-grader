# Canonical fundamentals fields

Generated from `backend/app/data/canonical.py` (`CANONICAL_FIELDS`) by
`uv run python -m app.data.canonical > ../docs/CANONICAL_FIELDS.md`. Do not edit by hand.

Units: `cr` ₹ crore, `rs` ₹ per share, `cr_shares` shares in crore, `pct` 0-100.
Labels are tried left to right; `—` means the source cannot supply the field (it stays
NULL and is recorded as a data gap). For NSE, fin-table labels are XBRL element names
from `backend/app/fundamentals/xbrl_map.yaml` (namespace ignored; `group:` says which
results format: ind_as, bank or pre_ind_as). Check a real filing with
`python -m app.jobs xbrl-inspect`.

| Field | Unit | Tables | Description | Screener (Data Sheet) | yfinance | NSE (results XBRL; shareholding API) |
|---|---|---|---|---|---|---|
| `revenue` | cr | fin_annual, fin_quarterly | Net sales / revenue from operations (banks: interest earned) | `Sales` | `TotalRevenue` / `OperatingRevenue` | `ind_as:RevenueFromOperations` / `bank:InterestEarned` / `pre_ind_as:NetSalesIncomeFromOperations` / `pre_ind_as:TotalIncomeFromOperationsNet` / `pre_ind_as:IncomeFromOperations` |
| `cogs` | cr | fin_annual, fin_quarterly | Cost of goods sold (materials consumed) | derived: Raw Material Cost - Change in Inventory (annual only) | `CostOfRevenue` | sum of ind_as: `CostOfMaterialsConsumed` + `PurchasesOfStockInTrade` + `ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade` / sum of pre_ind_as: `ConsumptionOfRawMaterials` |
| `ebitda` | cr | fin_annual, fin_quarterly | Operating profit before D&A, excluding other income (yfinance EBITDA may include it) | `Operating Profit` / derived: annual: pbt + interest + depreciation - other_income (quarterly uses the 'Operating Profit' row) | `EBITDA` | derived: pbt + interest + depreciation - other_income |
| `other_income` | cr | fin_annual, fin_quarterly | Non-operating / other income | `Other Income` | `OtherNonOperatingIncomeExpenses` / `OtherIncomeExpense` | `ind_as:OtherIncome` |
| `depreciation` | cr | fin_annual, fin_quarterly | Depreciation and amortisation | `Depreciation` | `ReconciledDepreciation` | `ind_as:DepreciationDepletionAndAmortisationExpense` / `pre_ind_as:DepreciationAndAmortisationExpense` |
| `ebit` | cr | fin_annual, fin_quarterly | Earnings before interest and tax (PBT + interest; includes other income) | derived: pbt + interest | `EBIT` | derived: pbt + interest |
| `interest` | cr | fin_annual, fin_quarterly | Finance cost (banks: interest expended) | `Interest` | `InterestExpense` | `ind_as:FinanceCosts` / `bank:InterestExpended` |
| `pbt` | cr | fin_annual, fin_quarterly | Profit before tax | `Profit before tax` | `PretaxIncome` | `ind_as:ProfitBeforeTax` / `bank:ProfitLossFromOrdinaryActivitiesBeforeTax` |
| `tax` | cr | fin_annual, fin_quarterly | Tax expense | `Tax` | `TaxProvision` | `ind_as:TaxExpense` / sum of ind_as: `CurrentTax` + `DeferredTax` |
| `pat` | cr | fin_annual, fin_quarterly | Net profit attributable to shareholders | `Net profit` | `NetIncomeCommonStockholders` / `NetIncome` | `ind_as:ProfitOrLossAttributableToOwnersOfParent` / `ind_as:ProfitLossForPeriod` / `bank:NetProfitLossForThePeriod` / `pre_ind_as:NetProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates` / `pre_ind_as:NetProfitLossForThePeriod` |
| `sga` | cr | fin_annual | Selling, general and administrative expenses (Beneish SGAI) | `Selling and admin` | `SellingGeneralAndAdministration` | — |
| `minority_interest_pl` | cr | fin_annual, fin_quarterly | Profit attributable to minority interests | — | `MinorityInterests` (negated) | `ind_as:ProfitOrLossAttributableToNonControllingInterests` / `pre_ind_as:MinorityInterest` |
| `eps_diluted` | rs | fin_annual, fin_quarterly | Diluted EPS (quarterly: for the quarter, not annualised) | derived: pat / shares_diluted_cr (annual only) | `DilutedEPS` | `ind_as:DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations` / `ind_as:DilutedEarningsLossPerShareFromContinuingOperations` / `pre_ind_as:DilutedEarningsPerShareAfterExtraordinaryItems` / `pre_ind_as:DilutedEarningsPerShareBeforeExtraordinaryItems` |
| `shares_diluted_cr` | cr_shares | fin_annual, fin_quarterly | Diluted (bonus/split-adjusted) share count, crore | `Adjusted Equity Shares in Cr` | `DilutedAverageShares` | derived: pat / eps_diluted (the filing states no share count) |
| `total_assets` | cr | fin_annual | Total assets | `Total` | `TotalAssets` | `ind_as:Assets` / `pre_ind_as:TotalAssets` |
| `current_assets` | cr | fin_annual | Current assets (Piotroski current ratio, Altman working capital) | — | `CurrentAssets` | `ind_as:CurrentAssets` / `pre_ind_as:TotalCurrentAssets` |
| `current_liabilities` | cr | fin_annual | Current liabilities | — | `CurrentLiabilities` | `ind_as:CurrentLiabilities` / `pre_ind_as:TotalCurrentLiabilities` |
| `total_equity` | cr | fin_annual | Shareholders' equity (excluding minority interest) | derived: Equity Share Capital + Reserves | `StockholdersEquity` | `ind_as:EquityAttributableToOwnersOfParent` / `ind_as:Equity` / `pre_ind_as:ShareholdersFunds` / `pre_ind_as:TotalShareholdersFunds` / sum of bank: `Capital` + `ReservesAndSurplus` |
| `retained_earnings` | cr | fin_annual | Retained earnings (Screener/XBRL: reserves / other equity, which also holds share premium) | `Reserves` | `RetainedEarnings` | `ind_as:OtherEquity` / `bank:ReservesAndSurplus` / `pre_ind_as:ReservesAndSurplus` |
| `minority_interest_bs` | cr | fin_annual | Minority interest (balance sheet) | — | `MinorityInterest` | `ind_as:NonControllingInterest` / `pre_ind_as:MinorityInterest` |
| `total_debt` | cr | fin_annual | Total borrowings | `Borrowings` | `TotalDebt` | `bank:Borrowings` / sum of ind_as: `BorrowingsNoncurrent` + `BorrowingsCurrent` / sum of pre_ind_as: `LongTermBorrowings` + `ShortTermBorrowings` |
| `cash_and_equivalents` | cr | fin_annual | Cash and bank balances | `Cash & Bank` | `CashAndCashEquivalents` | sum of ind_as: `CashAndCashEquivalents` + `BankBalanceOtherThanCashAndCashEquivalents` / sum of pre_ind_as: `CashAndBankBalances` |
| `non_operating_investments` | cr | fin_annual | Investments (treated as non-operating) | `Investments` | `LongTermEquityInvestment` / `InvestmentsAndAdvances` | sum of ind_as: `NoncurrentInvestments` + `CurrentInvestments` / sum of pre_ind_as: `NonCurrentInvestments` |
| `receivables` | cr | fin_annual | Trade receivables | `Receivables` | `AccountsReceivable` / `Receivables` | sum of ind_as: `TradeReceivablesCurrent` + `TradeReceivablesNoncurrent` / sum of pre_ind_as: `TradeReceivables` |
| `inventory` | cr | fin_annual | Inventories | `Inventory` | `Inventory` | `ind_as:Inventories` |
| `payables` | cr | fin_annual | Trade payables | — | `AccountsPayable` / `Payables` | `ind_as:TradePayablesCurrent` / `pre_ind_as:TradePayables` |
| `net_block` | cr | fin_annual | Net fixed assets | `Net Block` | `NetPPE` | sum of ind_as: `PropertyPlantAndEquipment` + `OtherIntangibleAssets` / sum of pre_ind_as: `TangibleAssets` + `IntangibleAssets` |
| `book_value_per_share` | rs | fin_annual | Book value per share | derived: total_equity / shares_diluted_cr | — | derived: total_equity / shares_diluted_cr |
| `cfo` | cr | fin_annual | Cash from operating activities | `Cash from Operating Activity` | `OperatingCashFlow` | `ind_as:CashFlowsFromUsedInOperatingActivities` |
| `purchase_of_fixed_assets` | cr | fin_annual | Capex outflow (positive) | — | `PurchaseOfPPE` (negated) | `ind_as:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities` / (magnitude) |
| `sale_of_fixed_assets` | cr | fin_annual | Proceeds from sale of fixed assets | — | `SaleOfPPE` | `ind_as:ProceedsFromSalesOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities` / (magnitude) |
| `dividends_paid` | cr | fin_annual | Dividends paid (Screener: dividend amount for the year) | `Dividend Amount` | `CashDividendsPaid` (negated) / `CommonStockDividendPaid` (negated) | `ind_as:DividendsPaidClassifiedAsFinancingActivities` / (magnitude) |
| `promoter_pct` | pct | shareholding | Promoter & promoter group holding | `Promoters` | — | `pr_and_prgrp` |
| `promoter_pledge_pct` | pct | shareholding | Share of promoter holding pledged | — | — | — |
| `fii_pct` | pct | shareholding | Foreign institutional holding | `FIIs` | — | — |
| `dii_pct` | pct | shareholding | Domestic institutional holding | `DIIs` | — | — |
| `mf_pct` | pct | shareholding | Mutual fund holding | — | — | — |
| `public_pct` | pct | shareholding | Public / non-institutional holding | `Public` | — | `public_val` |
| `num_shareholders` | count | shareholding | Number of shareholders | `No. of Shareholders` | — | — |
