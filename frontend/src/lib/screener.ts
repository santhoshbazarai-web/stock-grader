// Screener filter state ⇄ URL query, and client-side column sorting.
import type { Grade, ScreenerFilters, ScreenerRow, SortKey, ZoneName } from "@/lib/types";

export const GRADES: Grade[] = ["A_plus", "A", "B", "C", "D"];
export const ZONES: ZoneName[] = ["deep_discount", "discount", "fair", "premium", "extreme_premium"];
export const ACTIONS = [
  "strong_buy", "buy", "accumulate", "buy_on_pullback", "momentum_entry", "wait", "hold",
  "book_profits", "avoid",
];
/** Columns the server can sort; the rest are sorted in the browser. */
export const SERVER_SORTS: SortKey[] = [
  "symbol", "total_score", "pct_to_buy_zone", "earned_premium", "rs_percentile", "market_cap_cr", "cmp",
];
export type Column = SortKey | "grade" | "zone" | "sector" | "fair_value" | "action";

export type Filters = Omit<ScreenerFilters, "sort"> & { sort: Column };

export const EMPTY: Filters = {
  grade: [],
  zone: [],
  sector: [],
  action: [],
  min_earned_premium: null,
  max_distance_to_buy_zone: null,
  min_mcap_cr: null,
  max_mcap_cr: null,
  sort: "total_score",
  order: "desc",
};

const LISTS = ["grade", "zone", "sector", "action"] as const;
const NUMS = ["min_earned_premium", "max_distance_to_buy_zone", "min_mcap_cr", "max_mcap_cr"] as const;

export function fromQuery(q: URLSearchParams): Filters {
  const f: Filters = { ...EMPTY, grade: [], zone: [], sector: [], action: [] };
  for (const k of LISTS) (f[k] as string[]) = q.getAll(k);
  for (const k of NUMS) {
    const v = q.get(k);
    f[k] = v != null && v !== "" && Number.isFinite(Number(v)) ? Number(v) : null;
  }
  f.sort = (q.get("sort") as Column) ?? EMPTY.sort;
  f.order = q.get("order") === "asc" ? "asc" : "desc";
  return f;
}

export function toQuery(f: Filters, { forServer = false } = {}): URLSearchParams {
  const q = new URLSearchParams();
  for (const k of LISTS) for (const v of f[k]) q.append(k, v);
  for (const k of NUMS) if (f[k] != null) q.set(k, String(f[k]));
  if (!forServer || SERVER_SORTS.includes(f.sort as SortKey)) {
    if (f.sort !== EMPTY.sort || forServer) q.set("sort", f.sort);
    if (f.order !== EMPTY.order || forServer) q.set("order", f.order);
  }
  if (forServer) q.set("limit", "1000");
  return q;
}

const GRADE_RANK: Record<string, number> = { A_plus: 0, A: 1, B: 2, C: 3, D: 4 };
const ZONE_RANK = Object.fromEntries(ZONES.map((z, i) => [z, i]));

function key(r: ScreenerRow, c: Column): number | string | null {
  if (c === "grade") return r.grade ? GRADE_RANK[r.grade] : null;
  if (c === "zone") return r.zone ? ZONE_RANK[r.zone] : null;
  return r[c] as number | string | null;
}

/** Sort rows by a column; missing values always last. Grade sorts A+ first when descending. */
export function sortRows(rows: ScreenerRow[], c: Column, order: "asc" | "desc"): ScreenerRow[] {
  // For grade/zone the rank is "best first", so flip so that desc = A+ / Deep Discount first.
  const flip = c === "grade" || c === "zone" ? -1 : 1;
  const dir = (order === "asc" ? 1 : -1) * flip;
  return [...rows].sort((a, b) => {
    const x = key(a, c);
    const y = key(b, c);
    if (x == null && y == null) return a.symbol.localeCompare(b.symbol);
    if (x == null) return 1;
    if (y == null) return -1;
    const cmp = typeof x === "string" ? x.localeCompare(String(y)) : (x as number) - (y as number);
    return cmp * dir || a.symbol.localeCompare(b.symbol);
  });
}
