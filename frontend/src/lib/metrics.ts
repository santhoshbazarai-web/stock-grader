// Metric catalogue for the stock page: one definition per metric (label, group, how to read it
// from the report, how to show it). The Overview tables and the "My metrics" card both use it.
// A metric that is null carries the reason, shown on hover; metrics that mean nothing for a
// bank (or only for one) are left out of the other model's tables.
import { croreCompact, inr, num, pct, pctPoints, signedPct, times, titleCase } from "@/lib/format";
import type { StockReport } from "@/lib/types";

export type MetricValue = { text: string | null; reason?: string | null; tone?: "discount" | "fair" | "premium" | "unknown" };
export type MetricDef = {
  id: string;
  label: string;
  group: string;
  /** "bank": only for bank / insurer models; "general": only for the rest */
  only?: "bank" | "general";
  get: (r: StockReport) => MetricValue;
  hint?: string;
};

export const isBankModel = (r: StockReport) => r.valuation.model === "bank" || r.valuation.model === "insurance";

function fund(key: string, fmt: (v: number) => string, label?: string) {
  return (r: StockReport): MetricValue => {
    const v = r.fundamentals[key];
    if (v != null) return { text: fmt(v) };
    return { text: null, reason: r.fundamentals_notes?.[key] ?? `${label ?? key} is not on file` };
  };
}
function bank(name: string, fmt: (v: number) => string) {
  return (r: StockReport): MetricValue => {
    const m = r.bank_metrics?.find((b) => b.name === name);
    if (m?.value != null) return { text: fmt(m.value) };
    return { text: null, reason: m?.reason ?? `${name} is not reported` };
  };
}
const peer = (key: "pe" | "pb" | "ev_ebitda" | "roe" | "roce", fmt: (v: number) => string) => (r: StockReport): MetricValue => {
  const v = r.peer_stats?.[key] ?? null;
  return v != null ? { text: fmt(v) } : { text: null, reason: `${key} needs earnings / book value and a price` };
};
const shp = (key: "promoter_pct" | "fii_pct" | "dii_pct" | "public_pct") => (r: StockReport): MetricValue => {
  const v = r.shareholding?.[key];
  return v != null ? { text: pctPoints(v, 2) } : { text: null, reason: r.shareholding ? `${titleCase(key)} not in the latest pattern` : "no shareholding pattern on file" };
};
const pctF = (v: number) => pct(v, 1);

export const METRICS: MetricDef[] = [
  // Key statistics
  { id: "cmp", label: "Price (NSE)", group: "Key statistics", get: (r) => ({ text: inr(r.cmp) }) },
  {
    id: "market_cap", label: "Market cap", group: "Key statistics",
    get: (r) => (r.valuation.market_cap_cr != null ? { text: croreCompact(r.valuation.market_cap_cr) } : { text: null, reason: "shares outstanding not on file" }),
  },
  { id: "pe", label: "P/E", group: "Key statistics", only: "general", get: peer("pe", (v) => times(v)) },
  { id: "pb", label: "P/B", group: "Key statistics", get: (r) => (isBankModel(r) ? bank("pb", (v) => times(v))(r) : peer("pb", (v) => times(v))(r)) },
  { id: "roe", label: "ROE", group: "Key statistics", get: fund("roe_latest", pctF, "ROE") },
  { id: "beta", label: "Beta", group: "Key statistics", get: (r) => (r.valuation.beta != null ? { text: num(r.valuation.beta, 2) } : { text: null, reason: "needs weekly benchmark returns" }) },
  { id: "cost_of_equity", label: "Cost of equity", group: "Key statistics", get: (r) => (r.valuation.cost_of_equity != null ? { text: pct(r.valuation.cost_of_equity, 2) } : { text: null, reason: "beta or risk-free rate missing" }) },
  // Financial snapshot
  { id: "revenue_ttm", label: "Revenue (TTM)", group: "Financial snapshot", only: "general", get: fund("revenue_ttm", croreCompact, "revenue") },
  { id: "pat_ttm", label: "Net profit (TTM)", group: "Financial snapshot", get: fund("pat_ttm", croreCompact, "profit") },
  { id: "nii", label: "Net interest income", group: "Financial snapshot", only: "bank", get: bank("nii", croreCompact) },
  { id: "bvps", label: "Book value / share", group: "Financial snapshot", only: "bank", get: bank("bvps", inr) },
  { id: "eps_cagr_5y_snap", label: "EPS growth (5y CAGR)", group: "Financial snapshot", get: fund("eps_cagr_5y", pctF) },
  // Market info
  { id: "from_52w_high", label: "From 52-week high", group: "Market info", get: (r) => (r.technical.from_52w_high != null ? { text: signedPct(r.technical.from_52w_high) } : { text: null, reason: "needs a year of prices" }) },
  { id: "rsi", label: "RSI (14)", group: "Market info", get: (r) => (r.technical.rsi != null ? { text: num(r.technical.rsi, 0) } : { text: null, reason: "needs 15 sessions" }) },
  { id: "stage", label: "Weinstein stage", group: "Market info", get: (r) => (r.technical.stage != null ? { text: `Stage ${r.technical.stage}` } : { text: null, reason: "needs 30 weeks of prices" }) },
  { id: "rs", label: "RS percentile", group: "Market info", get: (r) => (r.technical.rs_percentile != null ? { text: num(r.technical.rs_percentile, 0) } : { text: null, reason: "RS percentile snapshot not available" }) },
  { id: "promoter", label: "Promoter holding", group: "Market info", get: shp("promoter_pct") },
  { id: "fii", label: "FII holding", group: "Market info", get: shp("fii_pct") },
  { id: "dii", label: "DII holding (incl. MF)", group: "Market info", get: shp("dii_pct") },
  { id: "public", label: "Public holding", group: "Market info", get: shp("public_pct") },
  // Growth
  { id: "sales_cagr_3y", label: "Sales CAGR 3y", group: "Growth", only: "general", get: fund("sales_cagr_3y", pctF) },
  { id: "sales_cagr_5y", label: "Sales CAGR 5y", group: "Growth", only: "general", get: fund("sales_cagr_5y", pctF) },
  { id: "sales_cagr_10y", label: "Sales CAGR 10y", group: "Growth", only: "general", get: fund("sales_cagr_10y", pctF) },
  { id: "eps_cagr_3y", label: "EPS CAGR 3y", group: "Growth", get: fund("eps_cagr_3y", pctF) },
  { id: "eps_cagr_5y", label: "EPS CAGR 5y", group: "Growth", get: fund("eps_cagr_5y", pctF) },
  { id: "eps_cagr_10y", label: "EPS CAGR 10y", group: "Growth", get: fund("eps_cagr_10y", pctF) },
  { id: "loan_growth", label: "Loan growth", group: "Growth", only: "bank", get: bank("loan_growth_pct", (v) => pctPoints(v)) },
  { id: "deposit_growth", label: "Deposit growth", group: "Growth", only: "bank", get: bank("deposit_growth_pct", (v) => pctPoints(v)) },
  // Profitability
  { id: "roe_p", label: "ROE", group: "Profitability", get: fund("roe_latest", pctF, "ROE") },
  { id: "roa", label: "ROA", group: "Profitability", only: "bank", get: bank("roa_pct", (v) => pctPoints(v, 2)) },
  { id: "nim", label: "Net interest margin", group: "Profitability", only: "bank", get: bank("nim_pct", (v) => pctPoints(v, 2)) },
  { id: "roce", label: "ROCE", group: "Profitability", only: "general", get: fund("roce_latest", pctF) },
  { id: "roic", label: "ROIC", group: "Profitability", only: "general", get: fund("roic_latest", pctF) },
  { id: "opm", label: "Operating margin", group: "Profitability", only: "general", get: fund("opm_ttm", pctF) },
  // Valuation ratios
  { id: "pe_v", label: "P/E", group: "Valuation ratios", only: "general", get: peer("pe", (v) => times(v)) },
  { id: "pb_v", label: "P/B", group: "Valuation ratios", get: (r) => (isBankModel(r) ? bank("pb", (v) => times(v))(r) : peer("pb", (v) => times(v))(r)) },
  { id: "ev_ebitda", label: "EV / EBITDA", group: "Valuation ratios", only: "general", get: peer("ev_ebitda", (v) => times(v)) },
  { id: "fair_value", label: "Fair value", group: "Valuation ratios", get: (r) => (r.levels.fair_value != null ? { text: inr(r.levels.fair_value) } : { text: null, reason: r.valuation.reasons.at(-1) ?? "too few valuation methods" }) },
  // Financial strength
  { id: "debt_to_equity", label: "Debt / equity", group: "Financial strength", only: "general", get: fund("debt_to_equity", (v) => num(v, 2)) },
  { id: "net_debt_ebitda", label: "Net debt / EBITDA", group: "Financial strength", only: "general", get: fund("net_debt_to_ebitda", (v) => times(v)) },
  { id: "interest_cover", label: "Interest coverage", group: "Financial strength", only: "general", get: fund("interest_coverage", (v) => times(v)) },
  { id: "altman", label: "Altman Z''", group: "Financial strength", only: "general", get: fund("altman_z2", (v) => num(v, 2)) },
  { id: "piotroski", label: "Piotroski F", group: "Financial strength", only: "general", get: fund("piotroski", (v) => num(v, 0)) },
  { id: "car", label: "Capital adequacy", group: "Financial strength", only: "bank", get: bank("car_pct", (v) => pctPoints(v)) },
  { id: "gnpa", label: "Gross NPA", group: "Financial strength", only: "bank", get: bank("gnpa_pct", (v) => pctPoints(v, 2)) },
  { id: "nnpa", label: "Net NPA", group: "Financial strength", only: "bank", get: bank("nnpa_pct", (v) => pctPoints(v, 2)) },
  { id: "credit_cost", label: "Credit cost", group: "Financial strength", only: "bank", get: bank("credit_cost_pct", (v) => pctPoints(v, 2)) },
  { id: "equity_assets", label: "Equity / assets", group: "Financial strength", only: "bank", get: bank("equity_to_assets_pct", (v) => pctPoints(v)) },
  // Efficiency
  { id: "ccc", label: "Cash conversion cycle", group: "Efficiency", only: "general", get: fund("ccc_days", (v) => `${num(v, 0)} days`) },
  { id: "cfo_pat", label: "Operating cash flow / profit (5y)", group: "Efficiency", only: "general", get: fund("cfo_to_pat_5y", (v) => times(v, 2)) },
  { id: "fcf_conv", label: "FCF conversion (5y)", group: "Efficiency", only: "general", get: fund("fcf_conversion_5y", pctF) },
  { id: "cost_income", label: "Cost-to-income", group: "Efficiency", only: "bank", get: bank("cost_to_income_pct", (v) => pctPoints(v)) },
  { id: "cd_ratio", label: "Credit-deposit ratio", group: "Efficiency", only: "bank", get: bank("cd_ratio_pct", (v) => pctPoints(v)) },
  { id: "casa", label: "CASA ratio", group: "Efficiency", only: "bank", get: bank("casa_pct", (v) => pctPoints(v)) },
  // Dividends
  { id: "payout", label: "Dividend payout", group: "Dividends", only: "bank", get: bank("payout_pct", (v) => pctPoints(v)) },
  { id: "div_yield", label: "Dividend yield", group: "Dividends", get: () => ({ text: null, reason: "dividend history is not on file yet" }) },
];

export const GROUPS = ["Key statistics", "Financial snapshot", "Market info", "Growth", "Profitability", "Valuation ratios", "Financial strength", "Efficiency", "Dividends"];

export function metricsFor(r: StockReport): MetricDef[] {
  const bankModel = isBankModel(r);
  return METRICS.filter((m) => !m.only || (m.only === "bank") === bankModel);
}

export const DEFAULT_MY_METRICS = ["market_cap", "pe", "pb", "roe", "roa", "div_yield"];
