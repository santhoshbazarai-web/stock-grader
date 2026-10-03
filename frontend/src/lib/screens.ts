// Screener v2 types and URL <-> filter conversion. Filters live in the URL as
// `min.<field>=5`, `max.<field>=20`, `in.<field>=a,b` (the same names the API takes), so a scan
// is shareable and a Screening Idea can open the screener already filled in.
export type FieldDef = {
  key: string;
  label: string;
  group: string;
  type: "number" | "percent" | "enum" | "text";
  unit: string | null;
  options: string[] | null;
};
export type FieldsOut = { groups: string[]; fields: FieldDef[] };

export type Filter = { key: string; min?: number | null; max?: number | null; in?: string[] | null };
export type Definition = { filters: Filter[]; sort: string; order: "asc" | "desc" };

export type ScanRow = Record<string, string | number | null | { pillar: string; score: number | null }[]> & {
  symbol: string;
  pillars: { pillar: string; score: number | null }[];
};
export type ScanOut = { rows: ScanRow[]; total: number; page: number; page_size: number; universe: number; scanned_at: string };

export type Idea = { id: string; name: string; icon: string; description: string; sort: string; order: "asc" | "desc"; filters: Filter[] };
export type SavedScreen = { name: string; definition: Definition; updated_at: string };

export const PAGE_SIZES = [10, 20, 40] as const;

export function filtersFromParams(params: URLSearchParams): Filter[] {
  const byKey = new Map<string, Filter>();
  for (const [name, raw] of params.entries()) {
    const [op, ...rest] = name.split(".");
    const key = rest.join(".");
    if (!key || !["min", "max", "in"].includes(op)) continue;
    const f = byKey.get(key) ?? { key };
    if (op === "in") f.in = raw.split(",").filter(Boolean);
    else {
      const n = Number(raw);
      if (raw !== "" && Number.isFinite(n)) f[op as "min" | "max"] = n;
    }
    byKey.set(key, f);
  }
  return [...byKey.values()];
}

export function paramsFromFilters(filters: Filter[], extra: Record<string, string | number | undefined> = {}): URLSearchParams {
  const p = new URLSearchParams();
  for (const f of filters) {
    if (f.min != null) p.set(`min.${f.key}`, String(f.min));
    if (f.max != null) p.set(`max.${f.key}`, String(f.max));
    if (f.in?.length) p.set(`in.${f.key}`, f.in.join(","));
  }
  for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== "") p.set(k, String(v));
  return p;
}
