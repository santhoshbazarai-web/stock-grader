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

export type InstrumentHit = {
  symbol: string;
  name: string | null;
  sector: string | null;
  industry: string | null;
  is_index: boolean;
};
