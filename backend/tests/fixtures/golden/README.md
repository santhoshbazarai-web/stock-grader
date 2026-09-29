# Golden stocks (SPEC §13)

One JSON file per stock, validated against [`schema.json`](schema.json). `test_golden.py`
recomputes every expected number from the file's own inputs and checks it within the AGENTS.md
tolerance: 0.5% relative for ratios and days, 2% for valuations (DCF, bands). An empty `expected`
section fails validation; an expected metric the code doesn't produce fails the test.

`EXAMPLE.json` is a fictional company, used as the worked format example. Copy it to
`<SYMBOL>.json` for each of the ten stocks, one per model type:

| `model_type` | e.g. |
|---|---|
| `private_bank` | a large private bank |
| `nbfc` | an NBFC |
| `insurer` | an insurer |
| `it_services` | a large IT company |
| `fmcg` | an FMCG company |
| `metal_cyclical` | a metal cyclical |
| `cement` | a cement company |
| `capital_goods_compounder` | a capital-goods compounder |
| `holding_company` | a holding company |
| `midcap_growth` | a mid-cap growth stock |

## Filling a file

- `inputs.annual`: one object per fiscal year, oldest first, **at least six years** so the
  five-year windows (ROCE average, cumulative CFO/EBITDA) have an opening balance.
  Field names are the canonical ones in [`docs/CANONICAL_FIELDS.md`](../../../../docs/CANONICAL_FIELDS.md).
  Amounts are in **₹ crore**, EPS and book value per share in ₹, shares in crore.
  Leave a field out (or `null`) when the annual report doesn't give it. Never put 0 for unknown.
- Banks and NBFCs: put the bank inputs (advances, deposits, NPAs...) under `extra`. The keys
  are listed in the schema and in `app/fundamentals/banking.py`.
- `expected.metrics`: required for non-financials: `roce_5y_avg`, `cfo_to_ebitda_5y`,
  `ccc_days`, `debt_to_equity` (latest year). Optional: any other name `summary_metrics`
  returns (`roce_latest`, `opm_5y_avg`, `sales_cagr_3y`, `interest_coverage`, ...). Ratios are
  fractions: 18% → `0.18`.
- `expected.bank_metrics` (banks/NBFCs): required `gnpa_pct`, `nim_pct`, `roa_pct`, in percent.
- `expected.forensic` (optional): `piotroski` (0-9), `beneish_m`, `altman_z2`.
- `expected.valuation` (optional until the valuation engine lands in P8): one DCF run with its
  fixed inputs and the per-share value, plus the PE band median and sigma.
- `verified`: who checked the numbers, when, and against what (annual report pages, Screener).

The definitions the numbers must follow are in SPEC §4, including its "Implementation notes"
(average capital employed for ROCE, how capex, CCC, NIM and the forensic inputs are defined).
If your hand calculation uses a different definition, the test will show the gap. Decide which
one is right before changing the tolerance.
