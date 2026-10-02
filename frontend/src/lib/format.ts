// Number formatting (Indian locale) and display labels shared by the report page.

const inrFmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const intFmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

export const DASH = "—";

export function inr(v: number | null | undefined): string {
  return v == null || !Number.isFinite(v) ? DASH : `₹${inrFmt.format(v)}`;
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits: digits }).format(v);
}

export function crore(v: number | null | undefined): string {
  return v == null || !Number.isFinite(v) ? DASH : `₹${intFmt.format(v)} Cr`;
}

/** Fraction → percent text (0.123 → "12.3%"). */
export function pct(v: number | null | undefined, digits = 1): string {
  return v == null || !Number.isFinite(v) ? DASH : `${(v * 100).toFixed(digits)}%`;
}

export function signedPct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  return `${v > 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`;
}

/** ₹ crore → "₹4.2 L Cr" (lakh crore) from 1 lakh crore, else "₹52,300 Cr". */
export function croreCompact(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  const a = Math.abs(v);
  if (a >= 1e5) return `₹${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }).format(v / 1e5)} L Cr`;
  return `₹${new Intl.NumberFormat("en-IN", { maximumFractionDigits: a >= 100 ? 0 : 2 }).format(v)} Cr`;
}

/** A value already in percent (13.7 → "13.7%"). */
export function pctPoints(v: number | null | undefined, digits = 1): string {
  return v == null || !Number.isFinite(v) ? DASH : `${v.toFixed(digits)}%`;
}

/** Multiple (2.4 → "2.4x"). */
export function times(v: number | null | undefined, digits = 1): string {
  return v == null || !Number.isFinite(v) ? DASH : `${v.toFixed(digits)}x`;
}

export type Tone = "discount" | "fair" | "premium" | "unknown";

/** Zone → semantic tone: green discount, amber fair, red premium, grey unknown. */
export function zoneTone(zone: string | null | undefined): Tone {
  if (zone === "deep_discount" || zone === "discount") return "discount";
  if (zone === "fair") return "fair";
  if (zone === "premium" || zone === "extreme_premium") return "premium";
  return "unknown";
}

/** A step / run message safe to show: no SQL, driver errors or stack traces reach the UI. */
export function friendlyMessage(msg: string | null | undefined): string {
  if (!msg) return "";
  if (/Traceback|psycopg|sqlalchemy|SQLSTATE|\bSELECT\b|\bINSERT\b|CardinalityViolation|\n\s+File "/i.test(msg)) {
    return "A data step hit an internal error. The report is still built from the data on file; details are in the worker log.";
  }
  return msg.length > 240 ? `${msg.slice(0, 237)}…` : msg;
}

const ACRONYMS: Record<string, string> = { it: "IT", fmcg: "FMCG", nbfc: "NBFC", ev: "EV", pb: "PB" };

export function titleCase(s: string): string {
  return s
    .split("_")
    .map((w) => ACRONYMS[w.toLowerCase()] ?? w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export const ZONE_LABEL: Record<string, string> = {
  deep_discount: "Deep Discount",
  discount: "Discount",
  fair: "Fair",
  premium: "Premium",
  extreme_premium: "Extreme Premium",
};

export const ACTION_LABEL: Record<string, string> = {
  strong_buy: "Strong Buy",
  buy: "Buy",
  accumulate: "Accumulate",
  buy_on_pullback: "Buy on Pullback",
  momentum_entry: "Momentum Entry",
  wait: "Wait",
  hold: "Hold",
  book_profits: "Book Profits",
  avoid: "Avoid",
};

export const METHOD_LABEL: Record<string, string> = {
  dcf_base: "DCF (base)",
  band_pe: "PE band",
  band_ev_ebitda: "EV/EBITDA band",
  band_pb: "P/B band",
  band_p_ev: "P/EV band",
  relative: "Relative PE",
  relative_pb: "Relative P/B",
  relative_p_ev: "Relative P/EV",
  justified_pb: "Justified P/B",
  normalised_ev_ebitda: "Normalised EV/EBITDA",
  epv: "EPV",
  graham_number: "Graham number",
  residual_income: "Residual income",
  appraisal_value: "Appraisal value",
};

export const AVWAP_LABEL: Record<string, string> = {
  low_52w: "AVWAP 52-wk low",
  last_results_date: "AVWAP results",
  last_major_swing_low: "AVWAP swing low",
};

/** Read a CSS custom property (for canvas charts that need concrete colours). */
export function cssVar(name: string, el: Element = document.documentElement): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}
