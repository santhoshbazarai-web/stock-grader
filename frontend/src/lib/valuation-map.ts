// Pure helpers for the Valuation Map page: colour scales, grouping, the zone × grade table.
import { inr, signedPct, ZONE_LABEL } from "@/lib/format";

export type MapRow = {
  symbol: string;
  name: string | null;
  sector: string | null;
  industry: string | null;
  market_cap_cr: number | null;
  price: number;
  day_change_pct: number | null;
  fair_value: number;
  discount_pct: number; // price / fair value - 1
  zone: string | null;
  grade: string | null;
  depth: string | null;
  confidence: string | null;
  as_of: string;
};
export type ValuationMapData = {
  universe: string;
  universe_note: string | null;
  rows: MapRow[];
  excluded: number;
  total: number;
};

export type ColourBy = "discount" | "day_change" | "grade";
export type GroupBy = "sector" | "industry";

// Seven steps, green (cheap / good) → grey → red-orange (expensive / bad). Lightness changes
// strongly from step to step and every tile also prints its value, so the scale does not rely
// on hue alone (red-green colour blindness).
export const SCALE = ["#0a5c32", "#2b9a5f", "#a3d8b8", "#e6e5e0", "#f4bba7", "#d96a4a", "#9b2a1c"];
export const UNKNOWN_FILL = "#b9b8b2";

const DISCOUNT_EDGES = [-0.3, -0.15, -0.05, 0.05, 0.15, 0.3]; // below -30% … above +30%
const DAY_EDGES = [-0.03, -0.01, -0.002, 0.002, 0.01, 0.03]; // up = good, so the scale is flipped
const GRADE_FILL: Record<string, string> = { "A+": SCALE[0], A: SCALE[1], B: SCALE[2], C: SCALE[5], D: SCALE[6] };

export function bin(v: number, edges: number[]): number {
  let i = 0;
  while (i < edges.length && v >= edges[i]) i++;
  return i;
}

export function fillFor(r: MapRow, by: ColourBy): string {
  if (by === "grade") return (r.grade && GRADE_FILL[r.grade]) || UNKNOWN_FILL;
  if (by === "day_change") return r.day_change_pct == null ? UNKNOWN_FILL : SCALE[6 - bin(r.day_change_pct, DAY_EDGES)];
  return SCALE[bin(r.discount_pct, DISCOUNT_EDGES)];
}

/** Legend entries (swatch + range text) for the chosen colouring. */
export function legendFor(by: ColourBy): { fill: string; label: string }[] {
  if (by === "grade") return ["A+", "A", "B", "C", "D"].map((g) => ({ fill: GRADE_FILL[g], label: g }));
  const [edges, fmt] = by === "discount" ? [DISCOUNT_EDGES, (v: number) => `${v > 0 ? "+" : ""}${(v * 100).toFixed(0)}%`] : [DAY_EDGES, (v: number) => `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}%`];
  const order = by === "discount" ? [0, 1, 2, 3, 4, 5, 6] : [6, 5, 4, 3, 2, 1, 0];
  return order.map((step, k) => {
    const lo = edges[step - 1];
    const hi = edges[step];
    const label = lo === undefined ? `< ${fmt(hi)}` : hi === undefined ? `> ${fmt(lo)}` : `${fmt(lo)} … ${fmt(hi)}`;
    void k;
    return { fill: SCALE[by === "discount" ? step : 6 - step], label };
  });
}

export function textOn(fill: string): string {
  const n = parseInt(fill.slice(1), 16);
  const lum = (0.299 * ((n >> 16) & 255) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
  return lum > 0.6 ? "#111111" : "#ffffff";
}

export type TreeNode = { name: string; size?: number; row?: MapRow; children?: TreeNode[] };

/** sector (or industry) → stock, sized by market cap; stocks without a market cap are counted apart. */
export function buildTree(rows: MapRow[], by: GroupBy, only: string | null): { tree: TreeNode[]; noCap: number } {
  const groups = new Map<string, TreeNode>();
  let noCap = 0;
  for (const r of rows) {
    if (!r.market_cap_cr || r.market_cap_cr <= 0) {
      noCap++;
      continue;
    }
    const g = (by === "sector" ? r.sector : r.industry) ?? "Unclassified";
    if (only && g !== only) continue;
    const node = groups.get(g) ?? { name: g, children: [] };
    node.children!.push({ name: r.symbol, size: r.market_cap_cr, row: r });
    groups.set(g, node);
  }
  return { tree: [...groups.values()].sort((a, b) => sum(b) - sum(a)), noCap };
}
const sum = (n: TreeNode) => (n.children ?? []).reduce((s, c) => s + (c.size ?? 0), 0);

export const ZONES = ["deep_discount", "discount", "fair", "premium", "extreme_premium"] as const;
export const BANDS = [
  { id: "ap", label: "A+/A", grades: ["A+", "A"], fill: SCALE[0] },
  { id: "b", label: "B", grades: ["B"], fill: SCALE[2] },
  { id: "cd", label: "C/D", grades: ["C", "D"], fill: SCALE[5] },
  { id: "na", label: "No grade", grades: [] as string[], fill: UNKNOWN_FILL },
];

export type ZoneRow = { zone: string; label: string; total: number; pct: number; bands: { id: string; label: string; fill: string; count: number }[] };

export function distribution(rows: MapRow[]): { zones: ZoneRow[]; atDiscountOrFairPct: number | null } {
  const n = rows.length;
  const zones = ZONES.map((z) => {
    const inZone = rows.filter((r) => r.zone === z);
    const bands = BANDS.map((b) => ({
      id: b.id,
      label: b.label,
      fill: b.fill,
      count: inZone.filter((r) => (b.id === "na" ? !r.grade || !["A+", "A", "B", "C", "D"].includes(r.grade) : r.grade != null && b.grades.includes(r.grade))).length,
    }));
    return { zone: z, label: ZONE_LABEL[z], total: inZone.length, pct: n ? inZone.length / n : 0, bands };
  });
  const cheap = rows.filter((r) => r.zone === "deep_discount" || r.zone === "discount" || r.zone === "fair").length;
  return { zones, atDiscountOrFairPct: n ? cheap / n : null };
}

/** Tooltip text for a tile: name, price, fair value, zone, grade. */
export function describe(r: MapRow): string {
  const day = r.day_change_pct != null ? ` · day ${signedPct(r.day_change_pct, 2)}` : "";
  return `${r.name ?? r.symbol} (${r.symbol}) · ${inr(r.price)} vs fair value ${inr(r.fair_value)} (${signedPct(r.discount_pct, 1)}) · ${r.zone ? ZONE_LABEL[r.zone] : "no zone"} · grade ${r.grade ?? "n/a"} · ${r.confidence ?? "?"} confidence${day}`;
}
