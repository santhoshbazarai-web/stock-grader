# Stock Grader (Indian Equities)

Personal research tool that grades NSE stocks — Baseline / Fair value / Top band, zone, buy zone,
grade and action. See [`AGENTS.md`](AGENTS.md) and [`docs/SPEC.md`](docs/SPEC.md).

## Layout

| Path | What |
| --- | --- |
| `backend/` | FastAPI api + APScheduler worker (Python 3.12, uv) |
| `frontend/` | Next.js 15 + Tailwind + shadcn/ui |
| `config/` | `providers.yaml`, `valuation.yaml`, `sectors.yaml`, `scoring.yaml`, `technical.yaml` — every threshold and weight, validated at startup |
| `docs/` | Spec, build prompts and the [deployment guide](docs/DEPLOY.md) |
| `deploy/` | Production Caddyfile, backup and restore scripts |

## Quick start

```bash
cp .env.example .env
# set FERNET_KEY and APP_PASSWORD (required — the api and worker refuse to start without them)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

make up        # db, redis, api (:8000), worker, web (:3000)
make migrate   # alembic upgrade head
```

Health check: `curl localhost:8000/api/health`.

## Production

`docker-compose.prod.yml` runs the production stack:

- Caddy with automatic HTTPS in front of the web app and API
- the worker
- Postgres with nightly verified backups and a one-command restore
- Redis

Only ports 80 and 443 are published. All settings come from `.env.production`: copy
`.env.production.example`, then run `make prod-up`.

With `APP_ENV=production`, start-up refuses unsafe settings: plain HTTP, an insecure cookie,
a weak password, the development database password, or broker redirect URIs that don't match
the domain.

The full guide is [`docs/DEPLOY.md`](docs/DEPLOY.md). It covers server setup, registering the
broker redirect URIs (`https://<DOMAIN>/api/brokers/{fyers,kite}/callback`), backups, restore
and updates.

## Development

```bash
make install   # uv sync (backend) + npm ci (frontend)
make test      # pytest; DB tests use TEST_DATABASE_URL
               # (default: stockgrader_test on localhost:5432, created by `make up`)
               # and are skipped if Postgres is unreachable; rate-limiter tests
               # likewise use TEST_REDIS_URL (default redis://localhost:6379/15)
make check     # ruff + mypy --strict + eslint + tsc
```

## Config

`app.core.config` loads every `config/*.yaml` into Pydantic models on api/worker startup and
refuses to start on any problem: missing file or key, unknown key, out-of-range value,
weights that do not sum to 1 (sectors) or 100 (pillars), non-monotonic score maps, a bank or
insurance sector that uses FCFF DCF, etc. The config directory is `CONFIG_DIR` (defaults to
`./config`; mounted read-only at `/config` in Docker).

## Database

Models live in `backend/app/db/models.py` (all SPEC §3.4 tables). Ingested data tables carry
`source` + `fetched_at`; derived snapshots carry `computed_at`. Write with
`app.db.upsert.upsert(session, Model, rows)`, which does `INSERT … ON CONFLICT DO UPDATE` on
each model's `__upsert_key__`, so re-running a job is idempotent.

After changing models: `make revision m="describe change"`, review the generated file, then
`make migrate`.

## Data routing

`app.data.router.DataRouter` serves each dataset from the providers listed in
`providers.yaml` `priority`, in order. Per provider it takes a Redis token-bucket token
(`app.core.rate_limiter`, limits from `rate_limits`) before every try and retries transient
`ProviderError`s with exponential backoff (`retry`). It falls through on
`ProviderUnavailable`, exhausted retries, empty data, stale data (`staleness_hours`) or a
rate-limit timeout. If nothing usable comes back it returns `data=None` and records a
`data_gaps` row; if only stale data came back it returns the freshest copy flagged `stale`.
Every result carries `source`, `fetched_at` and `reasons`.

## Brokers (read-only)

Fyers: set `FYERS_APP_ID`, `FYERS_SECRET`, `FYERS_REDIRECT_URI` (register the same redirect URI,
`http://localhost:8000/api/brokers/fyers/callback` locally, in the Fyers developer console), then
open `http://localhost:8000/api/brokers/fyers/login`. After login Fyers calls back, the token is
Fernet-encrypted into `broker_tokens` with its JWT expiry, and you are redirected to
`$WEB_URL/settings?broker=fyers&status=connected`. Tokens expire daily; `GET /api/brokers/status`
shows validity. With no valid token the router falls through to the next provider.

Kite: set `KITE_API_KEY`, `KITE_API_SECRET` and register `KITE_REDIRECT_URI`
(`http://localhost:8000/api/brokers/kite/callback` locally) as the redirect URL in the Kite
developer console, then open `http://localhost:8000/api/brokers/kite/login`. Kite tokens expire
at 06:00 IST (`token_daily_expiry_ist`). Historical candles need Kite's paid historical-data
add-on; without it the provider reports `unavailable` and the router uses the next provider.
The NSE instruments dump (symbol → instrument token) is cached in Redis for
`instruments_cache_hours`.

## Fundamentals: exchange results filings (XBRL)

Every listed company files its quarterly and annual results with NSE and BSE as an XBRL
document (SEBI's Ind AS results format). These filings are the primary source of fundamentals;
a Screener export is an optional top-up.

- **Nightly job:** the worker's `results_backfill` job reads each stock's filing list from NSE
  and downloads new documents into `fin_quarterly` / `fin_annual`.
  - Q4 filings also give the fiscal year's P&L, balance sheet and cash flow.
  - Download backlog lives in the `result_filings` table.
- **Backfill:** 10 years of history take a few nights at `max_downloads_per_run` (300, in
  `jobs.yaml`). To fetch one stock now: `python -m app.jobs run results_backfill --symbols TCS`.
- **Real announcement dates:** each filing is dated by when NSE published it. A filing
  published after the 15:30 close counts from the next day, so backtests only use results the
  market had seen.
- **Precedence:** figures from filings win. A Screener upload never overwrites them; it only
  fills what filings lack (SG&A, years before XBRL filing began).
- **Settings → Results filings** shows the backlog, failed documents (with Retry), and takes
  XBRL files uploaded by hand. Use uploads for BSE-only companies, or when NSE can't be reached;
  the XBRL link is on each company's results page on either exchange.
- **Raw cache:** every downloaded XBRL document, filing list and upload is saved under
  `RAW_DATA_DIR` (default `data/raw/`; the `rawdata` volume in Docker) as
  `<source>/<yyyy>/<mm>/<dd>/<file>` before parsing. After changing the tag map, run
  `python -m app.jobs xbrl-reparse` to rebuild from the cache instead of re-downloading.
- **Versions and derived years:** every figure is also kept in long format (`fin_line_items`).
  A later filing that restates a period adds a version instead of overwriting; reports use the
  latest, backtests the one public at the time. A year with four stored quarters but no filed
  annual figures gets its P&L summed from them, flagged as derived.
- **Golden filings:** `backend/tests/fixtures/golden_xbrl/` has a hand-checked test set (a bank,
  a manufacturer, an IT company; 3 years each). It still needs the real XBRL files and figures;
  its README says how.
- **Checking the mapping:** element names can change with new taxonomy years. Before relying
  on a new kind of filing, run
  `python -m app.jobs xbrl-inspect filing.xml`. It prints what was read and the numeric
  elements the mapping ignores. Add names to `backend/app/fundamentals/xbrl_map.yaml` (and bump its
  `version`); it covers the Ind AS, bank and pre-Ind-AS (Indian GAAP) results formats.

## Fundamentals: annual-report PDFs (gap filler)

Results XBRL carries the balance sheet only half-yearly, and the cash flow only from FY2020, so
older years lack them. Annual reports fill those years (SPEC §3.6 step 3).

- **Weekly job:** `annual_reports` (Saturdays 22:30) finds each stock's fiscal years missing a
  required item (`jobs.yaml` → `annual_reports.required_items`: total assets, total equity,
  cash from operations). It fetches that year's report from NSE's annual-report list, or the
  next year's (whose comparative column covers the year). Reports are PDFs or ZIPs; each is
  cached under `RAW_DATA_DIR` before reading. It fetches at most `max_downloads_per_run` (25).
- **Reading:** the reader finds the standalone and consolidated balance sheet and cash flow pages
  by their titles. It reads the rows with pdfplumber, falling back to camelot, and maps each
  label with a fuzzy dictionary, `backend/app/fundamentals/pdf_labels.yaml` (versioned like
  the XBRL map). Amounts are scaled by the unit the page states (crore, lakh, million).
- **Confidence:** every value carries a score: label similarity × label weight, lowered for
  the camelot fallback, an ambiguous label, an undated column or a failed cross-check.
  - The cross-checks are: total assets = total equity and liabilities, and operating +
    investing + financing cash = the net change in cash.
  - At or above `providers.yaml` → `nse.annual_reports.confidence.auto_accept` (0.9), the value
    is stored straight away. Below it, the value waits in the **review queue**.
- **Review (`/review`):** each value shows the printed label, a link to its PDF page and why its
  confidence is what it is. Accept it as read, type the right figure (₹ crore) and Save, or
  Reject it; the fundamentals update at once.
  - The same page takes reports uploaded by hand (e.g. BSE-only companies). Give the
    publication date so backtests can use the values.
- **Gap filler only:** a PDF value is never stored where the exchange XBRL has the figure, and
  it is replaced when an XBRL figure arrives. Values are stored in `fin_line_items` with
  `source=annual_report_pdf`, the report's publication date and their confidence. As with XBRL,
  a restated comparative becomes a new version.
- **Coverage:** the stock page's **Data coverage** grid shows fiscal years × P&L / BS / CF per
  basis, coloured by source (XBRL, PDF, summed quarters, Screener, yfinance), with gaps and
  values to review marked.
- **Tuning:** `python -m app.jobs pdf-inspect report.pdf --fy 2014` shows the pages found, every
  value, its confidence and the warnings, without a database. After editing `pdf_labels.yaml`
  (bump `version`), `python -m app.jobs pdf-reparse` re-reads the cached reports; review
  decisions are kept. Scanned reports (no text layer) are refused; OCR is not supported.
- **Untested against real reports:** NSE is unreachable from the build environment, so the
  reader and NSE's annual-report list format have only been tested on synthetic reports.
  Check a few real ones with `pdf-inspect` first.

## On-demand pipeline

Opening a stock shows its stored report at once. When the report is older than its latest
prices or filings (or 20 hours), a pipeline run brings it up to date in the background, with
a live progress panel. A stock seen for the first time is built step by step, with the
progress on screen:

symbol → prices → corporate actions → filings index → results XBRL → annual-report PDFs →
shareholding → reconciliation → metrics → valuation → technicals → scoring → report.

- **Warnings:** each step shows ✓, ⚠ or ✗ with its message. An optional step that fails
  (e.g. NSE unreachable) is a warning, and the report is still built with that gap listed.
  With no prices at all there is no report, and the run fails.
- **Refresh data** on the report page starts a run even when the report is fresh.
- **Where runs happen:** the worker picks runs up within a second and resumes a run
  interrupted by a restart. Progress streams over Server-Sent Events. Per-run budgets are
  in `jobs.yaml` → `pipeline`.
- **Nightly:** Nifty 500 reports are rebuilt every night (`valuation_scores`, 23:30), so they
  normally load instantly.

## Results watch, corporate events and reconciliation

- **`results_watch`** (every 15 min, 07:00–22:45 IST) reads the NSE results-filing list, the
  NSE board-meeting calendar and BSE announcements (BSE's "Result" category is a results
  filing).
  - A new results filing for a universe stock (Nifty 500 + watchlist) starts that stock's
    pipeline with `trigger=results`. That run carries the previous report's grade, zone,
    action and FV.
  - When the run's report step finds the grade, zone or action changed, or FV moved more than
    5% (`jobs.yaml` → `results_watch.notify.fv_change_rel`), it creates a notification (in-app,
    plus Telegram when configured). Example: "XYZ Q2 FY25 results: Grade B→A,
    FV ₹1,240→₹1,390, zone Fair→Discount".
  - A second filing of the same results (the other basis, or BSE after NSE) within
    `rerun_after_hours` does not start another run.
  - The job's details list the results board meetings expected over the next 14 days.
- **`events`** (every 30 min, at :07 and :37) reads these NSE feeds: announcements, promoter
  pledges, SAST reg. 29, insider trades (PIT), and the bulk / block deal files.
  - Each event is linked to a stock by symbol, ISIN, BSE code or company name.
  - It is classified by keyword rules (`jobs.yaml` → `event_classification`). Auditor
    resignations, rating downgrades, defaults and pledge invocations are red flags.
  - An auditor resignation feeds the auditor knock-out. Once the stored announcements cover
    the whole knock-out window, "none on record" counts as clean rather than unknown.
  - The stock page shows an **events card**: upcoming board meetings, then recent events.
- **Fetching:** both jobs follow SPEC §3.2a. They take a shared Redis lock, so they never hit
  the exchanges at the same time. Every raw feed file is cached under `data/raw/` before
  parsing. They re-read the last `lookback_days` and dedupe on the feed's own ids.
- **Reconciliation** (a pipeline step, plus nightly `reconcile` at 23:00 for stocks with new
  filings) compares sales, EBITDA, PAT, CFO, total assets and equity for the latest 3 fiscal
  years and 4 quarters.
  - Sources: NSE XBRL (the reference), accepted annual-report PDF values, yfinance and, when
    enabled, Market Lens.
  - A difference over 2% (and over ₹50 lakh) becomes a `reconciliation_issue`, with a likely
    cause: units, consolidated/standalone mix-up, or restatement.
  - Open issues lower the valuation confidence by one level and show a banner on the stock
    page. **Ignore** dismisses an issue you have explained; it stays ignored while the
    figures are unchanged.
- **Market Lens** (`providers.yaml` → `market_lens`) is off by default. Its JSON is
  undocumented, so every field name is config. Check them against the page's own requests
  before setting `enabled: true`.
- **Untested against live feeds:** the parsers are tested on hand-written files in the
  published shapes (`backend/tests/fixtures/events/README.md`), because NSE and BSE are
  unreachable from the build environment. Run `python -m app.jobs run events` and
  `python -m app.jobs run results_watch` once, and check their details.

## Symbol master and search

The header search box finds a stock by NSE symbol ("HDFCBANK", or "hdfc bank"), BSE code
("500180"), ISIN, company name, or a former name or symbol ("Bharti Tele-Ventures",
"INFOSYSTCH"). It tolerates typos. Keyboard: `/` or Ctrl/⌘+K to focus, ↑/↓ to move, Enter to
open, Esc to close.

- **Daily job `symbol_master`** (07:15): joins NSE `EQUITY_L.csv`, the BSE scrip master and the
  Fyers symbol master on ISIN into `symbols`. It adds aliases from NSE's symbol-change and
  name-change files and BSE's own names, and keeps `instruments` in step.
  - When NSE changes a symbol, the stock's instrument is renamed, so its price and
    fundamentals history carries over.
  - BSE-only companies are searchable but have no stock page (no NSE data).
- **Ranking:** exact code matches first, then Nifty 500 members, then pg_trgm similarity
  (threshold `providers.yaml` → `symbols.search.min_similarity`).
- **Your own aliases:** `POST /api/stocks/{symbol}/aliases {"alias": "..."}`.
- **Untested against live files:** the parsers are tested on files written in the published
  layouts. NSE and BSE are unreachable from the build environment, so run
  `python -m app.jobs run symbol_master` once and check its details.

## Other data sources

- **yfinance** (`data/providers/yf.py`): price fallback (`TCS.NS`, index tickers from
  `yfinance_index_tickers`), corporate actions, and fundamentals. Yahoo's prices are
  split-adjusted even unadjusted, so the provider reverses that to return raw prices. Annual
  statements cover only ~4 years; results carry a `limited history` warning in `reasons`.
- **NSE** (`data/providers/nse.py`): delivery % from `sec_bhavdata_full`, index constituents
  (niftyindices CSV), ASM/GSM + F&O ban lists, corporate actions, shareholding, and the results
  filing list + XBRL documents (parsed by `data/xbrl.py`). Browser headers,
  homepage cookie warm-up (refreshed after `nse.cookie_ttl_s` or on 401/403), `rate_limits.nse`.
- **Screener** (optional; `data/providers/screener_import.py`): parses the Excel export's "Data
  Sheet" into `fin_annual` / `fin_quarterly` / `shareholding` (you state consolidated vs
  standalone at upload), filling only what the XBRL filings don't cover, and records data gaps
  for what the export lacks.

Every source's labels map onto one canonical schema in `backend/app/data/canonical.py` (XBRL
element names are in the versioned `backend/app/fundamentals/xbrl_map.yaml`);
the generated table is in [`docs/CANONICAL_FIELDS.md`](docs/CANONICAL_FIELDS.md).

## Jobs

The `worker` service runs the SPEC §10 jobs on the cron schedules in `config/jobs.yaml`
(IST). Each run takes a Redis lock (a concurrent run is recorded as `skipped`) and is logged
in `job_runs` with rows written, a details summary and any error.

```bash
python -m app.jobs list                                  # jobs, schedules, readiness
python -m app.jobs run eod_prices --symbols TCS,INFY     # one job now (add --full to backfill)
python -m app.jobs run nse_bhavcopy --date 2024-03-28
python -m app.jobs run shareholding --force              # ignore the filing-season window
python -m app.jobs verify-adjustment --symbol INFY       # raw vs adjusted around splits/bonuses
python -m app.jobs run results_backfill --symbols TCS       # fetch a stock's results filings now
python -m app.jobs xbrl-inspect filing.xml               # what the XBRL parser reads (no DB)
python -m app.jobs xbrl-reparse --symbols TCS            # re-parse cached XBRL after a map change
python -m app.jobs xbrl-coverage --symbols TCS,INFY      # fiscal years parsed per statement
python -m app.jobs run symbol_master                     # NSE/BSE/Fyers symbol master + aliases
python -m app.jobs run results_watch                     # results filings in the feeds → pipelines
python -m app.jobs run events                            # announcements, pledge, SAST, PIT, deals
python -m app.jobs run reconcile --symbols TCS           # cross-source checks for one stock
python -m app.jobs pipeline-worker                       # only the on-demand pipeline loop (dev)
python -m app.jobs run annual_reports --symbols TCS      # annual-report PDFs for BS/CF gap years
python -m app.jobs pdf-inspect report.pdf --fy 2014      # what the PDF reader finds (no DB)
python -m app.jobs pdf-reparse --symbols TCS             # re-read cached reports after a label change
```

In Docker: `docker compose run --rm worker python -m app.jobs run eod_prices --symbols TCS`.

Prices are stored raw and split/bonus-adjusted (`adj_*`, `data/adjust.py`); adjustment is
recomputed whenever new bars or corporate actions arrive. `valuation_scores` builds every
report in two passes: the first collects each stock's multiples as sector peers, the second
builds the reports. `refresh_queue` is the fallback for on-demand pipeline runs (below).
`backtests` runs queued backtest requests (below). `results_backfill` ingests exchange results
filings, and `annual_reports` reads annual-report PDFs for the years they lack (above).
`results_watch`, `events` and `reconcile` are described under "Results watch, corporate events
and reconciliation".

## API

Interactive docs are served at `/api/docs` (Swagger) and `/api/redoc`, and the schema at
`/api/openapi.json`. There is a single user. Log in with `APP_PASSWORD`; the session comes
back as an HttpOnly cookie and as a Bearer token:

```bash
TOKEN=$(curl -s localhost:8000/api/auth/login -H 'content-type: application/json' \
  -d '{"password": "..."}' | jq -r .token)
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/stocks/TCS/report
```

| Endpoint | Purpose |
|---|---|
| `GET /api/stocks/search?q=` | Fuzzy search: NSE symbol, BSE code, ISIN, name, former name/symbol |
| `GET/POST/DELETE /api/stocks/{symbol}/aliases` | Aliases a stock is found by; add / delete your own |
| `GET /api/stocks/{symbol}/report[?rebuild=true]` | StockReport DTO: the stored one, built on first request |
| `POST /api/stocks/{symbol}/refresh` | Start (or join) a forced pipeline run: re-fetch the stock's data and rebuild |
| `POST /api/pipeline`, `GET /api/pipeline[/{id}]` | On-demand pipeline: fresh report → no run; else a run to follow |
| `GET /api/pipeline/{id}/events` | Server-Sent Events: live progress of a run |
| `GET /api/stocks/{symbol}/events` | Corporate events: upcoming board meetings and recent events (`kinds`, `days`) |
| `GET /api/stocks/{symbol}/reconciliation` | Open and closed cross-source differences (the banner) |
| `POST /api/stocks/{symbol}/reconciliation/{id}/ignore` \| `/reopen` | Dismiss an explained difference, or reopen it |
| `GET /api/stocks/{symbol}/valuation/sensitivity` | DCF WACC × terminal-growth grid |
| `GET/POST/DELETE /api/stocks/{symbol}/overrides` | User assumptions and manual inputs; POST recomputes |
| `GET /api/screener` | Filter by grade / zone / sector / action / EP / buy-zone distance / mcap, and sort |
| `GET/POST/DELETE /api/watchlist`, `/api/alerts` | Watchlist; in-app price alerts (no broker orders) |
| `POST /api/uploads/screener` | Screener.in Excel export (state consolidated / standalone); optional top-up |
| `POST /api/uploads/xbrl` | Results XBRL documents (NSE/BSE) for one stock, several at once |
| `GET /api/filings`, `/api/filings/summary`, `POST /api/filings/{id}/retry` | Results filings ledger |
| `POST /api/uploads/annual-report` | An annual report (PDF/ZIP) for one stock and fiscal year |
| `GET /api/annual-reports`, `POST .../{id}/reparse`, `GET .../{id}/document` | Annual-report ledger; re-read; the cached PDF |
| `GET /api/review/annual-reports[/summary]`, `POST /api/review/annual-reports/{id}` | Review queue: accept / correct / reject a PDF value |
| `GET /api/stocks/{symbol}/coverage` | Fiscal years × P&L / BS / CF grid by source |
| `GET /api/config`, `PUT /api/config` | View the YAML; replace one file, validated first |
| `POST /api/backtests`, `GET /api/backtests/{id}` | Queue a backtest / poll it (engine: P15) |
| `GET /api/jobs` | Job history, data freshness, open data gaps, refresh queue |
| `GET /api/brokers/status`, `/api/brokers/{fyers,kite}/login` | Broker connections |

## Web app pages

Sign in with `APP_PASSWORD`. Every page is behind the login (SPEC §9).

| Page | What it does |
|---|---|
| `/` Dashboard | Broker connection status; data freshness (latest prices, delivery, technicals, reports, shareholding), open data gaps and recent failed jobs; A-grade stocks at or within 5% of their buy zone; recently triggered alerts |
| `/screener` | Filter by grade, zone, sector, action, EP score, % above the buy zone and market cap. Every column sorts. Filters live in the URL, so any view is linkable, and can be saved as named presets on the server |
| `/watchlist` | Watchlist (unknown symbols are added and picked up by the data jobs) and in-app price alerts (enters buy zone / crosses FV / top band / invalidation), which can be paused or deleted. Alerts never place orders |
| `/backtests` | Queue a backtest (grades × zones × holding period, a date range, optionally a symbol list) and follow its progress; `/backtests/{id}` shows the results (below) |
| `/settings` | **Brokers:** status, plus Connect / Reconnect for configured brokers (Fyers or Kite OAuth, via the API; the callback returns here with a banner). **Config:** a YAML editor for each `config/*.yaml`, validated as you type exactly as at startup; only a valid file can be saved. **Results filings:** the XBRL ledger (stored / pending / failed, with Retry) and an upload for XBRL documents. **Screener uploads (optional):** import a Screener.in export (you choose consolidated or standalone) to fill what filings lack; both rebuild the report |
| `/review` | Annual reports: upload a PDF, the reports read, and the review queue of low-confidence values (accept / correct / reject, each with its PDF page and reasons) |
| `/stocks/{SYMBOL}` | The stock report (below); a pipeline progress panel while it is built or updated |

Broker tokens expire daily (Kite at 06:00 IST), so reconnect from Settings each morning. The
API only uses them for market data.

### Price alerts

Create alerts on the Watchlist & alerts page. Each one watches for a single event: the price
enters the buy zone, or crosses FV, the top band or the invalidation level.

The worker's `alerts_intraday` job checks them every 5 minutes during market hours (09:15–15:30
IST). Live prices come from Fyers first, with Kite asked for any symbol Fyers didn't price, and
the levels come from each stock's latest report.

An alert fires once per transition. A 0.2% hysteresis band and a 60-minute cooldown (both in
`jobs.yaml` → `alerts`) stop a price hovering on a level from spamming you.

Triggered alerts appear under the bell in the nav and on the dashboard. Set
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to also get them on Telegram, then use Settings →
Notifications → "Send test notification" to check delivery.

To run a check outside market hours:
`python -m app.jobs run alerts_intraday --force`.

### Backtests

Backtests ask one question: over a period, would buying the stocks whose grade and zone
matched your rules have beaten the Nifty 500? They follow SPEC §11.

- **Rebalancing:** monthly, over the point-in-time Nifty 500. Each stock's report is rebuilt
  for each month from only the data available then: statements only after their announcement
  date, shareholding only after its filing date, and adjusted prices.
- **Trading:** entries fill at the close one session after the rebalance. Costs are 0.1% per
  side plus STT; all parameters are in `jobs.yaml` → `backtest`.
- **Results page:**
  - CAGR, total return, max drawdown, hit rate, average holding period and trades, each
    beside the Nifty 500.
  - The equity curve against the Nifty 500, both rebased to 100.
  - A grade × zone table in which every cell is its own portfolio, coloured by CAGR against
    the benchmark.
  - A sample of trades, and the run's caveats.

Queued runs are picked up by the worker within a minute. To run them now:
`python -m app.jobs run backtests`.

What the results depend on:
- **Membership history:** a point-in-time universe needs index membership history.
  `index_constituents` records it from the day it first runs; load older constituents for
  longer tests. A run over a period with no membership says so instead of reporting 0%.
- **Symbol lists:** a symbol list is quick to run but biased by survivorship.
- **Announcement dates:** statements without an announcement date (Screener-only periods)
  are excluded, and counted in the caveats.
- **Benchmark:** the Nifty 500 TRI is used when its prices are stored, else the price index.
  The price index understates the benchmark by the dividend yield.

## Stock report page

`/stocks/{SYMBOL}` in the web app (SPEC §9). It needs the single-user login and is served
through a same-origin `/api` proxy in Next.js, so the session cookie works without CORS.

- **Header:** CMP, grade and action badges, and data sources. "Refresh data" queues a refresh.
- **Zone gauge:** the five valuation zones, with Baseline / FV / Top band ticks, the buy-zone
  track and a CMP marker.
- **Chart:** weekly or daily candles (lightweight-charts) with these overlays:
  - demand and supply zone rectangles, and the buy zone
  - 30-week SMA and AVWAP lines
  - Baseline, FV, Top band, POC and invalidation lines
- **Valuation:**
  - the method table, reverse DCF and scenarios
  - a WACC × terminal-growth sensitivity heatmap, coloured by value versus CMP
  - editable assumptions, which POST overrides and swap in the recomputed report, chart and
    heatmap
- **Scorecard:** a six-pillar radar plus expandable sub-metrics, each with its reason.
- **Decision:** the reasons, earned-premium conditions, "why is it cheap?" / value-trap
  checklists and a technical summary.
- **Reconciliation banner:** shown when sources disagree on a filed figure (above), with the
  likely cause and an Ignore button.
- **Red flags and data gaps.**
- **Corporate events:** upcoming board meetings and the latest announcements, results,
  pledge / SAST / insider-trading disclosures and bulk / block deals; red flags are marked.
- **10-year fundamentals:** small multiples for sales, EBITDA, PAT, CFO, FCF, ROCE and CCC,
  plus the shareholding trend. Each chart has a table view.
- **Data coverage:** fiscal years × P&L / BS / CF per basis, each cell labelled and coloured
  by source (XBRL, annual-report PDF, summed quarters, Screener, yfinance); dashed cells are
  gaps, and ⚑ links to values waiting for review.

Chart colours are one validated palette, defined as `--viz-*` tokens in `globals.css`, with
light and dark steps. The app follows the OS colour scheme.

**Demo data.** To explore the UI before any broker or Screener data exists, seed six synthetic
stocks (`DEMOIT`, `DEMOSOFT`, `DEMOCODE`, `DEMOTECH`, `DEMOBANK`, `DEMOFMCG`):

```bash
make demo                                                # or: python -m app.devtools.demo
python -m app.devtools.demo --purge                      # remove them again
```

They are clearly synthetic: names end in "(synthetic demo)" and prices have `source=demo`. A
synthetic NIFTY500 is written only when no NIFTY500 prices exist, and it is purged with the
demo. The demo also adds symbol-master entries with made-up ISINs (`INE9DEMO…`) and BSE codes
(`990001`–`990006`), a former name and symbol for DEMOIT ("Demo Infotech Systems", `DEMOINFO`),
and a BSE-only company (`990099`), so search can be tried. DEMOIT and DEMOCODE get a few
synthetic corporate events, and DEMOSOFT gets one open reconciliation issue, so the events card
and the banner can be seen.

**UI tests.** With the stack running (API, web and `python -m app.jobs pipeline-worker`,
or the worker) and demo data seeded, run `E2E_PASSWORD=<APP_PASSWORD> make e2e`. Playwright covers:

- the login gate and every report section
- saving and clearing an assumption
- the screener's filters, sorting and presets
- watchlist and alert changes
- live config validation (it never saves)
- the broker Connect redirect and callback banner
- the uploads list, and XBRL results filings upload (per-file outcome, refused mismatches)
- backtest form validation, queueing and history; with
  `E2E_BACKTEST_CMD="cd backend && uv run python -m app.jobs run backtests"` set, it also
  runs the job and checks the results page (metrics, equity curve, grade × zone table)

## Technical debug endpoint

`GET /api/stocks/{symbol}/technical/debug` runs the weekly technical engine (SPEC §6) on
stored adjusted prices. It returns everything as JSON so you can overlay it on a
lightweight-charts chart and check it by eye:

- bars and the 30-week SMA
- swings, major swings and BOS/CHoCH events
- demand/supply zones and order blocks, with retests and freshness
- FVGs and the dealing range (EQ/OTE)
- AVWAP series
- volume profile, stage, RS, momentum and participation
- supports

Times are `YYYY-MM-DD`, the last trading day of each week.

To test the buy-zone logic, pass valuation levels as query parameters:

```bash
curl 'localhost:8000/api/stocks/TCS/technical/debug?baseline=3100&fair_value=4000&top_band=4800&grade=B'
```

- `mos` defaults to `mos_by_grade[grade]`. `grade` is one of `A_plus`, `A`, `B`, `C` or `D`, and defaults to `B`.
- Without `fair_value`, `buy_zone` is `null`.
- A symbol with no prices returns 404. Prices that are not yet adjusted return 409.
