# Golden XBRL filings (P17)

Real results filings for three companies, one per results format, with fiscal-year figures
checked by hand. `tests/test_golden_xbrl.py` parses each file and compares the fiscal-year
record with `expected.yaml` within 0.5% (AGENTS.md rule 3).

| Company | Kind | Basis | Years |
|---|---|---|---|
| HDFCBANK | bank (banking format, Indian GAAP) | standalone | FY2016, FY2020, FY2024 |
| MARUTI | manufacturer | consolidated | FY2016 (pre-Ind-AS), FY2020, FY2024 |
| TCS | IT services | consolidated | FY2016 (pre-Ind-AS), FY2020, FY2024 |

**Status: not filled in yet.** The environment that built the parser could not reach NSE or
BSE, so these files have not been downloaded and no value has been checked. Until then, each
entry is skipped, and the skip message names what is missing. The structure test in the same
file does run, and it keeps the set at 3 kinds × 3 years with one year before 2017.

To fill an entry:

1. **Download the file.** Save the XBRL of the Q4 results (quarter ended 31 March, which
   includes the full year) as `<SYMBOL>/FY<year>_Q4_<basis>.xml`.
   - NSE: company page → Corporate Filings → Financial Results → the period → the XBRL link.
   - BSE: Corp Filings → Results → XBRL.
   - The results_backfill job also caches it under `data/raw/nse/...` once network access works.
2. **Inspect it.** Run `python -m app.jobs xbrl-inspect <file>` and check it reads the
   expected contexts. Pre-2017 filings use the `pre_ind_as` tags in
   `app/fundamentals/xbrl_map.yaml`, which are still unverified: fix any wrong names there and
   bump its `version`.
3. **Fill in the figures.** In `expected.yaml`, fill `expected_crore` (₹ crore; EPS in ₹) from
   the published results or the annual report for that fiscal year and basis, and set
   `checked_against` (document and page).
   For HDFCBANK, also fill `total_equity` (net worth), `expected_line_crore.total_income`,
   and `advances` / `deposits` in `expected_extra_crore`.

## Regression files (`regression/`)

Real filings that once failed to parse go in `regression/` under their original names, e.g.
3–4 of the `BANKING_*.xml` files (periods 2018-06-30 to 2023-09-30) copied from
`data/raw/nse/<yyyy>/<mm>/<dd>/`. `test_regression_filings_parse` requires each to parse
(a quarter or a fiscal year). If one fails, `python -m app.jobs xbrl-inspect <file>` now
prints `NOT PARSED: …` with every context (its dimension members) and every numeric fact,
which shows the cause: a dimension axis to add to `basis_axes`, or element names to add to
`xbrl_map.yaml` (bump `version`; the stored failures are then retried).
