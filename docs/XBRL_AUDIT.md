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

Each Partial or Missing item gets its own commit. This section is filled in as they land.
