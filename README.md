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

## Other data sources

- **yfinance** (`data/providers/yf.py`): price fallback (`TCS.NS`, index tickers from
  `yfinance_index_tickers`), corporate actions, and fundamentals. Yahoo's prices are
  split-adjusted even unadjusted, so the provider reverses that to return raw prices. Annual
  statements cover only ~4 years; results carry a `limited history` warning in `reasons`.
- **NSE** (`data/providers/nse.py`): delivery % from `sec_bhavdata_full`, index constituents
  (niftyindices CSV), ASM/GSM + F&O ban lists, corporate actions, shareholding. Browser headers,
  homepage cookie warm-up (refreshed after `nse.cookie_ttl_s` or on 401/403), `rate_limits.nse`.
- **Screener** (`data/providers/screener_import.py`): parses the Excel export's "Data Sheet"
  into `fin_annual` / `fin_quarterly` / `shareholding` (you state consolidated vs standalone at
  upload) and records data gaps for what the export lacks.

Every source's labels map onto one canonical schema in `backend/app/data/canonical.py`;
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
```

In Docker: `docker compose run --rm worker python -m app.jobs run eod_prices --symbols TCS`.

Prices are stored raw and split/bonus-adjusted (`adj_*`, `data/adjust.py`); adjustment is
recomputed whenever new bars or corporate actions arrive. `valuation_scores` builds every
report in two passes: the first collects each stock's multiples as sector peers, the second
builds the reports. `refresh_queue` handles `POST /api/stocks/{symbol}/refresh` requests.
`backtests` runs queued backtest requests (below).

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
| `GET /api/stocks/search?q=` | Symbol / name search |
| `GET /api/stocks/{symbol}/report[?rebuild=true]` | StockReport DTO: the stored one, built on first request |
| `POST /api/stocks/{symbol}/refresh` | Queue a data refresh + rebuild (worker, within a minute) |
| `GET /api/stocks/{symbol}/valuation/sensitivity` | DCF WACC × terminal-growth grid |
| `GET/POST/DELETE /api/stocks/{symbol}/overrides` | User assumptions and manual inputs; POST recomputes |
| `GET /api/screener` | Filter by grade / zone / sector / action / EP / buy-zone distance / mcap, and sort |
| `GET/POST/DELETE /api/watchlist`, `/api/alerts` | Watchlist; in-app price alerts (no broker orders) |
| `POST /api/uploads/screener` | Screener.in Excel export (state consolidated / standalone) |
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
| `/settings` | **Brokers:** status, plus Connect / Reconnect for configured brokers (Fyers or Kite OAuth, via the API; the callback returns here with a banner). **Config:** a YAML editor for each `config/*.yaml`, validated as you type exactly as at startup; only a valid file can be saved. **Uploads:** import a Screener.in export (you choose consolidated or standalone), which rebuilds the report, and a list of uploaded datasets |
| `/stocks/{SYMBOL}` | The stock report (below) |

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
- **Announcement dates:** statements without an announcement date (such as Screener uploads)
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
- **Red flags and data gaps.**
- **10-year fundamentals:** small multiples for sales, EBITDA, PAT, CFO, FCF, ROCE and CCC,
  plus the shareholding trend. Each chart has a table view.

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
demo.

**UI tests.** With the stack running and demo data seeded, run
`E2E_PASSWORD=<APP_PASSWORD> make e2e`. Playwright covers:

- the login gate and every report section
- saving and clearing an assumption
- the screener's filters, sorting and presets
- watchlist and alert changes
- live config validation (it never saves)
- the broker Connect redirect and callback banner
- the uploads list
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
