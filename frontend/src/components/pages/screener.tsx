"use client";

// Screener (SPEC §9): filters (kept in the URL, so a view is linkable), a sortable table of the
// latest reports, and named presets saved on the server.
import { ArrowDown, ArrowUp } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { ActionBadge, Empty, ErrorText, GradeBadge, Page } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { ACTION_LABEL, crore, inr, num, pct, titleCase, ZONE_LABEL } from "@/lib/format";
import { ACTIONS, type Column, EMPTY, type Filters, fromQuery, GRADES, sortRows, toQuery, ZONES } from "@/lib/screener";
import type { Preset, ScreenerRow } from "@/lib/types";

const COLUMNS: { key: Column; label: string; align?: "right" }[] = [
  { key: "symbol", label: "Symbol" },
  { key: "sector", label: "Sector" },
  { key: "grade", label: "Grade" },
  { key: "zone", label: "Zone" },
  { key: "cmp", label: "CMP", align: "right" },
  { key: "fair_value", label: "FV", align: "right" },
  { key: "pct_to_buy_zone", label: "% to buy zone", align: "right" },
  { key: "earned_premium", label: "EP", align: "right" },
  { key: "rs_percentile", label: "RS", align: "right" },
  { key: "total_score", label: "Score", align: "right" },
  { key: "market_cap_cr", label: "Mcap", align: "right" },
  { key: "action", label: "Action" },
];

export function Chips<T extends string>({
  label,
  options,
  value,
  onChange,
  render,
}: {
  label: string;
  options: T[];
  value: T[];
  onChange: (v: T[]) => void;
  render: (o: T) => string;
}) {
  return (
    <fieldset className="flex flex-wrap items-center gap-1">
      <legend className="text-muted-foreground mb-1 text-xs">{label}</legend>
      {options.map((o) => {
        const on = value.includes(o);
        return (
          <button
            key={o}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(on ? value.filter((x) => x !== o) : [...value, o])}
            className={`rounded-md border px-2 py-0.5 text-xs ${on ? "bg-primary text-primary-foreground border-primary" : "hover:bg-accent"}`}
          >
            {render(o)}
          </button>
        );
      })}
    </fieldset>
  );
}

function NumberField({
  label,
  value,
  onChange,
  step,
  hint,
}: {
  label: string;
  value: number | null;
  onChange: (v: number | null) => void;
  step?: string;
  hint?: string;
}) {
  return (
    <label className="flex flex-col gap-1 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <input
        type="number"
        step={step ?? "any"}
        value={value ?? ""}
        placeholder={hint}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
        className="border-input bg-background h-8 w-28 rounded-md border px-2 text-sm tabular-nums"
      />
    </label>
  );
}

function Presets({ filters, apply }: { filters: Filters; apply: (f: Filters) => void }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [selected, setSelected] = useState("");
  const [name, setName] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const load = () => api<Preset[]>("/screener/presets").then(setPresets).catch(() => setPresets([]));
  useEffect(() => {
    load();
  }, []);

  async function save() {
    const n = name.trim() || selected;
    if (!n) return setMsg("Name the preset first");
    try {
      await api(`/screener/presets/${encodeURIComponent(n)}`, { method: "PUT", body: JSON.stringify({ filters }) });
      setMsg(`Saved “${n}”`);
      setName("");
      setSelected(n);
      load();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.detail : "save failed");
    }
  }
  async function remove() {
    if (!selected) return;
    await api(`/screener/presets/${encodeURIComponent(selected)}`, { method: "DELETE" }).catch(() => undefined);
    setMsg(`Deleted “${selected}”`);
    setSelected("");
    load();
  }
  return (
    <div className="flex flex-wrap items-end gap-2" aria-label="Presets">
      <label className="flex flex-col gap-1 text-xs">
        <span className="text-muted-foreground">Preset</span>
        <select
          aria-label="Preset"
          value={selected}
          onChange={(e) => {
            setSelected(e.target.value);
            const p = presets.find((x) => x.name === e.target.value);
            if (p) apply({ ...EMPTY, ...p.filters } as Filters);
          }}
          className="border-input bg-background h-8 rounded-md border px-2 text-sm"
        >
          <option value="">— choose —</option>
          {presets.map((p) => (
            <option key={p.name} value={p.name}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <input
        aria-label="Preset name"
        placeholder="Save current filters as…"
        value={name}
        onChange={(e) => setName(e.target.value)}
        maxLength={64}
        className="border-input bg-background h-8 w-52 rounded-md border px-2 text-sm"
      />
      <Button size="sm" variant="outline" onClick={save}>
        Save preset
      </Button>
      <Button size="sm" variant="ghost" onClick={remove} disabled={!selected}>
        Delete
      </Button>
      {msg && (
        <span className="text-muted-foreground text-xs" role="status">
          {msg}
        </span>
      )}
    </div>
  );
}

export function Screener() {
  const router = useRouter();
  const path = usePathname();
  const params = useSearchParams();
  const filters = useMemo(() => fromQuery(new URLSearchParams(params.toString())), [params]);
  const [rows, setRows] = useState<ScreenerRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sectors, setSectors] = useState<string[]>([]);

  const set = (f: Filters) => {
    const q = toQuery(f).toString();
    router.replace(q ? `${path}?${q}` : path, { scroll: false });
  };
  const serverQuery = toQuery(filters, { forServer: true }).toString();

  useEffect(() => {
    let live = true;
    setError(null);
    api<ScreenerRow[]>(`/screener?${serverQuery}`)
      .then((r) => live && setRows(r))
      .catch((e) => live && setError(e instanceof ApiError ? e.detail : "failed"));
    return () => {
      live = false;
    };
  }, [serverQuery]);

  useEffect(() => {
    api<{ parsed: { sectors: Record<string, unknown> } }>("/config")
      .then((c) => setSectors(Object.keys(c.parsed.sectors).sort()))
      .catch(() => undefined);
  }, []);

  const sorted = rows ? sortRows(rows, filters.sort, filters.order) : null;
  const sortBy = (c: Column) =>
    set({ ...filters, sort: c, order: filters.sort === c && filters.order === "desc" ? "asc" : "desc" });

  return (
    <Page>
      <div className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Screener</h1>
        <span className="text-muted-foreground text-sm">{sorted ? `${sorted.length} stocks` : ""}</span>
      </div>
      <Card className="gap-4 py-4">
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap gap-x-8 gap-y-3">
            <Chips label="Grade" options={GRADES} value={filters.grade} onChange={(grade) => set({ ...filters, grade })} render={(g) => g.replace("_plus", "+")} />
            <Chips label="Zone" options={ZONES} value={filters.zone} onChange={(zone) => set({ ...filters, zone })} render={(z) => ZONE_LABEL[z]} />
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">Sector</span>
              <select
                value={filters.sector[0] ?? ""}
                onChange={(e) => set({ ...filters, sector: e.target.value ? [e.target.value] : [] })}
                className="border-input bg-background h-8 rounded-md border px-2 text-sm"
              >
                <option value="">All</option>
                {sectors.map((s) => (
                  <option key={s} value={s}>
                    {titleCase(s)}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">Action</span>
              <select
                value={filters.action[0] ?? ""}
                onChange={(e) => set({ ...filters, action: e.target.value ? [e.target.value] : [] })}
                className="border-input bg-background h-8 rounded-md border px-2 text-sm"
              >
                <option value="">All</option>
                {ACTIONS.map((a) => (
                  <option key={a} value={a}>
                    {ACTION_LABEL[a]}
                  </option>
                ))}
              </select>
            </label>
            <NumberField label="Min EP score (0–8)" value={filters.min_earned_premium} step="1" onChange={(v) => set({ ...filters, min_earned_premium: v })} />
            <NumberField
              label="Max % above buy zone"
              value={filters.max_distance_to_buy_zone == null ? null : Math.round(filters.max_distance_to_buy_zone * 1000) / 10}
              hint="e.g. 5"
              onChange={(v) => set({ ...filters, max_distance_to_buy_zone: v == null ? null : v / 100 })}
            />
            <NumberField label="Min mcap (₹ Cr)" value={filters.min_mcap_cr} step="100" onChange={(v) => set({ ...filters, min_mcap_cr: v })} />
            <NumberField label="Max mcap (₹ Cr)" value={filters.max_mcap_cr} step="100" onChange={(v) => set({ ...filters, max_mcap_cr: v })} />
            <Button size="sm" variant="ghost" onClick={() => set(EMPTY)}>
              Reset
            </Button>
          </div>
          <Presets filters={filters} apply={set} />
        </CardContent>
      </Card>

      {error && <ErrorText>{error}</ErrorText>}
      {!sorted && !error && <p className="text-muted-foreground text-sm">Loading…</p>}
      {sorted && sorted.length === 0 && <Empty>No stocks match these filters.</Empty>}
      {sorted && sorted.length > 0 && (
        <div className="overflow-x-auto rounded-xl border">
          <table className="w-full text-sm tabular-nums" aria-label="Screener results">
            <thead className="bg-muted/50 text-muted-foreground text-xs">
              <tr>
                {COLUMNS.map((c) => {
                  const active = filters.sort === c.key;
                  return (
                    <th
                      key={c.key}
                      scope="col"
                      aria-sort={active ? (filters.order === "asc" ? "ascending" : "descending") : "none"}
                      className={`px-3 py-2 font-medium ${c.align === "right" ? "text-right" : "text-left"}`}
                    >
                      <button type="button" onClick={() => sortBy(c.key)} className="hover:text-foreground inline-flex items-center gap-1">
                        {c.label}
                        {active && (filters.order === "asc" ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => (
                <tr key={r.symbol} className="hover:bg-muted/40 border-t">
                  <td className="px-3 py-2">
                    <Link href={`/stocks/${r.symbol}`} className="font-medium hover:underline">
                      {r.symbol}
                    </Link>
                    <div className="text-muted-foreground max-w-48 truncate text-xs">{r.name}</div>
                  </td>
                  <td className="px-3 py-2 text-xs">{titleCase(r.sector)}</td>
                  <td className="px-3 py-2">
                    <GradeBadge label={r.grade_label} />
                  </td>
                  <td className="px-3 py-2 text-xs">{r.zone ? ZONE_LABEL[r.zone] : "—"}</td>
                  <td className="px-3 py-2 text-right">{inr(r.cmp)}</td>
                  <td className="px-3 py-2 text-right">{inr(r.fair_value)}</td>
                  <td className="px-3 py-2 text-right">
                    {r.pct_to_buy_zone == null ? "—" : r.pct_to_buy_zone === 0 ? "in zone" : pct(r.pct_to_buy_zone)}
                  </td>
                  <td className="px-3 py-2 text-right">{r.earned_premium ?? "—"}</td>
                  <td className="px-3 py-2 text-right">{num(r.rs_percentile, 0)}</td>
                  <td className="px-3 py-2 text-right">{num(r.total_score, 1)}</td>
                  <td className="px-3 py-2 text-right text-xs">{crore(r.market_cap_cr)}</td>
                  <td className="px-3 py-2">
                    <ActionBadge action={r.action} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-muted-foreground text-xs">
        % to buy zone: 0 = inside the technical buy zone; positive = how far the price must fall to reach it; negative = below it.
      </p>
    </Page>
  );
}
