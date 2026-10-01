// Mirrors backend/app/reports/dto.py, app/reports/history.py, app/api/schemas.py and the
// technical debug payload (app/technical/engine.py::debug_payload). Keep in sync.

export type Grade = "A_plus" | "A" | "B" | "C" | "D";
export type ZoneName = "deep_discount" | "discount" | "fair" | "premium" | "extreme_premium";

export type Levels = {
  baseline: number | null;
  fair_value: number | null;
  top_band: number | null;
  mos_pct: number | null;
  confidence: "high" | "medium" | "low" | null;
  discount_edge: number | null;
  fair_upper: number | null;
};

export type BuyZone = {
  status: "zone" | "none" | "suppressed" | "unavailable";
  low: number | null;
  high: number | null;
  basis: string[];
  reasons: string[];
};

export type Method = {
  name: string;
  value: number | null;
  weight: number;
  effective_weight: number;
  reasons: string[];
};

export type ReverseDcf = {
  implied_growth: number | null;
  hist_growth: number | null;
  gap: number | null;
  reasons: string[];
};

export type DcfScenario = {
  scenario: "bear" | "base" | "bull";
  value_per_share: number | null;
  terminal_share: number | null;
  reasons: string[];
};

export type Valuation = {
  sector: string;
  model: string;
  methods: Method[];
  reverse_dcf: ReverseDcf | null;
  dcf: DcfScenario[];
  wacc: number | null;
  cost_of_equity: number | null;
  beta: number | null;
  market_cap_cr: number | null;
  extra_methods: Record<string, number | null>;
  dcf_inputs: Record<string, number> | null;
  reasons: string[];
};

export type Scores = Record<
  "quality" | "growth" | "valuation" | "health" | "governance" | "technical" | "total",
  number | null
>;

export type SubScore = {
  name: string;
  value: number | string | boolean | null;
  score: number | null;
  reason: string;
};

export type Pillar = {
  pillar: string;
  score: number | null;
  weight: number;
  subs: SubScore[];
  missing: string[];
  reasons: string[];
};

export type Condition = { code: string; met: boolean | null; reason: string };

export type StockReport = {
  symbol: string;
  name: string | null;
  cmp: number;
  as_of: string;
  sources: Record<string, string | null>;
  levels: Levels;
  zone: ZoneName | null;
  buy_zone: BuyZone | null;
  invalidation: number | null;
  rr_to_fv: number | null;
  rr_to_top: number | null;
  valuation: Valuation;
  scores: Scores;
  grade: Grade | null;
  grade_label: string | null;
  earned_premium: number | null;
  action: string | null;
  reasons: string[];
  red_flags: string[];
  data_gaps: string[];
  thesis: string | null;
  reconciliation_issues?: string[]; // SPEC v0.2 §3.9 (absent in reports built before P21)
  provisional_grade: Grade | null;
  mos_grade: Grade | null;
  pillars: Pillar[];
  knockouts: { cap: Grade | null; triggered: string[]; unknown: string[]; reasons: string[] };
  earned_premium_detail: { score: number; max_possible: number; conditions: Condition[] };
  decision: {
    action: string | null;
    rule: string | null;
    overridden_by_stage4: boolean;
    checklist: string[];
    reasons: string[];
  };
  technical: {
    as_of: string;
    stage: number | null;
    trend: string | null;
    rs_percentile: number | null;
    mansfield_rs: number | null;
    atr: number | null;
    rsi: number | null;
    from_52w_high: number | null;
    delivery_ratio: number | null;
    vcp: boolean;
    reasons: string[];
  };
  fundamentals: Record<string, number | null>;
  overrides: Partial<Overrides>;
};

export type Overrides = {
  g1: number | null;
  ebit_margin: number | null;
  wacc: number | null;
  g_terminal: number | null;
  tax_rate: number | null;
  da_pct: number | null;
  capex_pct: number | null;
  nwc_pct: number | null;
  sector: string | null;
  nav_per_share: number | null;
  embedded_value_per_share: number | null;
  vnb_per_share: number | null;
  vnb_multiple: number | null;
  p_ev_band_median: number | null;
  peer_p_ev: number | null;
  listed_holdings_value_cr: number | null;
  standalone_value_cr: number | null;
  rpt_flagged: boolean | null;
  auditor_resignations: string[] | null;
};

export type OverridesResponse = {
  symbol: string;
  overrides: Overrides;
  report: StockReport | null;
  reasons: string[];
};

export type Sensitivity = {
  symbol: string;
  as_of: string;
  waccs: number[];
  terminal_growths: number[];
  values: (number | null)[][];
  base_wacc: number;
  base_g_terminal: number;
};

export type YearPoint = {
  fiscal_year: number;
  revenue: number | null;
  ebitda: number | null;
  pat: number | null;
  cfo: number | null;
  fcf: number | null;
  roce: number | null;
  ccc_days: number | null;
};

export type ShareholdingPoint = {
  period_end: string;
  promoter_pct: number | null;
  fii_pct: number | null;
  dii_pct: number | null;
  public_pct: number | null;
  promoter_pledge_pct: number | null;
};

export type FundamentalsHistory = {
  symbol: string;
  statement_type: string | null;
  source: string | null;
  years: YearPoint[];
  shareholding: ShareholdingPoint[];
  missing: string[];
};

// ── technical debug payload (chart overlays) ──
export type Bar = { time: string; open: number; high: number; low: number; close: number; volume: number };
export type TimeValue = { time: string; value: number };

export type ChartZone = {
  side: "demand" | "supply";
  source: "base" | "order_block";
  bottom: number;
  top: number;
  start: string;
  impulse: string;
  retests: number;
  broken: boolean;
  broken_at: string | null;
  fresh: boolean;
};

export type ChartAvwap = { anchor: string; anchor_time: string; value: number | null; series: TimeValue[] };

export type TechnicalDebug = {
  symbol: string;
  timeframe: "weekly";
  bars: Bar[];
  daily_bars?: Bar[];
  sma_30w: TimeValue[];
  zones: ChartZone[];
  avwaps: ChartAvwap[];
  volume_profile: { poc: number; vah: number; val: number } | null;
  stage: { stage: number | null };
  buy_zone: { status: string; low: number | null; high: number | null } | null;
};

// GET /api/stocks/search (SPEC §3.5)
export type SearchMatch =
  | "symbol"
  | "name"
  | "isin"
  | "bse_code"
  | "fyers_symbol"
  | "former_symbol"
  | "former_name"
  | "bse_symbol"
  | "bse_name"
  | "user";

export type SearchHit = {
  symbol: string | null; // NSE symbol; null for a BSE-only company
  name: string | null;
  isin: string | null;
  bse_code: string | null;
  series: string | null;
  sector: string | null;
  industry: string | null;
  is_index: boolean;
  in_universe_index: boolean;
  active: boolean;
  match: SearchMatch;
  matched: string;
  exact: boolean;
  score: number;
};

// ── P13 pages (app/api/schemas.py, app/api/brokers.py) ──
export type BrokerStatus = {
  broker: "fyers" | "kite";
  enabled: boolean; // providers.yaml brokers.<name>.enabled (Kite is off by default)
  configured: boolean;
  connected: boolean;
  expires_at: string | null;
  reason: string;
};

export type JobRun = {
  id: number;
  job_name: string;
  status: "running" | "success" | "failed" | "skipped";
  started_at: string;
  finished_at: string | null;
  params: Record<string, unknown> | null;
  rows_written: number | null;
  details: Record<string, unknown> | null;
  error: string | null;
};

export type JobsView = {
  runs: JobRun[];
  freshness: {
    prices: string | null;
    delivery: string | null;
    fundamentals_fetched: string | null;
    shareholding_period: string | null;
    technicals: string | null;
    reports: string | null;
  };
  open_data_gaps: number;
  refresh_queue: string[];
};

export type ScreenerRow = {
  symbol: string;
  name: string | null;
  sector: string;
  as_of: string;
  cmp: number;
  grade: Grade | null;
  grade_label: string | null;
  zone: ZoneName | null;
  action: string | null;
  total_score: number | null;
  fair_value: number | null;
  buy_zone_low: number | null;
  buy_zone_high: number | null;
  pct_to_buy_zone: number | null;
  earned_premium: number | null;
  rs_percentile: number | null;
  market_cap_cr: number | null;
};

export type SortKey =
  | "symbol"
  | "total_score"
  | "pct_to_buy_zone"
  | "earned_premium"
  | "rs_percentile"
  | "market_cap_cr"
  | "cmp";

export type ScreenerFilters = {
  grade: Grade[];
  zone: ZoneName[];
  sector: string[];
  action: string[];
  min_earned_premium: number | null;
  max_distance_to_buy_zone: number | null;
  min_mcap_cr: number | null;
  max_mcap_cr: number | null;
  sort: SortKey;
  order: "asc" | "desc";
};

export type Preset = { name: string; filters: ScreenerFilters; updated_at: string };

export type WatchlistItem = {
  symbol: string;
  name: string | null;
  notes: string | null;
  added_at: string;
  grade: Grade | null;
  zone: ZoneName | null;
  action: string | null;
  cmp: number | null;
};

export type AlertType = "enters_buy_zone" | "crosses_fv" | "crosses_top_band" | "crosses_invalidation";

export type Alert = {
  id: number;
  symbol: string;
  alert_type: AlertType;
  is_active: boolean;
  last_triggered_at: string | null;
  last_triggered_price: number | null;
  created_at: string;
};

export type UploadedDataset = {
  symbol: string;
  name: string | null;
  statement_type: "consolidated" | "standalone";
  annual_years: number;
  first_fiscal_year: number | null;
  last_fiscal_year: number | null;
  quarters: number;
  uploaded_at: string;
};

export type ConfigFileName = "providers" | "valuation" | "sectors" | "scoring" | "technical" | "jobs";

export type ConfigView = {
  files: { name: ConfigFileName; yaml: string }[];
  parsed: Record<string, unknown>;
};

// ── P14 notifications (app/api/notifications.py) ──
export type Notification = {
  id: number;
  created_at: string;
  symbol: string | null;
  kind: string;
  title: string;
  body: string;
  price: number | null;
  read: boolean;
  telegram: "sent" | "failed" | "disabled";
  telegram_error: string | null;
};

export type NotificationsView = {
  items: Notification[];
  unread: number;
  telegram_configured: boolean;
  kinds?: Record<string, number>; // all notifications by kind (P23)
  next_before_id?: number | null; // paging: pass as before_id
};

export type TelegramStatus = {
  configured: boolean;
  bot_enabled: boolean;
  state: string | null; // polling | standby (another worker polls) | stopped | null = never ran
  last_poll_at: string | null;
  last_command_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  ignored_messages: number;
};

// ───────── backtests (backend/app/backtest/runner.py::run_backtest) ─────────

export type BacktestStatus = "queued" | "running" | "done" | "failed";

export type BacktestParams = {
  grades: Grade[];
  zones: ZoneName[];
  holding_days: number;
  start: string;
  end: string;
  symbols?: string[];
};

export type BacktestMetrics = {
  cagr: number | null;
  max_drawdown: number | null;
  hit_rate: number | null;
  avg_holding_days: number | null;
  avg_trade_return: number | null;
  trades: number;
  exposure: number | null;
  total_return: number | null;
};

export type BacktestCell = BacktestMetrics & {
  grade: Grade;
  zone: ZoneName;
  signals: number;
  selected: boolean;
};

export type BacktestTrade = {
  symbol: string;
  entry: string;
  exit: string;
  days: number;
  return: number;
  closed_by: "holding_period" | "data_end" | "backtest_end";
};

export type BacktestResults = {
  period: { start: string; end: string; rebalances: number };
  universe: { source: "symbols" | "index_membership"; avg_size: number; stocks_with_data: number };
  portfolio: BacktestMetrics;
  benchmark: { label: string; cagr: number | null; max_drawdown: number | null; total_return: number | null };
  equity: { time: string; portfolio: number; benchmark: number | null }[];
  cells: BacktestCell[];
  trades_sample: BacktestTrade[];
  caveats: string[];
  notes: string[];
  failures: Record<string, string>;
};

export type Backtest = {
  id: number;
  status: BacktestStatus;
  params: BacktestParams;
  /** While running: {progress: {done, total}}; when done: the full results. */
  results: (Partial<BacktestResults> & { progress?: { done: number; total: number } }) | null;
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type BacktestSummary = {
  id: number;
  status: BacktestStatus;
  params: BacktestParams;
  created_at: string;
  updated_at: string;
  progress: { done: number; total: number } | null;
  trades: number | null;
  cagr: number | null;
  benchmark_cagr: number | null;
  error: string | null;
};

// ───────── exchange results filings (XBRL; app/api/schemas.py) ─────────

export type FilingStatus = "pending" | "parsed" | "failed";

export type ResultFiling = {
  id: number;
  symbol: string;
  exchange: "nse" | "upload" | "offline";
  document: string;
  period_start: string | null;
  period_end: string | null;
  statement_type: "consolidated" | "standalone" | null;
  audited: boolean | null;
  is_bank: boolean | null;
  disseminated_at: string | null;
  announcement_date: string | null;
  status: FilingStatus;
  attempts: number;
  error: string | null;
  periods: string[] | null;
  warnings: string[] | null;
  parsed_at: string | null;
  updated_at: string;
};

export type FilingsSummary = {
  pending: number;
  parsed: number;
  failed: number;
  symbols: number;
  last_parsed_at: string | null;
};

export type XbrlFileResult = {
  filename: string;
  status: FilingStatus;
  periods: string[];
  statement_type: "consolidated" | "standalone" | null;
  announcement_date: string | null;
  warnings: string[];
  error: string | null;
};

export type XbrlUploadSummary = { symbol: string; files: XbrlFileResult[] };

// ───────────── annual-report PDFs, review queue, coverage (SPEC v0.2 §3.6 steps 3-4) ─────────────

export type ReviewStatus = "auto_accepted" | "pending" | "accepted" | "corrected" | "rejected";

export type AnnualReportStatement = {
  statement: "bs" | "cf";
  basis: "consolidated" | "standalone";
  pages: number[];
  method: "pdfplumber" | "camelot";
  unit: string | null;
  checks: ("passed" | "failed" | "none")[];
  column_dates: (string | null)[];
};

export type AnnualReport = {
  id: number;
  symbol: string;
  exchange: "nse" | "upload";
  document: string;
  fiscal_year: number | null;
  disseminated_at: string | null;
  usable_from: string | null;
  status: FilingStatus;
  attempts: number;
  error: string | null;
  page_count: number | null;
  statements: AnnualReportStatement[] | null;
  warnings: string[] | null;
  has_document: boolean;
  candidates: Partial<Record<ReviewStatus, number>>;
  parsed_at: string | null;
  updated_at: string;
};

export type PdfCandidate = {
  id: number;
  symbol: string;
  annual_report_id: number;
  fiscal_year: number | null;
  statement: "bs" | "cf";
  basis: "consolidated" | "standalone";
  period_end: string;
  item_code: string;
  value_cr: number | null;
  corrected_value_cr: number | null;
  raw_value: number;
  raw_label: string;
  pages: number[];
  method: string;
  confidence: number;
  reasons: string[];
  status: ReviewStatus;
  stored: boolean;
  note: string | null;
  reviewed_at: string | null;
};

export type ReviewSummary = { pending: number; reports_parsed: number; reports_failed: number };

export type CoverageSource = "xbrl" | "pdf" | "derived" | "screener" | "yfinance" | "nse";

export type CoverageCell = {
  fiscal_year: number;
  statement: "P&L" | "BS" | "CF";
  sources: CoverageSource[];
  items: number;
  pending_review: number;
};

export type CoverageGrid = {
  symbol: string;
  years: number[];
  fy_end_month: number;
  bases: { basis: "consolidated" | "standalone"; cells: CoverageCell[] }[];
};

// ── on-demand pipeline (SPEC §3.7; app/api/pipeline.py) ──
export type PipelineStepStatus = "pending" | "running" | "ok" | "warning" | "failed" | "skipped";

export type PipelineStep = {
  name: string;
  label: string;
  optional: boolean;
  status: PipelineStepStatus;
  message: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type PipelineRun = {
  id: number;
  symbol: string;
  trigger: string;
  status: "queued" | "running" | "done" | "failed";
  steps: PipelineStep[];
  version: number;
  attempts: number;
  error: string | null;
  report_as_of: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type PipelineStart = {
  symbol: string;
  fresh: boolean;
  reason: string;
  report_as_of: string | null;
  run: PipelineRun | null;
};

// ── corporate events + reconciliation (SPEC v0.2 §3.8-3.9; app/api/events.py) ──
export type EventKind =
  | "announcement"
  | "board_meeting"
  | "results"
  | "pledge"
  | "sast"
  | "insider_trade"
  | "bulk_deal"
  | "block_deal";

export type StockEvent = {
  id: number;
  exchange: string;
  kind: EventKind;
  category: string | null;
  red_flag: boolean;
  title: string;
  detail: string | null;
  event_date: string | null;
  disseminated_at: string | null;
  url: string | null;
  data: Record<string, unknown> | null;
};

export type StockEvents = {
  symbol: string;
  upcoming: StockEvent[];
  events: StockEvent[];
};

export type ReconciliationIssue = {
  id: number;
  period_end: string;
  period_type: string;
  basis: string;
  item_code: string;
  source: string;
  reference_source: string;
  value_inr: number;
  reference_value_inr: number;
  diff_rel: number;
  values: Record<string, number>;
  cause: "units" | "basis" | "restatement" | null;
  reasons: string[];
  status: "open" | "resolved" | "ignored";
  detected_at: string;
  checked_at: string;
  resolved_at: string | null;
};

export type Reconciliation = {
  symbol: string;
  tolerance_rel: number;
  checked_at: string | null;
  open: ReconciliationIssue[];
  closed: ReconciliationIssue[];
};

export type Thesis = {
  symbol: string;
  as_of: string;
  enabled: boolean;
  status: "ok" | "rejected" | "failed" | "missing" | "disabled";
  text: string | null;
  model: string | null;
  generated_at: string | null;
  attempts: number;
  problems: string[];
  reasons: string[];
};

export type DiagRow = {
  method: string;
  endpoint: string;
  url: string;
  status: number | null;
  server: string | null;
  content_type: string | null;
  cookie_names: string[];
  length: number;
  verdict: string;
};

export type SiteDiag = {
  site: "nse" | "bse";
  checked_at: string;
  methods: string[];
  working_method: string | null;
  summary: Record<string, { verdict: string; method: string }>;
  rows: DiagRow[];
};

export type DataSources = {
  running: boolean;
  nse: SiteDiag | null;
  bse: SiteDiag | null;
};
