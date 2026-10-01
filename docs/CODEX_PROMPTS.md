# Codex Prompt Script — run in order, one prompt per task/PR

Before starting: create an empty GitHub repo `stock-grader`, put `AGENTS.md` at the root and `SPEC.md` + `config.yaml` in `docs/`. Every prompt assumes Codex reads AGENTS.md and docs/SPEC.md.

---

### P0 — Scaffold
> Read AGENTS.md and docs/SPEC.md. Scaffold the repo exactly as the layout in AGENTS.md: docker-compose with db, redis, api, worker, web; FastAPI app with health endpoint; Next.js 15 app with Tailwind + shadcn; Alembic setup; Makefile targets `up`, `test`, `check`, `migrate`. Split docs/config.yaml into config/providers.yaml, valuation.yaml, sectors.yaml, scoring.yaml plus a `technical` section, and add Pydantic models that load and validate them at startup (fail fast on invalid config). Add `.env.example` with FYERS_APP_ID, FYERS_SECRET, FYERS_REDIRECT_URI, KITE_API_KEY, KITE_API_SECRET, KITE_REDIRECT_URI, FERNET_KEY, DATABASE_URL, REDIS_URL, APP_PASSWORD. No business logic yet.

### P1 — DB models
> Implement SQLAlchemy models + first Alembic migration for every table in SPEC §3.4. Every data table has a `source` column and `fetched_at`. Add upsert helpers. Tests: migration up/down, upsert idempotency.

### P2 — Rate limiter, token vault, provider protocol, router
> Implement core/rate_limiter.py (Redis token bucket, per-provider limits from providers.yaml), core/security.py (Fernet encrypt/decrypt), data/providers/base.py Protocols from SPEC §3.1, and data/router.py with priority + fallback + staleness rules. Record DataGap rows when all providers fail. Unit-test the router with fake providers (success, empty, exception, stale).

### P3 — Fyers provider + OAuth
> Implement data/providers/fyers.py with fyers-apiv3: OAuth login/callback endpoints per SPEC §3.3, encrypted token storage with expiry, daily OHLCV (chunk requests to the API's max range per call), index OHLCV, LTP. Symbol mapping NSE:XYZ-EQ. Tests with recorded responses (vcrpy or fixtures); never hit the network in tests.

### P4 — Kite provider + OAuth
> Same as P3 for kiteconnect, including instrument-token lookup cache from the instruments dump. Handle missing historical-data entitlement gracefully (raise ProviderUnavailable so router falls back).

### P5 — yfinance + NSE providers + Screener import
> yfinance provider: OHLCV fallback (.NS / index tickers), corporate actions, annual & quarterly financials mapped to our canonical schema (flag that annual history is limited). NSE provider: session with homepage cookie warm-up and browser headers, polite rate limit; fetch sec_bhavdata_full (delivery %), index constituents CSV, ASM/GSM lists, corporate actions, shareholding. Screener import: parse the Screener Excel export ("Data Sheet") into fin_annual, fin_quarterly, shareholding. Canonical field mapping in one dict, documented. Tests on saved sample files.

### P6 — Adjustment + ingestion jobs
> data/adjust.py for split/bonus adjustment (verify on a stock with a known bonus). Worker with APScheduler jobs from SPEC §10, Redis locks, job_runs logging. CLI: `python -m app.jobs run <job> --symbols XYZ`.

### P7 — Fundamental metrics
> Implement fundamentals/metrics.py, forensic.py, banking.py per SPEC §4 as pure functions. Create tests/fixtures/golden/ JSON schema; I will fill the numbers for 10 stocks. Tests compare within tolerance from AGENTS.md.

### P8 — Valuation engine
> Implement valuation/ per SPEC §5: DCF with 3 scenarios + sensitivity grid, reverse DCF (brentq), own-history bands, relative, EPV/Graham, sector models, blend → Baseline/FV/Top band/zones/confidence. Include worked-example tests with hand-computed numbers in the test file comments.

### P9 — Technical engine
> Implement technical/ per SPEC §6 on weekly bars. Include a debug endpoint returning zones/swings as JSON so I can overlay and visually verify on the chart. Buy-zone intersection logic per SPEC §6.

### P10 — Scoring + decision
> Implement scoring/ per SPEC §7 incl. provisional-grade logic to break the MoS circularity, earned-premium score, decision matrix, Stage-4 override, and `reasons` on every output. Table-driven tests covering every cell of the decision matrix.

### P11 — Report assembly + API
> reports/ builds the StockReport DTO (SPEC §8). Implement all API endpoints in SPEC §8 with Pydantic schemas and OpenAPI docs. Single-user auth.

### P12 — Frontend: stock report page
> Build the Stock Report page per SPEC §9.3: zone gauge component, lightweight-charts weekly chart with zone rectangles, AVWAP lines, 30-wk SMA, valuation level lines; valuation panel with sensitivity heatmap and editable assumptions (POST overrides → re-fetch); radar scorecard; 10-yr fundamentals charts; red flags and data gaps.

### P13 — Frontend: dashboard, screener, watchlist/alerts, settings
> Per SPEC §9. Settings page shows broker connection status with Connect buttons (Fyers/Kite OAuth) and a validated YAML config editor.

### P14 — Alerts
> Intraday alert job using Fyers LTP (fallback Kite). Alert types: enters buy zone, crosses FV, crosses Top band, crosses invalidation. In-app notifications + optional Telegram bot (token in env).

### P15 — Backtest
> Per SPEC §11, point-in-time, results table by grade×zone cell, equity curve vs Nifty 500.

### P16 — Deploy
> Production docker-compose with Caddy (HTTPS), backups for Postgres, env-based config. Document broker redirect-URI registration for the production domain.

---
## v0.2 prompts — run after the XBRL parser task Codex is doing now
Read SPEC §0 and §3.2–3.10 first; they override earlier sections.

### P17 — Harden the XBRL parser (acceptance for the current task)
> Make the NSE+BSE XBRL parser meet SPEC §3.6 steps 1, 2 and 5: a versioned `xbrl_map.yaml` covering Ind-AS and pre-Ind-AS tags; normalisation of units to ₹; consolidated and standalone both stored; restatements stored as versions; long-format `fin_line_items`; FY totals derived from quarters and flagged. Save raw files under data/raw before parsing. Tests: 3 golden companies (a bank, a manufacturer, an IT company) with hand-checked values for 3 different years, including one pre-2017 year.

### P18 — Annual-report PDF gap filler + review queue
> Implement SPEC §3.6 step 3: find the annual report PDF on NSE/BSE, locate the standalone and consolidated balance sheet and cash flow pages, extract with pdfplumber (fallback camelot), fuzzy-map labels to item_codes, and assign a confidence score. Add a review-queue API and a UI table to accept or edit low-confidence rows. Add the coverage-grid endpoint for §3.6 step 4.

### P19 — Symbol master + fuzzy search
> Implement SPEC §3.5 with pg_trgm, an ISIN join across NSE/BSE/Fyers masters, and aliases from NSE symbol/name-change files. Search box in the Next.js header with keyboard navigation. Test that "hdfc bank", "HDFCBANK", "500180" and an old company name all resolve correctly.

### P20 — On-demand pipeline with live progress
> Implement SPEC §3.7: a resumable per-symbol pipeline, an SSE progress endpoint, and a frontend progress panel. Optional-step failures still produce a report with data_gaps. Nightly precompute for Nifty 500.

### P21 — Events + results-driven refresh + reconciliation
> Implement SPEC §3.8 and §3.9: NSE/BSE announcement, results, board-meeting, pledge/SAST, PIT and bulk/block deal fetchers following §3.2a rules; a results_watch job that triggers pipelines; before/after report diffs into notifications; a reconciliation job and a stock-page banner. Market Lens adapter is optional and guarded by config.

### P22 — Broker connect UX + price fallback chain
> Implement the Settings → Brokers page and flow in SPEC §3.3 for Fyers (enabled) and Kite (disabled by config, but code complete). Price router order: Fyers → NSE bhavcopy history builder → yfinance. Morning token reminder. Assert in a test that no order, GTT, holdings, positions or funds endpoints are ever called.

### P23 — Telegram bot + notifications
> In-app notification centre plus a Telegram bot (long-polling, outbound only, allow-listed to the owner's chat_id) with /grade SYMBOL, /buyzone and /status. Alerts from P14 and change-notifications from P21 go to both channels.

### P24 — Home deployment
> Implement SPEC §3.10: a docker-compose profile for Windows/WSL2, restart policies, nightly pg_dump to a configurable path, catch-up jobs on worker start, and a README section for Tailscale access and broker redirect-URI registration. A `make doctor` command checks env vars, DB, Redis, broker token status, NSE reachability and disk space.

### P25 — End-to-end acceptance test
> Playwright test: type "hdfc bank" in the search box, select it, watch the pipeline complete, and verify the report shows Baseline/FV/Top band, zone, grade, action, the coverage grid with ≥10 years of P&L, and a sources panel. Run it against 5 golden stocks. This test must pass before calling v1 done.

### P26 — LLM thesis (local model)
> Optional thesis paragraph on the stock page, written by a local model (Ollama, off by default; SPEC §0 rules out paid APIs) from the report's numbers only. Reject any draft that cites a number not in the report, retry with the problems listed, and tie the stored text to the exact numbers it was written from. See SPEC §8a.

### Later
- ~~NSE/BSE XBRL parser to replace manual Screener uploads~~ (done in P17)
- ~~LLM thesis generator (numbers in → text out, no new facts)~~ (done in P26)
- Optional Fyers/Kite GTT creation with explicit confirmation modal
