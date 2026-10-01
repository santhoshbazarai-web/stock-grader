# XBRL parser audit (P17)

This audits the NSE/BSE results-XBRL pipeline, as built in commit `bb47433`, against SPEC v0.2
§3.2a and §3.6 steps 1, 2 and 5. Line numbers refer to that commit. Status is **Met**,
**Partial** or **Missing**. The last column says what P17 changes; see
[Resolution](#resolution) for the commits.

The pipeline has four parts:
- `backend/app/data/xbrl.py`: pure parser.
- `backend/app/data/canonical.py`: element names.
- `backend/app/data/results_store.py`, `results_ingest.py`: writing.
- `backend/app/jobs/fundamentals.py::results_watch`: the job.

| # | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| 1 | A versioned tag-mapping file covering Ind-AS and pre-Ind-AS tags | **Partial** | Element names are in one place, `data/canonical.py` (`labels["nse"]` inside `CANONICAL_FIELDS`, l. 64-233; `XBRL_SUMS` l. 271, `XBRL_MAGNITUDES` l. 284, `XBRL_INFO` l. 289, `XBRL_BANK_EXTRA` l. 305) | It is Python, not the `fundamentals/xbrl_map.yaml` SPEC names. It has no version, and parsed values don't record which mapping produced them. Ind-AS tags only: a pre-2017 (Indian GAAP / Clause 41) filing maps almost nothing |
| 2 | Unit normalisation to ₹ (lakhs / crores / millions) | **Partial** | `xbrl.py` l. 61 and l. 221-230: facts with unit `INR` are assumed to be absolute rupees and divided by 1e7 into crore; per-share and `pure` units are handled | The filing's rounding level (`LevelOfRoundingUsedInFinancialStatements`) and each fact's `decimals` are not read. A fact filed as "1,234.56 (lakhs)" instead of absolute rupees would be 10^5× too small. Nothing is stored in ₹; the wide tables hold crore |
| 3 | Consolidated and standalone both stored and labelled | **Met** | Listing keeps both bases (`providers/nse.py::parse_results_index`); the basis comes from the document (`xbrl.py` l. 366-367), else the listing; storage is keyed by basis (`db/models.py` l. 185, 219: `UniqueConstraint(instrument_id, statement_type, period_end)`; `results_store.py` l. 70-77); a filing with no stated basis is refused (`results_ingest.py` l. 55-59); reports read consolidated first (`reports/data.py::load_financials`) | — |
| 4 | Restated periods stored as new versions, not overwrites | **Missing** | Comparative contexts, where restatements appear, are skipped (`xbrl.py` l. 328-340). A revised filing of the same period overwrites the row in place; only the earliest announcement date is kept (`results_store.py` l. 47-85) | There is no version history. Backtests can see revised figures under the original date |
| 5 | Long-format `fin_line_items` with `announced_at` and `filing_id` | **Missing** | Only the wide `fin_quarterly` / `fin_annual` tables exist. `result_filings` (`db/models.py` l. 227) records each document but is not linked to the values it produced | — |
| 6 | FY totals derived from quarters and flagged | **Missing** | A fiscal-year row comes only from a year-length context (Q4 or annual filing, `xbrl.py` l. 339) | A year whose Q4 filing is missing or failed has no annual row, even when all four quarters are stored |
| 7 | Raw files cached under `data/raw` before parsing | **Missing** | Downloaded bytes go straight to the parser (`jobs/fundamentals.py` l. 135-143); uploads are parsed in memory (`api/admin.py::upload_xbrl`); the NSE filing list is parsed inside the provider | Re-parsing after a mapping fix means re-downloading everything |
| 8 | Coverage: earliest year parsed per statement (P&L / BS / CF) for 5 Nifty 500 stocks | **Missing** | Not measured, and there is no tool to measure it. NSE, NSE archives and BSE are blocked by this build environment's network policy (HTTP 403 on CONNECT), so no real filing has ever been parsed here | Needs a coverage report, plus a run with network access |

## Resolution

Each Partial or Missing item got its own commit. Status after P17:

| # | Requirement | Before | After | Commit | Where now |
|---|---|---|---|---|---|
| 1 | Versioned tag map, Ind-AS + pre-Ind-AS | Partial | **Met**, with one caveat | `2040395` | `backend/app/fundamentals/xbrl_map.yaml` (`version: 2`; tag groups `ind_as`, `bank`, `pre_ind_as`), validated by `fundamentals/xbrl_map.py`; each line item records its tag and map version. **Caveat:** the `pre_ind_as` element names follow the Clause 41 layout but are unverified against a real filing |
| 2 | Units to ₹ (lakhs / crores / millions) | Partial | **Met** | `04703fa` | `data/xbrl.py::amount_scale`: detects amounts keyed in the stated rounding level via PAT ÷ EPS, else `decimals`. Factors are in `providers.yaml` `nse.results.rounding_levels`. Line items hold ₹ |
| 3 | Consolidated and standalone stored and labelled | Met | Met | — | Unchanged; line items also carry `basis` |
| 4 | Restatements as versions | Missing | **Met** | `a77f37b` | Comparative contexts are now parsed; `results_store.record_line_items` versions each period's figures by `usable_from` (not download order), with a rounding tolerance. Wide tables use the latest version; backtests use the version public at each date (`backtest/pit.py::versioned_frame`) |
| 5 | Long-format `fin_line_items` with `announced_at`, `filing_id` | Missing | **Met** | `a77f37b` | Table `fin_line_items` (migration `e2c3d4f5a6b7`): value in ₹, unit, basis, statement, period type, version, `filing_id`, `announced_at`, `usable_from`, `tag`, `map_version`, `isin` |
| 6 | FY totals derived from quarters, flagged | Missing | **Met** | `bc0565e` | `results_store.derive_years`: `derived = true` line items plus `fin_annual.is_derived`; the report lists derived years; a later filed annual figure supersedes the sum |
| 7 | Raw files cached under `data/raw` before parsing | Missing | **Met** | `89e66d5` | `data/raw_store.py`: `RAW_DATA_DIR/<source>/<yyyy>/<mm>/<dd>/`, for XBRL documents, NSE results lists and uploads; `result_filings.raw_path`; `python -m app.jobs xbrl-reparse`. Other NSE fetchers (bhavcopy, surveillance) are outside this audit and not cached yet (SPEC §3.2a; P21) |
| 8 | Coverage for 5 Nifty 500 stocks | Missing | **Partial**: tool only | `3d96acc` | `python -m app.jobs xbrl-coverage --symbols …` reports the earliest and latest fiscal year per statement. It has **not been measured**: NSE and BSE are blocked in this build environment. See below |

**Golden tests** (bank, manufacturer, IT; 3 years each including a pre-2017 year): **Partial.**
- The harness is ready: `tests/test_golden_xbrl.py` and `tests/fixtures/golden_xbrl/expected.yaml`, covering HDFCBANK, MARUTI and TCS for FY2016, FY2020 and FY2024.
- The real XBRL files and hand-checked figures are not in yet, for the same network reason, so the nine entries skip and name what's missing.
- The structure test and a self-test of the harness run.

### Still to do (needs network access to NSE/BSE)

1. **Measure coverage (item 8).** Run `results_watch` for five Nifty 500 stocks, e.g.
   `python -m app.jobs run results_watch --symbols TCS,HDFCBANK,MARUTI,RELIANCE,ASIANPAINT`, with
   `max_downloads_per_run` raised, or over several nights. Then run
   `python -m app.jobs xbrl-coverage --symbols TCS,HDFCBANK,MARUTI,RELIANCE,ASIANPAINT` and record
   the table here.
2. **Fill the golden set.** Download the nine filings, check the `pre_ind_as` names with
   `xbrl-inspect` (fix `xbrl_map.yaml` and bump `version`), and enter the hand-checked figures with
   `checked_against` (see `tests/fixtures/golden_xbrl/README.md`).

Expected coverage limits:
- **P&L:** as far back as XBRL results filing goes (roughly the early-to-mid 2010s).
- **Balance sheet:** only from the half-yearly statement of assets and liabilities, and only
  once SEBI required it in results.
- **Cash flow:** from FY2020, when SEBI added the half-yearly cash flow to results.

Earlier balance sheets and cash flows come from the annual-report PDF gap filler (SPEC §3.6 step 3,
P18).

## Data-correctness pass (Prompt A, HDFCBANK report with empty fundamentals)

The HDFCBANK page showed real prices but empty fundamentals and valuation. Causes found, and
what changed. Commits are on branch `claude/gracious-wozniak-4lsfac`.

| # | Symptom | Cause | Fix | Commit |
|---|---|---|---|---|
| 1 | Bank valued with the default model | Nothing set `instruments.sector` | NSE `industryInfo.basicIndustry` (Yahoo `industry` as fallback) mapped through `config/industries.yaml` to a sectors.yaml key; `industry_classification` job and the pipeline's symbol step; an unmapped label is a "sector unmapped" data gap | `9fe45df` |
| 2 | Fundamentals empty; the CLI and the grid disagreed | A fiscal-year-end balance sheet without a P&L made no `fin_annual` row; the CLI counted quarters as years | Year rows from FY-end balance sheets; the grid and the CLI count years the same way (quarters reported apart); Indian FY labels everywhere | `7a5ed4d` |
| 3 | 36 `BANKING_*.xml` (2018-06 to 2023-09): "no quarter or fiscal-year results" | **Not confirmed**: the files weren't available here. Candidates: figures only in dimensional contexts, or period facts only in dimensional contexts | Contexts carry their dimension members. Contexts dimensioned only on a consolidated/standalone axis (`basis_axes`, xbrl_map v4) are read for the basis the filing states. Descriptive facts are read from any context. The error now lists the contexts at the period end, their members, the mapped P&L elements found and the unmapped ones; `xbrl-inspect` prints `NOT PARSED: …` with every context and fact. The NSE list (≈50 latest filings) is re-read in 2-year date windows back 12 years. Real failing files go in `tests/fixtures/golden_xbrl/regression/` and must parse | `f245edf`, `8ae56bf` |
| 4 | Bank cash flow shown as a failure | Bank results carry no cash flow | Grid cell "AR: not in XBRL, use the annual report" (banks, and every company before FY2020); the negative-CFO knock-out doesn't apply to lenders/insurers | `ab46460` |
| 5 | No shareholding in the report | Only a chart without source/date | Report `shareholding` (source, quarter, filing date, holdings, pledge); chart captioned with source and dates; none → gap | `bc9850c` |
| 6 | "No cost of equity"; WACC for a bank | Latest annual row (BS-only) had no share count → no market cap; banks got a WACC | Market cap from the latest share count on file, shown with its period; Ke build-up shown; beta clamp, missing beta, undated risk-free rate are gaps; banks run on Ke only (no WACC/FCFF, no reverse DCF) | `617bfba`, `9fbeeb7` |
| 7, 9 | Weekly chart jumps (Sep 2019, Aug–Sep 2025); "nse: empty; yfinance: empty" | Corporate actions were read for the last 30 days only (split/bonus never loaded); the same event from two sources a day apart, or a pre-adjusted price source, was applied twice | Full history for a stock with no actions on file; "no actions" from every source is an answer; one application per event (`duplicate_window_days`), pre-adjusted detection, abnormal-gap report; per-share fundamentals restated to today's share basis | `4b84108` |
| 10 | PDF step crash (`CardinalityViolation`) | The same line read twice in one upsert batch | One candidate per key (highest confidence); one report's failure doesn't stop the others | `cd8b0ff` |
| 11 | "0 of 36 stored; 36 failed" every run | Parse failures retried like download failures | `parse_failed_version`: not retried until the parser/map version changes, then re-parsed from the cache | `a8dbffc` |
| 12 | Panel didn't say what was found | — | XBRL step: "Years found: P&L n yr, BS n yr, CF n yr"; metrics fails loudly naming what's missing | `a8dbffc` |

### Evidence (offline synthetic exchange; NSE is unreachable from this build environment)

`OFFBANK` is the offline exchange's synthetic bank (banking XBRL layout, 10 years). The same
jobs were run on `main` before this work (`341916f`) and after it (results_backfill,
corporate_actions, shareholding, technicals, valuation_scores):

| | Before | After |
|---|---|---|
| CLI coverage, P&L | `FY2017-FY2027 11 yr` (quarters counted as a year) | `FY2017-FY2026 10 yr; 39 quarter(s)` |
| Baseline / FV / Top band | ₹1,096 / ₹2,334 / ₹1,388 | ₹1,096 / ₹2,334 / ₹1,388 |
| Zone, grade, action | discount, A, accumulate | discount, A, accumulate |
| WACC (bank) | 15.67% (computed for a bank) | not used: "bank model: valued on the cost of equity; no WACC or FCFF" |
| Cost of equity | 10.70%, no build-up | "Ke 10.70% = Rf 6.50% + beta 0.60 x ERP 7.00% + size premium 0.00%" |
| Market cap | shown, source of the share count not stated | "₹134,040 Cr = 100.01 Cr shares (quarter ended 2026-06-30) x price ₹1,340.27" |
| Shareholding in report | not present | offline, quarter 2026-06-30, filed 2026-07-21, promoter 51.3%, pledge 0% |
| Data gaps | 9, two not applicable to a bank (`knockout:negative_cfo`, `reverse_dcf_gap`) and a silently clamped beta | 9: the two non-applicable gone; `beta` (0.37 clamped at the 0.60 floor) and `risk_free_rate` (undated) added |

The synthetic bank's data is clean, so its levels don't move. On HDFCBANK the fixes act on
inputs that were missing (sector, year rows, corporate actions, share count).

Price adjustment, on a synthetic ₹1,000 series with HDFC Bank's split (ex 19 Sep 2019) and 1:1
bonus (ex 27 Aug 2025) (`tests/test_adjust_hdfcbank.py`):

| Case | Before: first close, largest daily move | After |
|---|---|---|
| Unadjusted prices; NSE + yfinance duplicates a day apart | ₹250, 100% | ₹1,000, 0% |
| Prices already adjusted by the source | ₹250, 100% | ₹1,000, 0% |

### Still to confirm with network access

1. Run the HDFCBANK sequence (see the PR) and record `xbrl-coverage --symbols HDFCBANK`; the
   target is P&L and BS back to FY2016 or earlier.
2. Copy 3–4 failing `BANKING_*.xml` files into `tests/fixtures/golden_xbrl/regression/`. If
   any still fails, `python -m app.jobs xbrl-inspect <file>` shows the cause: add the axis
   to `basis_axes` or the element names to `xbrl_map.yaml` (bump `version`).
3. Fill HDFCBANK's golden figures: total income, net profit, advances, deposits, net worth.
4. Set `valuation.risk_free_as_of` after checking the 10-year G-sec yield.
