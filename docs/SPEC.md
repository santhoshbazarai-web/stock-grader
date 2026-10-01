# SPEC — Stock Grader Web App (Indian Equities)

Version 0.2 · Owner: Santhosh · Single-user personal research tool (see §12 Compliance)

---

## 0. Decisions (v0.2) — these override anything below that conflicts

| Topic | Decision |
|---|---|
| Users | Single user (owner only). No multi-user, no sharing of outputs |
| Universe | Nifty 500 first (point-in-time membership kept); design tables for all NSE mainboard later. No SME |
| Brokers | **Read-only, permanently.** No order, GTT or alert-creation code anywhere in the repo |
| Broker role | Prices only. Brokers do **not** provide financial statements; fundamentals come from NSE/BSE filings |
| Fyers | Primary price source. Uses the owner's existing Fyers API app (free) |
| Zerodha | Optional. Kite Connect Personal (free) has **no historical or live data**; the paid plan (₹500/month) is needed for prices. Budget is ₹0, so the Kite adapter ships **disabled by default** behind `providers.kite.enabled: false` |
| Claude connectors | The Zerodha/Fyers connectors inside Claude **cannot** be used by this app. The app authenticates with brokers itself via OAuth |
| History | 10+ years. XBRL filings first; annual-report PDFs parsed for years/items XBRL lacks (older cash flows, full balance sheets) |
| Fundamental refresh | Automatic, triggered by new results filings on NSE/BSE |
| Hosting | Owner's home desktop via Docker. Redirect URIs on `http://127.0.0.1`. Remote/phone access only through Tailscale (no public exposure) |
| Alerts | In-app + Telegram bot |
| Budget | Free sources only. No paid data, no paid LLM API |
| LLM thesis | Optional, off by default. A **local** model only (Ollama on the owner's machine or network); numbers in → text out, no new facts (§8a) |
| NSE Market Lens | NSE's beta screener (marketlens.nseindia.com). Used only as an optional **reconciliation** source, never primary: it is beta, undocumented and may change or be restricted |

---

## 1. Goals
1. For any NSE stock: Baseline, Fair value, Top band, Zone, Buy zone, Grade, Action — with reasons.
2. Screener across Nifty 500 ranked by Grade × Zone.
3. Watchlist + alerts when a stock enters its buy zone.
4. Backtest: do A-grade + Discount entries beat Nifty 500?

Non-goals (MVP): auto-trading, multi-user SaaS, intraday signals, F&O analytics.

---

## 2. Architecture

```
 Browser (Next.js)
     │  REST/JSON
     ▼
 FastAPI (api) ──── Postgres
     │                ▲
     │ enqueue        │ writes
     ▼                │
 Worker (APScheduler) ─┴── Redis (cache, rate-limit buckets, locks)
     │
     ├── Fyers API v3   (prices, indices, LTP)
     ├── Kite Connect   (prices fallback, LTP)
     ├── yfinance       (fundamentals partial, price fallback)
     ├── NSE archives   (bhavcopy+delivery, index constituents, ASM/GSM, corp actions, shareholding)
     └── Screener export (10-yr fundamentals, manual upload)
```

### 2.1 Services (docker-compose)
| Service | Purpose |
|---|---|
| `db` | Postgres 16 |
| `redis` | cache + rate limiter |
| `api` | FastAPI, uvicorn |
| `worker` | scheduled jobs + on-demand refresh queue |
| `web` | Next.js |

---

## 3. Data layer

### 3.1 Provider abstraction
```python
class PriceProvider(Protocol):
    def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...
    def ltp(self, symbols: list[str]) -> dict[str, float]: ...

class FundamentalsProvider(Protocol):
    def annual(self, symbol: str) -> pd.DataFrame: ...     # rows = fiscal years
    def quarterly(self, symbol: str) -> pd.DataFrame: ...
```
`data/router.py` resolves each dataset using the priority list in `config/providers.yaml`, falling through on error/empty/stale data, and records which provider served it (`source` column on every table).

### 3.2 Dataset → source priority (v0.2)
| Dataset | Primary | Fallback 1 | Fallback 2 | Reconcile against | Notes |
|---|---|---|---|---|---|
| Daily OHLCV (stocks) | Fyers | NSE bhavcopy archive (build history day-by-day) | yfinance `.NS` | — | Kite only if enabled (paid) |
| Daily OHLCV (indices) | Fyers | niftyindices.com historical | yfinance | — | For RS and beta |
| LTP (alerts) | Fyers | — | — | — | Market hours |
| Delivery % | NSE `sec_bhavdata_full` | — | — | — | Daily after 18:00 |
| Symbol master | NSE `EQUITY_L.csv` + BSE scrip master + Fyers symbol master | — | — | — | Joined on **ISIN** |
| Corporate actions | NSE | BSE | yfinance | — | Split/bonus adjustment |
| Index constituents | niftyindices.com CSV | — | — | — | Monthly, stored point-in-time |
| Industry classification | NSE 4-level (macro/sector/industry/basic industry) | config override | — | — | Drives sector model + peers |
| ASM/GSM/F&O ban | NSE | — | — | — | Daily knock-out |
| Quarterly & annual results | **NSE XBRL** | **BSE XBRL** | — | Market Lens, yfinance | Keyed by announcement date |
| Bulk fundamentals (10+ yr, when NSE is blocked) | **Indian API** (stock.indianapi.in, `INDIANAPI_KEY`) | — | yfinance | NSE XBRL | Vendor-reclassified, consolidated; identity-checked by ISIN; monthly call budget |
| Balance sheet / cash flow (historic years XBRL lacks) | Annual-report PDFs from NSE/BSE | — | — | XBRL totals | Semi-automated, see §3.6 |
| Shareholding (promoter, pledge, FII, DII, MF) | NSE shareholding XBRL | BSE | — | — | Quarterly |
| Pledge / SAST / insider trades (PIT) | NSE | BSE | — | — | Governance pillar |
| Corporate announcements, board-meeting dates, auditor changes, credit ratings | NSE | BSE | — | — | Event feed + red flags |
| Bulk / block deals | NSE | — | — | — | Participation signal |
| Risk-free rate | config, weekly | — | — | — | Manual update |
| Screener Excel | **Retired as primary.** Kept as an optional manual import for gap-filling and testing only | | | | |

**Split / bonus adjustment (`data/adjust.py`, providers.yaml `adjustment`).** Prices are adjusted backwards: every bar before an ex-date is multiplied by shares-before / shares-after (bonus 1:1 halves earlier prices). Each event is applied once:
- the same split/bonus from two sources with ex-dates up to `duplicate_window_days` apart (yfinance reports a bonus as a split, sometimes a day off NSE) counts once, on the ex-date the raw closes confirm;
- with `detect_preadjusted`, an event whose ex-date shows no matching move in the raw closes (the price source already adjusted its history) is not applied again;
- after adjustment, a close-to-close move above `abnormal_gap` is reported as a possible missing or doubled event (a `corporate_actions` data gap on `adj_close`).

The corporate-actions job reads `lookback_days` incrementally, but a stock with no actions on file gets its full history (`nse.corporate_actions_from_years`). Every provider answering "no actions" is an answer, not a failure (no data gap).

Per-share fundamentals are put on today's share basis too (`restate_per_share`): a row known (announcement date, else period end) before a split/bonus ex-date has EPS and book value per share divided, and diluted shares multiplied, by shares-after / shares-before. A row announced after the ex-date is left alone: the company already restated it (Ind AS 33). The report notes each restatement.

NSE endpoints need a browser-like session (cookies from the homepage + headers) and polite rate limiting. Use archive CSVs where possible and cache aggressively. Respect each source's terms of use. The Screener import is for personal use only.

### 3.2a NSE/BSE fetching rules
- Prefer archive and bulk files (`nsearchives.nseindia.com`, BSE download files) over page APIs.
- Warm up a session from the homepage to get cookies, send browser-like headers, stay at ≤ 1 request/sec, and use exponential backoff on 429 and network errors.
- **Session strategies** (`data/providers/web_session.py`): page APIs on `www.nseindia.com` and `api.bseindia.com` go through an ordered list of methods, `providers.yaml` `nse.session` / `bse.session` (default `[curl_cffi, playwright, requests]`):
  - `curl_cffi` impersonating Chrome (TLS fingerprint, headers, cookie jar);
  - headless Chromium via Playwright, whose cookies and user agent are reused for the API calls, falling back to `fetch()` from inside the page;
  - plain `requests`.
  The warm-up visits `browser.warmup_urls` in order (homepage → page → API). A 401/403, or HTML where JSON is expected, refreshes cookies once; if it repeats, the method is marked blocked in Redis for `blocked_ttl_s` and the next one is tried. Network errors move on without marking a block. The method that worked is remembered for `remember_ttl_s` and tried first.
- When every method is refused, the call raises `ProviderUnavailable` (not retried) with the message "*<site> is blocking automated access from this connection. Upload XBRL files or a Screener export instead*". The UI shows it with links to the manual uploads (results XBRL upload, Screener export), which remain the fallback. BSE is best-effort: the symbol master is built from NSE + Fyers, and a missing BSE scrip master is a data gap.
- `python -m app.jobs nse-diagnose` / `bse-diagnose` request each endpoint once per method and report status, `server` / `content-type`, cookie **names** (never values), body length and a verdict. The result is shown in Settings → Data sources.
- Exchanges change their protection without notice, so this needs maintenance. No proxies, CAPTCHA solvers or paid services are used.
- Cache every raw file on disk under `data/raw/<source>/<yyyy>/<mm>/<dd>/` before parsing, so you can re-parse without re-downloading.
- Never run fetchers in parallel against the same host.
- Market Lens: read-only JSON the page itself loads, used only in the reconciliation job, and switchable off via config.

### 3.3 Broker authentication
- **Fyers:** OAuth flow. `GET /api/brokers/fyers/login` redirects to Fyers, which calls back `GET /api/brokers/fyers/callback?auth_code=…`. The backend exchanges the code for an access token, encrypts it and stores it with its expiry. Tokens expire daily, so the UI shows a "Reconnect" banner when a token is stale.
- **Kite:** `GET /api/brokers/kite/login` redirects to Kite, which calls back with `request_token`. The backend calls `generate_session`, then encrypts and stores the token. It also expires daily.
- Register the callback URLs in each broker's developer console. Keep API key and secret in `.env` only.
- If no broker token is valid, the price router falls through to the NSE bhavcopy archive and then yfinance automatically. The UI shows the data source.
- **Settings → Brokers page:** one card per broker showing Connected (expires at HH:MM) / Expired / Disabled status and a **Connect** button that starts the OAuth flow in the same tab and returns to Settings. A morning Telegram reminder is sent at 08:45 if the Fyers token is expired.
- No broker scopes beyond market data and profile are used, and nothing reads holdings, positions or funds.

Implementation notes (`api/brokers.py`, `jobs/brokers.py`, `data/bhavcopy_store.py`; `providers.yaml` → `brokers`, `bhavcopy`):
- **Switches:** `brokers.<name>.enabled`. Fyers is on; Kite is off by default (paid historical data), with its code complete.
  - A disabled broker's provider is not built, so the router reports it as not configured.
  - Its login returns 409.
  - `GET /api/brokers/status` reports `enabled`, `configured`, `connected`, `expires_at` and `reason`.
- **Price order:** `daily_ohlcv: [fyers, kite, nse, yfinance]`.
  - `nse` is the bhavcopy history builder: `sec_bhavdata_full` files stored per day for every EQ/BE symbol in `bhavcopy_prices`, with `bhavcopy_days` tracking loaded days and holidays.
  - A request fetches at most `on_demand_max_days` missing days. When more are missing, NSE is unavailable and the router moves on; a partial series is never returned.
  - A 404 at least `holiday_after_days` old is recorded as a holiday; a newer one means the file is not out yet.
  - `bhavcopy_history` (nightly) backfills `history_years`, newest first, `backfill_days_per_run` files per run.
  - `nse_bhavcopy` reads delivery % from the same stored file.
  - Bars recorded under a former symbol (symbol-change aliases) are included.
- **UI:** Settings → Brokers shows one card per broker (Connected (expires HH:MM) / Expired / Not connected / Not configured / Disabled). A "Reconnect" banner appears on every page while an enabled, configured broker lacks a valid token.
- **Morning reminder:** `broker_token_check` (08:45 weekdays, `morning_reminder`) notifies in-app and on Telegram when an enabled, configured broker's token has expired or is missing. Nothing is sent to the broker.
- **Read-only guarantee:** `tests/test_read_only_brokers.py` runs every broker code path through the real SDKs.
  - Every other SDK method is a tripwire.
  - Every HTTP path must be on the market-data / session allowlist.
  - A source scan forbids order, GTT, holdings, positions and funds calls anywhere in `app/`.

### 3.4 Database tables (core)
`instruments`, `prices_daily` (adjusted + raw), `corporate_actions`, `delivery_daily`, `fin_annual`, `fin_quarterly`, `shareholding`, `index_membership`, `surveillance_flags`, `valuation_snapshots`, `technical_snapshots`, `scores`, `reports`, `watchlist`, `alerts`, `broker_tokens` (encrypted), `job_runs`, `data_gaps`, `user_overrides` (per-stock assumption overrides, e.g. custom growth rate).
v0.2 adds `symbols` (ISIN master), `symbol_aliases`, `filings` (raw filing index), `fin_line_items` (long format: isin, period_end, period_type, statement, basis [consolidated/standalone], item_code, value_inr, source, filing_id, announced_at, version), `events`, `reconciliation_issues`, `pipeline_runs` (per-symbol progress), `notifications`.

### 3.5 Symbol master & search
- Build `symbols` nightly by joining NSE `EQUITY_L.csv`, the BSE scrip master and the Fyers symbol master on ISIN. Store NSE symbol, BSE code, Fyers symbol, company name, series, listing date and status.
- `symbol_aliases` holds former names and symbols (from NSE name/symbol-change files), common short names, and user-added aliases.
- Search uses Postgres `pg_trgm` fuzzy matching on symbol + name + aliases, ranked with exact symbol matches first, then Nifty 500 members, then trigram similarity.
- The endpoint `/api/stocks/search?q=` returns within 100 ms.

Implementation notes (`data/symbol_master.py` (pure parsers and join), `data/symbol_store.py`, `jobs/symbols.py`, `data/search.py`; parameters in `providers.yaml` → `nse.symbol_files`, `bse`, `symbols`):
- **Sources:** NSE `EQUITY_L.csv`, `symbolchange.csv` and `namechange.csv` (archives), the BSE `ListofScripData` API (active equity), and Fyers' public `sym_details` CSVs (no login; read when Fyers is configured).
  - Every file is cached raw before parsing (§3.2a).
  - Headers are matched by keywords, and the Fyers ISIN and ticker by pattern. An unknown shape raises an error rather than returning partial data. The layouts are unverified against live files (NSE and BSE are unreachable from the build environment).
- **Join (one `symbols` row per ISIN):**
  - NSE gives the symbol, series, listing date and preferred name.
  - BSE gives the scrip code and its own symbol and name, kept as `bse_symbol` / `bse_name` aliases when they differ (names compared without legal suffixes such as Ltd / Limited). A BSE-only company is a row with no NSE symbol and no instrument; delisted BSE-only scrips are skipped.
  - Fyers gives the broker ticker (the NSE one when both exchanges list it).
- **Aliases:** symbol changes are followed through chains (INFOSYSTCH → INFOSYS → INFY) to today's symbol, and each old symbol becomes a `former_symbol` alias. Old names become `former_name` aliases. The owner adds `user` aliases (`POST /api/stocks/{symbol}/aliases`).
- **`symbol_master` job (daily):** the NSE list is required and the job fails without it. The others are optional: a file that can't be fetched leaves its columns and aliases as they were.
  - Aliases are replaced per kind, from their own file, for the companies in the refresh. Owner aliases, and those of companies no longer listed, are kept.
  - Every NSE listing is upserted into `instruments` (name, ISIN, series, listing date, face value).
  - An instrument whose ISIN now trades under a new symbol is **renamed**, so its history follows the company. If the new symbol already has its own instrument, or a symbol now belongs to another ISIN, nothing is merged and the conflict is reported.
  - A company missing from both exchange masters becomes `inactive`. It keeps its aliases, so old names still resolve.
- **Search:** one `UNION ALL` over NSE symbols, instrument and master names, BSE codes, ISINs, Fyers tickers and aliases.
  - Each term scores max(pg_trgm `similarity`, `word_similarity`) against the lower-cased query. Terms below `symbols.search.min_similarity` are dropped.
  - Codes also match exactly: the symbol (the query with spaces removed, so "hdfc bank" is HDFCBANK), BSE code, ISIN, Fyers ticker, and former, BSE and user symbols.
  - Hits are grouped per company and ranked: exact code match, then current members of `jobs.universe_index` (Nifty 500), then the rest. Within a tier: active first, then score, then NSE-listed before BSE-only.
  - GIN trigram indexes (`fastupdate = off`) cover lower(instruments.symbol / name), lower(symbols.name) and lower(symbol_aliases.alias); btree indexes cover the codes.
  - The planner under-costs the trigram operators and would scan whole tables, so sequential scans are switched off for that one statement (transaction-local `set_config`).
  - Measured here: 12–60 ms per query at 5,000 companies with 15,000 searchable names; the tests assert a median under 100 ms.
- **UI:** the header search box is a WAI-ARIA combobox.
  - ↑/↓ move, Home/End jump, Enter opens the highlighted stock (with no results it opens the typed symbol), Esc closes the list and a second Esc clears the box. `/` or Ctrl/⌘+K focuses it.
  - Each result shows why it matched (e.g. "formerly Bharti Tele-Ventures Limited", "BSE 500180"), Nifty 500 membership and inactive status. A BSE-only company is shown but cannot be opened: it has no NSE data.

### 3.6 Financials pipeline (10+ years, free sources)
1. **XBRL (primary).** Parse NSE and BSE financial-results XBRL (quarterly/annual P&L, half-yearly balance sheet, half-yearly cash flow where filed). Map taxonomy tags to canonical `item_code`s in a single versioned mapping file, `fundamentals/xbrl_map.yaml`, covering both Ind-AS and pre-Ind-AS tags.
2. **Normalise.** Convert units (₹, lakhs, crores, millions) to ₹, sign conventions, and the fiscal year. Keep both consolidated and standalone. When a later filing restates a period, store it as a new `version`; analysis uses the latest version, while backtests use the version available at that date.
3. **Annual-report PDFs (gap filler).** For years where XBRL lacks a full balance sheet or cash flow statement, fetch the annual report PDF from NSE/BSE, locate the statement pages by heading search, and extract tables with pdfplumber, falling back to camelot. Map rows to `item_code`s with a fuzzy label dictionary.
   - Every PDF-derived value carries `source=annual_report_pdf` and a confidence score.
   - Low-confidence rows go to a **review queue** in the UI, where the owner accepts or corrects them in one click.
4. **Coverage report per stock.** Show a years × statements grid (P&L / BS / CF), coloured by source and gaps, on the stock page.
5. **Derived annual figures.** When only quarterly/half-yearly data exists, build the FY total from its quarters and flag it as derived.


Implementation notes (results XBRL, `data/xbrl.py`, `data/results_store.py`, `data/results_ingest.py`, `jobs/fundamentals.py::results_watch`; tag map in `fundamentals/xbrl_map.yaml`; parameters in `providers.yaml` → `nse.results` and `jobs.yaml` → `results_watch`; audit in `docs/XBRL_AUDIT.md`):
- **Source:** every quarterly/annual result is filed with NSE and BSE as an XBRL instance in the SEBI/BSE results taxonomy (prefix `in-bse-fin`). NSE's `corporates-financial-results` list gives each filing's document URL, period, basis (consolidated / non-consolidated), audit status and dissemination time.
- **Contexts** (non-dimensional only; segments are ignored):
  - A duration context ending on the reporting date whose length is in `quarter_days` is the quarter; one in `year_days` is the fiscal year (Q4 / annual filings).
  - The instant context on that date is the balance sheet. 6/9-month year-to-date contexts are skipped.
  - Comparatives (quarter / year / instant contexts ending on other dates) are stored as line items too; they are where restatements appear.
  - **Basis-only dimensions (`xbrl_map.yaml` v4, `basis_axes`):** a filing with no usable plain context, whose contexts carry a single dimension on a listed axis with a consolidated / standalone member, is read from the contexts of the basis it states (or the only one present), with a warning. It is never read from the other basis or from any other dimension. Descriptive facts (period, basis) are read from any context when no plain one has them.
  - A filing with no quarter or year results fails with the contexts ending on its date (plain, and the dimension members of the others), the mapped P&L elements and where they sit, and the unmapped numeric elements. `xbrl-inspect` prints `NOT PARSED: …` plus every context and numeric fact instead of stopping.
- **Listing depth:** NSE's list returns only the latest ~50 filings per `period`. A list of `list_truncated_at` or more is requested again in `from_date`/`to_date` windows of `list_window_years` back `list_history_years` (deduplicated by URL). A window that is itself full is reported as possibly cut off.
- **Mapping (§3.6 step 1):** element names live only in the versioned `fundamentals/xbrl_map.yaml`, with tag groups `ind_as`, `bank` and `pre_ind_as` (Indian GAAP / Clause 41). The pre-Ind-AS names are unverified until checked against a real filing with `xbrl-inspect`. The namespace year is ignored.
  - Each item has a statement (P&L / BS / CF / ratio): BS items are read from instant contexts, P&L and CF from duration contexts.
  - Every value records the tag that matched (`group:Element`, or `sum:group:A+B`) and the map `version`.
  - Split lines (COGS, borrowings, cash, investments, receivables, net block, tax) are the sum of the lines filed; the field is NULL if none is filed. Capex, asset sales and dividends are stored as magnitudes.
  - Derived in the wide rows: `ebit` = PBT + finance cost; `ebitda` = PBT + finance cost + D&A − other income; `shares_diluted_cr` = PAT / diluted EPS; book value per share = equity / shares.
  - Bank filings (marked by `InterestEarned`) fill `extra` for `fundamentals/banking.py`; period-end balances (`carry_to_year`: NPAs, CRAR, advances, deposits, investments) reported only in the Q4 quarter context also apply to the fiscal year.
  - SG&A is not in the results format (a data gap; a Screener upload can fill it).
- **Bank equity (`xbrl_map.yaml` v3):** banking results file `Capital` and `ReservesAndSurplus` but no total-equity element, so `total_equity` is their sum (`sum_of`) for the `bank` group. Before v3, banks had no book value (P/B and BVPS bands were missing).
- **Units (§3.6 step 2):** XBRL amounts are rupees by rule, so the rounding level a filing states (`LevelOfRoundingUsedInFinancialStatements`) is presentation only.
  - A filer that keyed amounts in that level instead is detected once per filing. The primary check: PAT ÷ diluted EPS implies fewer than `nse.results.min_plausible_shares` shares unscaled, but enough once scaled. Without EPS: most monetary facts carry `decimals ≥ 0`.
  - All amounts are then multiplied by the level's factor from `nse.results.rounding_levels` (lakh 1e5, million 1e6, crore 1e7...), with a warning on the filing.
  - Amounts in a currency other than INR are not read (warning).
  - Line items hold ₹; the wide tables ₹ crore; per-share stays ₹; `pure` percentages ×100.
- **Line items and versions (§3.4, §3.6 step 2):** `fin_line_items` holds one row per instrument, period end, period type (quarter / year / instant), statement, basis, item code and version. Each row carries `value_inr`, `unit`, `source` (`nse_xbrl` / `upload_xbrl` / `derived` / `annual_report_pdf`), `filing_id`, `announced_at` (publish time), `usable_from` (rule-4 date), `tag`, `map_version` and `isin`.
  - A figure for a period that differs from the previous one by more than both `restatement_tolerance_rel` and `restatement_tolerance_inr` (rounding noise) is a restatement and becomes the next version; an equal one adds nothing.
  - Versions are ordered by `usable_from`, not by download order: a backfill runs newest first.
  - Re-parsing a filing replaces its own figures in place.
- **Wide rows:** `fin_quarterly` / `fin_annual` are rebuilt from the latest versions of every period a filing touched (analysis uses the latest version). A quarter needs its P&L. A year row is written when its P&L is known, or when its fiscal-year-end balance sheet is (a period ending in the company's fiscal-year-end month; a half-year balance sheet never makes a year row). The P&L is then left NULL, so a bank whose full-year P&L isn't parsed still has book value. An existing row is updated.
  - `announcement_date` is the earliest date any figure of the period was usable. It is also never later than one already stored, so a quarter first seen via yfinance keeps the real, earlier filing date once the filing arrives.
  - Precedence: filed values overwrite what they cover and never blank other columns. A Screener upload fills only the empty columns of periods a filing stored, and leaves their source and date alone.
- **FY derived from quarters (§3.6 step 5):** after each filing, every fiscal year containing a quarter it touched is checked. If all four quarters are stored and the year has no filed P&L (e.g. the Q4 filing had no year context, or the annual filing failed), each P&L amount reported in all four quarters is summed from their latest versions.
  - The sums are stored as year line items with `derived = true`, source `derived`, and `usable_from` = the day the last quarter became usable.
  - `fin_annual.is_derived` is set, and the report lists those years under data gaps.
  - EPS is not summed; balances come from the Q4 quarter (`carry_to_year`); balance sheet and cash flow stay empty.
  - The year end is the company's filed year-end month, else `nse.results.default_fy_end_month`.
  - A later filed annual figure becomes the latest version even when it equals the sum.
- **Backtests:** `backtest/pit.py::versioned_frame` turns each period's line items into one row per date a figure became usable, carrying the latest version known by then, so a restatement is seen only from its own date (§3.6 step 2). Periods without line items keep their stored row.
- **Announcement date (rule 4):** the exchange's dissemination time; at or after `available_after_ist` (the close) it counts from the next day. An uploaded document has no dissemination time, so its date is the board-meeting date + 1 day.
- **Ingestion:** `result_filings` is the ledger (one row per document, pending → parsed / failed).
  - `results_backfill` re-reads every symbol's list in results season, or weekly (`index_recheck_days`) outside it.
  - It downloads at most `max_downloads_per_run` documents per run, newest period first, back to `providers.history_years`, and retries download failures up to `max_attempts`. A document that downloaded but did not parse is recorded with the parser version (`result_filings.parse_failed_version`, `map<xbrl_map version>.r<PARSER_REVISION>`): it is not downloaded or parsed again until that version changes, then it is re-parsed from the raw cache (downloaded only if the cache is missing).
  - Documents are fetched only over https from `xbrl_hosts`, capped at `max_xbrl_bytes`, and parsed with `defusedxml`.
  - A document naming another symbol, or with no stated basis, is refused (rule 5).
  - If a symbol's list can't be read in season, new quarters come from yfinance with their first-seen date and a `results_filing` data gap, which is resolved when the filing is stored.
- **Raw cache (§3.2a):** every XBRL document, every NSE results-list JSON and every uploaded file is written to `RAW_DATA_DIR/<source>/<yyyy>/<mm>/<dd>/` (IST fetch date; source `nse` or `upload`) before it is parsed.
  - The ledger's `raw_path` points at the document.
  - Files are never overwritten; a different body under a taken name gets a hash suffix.
  - An unwritable cache fails the filing (or the upload, with 503) rather than parsing uncached.
- **Tools:**
  - `python -m app.jobs xbrl-inspect <file.xml>` prints the contexts used, each item with its matched tag, and the numeric elements the map ignores.
  - `python -m app.jobs xbrl-reparse [--symbols …]` re-applies the current map to the cached documents without network access.

Implementation notes (annual-report PDFs, §3.6 steps 3-4: `data/annual_report.py` (pure reader), `data/annual_report_store.py`, `jobs/annual_reports.py`, `api/annual_reports.py`, `data/coverage.py`; label dictionary in `fundamentals/pdf_labels.yaml`; parameters in `providers.yaml` → `nse.annual_reports` and `jobs.yaml` → `annual_reports`):
- **Which reports:** the `annual_reports` job (weekly) looks at each stock's completed fiscal years, back to `providers.history_years` and `annual_reports.first_fiscal_year`. A year needs a report when any of `annual_reports.required_items` (total assets, total equity, CFO) is missing from fin_line_items for the company's basis (consolidated if it has any consolidated items, else standalone).
  - It takes that year's report from NSE's annual-report list (`nse.annual_reports.index_path`), else next year's, whose comparative column covers the year.
  - Reports are fetched only over https from `nse.annual_reports.hosts`, capped at `max_bytes`. A ZIP is unpacked to its largest PDF. The raw file is cached before reading (§3.2a).
  - Failures are retried up to `annual_reports.max_attempts`. BSE fetching is not implemented; BSE reports can be uploaded (`POST /api/uploads/annual-report`, with the publication date).
- **Locating statements:** a page is a candidate when one of its first `heading_lines` text lines holds a balance-sheet or cash-flow title (`headings`), and that line holds no `exclude` phrase (notes, contents, schedules).
  - The page is consolidated when its top lines say so; otherwise it is standalone.
  - Of several candidates for one statement and basis, the page mapping the most rows wins. Following pages continue it while they have no other title and map at least `continuation_min_rows` rows.
- **Rows:** pdfplumber words are grouped into lines (`line_tolerance_pt`).
  - Value columns are clusters of amount right-edges `column_gap_pt` apart, each with at least `min_column_rows` amounts. A cluster of note numbers is dropped.
  - Each column's date is the date printed above it in the header. When none is printed, the columns are dated from the report's fiscal year (first column that year, the next the year before).
  - Parentheses and a leading minus mark negatives; a printed dash is a nil (0), not a missing value.
  - When pdfplumber maps fewer than `camelot_min_rows` rows, camelot (stream mode) reads the pages, and its result is kept if it maps more.
- **Mapping:** labels are normalised (lower case, `&` → and, ₹/Rs., note references, list markers and punctuation removed) and compared with rapidfuzz `ratio`.
  - A row's candidates are the items and cross-check rows of its statement whose `sections` include the current section. Section headings are matched exactly. The row's `ignore` look-alikes also compete.
  - A label wrapped over two lines is joined when that matches better. A match below `min_label_score` is dropped.
  - Each non-sum item takes its best unclaimed row. A `sum` item adds every row it best matches, e.g. non-current + current borrowings, or MSME + other trade payables.
  - An item with no row but a `fallback_sum` is the sum of those items. For example, total equity = share capital + reserves in Indian GAAP layouts, which print no total.
  - Values are scaled to ₹ by the unit the page states (the `nse.results.rounding_levels` keywords, e.g. "Rs. in Lakhs" → ×1e5). A page with no unit, or two different ones, yields no ₹ value: it must be entered by hand. Outflow items with `magnitude` in `xbrl_map.yaml` are stored positive.
- **Confidence** (`nse.annual_reports.confidence`): similarity × label weight × each applicable factor:
  - `camelot_factor`: the camelot fallback read the value.
  - `ambiguous_factor`: another item or an ignore label scored within `ambiguity_margin`.
  - `no_header_dates_factor`: the column date is not printed.
  - `check_failed_factor` or `no_check_factor`: the column's cross-check failed, or it has no cross-check rows. The checks are total assets = total equity and liabilities, and CFO + investing + financing = net change in cash, both within `check_tolerance_rel`.
  - `fallback_sum_factor`: the value is a fallback sum; it uses the lowest confidence of its parts.
  - A sum item uses the lowest confidence of its rows.
- **Review queue:** every value is a `pdf_line_candidates` row.
  - At or above `auto_accept`, with a ₹ value, it is `auto_accepted` and stored; otherwise it is `pending`.
  - The owner accepts (stored with confidence 1.0), corrects (₹ crore, optionally to another item of the same statement), or rejects it. A rejected value is removed from fin_line_items, and its wide column is cleared.
  - Re-reading a report (`pdf-reparse`, or after a `pdf_labels.yaml` change) replaces its undecided values and keeps the owner's decisions.
- **Storage (gap filler only):** accepted values go to fin_line_items with `source = annual_report_pdf`, `annual_report_id`, `confidence`, `usable_from` (the report's dissemination time under the rule-4 close cut-off; for an upload, the publication date given, else none, so backtests ignore it) and `map_version` = the `pdf_labels.yaml` version.
  - A key (period, period type, statement, item) with an exchange-filed figure is never written; the candidate notes why.
  - An exchange figure arriving later removes the key's PDF versions, and the candidate is marked as superseded.
  - Across reports, versions follow the XBRL rules: ordered by `usable_from`, and a figure differing beyond the restatement tolerance is a new version.
  - The wide fin_annual row is updated when it exists, or created from a fiscal-year-end balance sheet. It keeps its `source`.
- **Coverage grid (§3.6 step 4):** `GET /api/stocks/{symbol}/coverage` returns the last `history_years` completed fiscal years × P&L (year), BS (instant) and CF (year) per basis. The `xbrl-coverage` CLI counts years the same way, so they agree:
  - a P&L year is a full-year figure, filed or summed from four quarters (`+n derived`); quarters are reported apart (`n quarter(s)`) and never make a year on their own;
  - a BS year is the balance sheet at the fiscal-year end.

  Labels are Indian fiscal years named by the year they end in (FY24 = April 2023 to March 2024) in the grid, the fundamentals charts and the CLI.
  - Each cell lists its line-item sources, best first: XBRL, then PDF, then summed quarters. A cell with no line items shows its wide row's source (Screener, yfinance) when the statement's marker column (revenue / total assets / CFO) is filled.
  - Each cell also counts the values pending review. The stock page shows it as the "Data coverage" grid.
  - An empty CF cell that results XBRL cannot fill is marked "not in XBRL: use the annual report" (`AR`), not shown as a failure: every year for a bank, NBFC or insurer (sector model, or results filed in the banking format), and years before `nse.results.cash_flow_from_fy` (2020) for everyone.
- **Tools:** `python -m app.jobs pdf-inspect <report.pdf> [--fy YEAR]` prints the pages found, each value with its confidence, and the warnings, without a database. `python -m app.jobs pdf-reparse [--symbols …]` re-reads cached reports. Scanned reports (no text layer) are refused; OCR is not supported.

### 3.7 On-demand pipeline (when you type a stock)
1. The user selects a symbol. If `reports` holds a result that is fresher than both the latest price date and the latest filing date, return it instantly.
2. Otherwise, enqueue a `pipeline_run` with these steps:
   - `symbol` → `indianapi` → `prices` → `corporate_actions/adjust` → `filings index` → `xbrl parse` → `pdf gap-fill` → `shareholding/events` → `reconcile` → `metrics` → `valuation` → `technical` → `scoring` → `report`
3. The frontend subscribes to `/api/pipeline/{run_id}/events` (Server-Sent Events) and shows a step-by-step progress list. Each step shows success, warning or failure with its message.
4. Steps are idempotent and resumable. A failure in an optional step (e.g. PDF gap-fill) still produces a report, with its `data_gaps` listed.
   - The optional `indianapi` step runs before every NSE step, so that 10+ years of statements exist even when NSE is blocked (§3.2).
   - It fetches only when the cache is due: a refresh-triggered run is a user refresh.
   - Its message gives the years per statement, the model and the month's quota, e.g. "P&L 12 yr, BS 12 yr, CF 12 yr (bank model; fetched) · Indian API: 5/500 calls this month".
   - A missing key or an exhausted budget is a warning, never a failure.
5. Nifty 500 is pre-computed nightly, so it normally loads instantly.

Implementation notes (`pipeline/runner.py`, `api/pipeline.py`, `pipeline_runs`; parameters in `jobs.yaml` → `pipeline`):
- **Step 1 (fresh?):** the stored report is served without a run when it is at least as new as the stock's latest price bar, was built after the latest parsed filing (results XBRL or annual report), and is younger than `max_report_age_hours`. `force` (the "Refresh data" button) runs anyway. A symbol has at most one queued or running run; asking again joins it.
- **Steps:** each step reuses the nightly job code for the one symbol.
  - Per-run budgets: `max_xbrl_downloads` and `max_annual_reports`; the nightly jobs fetch the rest.
  - Filings index and XBRL parse are the two halves of `results_backfill`.
  - Technicals rank the stock's RS against the universe's latest stored snapshots, never against itself alone.
  - Metrics, valuation and scoring report from one report build, rebuilt after the technical step so the new RS percentile counts.
  - Shareholding & events stores the shareholding pattern. It reports the stock's events on file, which come from the market-wide feeds (§3.8), not a per-stock fetch.
  - Reconciliation runs §3.9 for the stock. Open differences make it a warning.
  - Required steps: symbol (in the symbol master, once that is built), prices (stored bars suffice when the refresh fails), metrics, valuation, scoring, report.
- **Optional-step failures:** a failed optional step shows as failed (or a warning) and the run goes on. Its message is added to the report's `data_gaps` as "pipeline <step>: <message>", and the report is still built.
  - The Results XBRL step ends with the years found per statement ("Years found: P&L 3 yr, BS 3 yr, CF 0 yr (consolidated)") and the known parse failures it skipped.
  - Fundamental metrics is optional but fails loudly: no fiscal year with a P&L fails it with the years stored per statement and where to get the rest (Results XBRL step, XBRL / Screener upload); otherwise it lists the metrics it could not compute. A failed required step fails the run, and the steps after it are skipped.
- **Resumable:** the worker claims a queued run with `FOR UPDATE SKIP LOCKED`.
  - A heartbeat thread keeps long steps owned. A running run whose heartbeat is older than `stale_after_s` is taken over and resumed from its first unfinished step; the data steps are upserts, so repeating one is harmless.
  - After `max_attempts` claims the run fails.
  - The worker process runs a pipeline thread (queued runs start within `poll_interval_s`); the `refresh_queue` job is the fallback, and `python -m app.jobs pipeline-worker` runs the loop alone.
- **Events:** `GET /api/pipeline/{id}/events` is Server-Sent Events. It sends a `progress` event (the whole run) whenever its `version` changes, `end` when it is done or failed, and keep-alive comments every `sse_heartbeat_s`; the stream stops after `sse_max_minutes`. It polls the database every `sse_poll_s` with short-lived sessions.
- **UI:** the stock page shows a stored report at once and, if it is not fresh, a compact progress panel while a run updates it. A stock seen for the first time shows the full panel until its report exists. "Refresh data" starts a forced run.
- **Nightly precompute:** `valuation_scores` runs at 23:30, after that evening's prices, technicals, shareholding and results jobs, so Nifty 500 reports are fresh the next day.

### 3.8 Results-driven refresh
- `results_watch` job:
  - Polls the NSE and BSE corporate-announcement and financial-results feeds every 15 min from 07:00 to 23:00 IST.
  - Also reads the board-meeting calendar to know which companies to expect.
  - When a new results filing appears for a universe stock, it enqueues that stock's pipeline.
- Afterwards, the job compares the new report with the previous one. If the grade, zone, action or fair value changed by more than 5%, it creates a notification (in-app + Telegram), e.g. "XYZ Q2 results: Grade B→A, FV ₹1,240→₹1,390, zone Fair→Discount".

Implementation notes (`jobs/events.py`, `data/events.py`, `data/event_store.py`, `reports/diff.py`; `jobs.yaml` → `results_watch`, `events`, `event_classification`):
- **Feeds:** each job reads the `(provider, feed)` pairs in its `feeds` config.
  - `results_watch` (every 15 min, 07:00–22:45) reads NSE's results-filing list (all companies), NSE board meetings and BSE announcements. BSE's `Result` category counts as a results filing.
  - `events` (every 30 min, at :07 and :37) reads NSE announcements, pledges (SAST reg. 31), SAST reg. 29, PIT insider trades, and the bulk / block deal files on the archives host.
  - Each run re-reads the last `lookback_days`; a feed never read starts `first_run_days` back.
  - Rows are deduplicated on `(exchange, kind, source_id)`. The id is the feed's own id, else a hash of the fields that identify the row.
- **§3.2a:** every raw response is cached before parsing.
  - JSON feeds are read in windows of at most `max_days_per_request` days. BSE is paged, with a rate-limit token for each page after the first.
  - The two jobs share a Redis lock (`job-lock:exchange-feeds`), so they never fetch from the exchanges at the same time. A job that waits more than `events.lock_wait_s` is recorded as skipped.
  - A payload of an unknown shape is a provider error (after the raw file is cached), never partial data.
- **Linking and classification:**
  - Events are linked to an instrument by NSE symbol (including former symbols), ISIN, BSE code, then normalised company name. Unlinked events are kept.
  - The category comes from keyword rules in `event_classification.categories`: the first category with a rule whose words all appear. `red_flags` lists the categories marked as red flags.
  - Red-flag events of the last `red_flag_days` are listed in the report's `red_flags`.
  - Auditor-resignation events feed the auditor knock-out. The owner's `auditor_resignations` override wins. "None on record" counts only once stored announcements reach back over the whole knock-out window; before that the check stays unknown.
- **Triggering:** unhandled results events of the last `lookback_days` for universe stocks (Nifty 500 + watchlist) start a forced run with `trigger=results`.
  - The run's `context` carries the previous report's summary (`baseline`) and a label such as "Q4 FY24 results".
  - A second filing of the same results within `rerun_after_hours` (the other basis, or BSE after NSE) joins the recent run instead of starting another.
  - Events are marked `handled_at` with the run they started or joined. Events of stocks outside the universe are marked handled without a run.
- **Notification:** the pipeline's report step compares the new report with `baseline` (`reports/diff.py`) and notifies once per run, recording it in the run's `context`.
  - It notifies when the grade, zone or action changed, or FV moved more than `results_watch.notify.fv_change_rel`.
  - Title: "XYZ Q2 FY25 results: Grade B→A, FV ₹1,240→₹1,390, zone Fair→Discount". The FV is always shown.
  - A stock's first report has nothing to compare against, so it never notifies.
- **Calendar:** results board meetings from today to `board_meeting_days_ahead` are listed in the job's details and on the stock page's events card.
- **API/UI:** `GET /api/stocks/{symbol}/events` returns upcoming board meetings plus the latest events. The stock page shows them in a "Corporate events" card.
- **Unverified formats:** the field names follow NSE's and BSE's pages as of writing. The fixtures in `backend/tests/fixtures/events` are hand-written, not captured, because the build sandbox cannot reach the exchanges.

### 3.9 Reconciliation
- For each new period, compare sales, EBITDA, PAT, CFO, total assets and equity across XBRL (NSE), XBRL (BSE), Market Lens (optional) and PDF (if present).
- A difference above 2% creates a `reconciliation_issue`, lowers valuation confidence and shows a banner on the stock page. Common causes are a consolidated/standalone mix-up, units, or a restatement.

Implementation notes (`fundamentals/reconcile.py` (pure), `data/reconcile_store.py`, `jobs/reconcile.py`; `jobs.yaml` → `reconciliation`):
- **When:** as the pipeline's `reconcile` step, and nightly (`reconcile`, 23:00) for universe stocks with a results filing or annual report stored since the last successful run.
- **Periods:** the latest `years` fiscal years and `quarters` quarters with exchange-filed figures, on the company's basis (consolidated when filed, rule 5). BS and CF items are compared for years only.
- **Sources** (`sources`, in reference order; the first with a value is the reference):
  - `nse_xbrl`: latest line-item versions, excluding summed quarters. EBITDA = PBT + interest + depreciation − other income, as in the canonical tables.
  - `annual_report_pdf`: auto-accepted, accepted or corrected PDF values; values still in the review queue are not compared.
  - `market_lens`: only when `providers.market_lens.enabled`.
  - `yfinance`: its EBITDA includes other income, so it is not compared (`exclude`).
  - BSE XBRL is not ingested yet, so it is not a source.
- **Issue:** a source differing from the reference by more than `tolerance_rel` (2%) **and** `min_diff_inr` (₹50 lakh, the lakh-vs-crore rounding gap). Its cause is checked in this order:
  - **units:** the ratio is within tolerance of 100, 1,000, 1 lakh or 1 crore, or their inverse.
  - **basis:** the figure matches the reference source's other-basis figure.
  - **restatement:** the figure matches a superseded version of the reference.
  - otherwise **unexplained**.
- **Lifecycle:** one row per (period, item, differing source).
  - Re-checking updates an open issue.
  - An issue the owner **ignored** stays ignored while both figures are unchanged, and reopens if they change.
  - An open issue whose sources now agree is **resolved**.
- **Effect:**
  - Every open issue is listed in the report's `reconciliation_issues`.
  - The valuation confidence drops `valuation.yaml` → `confidence.reconciliation_steps_down` levels (1, floored at low), with a reason in the valuation reasons.
  - The stock page shows a banner with each difference, its likely cause and an Ignore button (`POST /api/stocks/{symbol}/reconciliation/{id}/ignore`, `/reopen`; `GET …/reconciliation`).
- **Market Lens** (`data/providers/market_lens.py`): disabled by default. It reads the page's JSON with every field name in `providers.market_lens`. Periods whose type or basis is not recognised are skipped, and non-numeric values are left out (never 0).

### 3.10 Home deployment
- Runs with Docker Desktop (WSL2 on Windows). `restart: unless-stopped` on all services, and Postgres data on a named volume with a nightly `pg_dump` to a separate drive.
- **Catch-up on startup:** the worker checks `job_runs`. If the machine was off or asleep during a scheduled job, it runs the missed jobs in order.
- Web UI at `http://127.0.0.1:3000`. For phone access, use Tailscale (private network). Never port-forward the router.
- Broker redirect URIs are `http://127.0.0.1:8000/api/brokers/{fyers|kite}/callback`, registered in each developer console. Check that the broker accepts localhost redirects; if not, use the Tailscale HTTPS hostname.
- Telegram uses outbound-only calls to the Bot API, so no public IP or webhook is needed. The bot token and chat ID live in `.env`. The bot also answers `/grade SYMBOL` and `/buyzone` read-only queries via long-polling.

Implementation notes for the home deployment (P24; `docker-compose.home.yml`, `.env.home.example`, `jobs/catch_up.py`, `doctor.py`; README "Home deployment"):
- **Stack:** db, redis, migrate (one-shot), api, worker, web and backup, plus a `doctor` service under the `tools` profile.
  - Every long-running service has `restart: unless-stopped` and a healthcheck.
  - Only `web` (127.0.0.1:3000) and `api` (127.0.0.1:8000, the broker callbacks) are published, bound to loopback; Postgres and Redis publish nothing.
  - Postgres data is in a named volume.
- **`APP_ENV=home`** refuses at start-up:
  - a `WEB_URL` that is neither loopback nor Tailscale (`*.ts.net`, 100.64.0.0/10);
  - a cookie `Secure` flag that doesn't match the scheme;
  - an `APP_PASSWORD` under 12 characters, or the development database password;
  - redirect URIs other than `http://127.0.0.1:8000/api/brokers/<broker>/callback` or `<WEB_URL>/api/brokers/<broker>/callback`.
- **Backups:** `pg_dump` (custom format, verified with `pg_restore --list`) at `BACKUP_AT` (02:00 IST) into `BACKUP_PATH`. That path is required, meant for another drive.
  - The 14 newest daily dumps and 12 monthly ones are kept.
  - A backup is taken at start-up when the last is older than a day.
  - `make home-restore` restores, taking a safety backup first.
- **Catch-up on worker start:** for each implemented, scheduled job not in `catch_up.skip`, the latest cron fire time within `lookback_hours` (72) is compared with the job's latest `job_runs` start.
  - A job with no run since that time is missed. Missed jobs run once each, in due order, through `run_job` (Redis lock, `job_runs` row), in a background thread, so the scheduler starts at once.
  - Seasonal jobs skip themselves off-season.
  - The default skip list is `refresh_queue`, `backtests` and `alerts_intraday`.
- **`make doctor`** (`python -m app.doctor`; `--json`) checks the following, each independently (a crashing check is reported as its own failure). It prints ✓ / ! / ✗ and exits 1 on any failure:
  - env (settings load, password length, database password, credentials for enabled brokers and Telegram) and config validity;
  - Postgres connection and migration head, and Redis;
  - each enabled broker's token;
  - NSE site / archives reachability (a warning only);
  - free disk at the raw-data and backup paths (`jobs.yaml` → `doctor` thresholds);
  - the last backup's age, the last job run (is the worker alive?) and the Telegram bot heartbeat.
  - The compose `doctor` service runs it with the backup folder mounted read-only.

Implementation notes for notifications and the Telegram bot (P23; `alerts/notify.py`, `alerts/bot.py`, `api/notifications.py`; `jobs.yaml` → `telegram_bot`):
- **Both channels:** every notification is created by `notify()`. This covers price alerts (P14), results changes (§3.8), broker-token reminders (§3.3) and tests.
  - It is stored in `notifications`. When the token and chat ID are set, it is also sent to Telegram, and the delivery outcome (`sent` / `failed` / `disabled`) is recorded.
  - A failed delivery can be re-sent (`POST /api/notifications/{id}/resend`).
- **Notification centre:** `GET /api/notifications` takes `unread_only`, `kind` (repeatable), `symbol`, `before_id` paging and `limit`. It returns the unread count, counts per kind and `next_before_id`.
  - Read state: `POST …/{id}/read`, `POST …/{id}/unread` and `POST …/read-all`.
  - `GET /api/notifications/telegram` reports whether delivery is configured, whether the bot is enabled, and the bot heartbeat: state, last poll, last command, last error, and how many messages were ignored.
  - The UI is `/notifications`, linked from the bell.
- **Bot:** a worker thread (or `python -m app.jobs telegram-bot`) long-polls `getUpdates` with `poll_timeout_s`; it is outbound only.
  - It answers only messages whose `chat.id` equals `TELEGRAM_CHAT_ID`. Other chats get no reply and are counted.
  - Commands older than `max_message_age_s` are skipped.
  - The update offset is kept in Redis and acknowledged before answering, so nothing is answered twice.
  - A Redis lock allows one poller; other workers stand by.
  - Errors back off from `error_backoff_s` up to `max_backoff_s`. A 409 (webhook set, or another poller) is reported with the remedy.
  - The token is never logged: errors carry the exception type or Telegram's description only.
- **Commands** (read-only; plain text, clipped to `max_reply_chars`):
  - `/grade SYMBOL` resolves by exact symbol, else fuzzy symbol search (§3.5). It answers from the latest stored report and never builds one.
  - `/buyzone` lists stocks in their buy zone or within `buyzone_near_pct` above it: in-zone first, then by grade and distance, up to `buyzone_limit`.
  - `/status` shows the latest price and report dates, the last run of each job in `status_jobs`, enabled brokers' token status, open data gaps, queued pipeline runs and unread notifications.
  - `/help` lists the commands.

---

## 4. Fundamental metrics (`fundamentals/metrics.py`)
Compute these over 10 years annually, plus TTM:

| Metric | Formula |
|---|---|
| ROCE | EBIT / (Total assets − Current liabilities), average of opening and closing |
| ROE | PAT / average equity |
| ROIC | NOPAT / (Equity + Debt − Cash − Non-op investments) |
| Sales / EBITDA / EPS CAGR | 3, 5, 10 yr |
| OPM | EBITDA / Sales |
| CFO/EBITDA, CFO/PAT | Annual and 5-yr cumulative |
| FCF | CFO − Capex (Capex = Purchase of fixed assets − Sale of fixed assets) |
| FCF conversion | Σ5yr FCF / Σ5yr PAT |
| Other income share | Other income / PBT |
| D/E, Net debt / EBITDA, ICR | ICR = EBIT / Interest |
| Debtor days | Receivables / Sales × 365 |
| Inventory days | Inventory / COGS × 365 |
| Payable days | Payables / COGS × 365 |
| CCC | Debtor days + Inventory days − Payable days |
| Capex intensity | Capex / CFO |
| Dilution | Share-count CAGR over 5 yr |
| Accruals ratio | (PAT − CFO) / Average total assets |

**Forensic scores (`forensic.py`):**
- Piotroski F-score: the standard 9 tests.
- Beneish M-score: 8-variable model. A reading above −2.22 is a flag.
- Altman Z″: the emerging-markets version. Skip for financials.

**Banks/NBFCs (`banking.py`):** NIM, CASA ratio, GNPA, NNPA, PCR, credit cost, CAR/CRAR, cost-to-income, RoA, RoE, loan growth.


Implementation notes (`fundamentals/`, pure functions; windows and thresholds in `scoring.yaml` → `fundamentals`, `forensic`):
- Units: ratios are fractions (0.18 = 18%), `*_days` in days, bank metrics in percent, amounts in ₹ crore.
- Years are fiscal years. "Opening" means the previous fiscal year's closing balance; a missing year is never bridged.
- ROCE, ROE, ROIC and the accruals ratio use the average of opening and closing balances (ROIC's invested capital is averaged too).
- ROIC: NOPAT = EBIT × (1 − effective tax rate), where the effective rate is tax / PBT when PBT > 0 and the rate lies in [0, 1]. Otherwise `valuation.tax_rate_default` is used, and the source is reported.
- A ratio whose denominator is not positive is undefined (NaN), not zero. Interest coverage with zero interest and positive EBIT is +∞ (debt-free).
- Multi-year figures (5-yr averages, 5-yr cumulative CFO/EBITDA, CFO/PAT, FCF conversion, capex intensity, CAGRs) need every year in the window. Otherwise there is no value, and the missing inputs are named.
- CAGR is undefined unless both endpoints are positive. Dilution = CAGR of the diluted share count.
- TTM = sum of the last four consecutive quarters.
- Piotroski: ROA and asset turnover use opening total assets. Leverage = total debt / total assets. The score is undefined if any of the nine tests is undetermined.
- Beneish: PP&E = net block, securities = non-operating investments. LVGI = (current liabilities + total debt) / total assets. Coefficients are Beneish (1999). A value above `forensic.beneish_flag_above` is flagged.
- Altman Z″ = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4, where X1 = working capital / TA, X2 = retained earnings / TA, X3 = EBIT / TA, X4 = book equity / (TA − equity). Zones come from `forensic.altman_*`. Not computed for financials.
- Banks: NIM = NII / average(advances + investments); GNPA uses gross advances; NNPA uses net advances; PCR = (GNPA − NNPA) / GNPA; credit cost = provisions / average advances. CRAR is taken as reported.

---

## 5. Valuation (`valuation/`)

### 5.1 FCFF DCF (`dcf.py`)
- FCFF = EBIT·(1−t) + D&A − Capex − ΔNWC
- Stages:
  - Stage 1: years 1–5, growth = g1.
  - Stage 2: years 6–10, growth fades linearly from g1 to g_T.
  - Terminal value: FCFF₁₁ / (WACC − g_T).
- WACC:
  - Ke = Rf + β·ERP (+ size premium from config).
  - Kd = Interest / Average debt × (1 − t).
  - Weights are at market value.
- β: 2-yr weekly regression against Nifty 500, Blume-adjusted (0.67·β + 0.33).
- g1 default = min(5-yr revenue CAGR, sector cap), overridable by the user.
- Output: per-share value = (EV − Net debt − Minority interest + Non-op investments) / diluted shares.
- **Sensitivity grid:** WACC ±2% in 0.5% steps × g_T from 4–7%.
- **Scenarios:** bear, base and bull by shifting g1, margins and WACC (deltas in `valuation.yaml`).

### 5.2 Reverse DCF (`reverse_dcf.py`)
Solve for the implied g1 such that DCF value = CMP, holding WACC and g_T at base. Use `scipy.optimize.brentq`. Output:
- `implied_growth`
- `gap = implied_growth − historical_5y_growth`

### 5.3 Own-history bands (`bands.py`)
For PE, EV/EBITDA and P/B, build a daily TTM multiple series over 5 and 10 years. Report the median, ±1σ and ±2σ, and convert each back to a price using current TTM earnings, EBITDA or book value. Exclude periods where earnings are ≤ 0.

### 5.4 Relative (`relative.py`)
Use the sector median multiple, adjusted for ROCE and growth relative to peers:
- `adj_multiple = peer_median × (ROCE/peer_ROCE)^a × (growth/peer_growth)^b`
- `a` and `b` come from config.

### 5.5 EPV & Graham (`epv.py`)
- EPV = normalised EBIT·(1−t) / WACC, adjusted for net cash.
- Graham number = √(22.5 × EPS × BVPS).

### 5.6 Sector models (`sector_models.py`)
Driven by `config/sectors.yaml`. A stock's sector key comes from its industry classification, mapped by `config/industries.yaml`:
- **Sources:** NSE's basic industry (quote API `industryInfo.basicIndustry`), else Yahoo's `industry`, in the order of `providers.yaml` `priority.industry`.
- **When:** the weekly `industry_classification` job, and the pipeline's symbol step for a stock not classified yet.
- **Routing:** banks, NBFCs and insurers map to their own models (rule 10).
- **Unmapped labels:** the default model is used, with a "sector unmapped" data gap naming the label. A per-stock sector override still wins.

The sector models:
- **Banks/NBFC:** justified P/B = (ROE − g)/(Ke − g) × BVPS; residual income model.
- **Insurance:** P/EV band; EV plus a VNB multiple (manual EV input allowed).
- **Cyclicals:** normalised mid-cycle EBITDA (7–10 yr median margin × current sales) × EV/EBITDA band median.
- **Real estate:** NAV (manual input) × discount.
- **Holding companies:** SOTP of listed holdings at market value × (1 − holding discount) + standalone business value.

### 5.7 Blend (`blend.py`)
- **Fair value** = Σ(weight × method value), with weights per sector from `sectors.yaml`.
- **Baseline** = min(bear DCF, EPV, band −1σ price), then take the max of that and (0.8 × book value) for asset-heavy sectors only.
- **Top band** = max(bull DCF, band +1σ price), capped at band +2σ.
- **MoS by provisional grade:** A 15%, B 27.5%, C 40% (from config).
- **Zones:**
  - Deep Discount: CMP < Baseline
  - Discount: Baseline ≤ CMP < FV·(1−MoS)
  - Fair: FV·(1−MoS) ≤ CMP ≤ FV·1.10
  - Premium: FV·1.10 < CMP ≤ Top band
  - Extreme Premium: CMP > Top band

Report dispersion across methods. If the coefficient of variation exceeds 35%, raise a "low valuation confidence" flag.


Implementation notes (`valuation/`, pure functions; parameters in `valuation.yaml` and `sectors.yaml`):
- DCF projection is revenue-driven: revenue grows at g_t, FCFF_t = revenue_t × EBIT margin × (1 − t) + revenue_t × (D&A% − capex%) − NWC% × Δrevenue. The base margin, D&A% and capex% are averages over `dcf.margin_years` (every year required); NWC% is from the latest year. A scenario's `margin_delta` shifts the EBIT margin. Cash flows are discounted at year end.
- Where filings omit working capital, minority interest or non-operating investments, the DCF takes them as nil so a value is possible. Each one is returned in `assumed_nil` for the caller to record as a data gap and show in the report.
- WACC uses market-value weights with book debt as the proxy for debt's market value. If there is debt but no interest cost, the cost of debt is unknown and there is no WACC.
- Beta = weekly-return slope over `beta.lookback_years`, Blume-adjusted (0.67β + 0.33), clamped to [floor, cap]. It needs at least half the expected weeks. A beta clamped at the floor or cap is a `beta` data gap (with the unclamped value); a missing beta (benchmark history) is one too.
- Ke = `risk_free_rate` + β × `equity_risk_premium` + size premium (by market cap), and the report shows the build-up ("Ke 13.45% = Rf 6.50% + beta 1.00 x ERP 7.00% + size premium 0.00%"). The risk-free rate is configured in valuation.yaml with `risk_free_source` and `risk_free_as_of`; an undated rate, or one older than `risk_free_max_age_days`, is a `risk_free_rate` data gap. No cost of equity lists what it lacks (beta / market cap).
- Banks, NBFCs and insurers (and NAV / SOTP models) use no WACC and no FCFF: their valuation runs on Ke (justified P/B, residual income); the report shows Ke and "WACC not used".
- Sector g1 cap: `sectors.<name>.g1_cap`, else `dcf.g1_cap_by_default`.
- Reverse DCF searches `dcf.reverse_growth_bracket` with brentq. If the price lies outside the values at the ends, there is no implied growth.
- Bands use the median and sample σ of daily multiples whose denominator is positive, over the lookback. Fundamentals are carried forward from their announcement date. A band needs `bands.min_observations` valid days. EV/EBITDA per share = price + net debt per share.
- Relative valuation uses ROCE as quality for PE and EV/EBITDA, and ROE for P/B and P/EV. It needs positive quality and growth on both sides.
- EPV: normalised EBIT = mean EBIT margin over `epv.normalise_years` × latest revenue. Graham multiplier: `graham_multiplier`.
- Banks: justified P/B uses `sectors.<bank>.long_run_growth` (required for the bank model). The residual-income model grows book value by ROE × (1 − payout).
- Blend: methods without a value are dropped and the remaining weights are renormalised (reported). If the available methods carry less than `blend.min_weight_coverage` of the weight, there is no fair value. The baseline book floor is `blend.asset_heavy_book_multiple`. The top band is capped at band +`zones.top_band_cap_sigma`σ. The primary band is the sector's highest-weighted band method.
- Confidence: CV (population σ / mean of the method values used) above `confidence.low_if_method_cv_above` → low; above `medium_if_method_cv_above` → medium; otherwise high. Fewer than two methods → low. Zone boundaries: FV(1 − MoS) is Fair; FV × `fair_upper_mult` is Fair; the top band itself is Premium.

---

## 6. Technical engine (`technical/`)
All calculations run on adjusted prices. The primary timeframe is **weekly**, with daily used for entry refinement.

| Module | Output |
|---|---|
| `stage.py` | Weinstein stage 1–4 from the 30-week SMA slope + price position + volume |
| `structure.py` | Swing points (fractal, N configurable), HH/HL/LH/LL labels, BOS, CHoCH, trend state |
| `zones.py` | Demand/supply zones: base candles before an impulsive move (range > k·ATR). Zone = base high/low. Freshness = count of retests. Order blocks and FVGs. Dealing-range equilibrium (50%) and OTE (0.618–0.79) |
| `avwap.py` | Anchored VWAP from the 52-wk low, the last results date and the last major swing low |
| `volume_profile.py` | POC, VAH, VAL over the last 1 yr (weekly bins) |
| `rs.py` | Mansfield RS vs Nifty 500 and vs sector index; RS percentile across the universe |
| `momentum.py` | Weekly RSI(14), distance from 200-DMA z-score, 52-wk high proximity |
| `participation.py` | Delivery % vs its own 50-day average; up-week vs down-week volume ratio; VCP detector (contracting pullbacks) |
| `risk.py` | ATR(14) weekly, invalidation = below demand-zone low − 0.5·ATR, R:R to FV and to the Top band |

**Buy zone** = intersection of `[Baseline, FV·(1−MoS)]` (or `[FV·(1−MoS), FV]` for A-grade stocks) with the nearest fresh demand zone, AVWAP or POC below CMP. If they don't intersect, the buy zone is the nearest technical support within the valuation range. If there is none, the output is "No technical buy zone yet". Stage 4 means the buy zone is suppressed.


Implementation notes (`technical/`, pure functions; every parameter is in `technical.yaml`):
- Weekly bars are Monday–Friday weeks built from adjusted daily bars. Each week is labelled with its last trading day. ATR and RSI use Wilder smoothing seeded with a simple mean. The engine needs `min_weekly_bars` weeks.
- Stage uses slope = SMA30_t / SMA30_{t−`stage_slope_weeks`} − 1, which counts as flat when |slope| ≤ `stage_flat_slope_pct`, and the prior slope over `stage_prior_weeks` before that. The stages are:
  - Stage 2: rising SMA and close above it.
  - Stage 4: falling SMA and close below it.
  - Stage 1 (after a decline, or a falling SMA with price above it) or Stage 3 (after an advance, or a rising SMA with price below it) otherwise.
  - A breakout week whose volume is at least `stage_breakout_volume_mult` × the `stage_volume_avg_weeks` average is flagged as volume-confirmed.
- Swings are fractal: a swing high is above the N highs before it and at least as high as the N highs after (N = `swing_fractal_n`; the major swings use `major_swing_fractal_n`). A swing is confirmed only N bars later, so there is no look-ahead.
- BOS and CHoCH are the first close beyond the latest confirmed swing level. It is a BOS if in the direction of the previous break, otherwise a CHoCH.
- The trend is up when the latest swings are HH and HL, down when they are LH and LL, and range otherwise.
- Zones:
  - Impulse bar: range > `zone_impulse_atr_mult` × the previous bar's ATR.
  - Base: the ≤ `zone_max_base_candles` consecutive bars before the impulse with range ≤ `zone_base_max_atr_mult` × ATR. The zone spans the base's [min low, max high]. A bullish impulse makes a demand zone; a bearish one makes a supply zone.
  - Order block: the last opposite-colour bar before the impulse.
  - Retest: each new entry into the zone after the impulse. A close through the zone's far side breaks it.
  - Fresh: not broken and retests ≤ `zone_max_retests_fresh`. Only impulses within `zone_lookback_weeks` are scanned.
- An FVG is a three-bar gap (low_{i+1} > high_{i−1}, or the bearish mirror). It is filled once price trades through the whole gap.
- The dealing range runs from the last major swing low to the last major swing high. EQ is the midpoint. OTE is the `ote_retracement` retracement of that leg.
- AVWAP = Σ typical × volume / Σ volume from the anchor bar, where typical = (H + L + C) / 3. The anchors are:
  - the 52-week-low bar;
  - the first bar on or after the latest results announcement;
  - the last major swing low.
- Volume profile:
  - The last `volume_profile_lookback_weeks` bars are split into `volume_profile_bins` equal bins. Each bar's volume is spread evenly over the bins its range touches.
  - POC is the midpoint of the fullest bin.
  - The value area grows from the POC bin toward the larger neighbour (on a tie, the upper one) until it holds `value_area_pct` of the volume.
- Mansfield RS = (RS / SMA(RS, `rs_sma_weeks`) − 1) × 100, where RS = stock close / benchmark close (weekly). RS is computed against Nifty 500, and also against the sector index when one is supplied. The universe percentile counts ties as half.
- Momentum:
  - DMA z-score = (close − SMA`dma_days`) / σ of the same daily closes.
  - 52-week proximity = close / max weekly high over `high_52w_weeks` − 1.
- Participation:
  - Delivery ratio = mean delivery % over the last `delivery_recent_days` / mean over the last `delivery_avg_days`.
  - Up/down volume = up-week volume ÷ down-week volume over `updown_volume_weeks`.
  - VCP: the last ≥ `vcp.min_contractions` pullbacks (swing high → next swing low, within `vcp.lookback_weeks`) each get shallower, the last is ≤ `vcp.max_final_depth`, and the close is within `vcp.max_distance_from_pivot` of the last swing high.
- Buy zone:
  - Primary supports: fresh demand zones, the AVWAPs and the POC. Secondary supports: unbroken non-fresh demand zones, VAL and the last three major swing lows.
  - A point level becomes the band [level − `buy_zone_point_band_atr` × ATR, level]. Supports are clipped at CMP.
  - Selection:
    1. The nearest primary support is intersected with the valuation range.
    2. Failing that, the nearest support of any kind that overlaps the range is used.
    3. Failing that, the output is "No technical buy zone yet", with a reason added when CMP is already below the range.
  - Invalidation = the chosen support's low − `invalidation_atr_buffer` × ATR. Entry = min(CMP, zone high). R:R = (target − entry) / (entry − invalidation), for FV and for the top band.
  - The `technicals` job stores stage, RS percentile, trend and ATR. The buy-zone columns are filled by `valuation_scores`, which holds the valuation levels.

---

## 7. Scoring (`scoring/`)

### 7.1 Knock-outs (`knockouts.py`)
A stock is capped at grade C if any of these apply:
- Promoter pledge > 10% (config)
- CFO negative in ≥ 3 of the last 5 years
- An auditor resignation in the last 2 years (manual flag or filing parse)
- On the ASM/GSM list
- Market cap < ₹500 Cr
- 20-day average traded value < ₹5 Cr
- Beneish M > −1.78

### 7.2 Pillars (0–100 each; sub-metrics scored by piecewise-linear maps in `scoring.yaml`)
| Pillar | Weight | Sub-metrics |
|---|---|---|
| Quality | 25 | 5-yr ROCE avg, ROCE trend, CFO/EBITDA, FCF conversion, Piotroski |
| Growth | 20 | 5-yr sales & EPS CAGR, last 4Q YoY EPS growth, acceleration |
| Valuation | 20 | Zone position: (FV − CMP)/FV mapped to score; reverse-DCF gap |
| Financial health | 15 | D/E, ICR, Net debt/EBITDA, CCC trend, Altman Z″ |
| Governance & ownership | 10 | Pledge, promoter trend, MF/FII/DII QoQ change, other-income share, RPT flag |
| Technical | 10 | Stage, RS percentile, structure trend, delivery trend |

Banks use a bank-specific Quality and Health map (asset quality, NIM, CAR).

### 7.3 Grade
A+ ≥ 85, A ≥ 75, B ≥ 60, C ≥ 45, D < 45, then apply knock-out caps. Note the circular dependency: the grade decides the MoS, which decides the zone, which feeds the valuation pillar. Resolve it by computing a **provisional grade that excludes the Valuation pillar** to pick the MoS, then compute the final grade.

### 7.4 Earned-premium score (`earned_premium.py`, 0–8)
One point for each condition met:
1. Reverse-DCF implied growth ≤ 5-yr historical growth
2. Last 2 quarters show YoY EPS growth accelerating
3. ROCE is up versus 3 years ago
4. OPM is up YoY with sales growth > 15% (operating leverage)
5. MF + FII + DII holding is up QoQ
6. Promoter holding is flat or up, with no new pledge
7. RS percentile ≥ 80
8. Stage 2 and within 10% of the 52-wk high

### 7.5 Decision matrix (`decision.py`)
| Grade \ Zone | Deep Discount | Discount | Fair | Premium | Extreme Premium |
|---|---|---|---|---|---|
| A+/A | Strong Buy* | Buy / Accumulate | Buy on Pullback → buy zone | EP ≥ 6: Momentum Entry; else Wait | Hold if owned; don't initiate |
| B | Buy w/ confirmation | Accumulate slowly | Wait | Avoid | Book Profits |
| C | Value-trap check | Watch | Avoid | Avoid | Avoid |
| D | Avoid | Avoid | Avoid | Avoid | Avoid |

\*Deep Discount on an A-grade stock always attaches a "Why is it cheap?" checklist (news, governance, regulatory action, one-off losses).

Stage 4 overrides any Buy into Wait, with the reason "downtrend — wait for Stage 1 base".


Implementation notes (`scoring/`, pure functions; every threshold, map and the matrix itself live in `scoring.yaml`):

Knock-outs
- A check with missing input is reported as *unknown* (a data gap). It never passes or fails silently.
- The CFO check does not apply to banks, NBFCs and insurers (their operating cash flow moves with deposits and loans; their results carry no cash-flow statement). Their valuation (justified P/B, P/B band, relative P/B) and pillars use no cash flow either.
- The CFO check looks at the last `negative_cfo_window_years` years. It is decided from partial data only when the known years settle it: enough negatives already, or too few even if every missing year were negative.
- An auditor resignation counts if it falls on or after `as_of` minus `auditor_resignation_years`.

Pillars
- A pillar is the equal-weighted mean of its available sub-metric scores. It needs at least `pillar_min_coverage` of its sub-metrics. Otherwise it has no score and the missing sub-metrics are listed.
- Sub-metrics that the table above leaves without a named map use these maps:
  - `roce_trend`: ROCE now − ROCE `trend_years` ago.
  - `eps_acceleration`: the latest quarter's YoY EPS growth − the previous quarter's.
  - `ccc_trend_days`: CCC now − CCC `trend_years` ago.
  - `altman_z2`.
  - `promoter_change_qoq_pp` and `institutional_change_qoq_pp` (MF+FII+DII).
  - `other_income_share`: other income / PBT.
  - `delivery_ratio`.
  - `trend_score` (up/range/down) and `rpt_score` (clean/flagged).
- Banks: Quality = ROA and NIM; Health = GNPA and CAR (`bank_maps`).
- Valuation pillar = (FV − CMP)/FV and the reverse-DCF gap (implied − historical growth).

Total and grade
- Pillar weights are renormalised over the pillars that have a score. That coverage must be at least `total_min_weight_coverage`, or there is no total, no grade and no action.
- Knock-out caps only lower a grade.

Provisional grade
1. The total over the other five pillars (weights renormalised), capped by knock-outs, gives the provisional grade.
2. The provisional grade picks the MoS for the valuation. That gives FV and the zone, and so the Valuation pillar.
3. The final grade uses all six pillars, capped by knock-outs.
- The MoS stays the provisional grade's. There is no second pass; when the final grade differs from the provisional one, a reason says so.

Earned premium
- Each condition is met, not met, or unknown. Unknown conditions earn no point and are listed, so `max_possible` = score + unknowns.
- Operating leverage needs sales growth above `operating_leverage_min_sales_growth`.
- "No new pledge" means pledge % is not above the previous quarter's.

Decision rules
- Cell rules:
  - A-grade Discount: Buy when CMP is inside the technical buy zone, otherwise Accumulate.
  - A-grade Fair: Buy on Pullback, with the buy zone as the target.
  - A-grade Premium: Momentum Entry if EP ≥ `momentum_entry_min`, otherwise Wait.
  - A-grade Extreme Premium: Hold.
  - B Deep Discount: Buy when the stage or trend is in `decision.confirmation`, otherwise Wait.
  - C Deep Discount: Wait with the value-trap checklist.
  - C Discount: Wait (watch).
- Checklist text is in `decision.checklists`.
- The Stage 4 override turns any buying action (Strong Buy, Buy, Accumulate, Buy on Pullback, Momentum Entry) into Wait. Checklists are kept.
- Missing inputs: no grade gives no action. No zone gives an action only when the grade's whole row is one rule (D → Avoid).
- Every result carries `reasons`.

---

## 8. API (FastAPI)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/stocks/search?q=` | Symbol search |
| GET | `/api/stocks/{symbol}/report` | Full StockReport DTO |
| POST | `/api/stocks/{symbol}/refresh` | Enqueue a data refresh |
| GET | `/api/stocks/{symbol}/valuation/sensitivity` | DCF grid |
| POST | `/api/stocks/{symbol}/overrides` | User assumptions (g1, margins, WACC, sector model) |
| GET | `/api/screener` | Filters: grade, zone, sector, distance_to_buy_zone, EP score, mcap |
| GET/POST/DELETE | `/api/watchlist` | |
| GET/POST/DELETE | `/api/alerts` | Price enters buy zone / crosses FV / Top band |
| POST | `/api/uploads/screener` | Upload a Screener Excel export (optional; fills fields the XBRL filings lack) |
| POST | `/api/uploads/xbrl` | Upload results XBRL documents (NSE/BSE) for one symbol; per-file outcome |
| GET/POST | `/api/filings`, `/api/filings/summary`; POST `/api/filings/{id}/retry` | Results filings ledger |
| GET | `/api/brokers/status` | Token validity per broker |
| GET | `/api/brokers/{fyers|kite}/login`, `/callback` | OAuth |
| GET | `/api/config` / PUT | View or edit YAML-backed config (validated) |
| POST | `/api/backtests` / GET `/api/backtests/{id}` | Run a backtest or fetch results |
| GET | `/api/jobs` | Job run history, data freshness |

### StockReport DTO (abridged)
```json
{
  "symbol": "XYZ", "cmp": 0, "as_of": "date", "sources": {"prices": "fyers", "fundamentals": "screener"},
  "levels": {"baseline": 0, "fair_value": 0, "top_band": 0, "mos_pct": 0, "confidence": "high|medium|low"},
  "zone": "discount", "buy_zone": {"low": 0, "high": 0, "basis": ["demand zone", "AVWAP 52wL"]},
  "invalidation": 0, "rr_to_fv": 0, "rr_to_top": 0,
  "valuation": {"methods": [{"name": "dcf_base", "value": 0, "weight": 0}], "reverse_dcf": {"implied_growth": 0, "hist_growth": 0}},
  "scores": {"quality": 0, "growth": 0, "valuation": 0, "health": 0, "governance": 0, "technical": 0, "total": 0},
  "grade": "A", "earned_premium": 0, "action": "buy_on_pullback",
  "reasons": ["..."], "red_flags": ["..."], "data_gaps": ["..."], "thesis": "optional LLM text"
}
```


Implementation notes (`reports/`, `api/`)

Report pipeline
- `reports/data.py` is the only step that reads the database. It loads adjusted prices, statements, shareholding, delivery, surveillance lists, the benchmark index, user overrides, and peers' latest `peer_stats`.
- Consolidated statements are used first. Standalone statements are used only when no consolidated ones exist, and the report then carries a red flag (rule 5).
- `reports/build.py` is pure and runs the steps in this order:
  1. fundamentals
  2. technicals
  3. the five non-valuation pillars and knock-outs, then the provisional grade (§7.3)
  4. valuation with that grade's MoS, then the Valuation pillar and the final grade
  5. earned premium
  6. buy zone, using the final grade for the A-range check and the provisional MoS
  7. decision
- `reports/service.py` persists one date's results: `valuation_snapshots` (including the sensitivity grid), the weekly `technical_snapshots` (now with buy zone and invalidation), `scores`, `reports` and `data_gaps`. The report date is the last price date.

Valuation wiring (§5)
- The PE band uses TTM EPS from four consecutive quarters, or annual EPS if there are not four. EV/EBITDA and P/B use annual figures.
- Band denominators are keyed by announcement date. When that date is unknown (Screener uploads), the live report assumes `bands.assumed_announcement_lag_days` (quarterly 45 / annual 60) and records a gap. Backtests must not use this assumption.
- Each band uses the first lookback in `bands.lookback_years` with enough observations. Its method value is the band median as a price. The primary band is the sector's highest-weighted band method.
- Market cap = CMP × the latest diluted share count on file (annual or quarterly, whichever period is later; a balance-sheet-only year row has none), shown with its period. No count is a `market_cap` data gap. The cost of equity uses the Blume beta against `jobs.universe_index`.
- DCF, reverse DCF and EPV run only for the FCFF and cyclical models (rule 10).
- Relative valuation needs `relative.min_peers` sector peers with a positive multiple. Peer figures come from this run (`valuation_scores` job, two passes) or from peers' latest stored reports (on-demand builds).
- Institutional holding = FII + DII, because DII already includes mutual funds.
- The liquidity knock-out is the mean traded value over `knockouts.traded_value_days`. With fewer sessions on file, the check is unknown.
- The technicals job's RS percentile is used only if it is at most `rs_percentile_max_age_days` old.

Overrides and manual inputs
- `POST /overrides` accepts DCF assumptions, which replace the computed base inputs, and `sector`, which selects the model.
- It also accepts manual inputs the sources cannot supply: NAV, embedded value, VNB, P/EV multiples, SOTP holdings, the related-party-transaction flag and auditor resignations.
- Fields sent with a value are saved. Fields sent as `null` are cleared. Fields left out are unchanged.

Auth
- There is one password (`APP_PASSWORD`). A successful login gets a signed, expiring token as an HttpOnly `SameSite=Lax` cookie, and as a Bearer token for API clients.
- The signing key is derived from `FERNET_KEY` and the password, so changing the password logs out every session.
- Failed logins per IP are counted in Redis. After `LOGIN_MAX_FAILURES` failures the IP is locked out (429).
- Public routes: `/api/health`, `/api/auth/login|logout` and the broker OAuth callbacks. The callbacks are protected by the signed `state`.

Refresh, config and backtests
- `POST /refresh` queues the symbol in Redis (deduplicated). The worker's `refresh_queue` job, run every minute, re-runs corporate actions, EOD prices, results and shareholding for it (ignoring seasons), then rebuilds the report.
- `PUT /api/config` validates the new YAML together with the other files, exactly as at startup, before an atomic write. The API applies it at once; the worker needs a restart.
- `POST /api/backtests` stores the request as `queued`; the worker's `backtests` job runs it (see §11). `GET /api/backtests` lists runs with progress and headline CAGR.

Implementation notes (alerts, P14):
- **Schedule:** `alerts_intraday` runs every 5 minutes (jobs.yaml). It does nothing outside `jobs.alerts.market_open`–`market_close` IST on weekdays, unless run with `--force`. NSE holidays are not modelled; on a holiday the price does not move, so nothing fires.
- **Levels:** taken from each stock's latest report: the technical buy zone (only when its status is `zone`), FV, top band and invalidation. An alert whose level is missing never fires, and says why.
- **Prices:** `DataRouter.ltp_filled` asks the providers in `priority.ltp` order (Fyers, then Kite). Each later provider is asked only for the symbols still unpriced, and symbols nobody priced are one data gap.
- **When an alert fires:** on a transition, using the state stored in `alerts.state`.
  - Enters buy zone: price in [low, high] after not being inside. The first observation counts.
  - Crossings: price on the other side of the level than at the previous observation, in either direction; the first observation only records the side.
  - Within `hysteresis_pct` of a level or a zone edge, the previous state is kept.
  - `cooldown_minutes` suppresses repeats; the transition is still recorded, so it is not replayed later.
- **Delivery:** each firing writes a `notifications` row (the in-app bell) and, when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, sends a Telegram message. A Telegram failure is stored on the row and never loses the in-app notification, and the bot token never reaches logs or stored errors.
- **Scope:** alerts are notifications only. No orders or broker GTTs (rule 7).

### 8a. LLM thesis (P26)
An optional paragraph explaining the report, written by a local model from the report's own numbers. It adds no facts and never changes a score, zone, grade or action.

- **Off by default.** It needs `jobs.yaml` → `thesis.enabled: true` and `THESIS_LLM_URL`: a local Ollama server (`/api/generate`, non-streaming, `temperature` and `seed` fixed). The URL must be loopback, a private address, a Docker service name, `host.docker.internal`, `*.local` or the tailnet, so the numbers never leave the owner's network and no paid API is used. `THESIS_LLM_URL=fake` (development only) is a built-in deterministic stand-in for tests and demos.
- **Fact sheet** (`reports/thesis.py`, pure). Labelled lines with numbers already in display units:
  - company, sector and model, report date, CMP, Baseline / FV / Top band, confidence, MoS;
  - zone, grade, action, buy zone and invalidation;
  - pillar and total scores, earned premium, valuation methods, reverse-DCF implied vs historical growth;
  - technical stage, trend, RS percentile, distance from the 52-week high;
  - the fundamentals listed in `thesis.fundamentals`, each with its label and unit;
  - red flags, knock-outs, open source differences, and the first `max_reasons` decision / report reasons.
  Missing values are left out, never written as 0. The prompt asks for one paragraph of `min_words`–`max_words` words using only these facts, copying numbers as written, with no predictions or targets.
- **Check** (pure, before anything is kept):
  - every number in the draft matches a number in the fact sheet, within `number_rel_tolerance` or the rounding of the number as written (₹2,452 cites ₹2,452.36). Thousands separators (Western or Indian) are ignored, and so are signs. Dates must be ones the facts state.
  - no `forbidden_phrases` (case-insensitive);
  - no grade, multi-word action or multi-word zone that the facts don't name;
  - length within bounds.
  A failing draft is retried with the problems listed, up to `max_attempts`; then the outcome is `rejected` and no text is shown. Numbers written as words and sign errors are not caught, so the UI labels the text as machine-written and unchecked in wording.
- **Storage:** `report_theses`, keyed by (instrument, digest of the fact sheet + `PROMPT_VERSION`), with status `ok` / `rejected` / `failed`, the model, attempts, problems and the facts used. A thesis is shown (`GET /stocks/{s}/report` → `thesis`, `GET /stocks/{s}/thesis`) only when its digest matches the latest report's, so text never sits next to numbers it was not written from. A digest that already passed is reused without calling the model.
- **When:** `POST /api/stocks/{s}/thesis` (the stock page's "Write thesis" button; `force` rewrites), and the nightly `thesis` job for watchlist stocks (`nightly_scope`, `max_per_run`) after `valuation_scores`. `make doctor` checks that the model server answers and has `thesis.model` pulled.

---

## 9. Frontend pages
1. **Dashboard:** broker connection status, data freshness, top A-grade stocks in the buy zone, triggered alerts.
2. **Screener:** a sortable table (symbol, sector, grade, zone, CMP, FV, % to buy zone, EP score, RS). Filters are saved as presets.
3. **Stock report**, the core page:
   - Header: CMP, grade badge, action badge, data sources.
   - **Zone gauge:** a horizontal bar marking Baseline, Buy zone, FV and Top band, with a CMP marker.
   - **Chart:** lightweight-charts weekly candles with overlays for demand/supply zones, AVWAPs, 30-wk SMA, POC, and valuation-level lines (Baseline, FV, Top band). Includes a daily/weekly toggle.
   - **Valuation panel:** a method table, a DCF sensitivity heatmap, reverse-DCF readout, and editable assumptions that save as an override and recompute live.
   - **Scorecards:** a six-pillar radar chart plus expandable sub-metrics, each with its reason.
   - **Fundamentals:** 10-yr charts for sales, EBITDA, PAT, CFO, FCF, ROCE and CCC, plus a shareholding trend (promoter, FII, DII, public, pledge per quarter) captioned with its source, quarter and filing date. The report also carries `shareholding` (latest pattern: source, quarter, filing date, holdings, promoter change and previous pledge); with no pattern on file it lists a `shareholding` data gap.
   - **Red flags and data gaps.**
4. **Watchlist & alerts.**
5. **Backtest:** choose rules (grade set × zone set × holding period) and see the equity curve against Nifty 500, CAGR, max drawdown and hit rate.
6. **Settings:** connect brokers, edit config (with a YAML validator), manage uploads.

Auth: a single user with a password login (NextAuth credentials or FastAPI session) and HTTPS in deployment.

---

## 10. Jobs (worker, IST)
| Job | Schedule | Work |
|---|---|---|
| `eod_prices` | Weekdays 18:15 | OHLCV for the universe + indices via the router |
| `nse_bhavcopy` | Weekdays 18:45 | Delivery %, surveillance lists, F&O ban |
| `technicals` | Weekdays 19:15 | Recompute technical snapshots |
| `valuation_scores` | Weekdays 19:45 | Recompute valuations, scores and reports for the universe |
| `alerts_intraday` | Every 5 min, 09:15–15:30 | LTP via Fyers/Kite and evaluate alerts |
| `shareholding` | Daily 20:30 during filing season | New filings |
| `index_constituents` | 1st of the month | Nifty 500 + sector index membership |
| `symbol_master` | Daily 07:00 | Rebuild the ISIN-joined symbol master + aliases |
| `results_watch` | Every 15 min, 07:00–23:00 (implemented: 07:00–22:45) | New results filings → per-stock pipeline → change notifications (§3.8) |
| `events` | Every 30 min, 07:00–23:00 (implemented: :07 and :37) | Announcements, pledge/SAST, insider trades, bulk/block deals, ratings |
| `reconcile` | After each pipeline (a pipeline step) + nightly 23:00 for stocks with new filings | Cross-source checks (§3.9) |
| `broker_token_check` | 08:45 weekdays | In-app + Telegram reminder if an enabled broker's token is expired (§3.3) |
| `bhavcopy_history` | Nightly 01:30 (implementation addition) | Backfill the NSE bhavcopy OHLCV history, the price fallback after the brokers (§3.2) |
| `backup` | Daily 02:00 (`BACKUP_AT`; at start-up if the last is over a day old) | pg_dump to `BACKUP_PATH` (home: another drive) |
| `catch_up` | On worker start | Run jobs missed while the machine was off: once each, in due order, within `catch_up.lookback_hours` (§3.10 notes) |

Every job writes to `job_runs`, uses a Redis lock so it doesn't run twice, and is idempotent (upserts).

---

## 11. Backtest rules
- Monthly rebalance. The universe is point-in-time Nifty 500 membership (include delisted stocks where data exists).
- Fundamentals are used only after their announcement date. Prices are adjusted.
- Costs: 0.1% per side plus STT. Compare against Nifty 500 TRI where available.
- Report CAGR, max drawdown, hit rate and average holding period, broken down by grade × zone cell.


Implementation notes (P15, `backend/app/backtest/`, parameters in `jobs.backtest`):
- **Queue:** `POST /api/backtests` stores the rules (grades, zones, holding period in trading sessions, start, end, optional symbols). The worker's `backtests` job (every minute) claims queued rows with `FOR UPDATE SKIP LOCKED`, writes `{progress: {done, total}}` as it goes, then stores the results (`done`) or the error (`failed`).
- **Rebalance dates:** the first trading session of each month in [start, end], taken from the stored benchmark sessions.
- **Universe:** stocks whose `index_membership` interval for `jobs.universe_index` covers the date. Delisted and inactive instruments are included when they have prices.
  - A symbol list replaces this and is flagged as survivorship-biased.
  - If membership history starts after `start`, the earlier months have no universe, and a caveat says so. An empty membership table gives an explicit caveat, not a silent 0%.
- **Point in time, at each rebalance date d:**
  - Prices, benchmark and delivery data up to d.
  - Annual and quarterly statements with `announcement_date ≤ d`. Rows without an announcement date are excluded and counted in a caveat; the live report's assumed lag (§8 notes) is never used.
  - Shareholding with `filing_date ≤ d`.
  - ASM/GSM rows effective at d, or "unknown" when no surveillance history is stored.
  - RS percentile: Mansfield RS on weekly closes to d, ranked across that month's universe.
  - Peers for relative valuation come from the previous rebalance's reports, to avoid a second pass.
  - Sector is today's classification; user overrides are not applied. Both are listed as caveats.
- **Signals:** each stock's report is built by the live pipeline (`build_report(..., lite=True)`, which skips only the DCF sensitivity grid). Its final grade and zone put it in one of 25 cells.
- **Execution:**
  - A signal on d is bought at the close `execution_lag_days` sessions later (default 1), so there is no same-close look-ahead.
  - It is held for `holding_days` sessions and sold at that close.
  - A stock already held is not bought again.
  - Each new position gets `min(NAV / (open + new positions), cash / new positions)`. Existing positions are never re-weighted. Uninvested cash earns nothing.
  - If a stock's prices end (delisting), it is sold at its last close (`data_end`). Positions open at the end are marked to the last close (`backtest_end`).
- **Costs:** buy `cost_per_side + stt_buy`, sell `cost_per_side + stt_sell` (defaults 0.1% + 0.1% each way). Net trade return = `exit·(1−sell)·(1−buy)/entry − 1`.
- **Metrics:**
  - CAGR = `(end/start)^(365.25/days) − 1` on the daily NAV.
  - Max drawdown = the minimum of `NAV/running max − 1`.
  - Hit rate = the share of closed trades with net return > 0.
  - Average holding period is in sessions. Exposure = the average invested fraction.
- **Benchmark:** buy-and-hold of `benchmark_tri` when its prices are stored, else the `benchmark` price index (labelled as such), rebased to 100 on the same sessions.
- **Grade × zone table:** every cell is simulated as its own portfolio of that cell's signals, under the same rules and costs. Cells in the chosen rules are marked. The chosen-rules portfolio is the union of those cells.
- **Equity curve:** `equity_curve_points` (default weekly): each week's last real session plus the first point, so both curves start at 100.

---

## 12. Compliance & safety
- The app is a personal research tool. Grades and targets are *not* published or distributed.
- Showing specific buy/sell calls or target prices publicly (including on YouTube) may fall under SEBI Research Analyst regulations and the finfluencer rules. Get registration and legal advice before doing so.
- The MVP is read-only on brokers. Any future alert or GTT creation requires explicit per-action confirmation.

---

## 13. Golden test set
Pick 10 stocks you know well, one per model type: a large private bank, an NBFC, an insurer, a large IT company, an FMCG company, a metal cyclical, a cement company, a capital-goods compounder, a holding company and a mid-cap growth stock. For each, hand-verify these from annual reports and Screener, and store them in `tests/fixtures/golden/<symbol>.json`:
- 5-yr ROCE, CFO/EBITDA, CCC, D/E
- One DCF run with fixed inputs
- PE band median and σ

Every valuation or scoring change must keep these tests green.

### 13.1 v1 acceptance (end to end)
v1 is done when `frontend/e2e/acceptance.spec.ts` passes against live data (`make acceptance`): for each of five golden stocks (default HDFC Bank, TCS, Hindustan Unilever, UltraTech Cement, Bajaj Finance) it types the company name in the header search, opens the stock, watches the on-demand pipeline (§3.7) complete, and checks the report shows Baseline / FV / Top band, the zone, grade and action, a coverage grid with ≥10 fiscal years of P&L, and the data-sources panel (where prices, fundamentals and shareholding came from).

The same test runs without the network on the **offline exchange** (`app/devtools/offline_exchange.py`, `make acceptance-offline`): five synthetic companies whose quarterly results XBRL (FY2014 onwards, SEBI Ind AS and banking layouts), prices and shareholding are generated deterministically. With `OFFLINE_EXCHANGE=1` (refused unless `APP_ENV=development`) a worker routes every dataset to it (provider `offline`, no rate limit, no other provider), and everything stored from it carries `source=offline` (results ledger `exchange=offline`, line items `offline_xbrl`), never a real provider's name. It checks the wiring, not the real data; it does not replace the live run.
