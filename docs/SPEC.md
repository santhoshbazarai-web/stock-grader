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

Implementation notes (`valuation/`, pure functions; parameters in `valuation.yaml` and `sectors.yaml`):
- DCF projection is revenue-driven: revenue grows at g_t, FCFF_t = revenue_t × EBIT margin × (1 − t) + revenue_t × (D&A% − capex%) − NWC% × Δrevenue. The base margin, D&A% and capex% are averages over `dcf.margin_years` (every year required); NWC% is from the latest year. A scenario's `margin_delta` shifts the EBIT margin. Cash flows are discounted at year end.
- Where filings omit working capital, minority interest or non-operating investments, the DCF takes them as nil so a value is possible. Each one is returned in `assumed_nil` for the caller to record as a data gap and show in the report.
- WACC uses market-value weights with book debt as the proxy for debt's market value. If there is debt but no interest cost, the cost of debt is unknown and there is no WACC.
- Beta = weekly-return slope over `beta.lookback_years`, Blume-adjusted (0.67β + 0.33), clamped to [floor, cap]. It needs at least half the expected weeks.
- Sector g1 cap: `sectors.<name>.g1_cap`, else `dcf.g1_cap_by_default`.
- Reverse DCF searches `dcf.reverse_growth_bracket` with brentq. If the price lies outside the values at the ends, there is no implied growth.
- Bands use the median and sample σ of daily multiples whose denominator is positive, over the lookback. Fundamentals are carried forward from their announcement date. A band needs `bands.min_observations` valid days. EV/EBITDA per share = price + net debt per share.
- Relative valuation uses ROCE as quality for PE and EV/EBITDA, and ROE for P/B and P/EV. It needs positive quality and growth on both sides.
- EPV: normalised EBIT = mean EBIT margin over `epv.normalise_years` × latest revenue. Graham multiplier: `graham_multiplier`.
- Banks: justified P/B uses `sectors.<bank>.long_run_growth` (required for the bank model). The residual-income model grows book value by ROE × (1 − payout).
- Blend: methods without a value are dropped and the remaining weights are renormalised (reported). If the available methods carry less than `blend.min_weight_coverage` of the weight, there is no fair value. The baseline book floor is `blend.asset_heavy_book_multiple`. The top band is capped at band +`zones.top_band_cap_sigma`σ. The primary band is the sector's highest-weighted band method.
- Confidence: CV (population σ / mean of the method values used) above `confidence.low_if_method_cv_above` → low; above `medium_if_method_cv_above` → medium; otherwise high. Fewer than two methods → low. Zone boundaries: FV(1 − MoS) is Fair; FV × `fair_upper_mult` is Fair; the top band itself is Premium.
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

Implementation notes (`scoring/`, pure functions; every threshold, map and the matrix itself live in `scoring.yaml`):

Knock-outs
- A check with missing input is reported as *unknown* (a data gap). It never passes or fails silently.
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
