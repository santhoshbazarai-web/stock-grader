"use client";

// Screener v2 (SPEC §9): pick parameters (search box + group tabs), tune them as chips with
// min / max inputs, run the scan, page through the results as a table or cards, and save the
// screen by name. State lives in the URL; "Run scan" commits the chips to it.
import { Search, X } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Page } from "@/components/common";
import { EmptyState, PillarMiniChart, Skeleton, Tabs, Term } from "@/components/ds";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { inr, num, signedPct, ZONE_LABEL, ACTION_LABEL, zoneTone } from "@/lib/format";
import {
  filtersFromParams,
  PAGE_SIZES,
  paramsFromFilters,
  type Definition,
  type FieldDef,
  type FieldsOut,
  type Filter,
  type SavedScreen,
  type ScanOut,
  type ScanRow,
} from "@/lib/screens";
import type { Pillar } from "@/lib/types";
import { TONE_CHIP } from "@/components/ds";

const COLUMNS: { key: string; label: string; align?: "right" }[] = [
  { key: "symbol", label: "Symbol" },
  { key: "name", label: "Name" },
  { key: "price", label: "Price", align: "right" },
  { key: "day_change_pct", label: "Change", align: "right" },
  { key: "sector", label: "Sector" },
  { key: "fair_value", label: "Fair value", align: "right" },
  { key: "discount_pct", label: "Discount", align: "right" },
  { key: "zone", label: "Zone" },
  { key: "grade", label: "Grade" },
  { key: "action", label: "Action" },
];

const s = (v: unknown) => (typeof v === "string" ? v : null);
const n = (v: unknown) => (typeof v === "number" ? v : null);

function ZoneChip({ zone }: { zone: string | null }) {
  const tone = zoneTone(zone);
  return <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${TONE_CHIP[tone]}`}>{zone ? ZONE_LABEL[zone] ?? zone : "—"}</span>;
}

function FilterChip({ def, f, onChange, onRemove }: { def: FieldDef; f: Filter; onChange: (f: Filter) => void; onRemove: () => void }) {
  const numeric = def.type === "number" || def.type === "percent";
  return (
    <li className="bg-card flex flex-wrap items-center gap-2 rounded-lg border px-2.5 py-1.5 text-sm" aria-label={`Filter ${def.label}`}>
      <span className="font-medium"><Term label={def.label} k={def.key} /></span>
      {numeric ? (
        <>
          <input aria-label={`${def.label} minimum`} placeholder="min" inputMode="decimal" className="bg-background w-20 rounded border px-1.5 py-0.5" defaultValue={f.min ?? ""} onBlur={(e) => onChange({ ...f, min: e.target.value === "" ? null : Number(e.target.value) })} />
          <span className="text-muted-foreground">to</span>
          <input aria-label={`${def.label} maximum`} placeholder="max" inputMode="decimal" className="bg-background w-20 rounded border px-1.5 py-0.5" defaultValue={f.max ?? ""} onBlur={(e) => onChange({ ...f, max: e.target.value === "" ? null : Number(e.target.value) })} />
          {def.unit && <span className="text-muted-foreground text-xs">{def.unit}</span>}
        </>
      ) : (
        <span className="flex flex-wrap gap-1">
          {(def.options ?? []).map((o) => {
            const on = f.in?.includes(o) ?? false;
            return (
              <button key={o} type="button" aria-pressed={on} onClick={() => onChange({ ...f, in: on ? (f.in ?? []).filter((x) => x !== o) : [...(f.in ?? []), o] })} className={`rounded-full border px-2 py-0.5 text-xs ${on ? "bg-primary text-primary-foreground" : ""}`}>
                {def.key === "zone" ? ZONE_LABEL[o] ?? o : o}
              </button>
            );
          })}
        </span>
      )}
      <button type="button" aria-label={`Remove ${def.label}`} onClick={onRemove} className="text-muted-foreground hover:text-foreground">
        <X className="size-4" />
      </button>
    </li>
  );
}

function ResultTable({ rows, sort, order, onSort }: { rows: ScanRow[]; sort: string; order: string; onSort: (key: string) => void }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm" aria-label="Screener results">
        <thead className="bg-muted">
          <tr>
            {COLUMNS.map((c) => (
              <th key={c.key} scope="col" aria-sort={sort === c.key ? (order === "asc" ? "ascending" : "descending") : "none"} className={`px-3 py-2 font-medium whitespace-nowrap ${c.align === "right" ? "text-right" : "text-left"}`}>
                <button type="button" onClick={() => onSort(c.key)} className="inline-flex items-center gap-1">
                  {c.label}
                  <span aria-hidden className="text-muted-foreground text-[10px]">{sort === c.key ? (order === "asc" ? "▲" : "▼") : "↕"}</span>
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.symbol} className="border-t">
              <td className="px-3 py-1.5 font-medium">
                <Link href={`/stocks/${r.symbol}`} className="hover:underline">{r.symbol}</Link>
              </td>
              <td className="max-w-48 truncate px-3 py-1.5">{s(r.name) ?? "—"}</td>
              <td className="tnum px-3 py-1.5 text-right">{inr(n(r.price))}</td>
              <td className="tnum px-3 py-1.5 text-right">{n(r.day_change_pct) == null ? "—" : signedPct(n(r.day_change_pct)! / 100, 2)}</td>
              <td className="px-3 py-1.5">{s(r.sector)?.replace(/_/g, " ") ?? "—"}</td>
              <td className="tnum px-3 py-1.5 text-right">{inr(n(r.fair_value))}</td>
              <td className="tnum px-3 py-1.5 text-right">{n(r.discount_pct) == null ? "—" : signedPct(n(r.discount_pct)! / 100, 1)}</td>
              <td className="px-3 py-1.5"><ZoneChip zone={s(r.zone)} /></td>
              <td className="px-3 py-1.5">{s(r.grade) ?? "—"}</td>
              <td className="px-3 py-1.5">{s(r.action) ? ACTION_LABEL[s(r.action)!] ?? s(r.action) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ResultCards({ rows }: { rows: ScanRow[] }) {
  return (
    <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3" aria-label="Screener cards">
      {rows.map((r) => (
        <li key={r.symbol} className="bg-card flex flex-col gap-2 rounded-xl border p-3 shadow-[var(--shadow-card)]">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <Link href={`/stocks/${r.symbol}`} className="font-semibold hover:underline">{r.symbol}</Link>
              <p className="text-muted-foreground truncate text-xs">{s(r.name)}</p>
            </div>
            <span className="text-sm font-medium">{s(r.grade) ?? "—"}</span>
          </div>
          <p className="flex flex-wrap items-baseline gap-2">
            <span className="tnum text-lg font-semibold">{inr(n(r.price))}</span>
            <span className="tnum text-xs">{n(r.day_change_pct) == null ? "" : signedPct(n(r.day_change_pct)! / 100, 2)}</span>
            <ZoneChip zone={s(r.zone)} />
            <span className="text-muted-foreground tnum text-xs">{n(r.discount_pct) == null ? "" : `${signedPct(n(r.discount_pct)! / 100, 0)} vs fair value`}</span>
          </p>
          <PillarMiniChart pillars={r.pillars as unknown as Pillar[]} />
        </li>
      ))}
    </ul>
  );
}

export function ScreenerScan() {
  const router = useRouter();
  const params = useSearchParams();
  const [fields, setFields] = useState<FieldsOut | null>(null);
  const [saved, setSaved] = useState<SavedScreen[]>([]);
  const [result, setResult] = useState<ScanOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [group, setGroup] = useState("All");
  const [query, setQuery] = useState("");
  const [name, setName] = useState("");

  const sort = params.get("sort") ?? "total_score";
  const order = (params.get("order") ?? "desc") as "asc" | "desc";
  const page = Number(params.get("page") ?? 1) || 1;
  const size = Number(params.get("size") ?? 20) || 20;
  const view = params.get("view") === "cards" ? "cards" : "table";
  const applied = useMemo(() => filtersFromParams(params), [params]);
  const [draft, setDraft] = useState<Filter[]>(applied);
  useEffect(() => setDraft(applied), [applied]);

  useEffect(() => {
    api<FieldsOut>("/screener/fields").then(setFields).catch((e) => setError(e instanceof ApiError ? e.detail : "fields unavailable"));
    api<SavedScreen[]>("/screens").then(setSaved).catch(() => undefined);
  }, []);

  const defs = useMemo(() => new Map((fields?.fields ?? []).map((f) => [f.key, f])), [fields]);

  // the scan itself follows the URL
  useEffect(() => {
    let live = true;
    setBusy(true);
    const q = paramsFromFilters(applied, { sort, order, page, page_size: size });
    api<ScanOut>(`/screener/scan?${q.toString()}`)
      .then((r) => live && (setResult(r), setError(null)))
      .catch((e) => live && setError(e instanceof ApiError ? e.detail : "scan failed"))
      .finally(() => live && setBusy(false));
    return () => {
      live = false;
    };
  }, [applied, sort, order, page, size]);

  const go = useCallback(
    (filters: Filter[], extra: Record<string, string | number | undefined>) => {
      const q = paramsFromFilters(filters, { sort, order, size: size === 20 ? undefined : size, view: view === "table" ? undefined : view, ...extra });
      router.replace(`/screener${q.toString() ? `?${q}` : ""}`);
    },
    [router, sort, order, size, view],
  );
  const runScan = () => go(draft.filter((f) => f.min != null || f.max != null || f.in?.length), { page: undefined });

  const addable = (fields?.fields ?? []).filter(
    (f) => !draft.some((d) => d.key === f.key) && (group === "All" || f.group === group) && (query === "" || f.label.toLowerCase().includes(query.toLowerCase()) || f.key.includes(query.toLowerCase())),
  );
  const tabs = [{ id: "All", label: "All" }, ...(fields?.groups ?? []).map((g) => ({ id: g, label: g }))];

  async function save() {
    if (!name.trim()) return setMsg("Name the screen first");
    const definition: Definition = { filters: applied, sort, order };
    try {
      await api("/screens", { method: "POST", body: JSON.stringify({ name: name.trim(), definition }) });
      setSaved(await api<SavedScreen[]>("/screens"));
      setMsg(`Saved “${name.trim()}”`);
    } catch (e) {
      setMsg(e instanceof ApiError ? e.detail : "save failed");
    }
  }
  function load(nm: string) {
    const sc = saved.find((x) => x.name === nm);
    if (!sc) return;
    const q = paramsFromFilters(sc.definition.filters, { sort: sc.definition.sort, order: sc.definition.order });
    router.replace(`/screener?${q}`);
    setName(nm);
  }
  function onSort(key: string) {
    go(applied, { sort: key, order: sort === key && order === "desc" ? "asc" : sort === key ? "desc" : key === "symbol" || key === "name" ? "asc" : "desc", page: undefined });
  }

  const totalPages = result ? Math.max(1, Math.ceil(result.total / result.page_size)) : 1;
  return (
    <Page>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">Screener</h1>
        <Link href="/screening-ideas" className="text-sm underline">Screening ideas</Link>
      </div>

      <Card>
        <CardContent className="flex flex-col gap-3">
          <div className="relative">
            <Search className="text-muted-foreground absolute top-2.5 left-2.5 size-4" aria-hidden />
            <input aria-label="Search parameters" placeholder="Search parameters (P/E, ROE, zone…)" value={query} onChange={(e) => setQuery(e.target.value)} className="bg-background w-full rounded-md border py-2 pr-3 pl-8 text-sm" />
          </div>
          <Tabs tabs={tabs} value={group} onChange={setGroup} label="Parameter groups" />
          <ul className="flex max-h-32 flex-wrap gap-1.5 overflow-auto" aria-label="Available parameters">
            {addable.map((f) => (
              <li key={f.key}>
                <button type="button" onClick={() => setDraft((d) => [...d, { key: f.key }])} className="hover:bg-secondary rounded-full border px-2.5 py-0.5 text-xs">
                  + {f.label}
                </button>
              </li>
            ))}
            {fields && addable.length === 0 && <li className="text-muted-foreground text-xs">No parameters match.</li>}
          </ul>
          {draft.length > 0 && (
            <ul className="flex flex-wrap gap-2" aria-label="Active filters">
              {draft.map((f) => {
                const def = defs.get(f.key);
                return def ? <FilterChip key={f.key} def={def} f={f} onChange={(nf) => setDraft((d) => d.map((x) => (x.key === f.key ? nf : x)))} onRemove={() => setDraft((d) => d.filter((x) => x.key !== f.key))} /> : null;
              })}
            </ul>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={runScan} disabled={busy}>Run scan</Button>
            <Button variant="outline" onClick={() => { setDraft([]); router.replace("/screener"); }}>Reset</Button>
            <span className="mx-1 hidden h-5 w-px bg-border sm:block" />
            <input aria-label="Screen name" placeholder="Screen name" value={name} onChange={(e) => setName(e.target.value)} className="bg-background w-40 rounded-md border px-2 py-1.5 text-sm" />
            <Button variant="outline" onClick={save}>Save</Button>
            {saved.length > 0 && (
              <select aria-label="Saved screens" value="" onChange={(e) => load(e.target.value)} className="bg-background rounded-md border px-2 py-1.5 text-sm">
                <option value="">Load saved…</option>
                {saved.map((x) => <option key={x.name} value={x.name}>{x.name}</option>)}
              </select>
            )}
            {msg && <span role="status" className="text-muted-foreground text-xs">{msg}</span>}
          </div>
        </CardContent>
      </Card>

      {error && <EmptyState title="Scan unavailable">{error}</EmptyState>}
      {!result && !error && <Skeleton className="h-64" />}
      {result && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
            <p>
              <strong data-testid="scan-total">{result.total}</strong> of {result.universe} stocks match
              <span className="text-muted-foreground"> · scan completed at {new Date(result.scanned_at).toLocaleTimeString("en-IN")}</span>
            </p>
            <div className="flex items-center gap-2">
              <div role="group" aria-label="View" className="flex gap-1">
                {(["table", "cards"] as const).map((v) => (
                  <Button key={v} size="sm" variant={view === v ? "default" : "outline"} aria-pressed={view === v} onClick={() => go(applied, { view: v === "table" ? undefined : v })}>
                    {v === "table" ? "Table" : "Cards"}
                  </Button>
                ))}
              </div>
              <label className="flex items-center gap-1 text-xs">
                Rows
                <select aria-label="Rows per page" value={size} onChange={(e) => go(applied, { size: Number(e.target.value) === 20 ? undefined : Number(e.target.value), page: undefined })} className="bg-background rounded-md border px-1.5 py-1">
                  {PAGE_SIZES.map((p) => <option key={p} value={p}>{p}</option>)}
                </select>
              </label>
            </div>
          </div>
          {result.rows.length === 0 ? (
            <EmptyState title="No stocks match">Loosen a filter. Stocks with no value for a filtered parameter are left out.</EmptyState>
          ) : view === "table" ? (
            <ResultTable rows={result.rows} sort={sort} order={order} onSort={onSort} />
          ) : (
            <ResultCards rows={result.rows} />
          )}
          <div className="flex items-center justify-between text-sm" aria-label="Pagination">
            <span className="text-muted-foreground">
              {result.total === 0 ? "0" : `${(result.page - 1) * result.page_size + 1}–${Math.min(result.total, result.page * result.page_size)}`} of {result.total}
            </span>
            <span className="flex items-center gap-2">
              <Button size="sm" variant="outline" disabled={result.page <= 1} onClick={() => go(applied, { page: result.page - 1 })}>Previous</Button>
              <span className="tnum text-xs">Page {result.page} / {totalPages}</span>
              <Button size="sm" variant="outline" disabled={result.page >= totalPages} onClick={() => go(applied, { page: result.page + 1 })}>Next</Button>
            </span>
          </div>
          <p className="text-muted-foreground text-xs">{num(result.universe, 0)} stored reports scanned. A stock with no value for a filtered parameter never matches it.</p>
        </>
      )}
    </Page>
  );
}
