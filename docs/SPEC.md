SPEC — Stock Grader Web App (Indian Equities)
Version 0.1 · Owner: Santhosh · Single-user personal research tool (see §12 Compliance)
---
1. Goals
For any NSE stock: Baseline, Fair value, Top band, Zone, Buy zone, Grade, Action — with reasons.
Screener across Nifty 500 ranked by Grade × Zone.
Watchlist + alerts when a stock enters its buy zone.
Backtest: do A-grade + Discount entries beat Nifty 500?
Non-goals (MVP): auto-trading, multi-user SaaS, intraday signals, F&O analytics.
---
2. Architecture
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
2.1 Services (docker-compose)
Service	Purpose
`db`	Postgres 16
`redis`	cache + rate limiter
`api`	FastAPI, uvicorn
`worker`	scheduled jobs + on-demand refresh queue
`web`	Next.js
---
3. Data layer
3.1 Provider abstraction
```python
class PriceProvider(Protocol):
    def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...
    def ltp(self, symbols: list[str]) -> dict[str, float]: ...

class FundamentalsProvider(Protocol):
    def annual(self, symbol: str) -> pd.DataFrame: ...     # rows = fiscal years
    def quarterly(self, symbol: str) -> pd.DataFrame: ...
```
`data/router.py` resolves each dataset using the priority list in `config/providers.yaml`, falling through on error/empty/stale data, and records which provider served it (`source` column on every table).
3.2 Dataset → source priority
Dataset	Primary	Fallback 1	Fallback 2	Notes
Daily OHLCV (stocks)	Fyers	Kite	yfinance `.NS`	≥ 10 yrs; Kite historical may need a paid add-on — verify
Daily OHLCV (indices: Nifty 500, sectoral)	Fyers	Kite	yfinance (`^CRSLDX`, etc.)	For RS and beta
LTP / quotes (alerts)	Fyers	Kite	—	Market hours only
Delivery %	NSE `sec_bhavdata_full` daily CSV	—	—	Daily after 18:00 IST
Corporate actions (split/bonus/dividend)	NSE	yfinance actions	—	Needed for adjustment
Index constituents (Nifty 500, sectors)	niftyindices.com CSV	—	—	Monthly refresh
ASM / GSM / F&O ban lists	NSE	—	—	Daily; knock-out filter
Shareholding (promoter, pledge, FII, DII, MF)	NSE/BSE filings	Screener export	—	Quarterly
Annual financials (10 yrs)	Screener Excel upload	yfinance (≈4 yrs only)	NSE/BSE XBRL (Phase 9)	yfinance alone is not enough for 10-yr metrics
Quarterly results	Screener export	yfinance	NSE results XBRL	Keyed by announcement date
Risk-free rate (10-yr G-sec)	config value, updated weekly	RBI/CCIL	—	
Sector classification	NSE industry	config override	—	Drives sector model
NSE endpoints need a browser-like session (cookies from the homepage + headers) and polite rate limiting. Use archive CSVs where possible and cache aggressively. Respect each source's terms of use. The Screener import is for personal use only.
3.3 Broker authentication
Fyers: OAuth flow. `GET /api/brokers/fyers/login` redirects to Fyers, which calls back `GET /api/brokers/fyers/callback?auth_code=…`. The backend exchanges the code for an access token, encrypts it and stores it with its expiry. Tokens expire daily, so the UI shows a "Reconnect" banner when a token is stale.
Kite: `GET /api/brokers/kite/login` redirects to Kite, which calls back with `request_token`. The backend calls `generate_session`, then encrypts and stores the token. It also expires daily.
Register the callback URLs in each broker's developer console. Keep API key and secret in `.env` only.
If no broker token is valid, the price router falls through to yfinance automatically, and the UI shows the data source.
3.4 Database tables (core)
`instruments`, `prices_daily` (adjusted + raw), `corporate_actions`, `delivery_daily`, `fin_annual`, `fin_quarterly`, `shareholding`, `index_membership`, `surveillance_flags`, `valuation_snapshots`, `technical_snapshots`, `scores`, `reports`, `watchlist`, `alerts`, `broker_tokens` (encrypted), `job_runs`, `data_gaps`, `user_overrides` (per-stock assumption overrides, e.g. custom growth rate).
---
4. Fundamental metrics (`fundamentals/metrics.py`)
Compute these over 10 years annually, plus TTM:
Metric	Formula
ROCE	EBIT / (Total assets − Current liabilities), average of opening and closing
ROE	PAT / average equity
ROIC	NOPAT / (Equity + Debt − Cash − Non-op investments)
Sales / EBITDA / EPS CAGR	3, 5, 10 yr
OPM	EBITDA / Sales
CFO/EBITDA, CFO/PAT	Annual and 5-yr cumulative
FCF	CFO − Capex (Capex = Purchase of fixed assets − Sale of fixed assets)
FCF conversion	Σ5yr FCF / Σ5yr PAT
Other income share	Other income / PBT
D/E, Net debt / EBITDA, ICR	ICR = EBIT / Interest
Debtor days	Receivables / Sales × 365
Inventory days	Inventory / COGS × 365
Payable days	Payables / COGS × 365
CCC	Debtor days + Inventory days − Payable days
Capex intensity	Capex / CFO
Dilution	Share-count CAGR over 5 yr
Accruals ratio	(PAT − CFO) / Average total assets
Forensic scores (`forensic.py`):
Piotroski F-score: the standard 9 tests.
Beneish M-score: 8-variable model. A reading above −2.22 is a flag.
Altman Z″: the emerging-markets version. Skip for financials.
Banks/NBFCs (`banking.py`): NIM, CASA ratio, GNPA, NNPA, PCR, credit cost, CAR/CRAR, cost-to-income, RoA, RoE, loan growth.

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
5. Valuation (`valuation/`)
5.1 FCFF DCF (`dcf.py`)
FCFF = EBIT·(1−t) + D&A − Capex − ΔNWC
Stages:
Stage 1: years 1–5, growth = g1.
Stage 2: years 6–10, growth fades linearly from g1 to g_T.
Terminal value: FCFF₁₁ / (WACC − g_T).
WACC:
Ke = Rf + β·ERP (+ size premium from config).
Kd = Interest / Average debt × (1 − t).
Weights are at market value.
β: 2-yr weekly regression against Nifty 500, Blume-adjusted (0.67·β + 0.33).
g1 default = min(5-yr revenue CAGR, sector cap), overridable by the user.
Output: per-share value = (EV − Net debt − Minority interest + Non-op investments) / diluted shares.
Sensitivity grid: WACC ±2% in 0.5% steps × g_T from 4–7%.
Scenarios: bear, base and bull by shifting g1, margins and WACC (deltas in `valuation.yaml`).
5.2 Reverse DCF (`reverse_dcf.py`)
Solve for the implied g1 such that DCF value = CMP, holding WACC and g_T at base. Use `scipy.optimize.brentq`. Output:
`implied_growth`
`gap = implied_growth − historical_5y_growth`
5.3 Own-history bands (`bands.py`)
For PE, EV/EBITDA and P/B, build a daily TTM multiple series over 5 and 10 years. Report the median, ±1σ and ±2σ, and convert each back to a price using current TTM earnings, EBITDA or book value. Exclude periods where earnings are ≤ 0.
5.4 Relative (`relative.py`)
Use the sector median multiple, adjusted for ROCE and growth relative to peers:
`adj_multiple = peer_median × (ROCE/peer_ROCE)^a × (growth/peer_growth)^b`
`a` and `b` come from config.
5.5 EPV & Graham (`epv.py`)
EPV = normalised EBIT·(1−t) / WACC, adjusted for net cash.
Graham number = √(22.5 × EPS × BVPS).
5.6 Sector models (`sector_models.py`)
Driven by `config/sectors.yaml`:
Banks/NBFC: justified P/B = (ROE − g)/(Ke − g) × BVPS; residual income model.
Insurance: P/EV band; EV plus a VNB multiple (manual EV input allowed).
Cyclicals: normalised mid-cycle EBITDA (7–10 yr median margin × current sales) × EV/EBITDA band median.
Real estate: NAV (manual input) × discount.
Holding companies: SOTP of listed holdings at market value × (1 − holding discount) + standalone business value.
5.7 Blend (`blend.py`)
Fair value = Σ(weight × method value), with weights per sector from `sectors.yaml`.
Baseline = min(bear DCF, EPV, band −1σ price), then take the max of that and (0.8 × book value) for asset-heavy sectors only.
Top band = max(bull DCF, band +1σ price), capped at band +2σ.
MoS by provisional grade: A 15%, B 27.5%, C 40% (from config).
Zones:
Deep Discount: CMP < Baseline
Discount: Baseline ≤ CMP < FV·(1−MoS)
Fair: FV·(1−MoS) ≤ CMP ≤ FV·1.10
Premium: FV·1.10 < CMP ≤ Top band
Extreme Premium: CMP > Top band
Report dispersion across methods. If the coefficient of variation exceeds 35%, raise a "low valuation confidence" flag.
---
6. Technical engine (`technical/`)
All calculations run on adjusted prices. The primary timeframe is weekly, with daily used for entry refinement.
Module	Output
`stage.py`	Weinstein stage 1–4 from the 30-week SMA slope + price position + volume
`structure.py`	Swing points (fractal, N configurable), HH/HL/LH/LL labels, BOS, CHoCH, trend state
`zones.py`	Demand/supply zones: base candles before an impulsive move (range > k·ATR). Zone = base high/low. Freshness = count of retests. Order blocks and FVGs. Dealing-range equilibrium (50%) and OTE (0.618–0.79)
`avwap.py`	Anchored VWAP from the 52-wk low, the last results date and the last major swing low
`volume_profile.py`	POC, VAH, VAL over the last 1 yr (weekly bins)
`rs.py`	Mansfield RS vs Nifty 500 and vs sector index; RS percentile across the universe
`momentum.py`	Weekly RSI(14), distance from 200-DMA z-score, 52-wk high proximity
`participation.py`	Delivery % vs its own 50-day average; up-week vs down-week volume ratio; VCP detector (contracting pullbacks)
`risk.py`	ATR(14) weekly, invalidation = below demand-zone low − 0.5·ATR, R:R to FV and to the Top band
Buy zone = intersection of `[Baseline, FV·(1−MoS)]` (or `[FV·(1−MoS), FV]` for A-grade stocks) with the nearest fresh demand zone, AVWAP or POC below CMP. If they don't intersect, the buy zone is the nearest technical support within the valuation range. If there is none, the output is "No technical buy zone yet". Stage 4 means the buy zone is suppressed.
---
7. Scoring (`scoring/`)
7.1 Knock-outs (`knockouts.py`)
A stock is capped at grade C if any of these apply:
Promoter pledge > 10% (config)
CFO negative in ≥ 3 of the last 5 years
An auditor resignation in the last 2 years (manual flag or filing parse)
On the ASM/GSM list
Market cap < ₹500 Cr
20-day average traded value < ₹5 Cr
Beneish M > −1.78
7.2 Pillars (0–100 each; sub-metrics scored by piecewise-linear maps in `scoring.yaml`)
Pillar	Weight	Sub-metrics
Quality	25	5-yr ROCE avg, ROCE trend, CFO/EBITDA, FCF conversion, Piotroski
Growth	20	5-yr sales & EPS CAGR, last 4Q YoY EPS growth, acceleration
Valuation	20	Zone position: (FV − CMP)/FV mapped to score; reverse-DCF gap
Financial health	15	D/E, ICR, Net debt/EBITDA, CCC trend, Altman Z″
Governance & ownership	10	Pledge, promoter trend, MF/FII/DII QoQ change, other-income share, RPT flag
Technical	10	Stage, RS percentile, structure trend, delivery trend
Banks use a bank-specific Quality and Health map (asset quality, NIM, CAR).
7.3 Grade
A+ ≥ 85, A ≥ 75, B ≥ 60, C ≥ 45, D < 45, then apply knock-out caps. Note the circular dependency: the grade decides the MoS, which decides the zone, which feeds the valuation pillar. Resolve it by computing a provisional grade that excludes the Valuation pillar to pick the MoS, then compute the final grade.
7.4 Earned-premium score (`earned_premium.py`, 0–8)
One point for each condition met:
Reverse-DCF implied growth ≤ 5-yr historical growth
Last 2 quarters show YoY EPS growth accelerating
ROCE is up versus 3 years ago
OPM is up YoY with sales growth > 15% (operating leverage)
MF + FII + DII holding is up QoQ
Promoter holding is flat or up, with no new pledge
RS percentile ≥ 80
Stage 2 and within 10% of the 52-wk high
7.5 Decision matrix (`decision.py`)
Grade \ Zone	Deep Discount	Discount	Fair	Premium	Extreme Premium
A+/A	Strong Buy*	Buy / Accumulate	Buy on Pullback → buy zone	EP ≥ 6: Momentum Entry; else Wait	Hold if owned; don't initiate
B	Buy w/ confirmation	Accumulate slowly	Wait	Avoid	Book Profits
C	Value-trap check	Watch	Avoid	Avoid	Avoid
D	Avoid	Avoid	Avoid	Avoid	Avoid
*Deep Discount on an A-grade stock always attaches a "Why is it cheap?" checklist (news, governance, regulatory action, one-off losses).
Stage 4 overrides any Buy into Wait, with the reason "downtrend — wait for Stage 1 base".
---
8. API (FastAPI)
Method	Path	Purpose
GET	`/api/stocks/search?q=`	Symbol search
GET	`/api/stocks/{symbol}/report`	Full StockReport DTO
POST	`/api/stocks/{symbol}/refresh`	Enqueue a data refresh
GET	`/api/stocks/{symbol}/valuation/sensitivity`	DCF grid
POST	`/api/stocks/{symbol}/overrides`	User assumptions (g1, margins, WACC, sector model)
GET	`/api/screener`	Filters: grade, zone, sector, distance_to_buy_zone, EP score, mcap
GET/POST/DELETE	`/api/watchlist`	
GET/POST/DELETE	`/api/alerts`	Price enters buy zone / crosses FV / Top band
POST	`/api/uploads/screener`	Upload a Screener Excel export
GET	`/api/brokers/status`	Token validity per broker
GET	`/api/brokers/{fyers	kite}/login`, `/callback`
GET	`/api/config` / PUT	View or edit YAML-backed config (validated)
POST	`/api/backtests` / GET `/api/backtests/{id}`	Run a backtest or fetch results
GET	`/api/jobs`	Job run history, data freshness
StockReport DTO (abridged)
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
---
9. Frontend pages
Dashboard: broker connection status, data freshness, top A-grade stocks in the buy zone, triggered alerts.
Screener: a sortable table (symbol, sector, grade, zone, CMP, FV, % to buy zone, EP score, RS). Filters are saved as presets.
Stock report, the core page:
Header: CMP, grade badge, action badge, data sources.
Zone gauge: a horizontal bar marking Baseline, Buy zone, FV and Top band, with a CMP marker.
Chart: lightweight-charts weekly candles with overlays for demand/supply zones, AVWAPs, 30-wk SMA, POC, and valuation-level lines (Baseline, FV, Top band). Includes a daily/weekly toggle.
Valuation panel: a method table, a DCF sensitivity heatmap, reverse-DCF readout, and editable assumptions that save as an override and recompute live.
Scorecards: a six-pillar radar chart plus expandable sub-metrics, each with its reason.
Fundamentals: 10-yr charts for sales, EBITDA, PAT, CFO, FCF, ROCE and CCC, plus a shareholding trend.
Red flags and data gaps.
Watchlist & alerts.
Backtest: choose rules (grade set × zone set × holding period) and see the equity curve against Nifty 500, CAGR, max drawdown and hit rate.
Settings: connect brokers, edit config (with a YAML validator), manage uploads.
Auth: a single user with a password login (NextAuth credentials or FastAPI session) and HTTPS in deployment.
---
10. Jobs (worker, IST)
Job	Schedule	Work
`eod_prices`	Weekdays 18:15	OHLCV for the universe + indices via the router
`nse_bhavcopy`	Weekdays 18:45	Delivery %, surveillance lists, F&O ban
`technicals`	Weekdays 19:15	Recompute technical snapshots
`valuation_scores`	Weekdays 19:45	Recompute valuations, scores and reports for the universe
`alerts_intraday`	Every 5 min, 09:15–15:30	LTP via Fyers/Kite and evaluate alerts
`shareholding`	Daily 20:30 during filing season	New filings
`index_constituents`	1st of the month	Nifty 500 + sector index membership
`results_watch`	Daily in results season	Flag stocks with new quarterly results so fundamentals get refreshed (via Screener upload / yfinance)
Every job writes to `job_runs`, uses a Redis lock so it doesn't run twice, and is idempotent (upserts).
---
11. Backtest rules
Monthly rebalance. The universe is point-in-time Nifty 500 membership (include delisted stocks where data exists).
Fundamentals are used only after their announcement date. Prices are adjusted.
Costs: 0.1% per side plus STT. Compare against Nifty 500 TRI where available.
Report CAGR, max drawdown, hit rate and average holding period, broken down by grade × zone cell.
---
12. Compliance & safety
The app is a personal research tool. Grades and targets are not published or distributed.
Showing specific buy/sell calls or target prices publicly (including on YouTube) may fall under SEBI Research Analyst regulations and the finfluencer rules. Get registration and legal advice before doing so.
The MVP is read-only on brokers. Any future alert or GTT creation requires explicit per-action confirmation.
---
13. Golden test set
Pick 10 stocks you know well, one per model type: a large private bank, an NBFC, an insurer, a large IT company, an FMCG company, a metal cyclical, a cement company, a capital-goods compounder, a holding company and a mid-cap growth stock. For each, hand-verify these from annual reports and Screener, and store them in `tests/fixtures/golden/<symbol>.json`:
5-yr ROCE, CFO/EBITDA, CCC, D/E
One DCF run with fixed inputs
PE band median and σ
Every valuation or scoring change must keep these tests green.
