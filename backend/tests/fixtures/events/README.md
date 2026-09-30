# Exchange event feed fixtures (SPEC v0.2 §3.8, §10 `events`)

Hand-written in the shapes NSE's and BSE's pages loaded when the parsers were written
(`app/data/events.py`). They are **not** captures of live responses — the sandbox these were
written in cannot reach nseindia.com / bseindia.com — so check the field names against a live
response (the raw cache under `data/raw/nse|bse/...` keeps every file read) before trusting a
feed, and update both the parser and these files if they differ.

- `nse_announcements.json` — `/api/corporate-announcements` (auditor resignation, credit rating downgrade, dividend, one row without a subject)
- `nse_board_meetings.json` — `/api/corporate-board-meetings`
- `nse_results.json` — `/api/corporates-financial-results` (all companies)
- `nse_pledge.json`, `nse_sast.json`, `nse_pit.json` — pledge, SAST reg. 29, insider trades
- `bulk.csv`, `block.csv` — NSE archives deal files
- `bse_announcements.json` — BSE `AnnSubCategoryGetData` (one "Result")
- `market_lens.json` — Market Lens financials, in the shape `providers.market_lens` describes
