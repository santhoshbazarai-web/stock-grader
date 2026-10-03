"use client";

// Compare tab: up to 4 stocks (this one plus peers from the same sector, or any stock found
// by search) side by side: grade, zone, fair-value gap, key ratios and the six pillar scores,
// a price chart rebased to 100 and a radar overlay of the pillars. "-" with a hover reason
// wherever a stock has no value.
import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { ActionBadge, GradeBadge } from "@/components/common";
import { EmptyState, Skeleton, Term } from "@/components/ds";
import { api, ApiError } from "@/lib/api";
import { inr, num, pct, signedPct, ZONE_LABEL } from "@/lib/format";
import type { ComparePrices, Peer, SearchHit, StockReport } from "@/lib/types";

const MAX = 4;
const COLORS = ["var(--viz-s1)", "var(--viz-s2)", "var(--viz-s3)", "var(--viz-s4)"];
const PILLARS = ["quality", "growth", "valuation", "health", "governance", "technical"] as const;
const axisTick = { fontSize: 11, fill: "var(--viz-muted)" };

type Row = { label: string; glossary?: string; get: (r: StockReport) => string | null; reason: string };
const fv = (r: StockReport) => r.levels.fair_value;
const f = (r: StockReport, k: string) => r.fundamentals[k] ?? null;
const ROWS: Row[] = [
  { label: "Grade", get: (r) => r.grade_label, reason: "no grade yet" },
  { label: "Zone", get: (r) => (r.zone ? ZONE_LABEL[r.zone] : null), reason: "no zone yet" },
  { label: "Price", get: (r) => inr(r.cmp), reason: "no price" },
  { label: "Fair value", glossary: "fair_value", get: (r) => (fv(r) == null ? null : inr(fv(r))), reason: "no fair value could be computed" },
  { label: "Fair-value gap", glossary: "discount_pct", get: (r) => (fv(r) ? signedPct(r.cmp / (fv(r) as number) - 1) : null), reason: "needs a fair value" },
  { label: "P/E", get: (r) => (r.peer_stats?.pe == null ? null : `${num(r.peer_stats.pe, 1)}x`), reason: "needs positive earnings" },
  { label: "P/B", get: (r) => (r.peer_stats?.pb == null ? null : `${num(r.peer_stats.pb, 2)}x`), reason: "needs book value" },
  { label: "ROE", get: (r) => (f(r, "roe_latest") == null ? null : pct(f(r, "roe_latest"), 1)), reason: "needs equity history" },
  { label: "ROCE", get: (r) => (f(r, "roce_latest") == null ? null : pct(f(r, "roce_latest"), 1)), reason: "not used for banks / needs capital employed" },
  { label: "Sales CAGR 5y", glossary: "sales_cagr_5y", get: (r) => (f(r, "sales_cagr_5y") == null ? null : pct(f(r, "sales_cagr_5y"), 1)), reason: "needs 5 years of sales" },
  { label: "Debt / equity", get: (r) => (f(r, "debt_to_equity") == null ? null : `${num(f(r, "debt_to_equity"), 2)}x`), reason: "not used for banks / needs debt and equity" },
  ...PILLARS.map((p) => ({
    label: `${p[0].toUpperCase()}${p.slice(1)} score`,
    glossary: `${p}_pillar`,
    get: (r: StockReport) => (r.scores[p] == null ? null : num(r.scores[p], 0)),
    reason: "pillar not scored for lack of data",
  })),
  { label: "Action", get: (r) => r.action, reason: "no action yet" },
];

export function CompareTab({ symbol }: { symbol: string }) {
  const [syms, setSyms] = useState<string[]>([symbol]);
  const [reports, setReports] = useState<Record<string, StockReport | string>>({});
  const [prices, setPrices] = useState<ComparePrices | null>(null);
  const [peers, setPeers] = useState<Peer[]>([]);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const asked = useRef(new Set<string>());

  useEffect(() => {
    api<Peer[]>(`/compare/peers/${encodeURIComponent(symbol)}`).then(setPeers).catch(() => setPeers([]));
  }, [symbol]);
  useEffect(() => {
    for (const s of syms) {
      if (asked.current.has(s)) continue;
      asked.current.add(s);
      api<StockReport>(`/stocks/${encodeURIComponent(s)}/report`)
        .then((r) => setReports((p) => ({ ...p, [s]: r })))
        .catch((e) => setReports((p) => ({ ...p, [s]: e instanceof ApiError ? e.detail : "report unavailable" })));
    }
    if (syms.length > 1) api<ComparePrices>(`/compare/prices?symbols=${syms.join(",")}&years=3`).then(setPrices).catch(() => setPrices(null));
    else setPrices(null);
  }, [syms]);
  useEffect(() => {
    const t = q.trim();
    if (!t) return setHits([]);
    const h = setTimeout(() => api<SearchHit[]>(`/stocks/search?q=${encodeURIComponent(t)}&limit=6`).then((x) => setHits(x.filter((y) => y.symbol && !syms.includes(y.symbol)))).catch(() => setHits([])), 150);
    return () => clearTimeout(h);
  }, [q, syms]);

  const add = (s: string) => {
    if (syms.length < MAX && !syms.includes(s)) setSyms([...syms, s]);
    setQ("");
    setHits([]);
  };
  const loaded = syms.map((s) => ({ s, r: reports[s] }));
  const ok = loaded.filter((x): x is { s: string; r: StockReport } => typeof x.r === "object");
  const merged = new Map<string, Record<string, number | string>>();
  for (const s of prices?.series ?? []) for (const p of s.points) merged.set(p.date, { ...(merged.get(p.date) ?? { date: p.date }), [s.symbol]: p.value });
  const priceData = [...merged.values()].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const radar = PILLARS.map((p) => ({ pillar: p, ...Object.fromEntries(ok.map(({ s, r }) => [s, r.scores[p]])) }));
  const suggestions = peers.filter((p) => !syms.includes(p.symbol));

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center gap-2" aria-label="Stocks compared">
        {syms.map((s, i) => (
          <span key={s} className="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-sm" style={{ borderColor: COLORS[i] }}>
            <span aria-hidden className="size-2 rounded-full" style={{ background: COLORS[i] }} />
            {s}
            {s !== symbol && (
              <button type="button" aria-label={`Remove ${s}`} onClick={() => setSyms(syms.filter((x) => x !== s))}>
                <X className="size-3" />
              </button>
            )}
          </span>
        ))}
        {syms.length < MAX && (
          <div className="relative">
            <input aria-label="Add a stock to compare" placeholder="Add a stock…" value={q} onChange={(e) => setQ(e.target.value)} className="border-input bg-background h-8 w-48 rounded-md border px-2 text-sm" />
            {hits.length > 0 && (
              <ul className="bg-popover absolute z-20 mt-1 w-72 rounded-md border shadow-lg" aria-label="Search results">
                {hits.map((h) => (
                  <li key={h.symbol}>
                    <button type="button" className="hover:bg-secondary flex w-full gap-2 px-3 py-1.5 text-left text-sm" onClick={() => add(h.symbol as string)}>
                      <span className="font-medium">{h.symbol}</span>
                      <span className="text-muted-foreground truncate text-xs">{h.name}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
      {syms.length < MAX && suggestions.length > 0 && (
        <p className="text-sm" aria-label="Suggested peers">
          <span className="text-muted-foreground">Peers in the same sector: </span>
          {suggestions.slice(0, 5).map((p) => (
            <button key={p.symbol} type="button" onClick={() => add(p.symbol)} className="bg-secondary mr-1.5 rounded-full px-2.5 py-0.5 text-xs" title={p.name ?? undefined}>
              + {p.symbol}
            </button>
          ))}
        </p>
      )}
      {loaded.some((x) => x.r === undefined) && <Skeleton className="h-40 w-full" />}
      {loaded.filter((x) => typeof x.r === "string").map((x) => (
        <p key={x.s} role="alert" className="text-destructive text-sm">
          {x.s}: {x.r as string}
        </p>
      ))}
      {ok.length > 0 && (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm tabular-nums" aria-label="Comparison">
            <thead>
              <tr className="text-muted-foreground text-xs">
                <th scope="col" className="px-3 py-2 text-left font-normal" />
                {ok.map(({ s, r }) => (
                  <th key={s} scope="col" className="px-3 py-2 text-right font-medium">
                    <span className="text-foreground">{s}</span>
                    <div className="ml-auto max-w-40 truncate text-right text-xs font-normal">{r.name}</div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ROWS.map((row) => (
                <tr key={row.label} className="border-t">
                  <th scope="row" className="text-muted-foreground px-3 py-1.5 text-left font-normal">
                    <Term label={row.label} k={row.glossary} />
                  </th>
                  {ok.map(({ s, r }) => {
                    const v = row.get(r);
                    return (
                      <td key={s} className="px-3 py-1.5 text-right">
                        {v == null ? (
                          <span tabIndex={0} title={row.reason} aria-label={`${row.label} for ${s}: not available, ${row.reason}`} className="text-muted-foreground cursor-help">
                            -
                          </span>
                        ) : row.label === "Grade" ? (
                          <GradeBadge label={v} />
                        ) : row.label === "Action" ? (
                          <ActionBadge action={v} />
                        ) : (
                          v
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {syms.length < 2 ? (
        <EmptyState title="Pick another stock">Add up to three more stocks to see the price and pillar charts.</EmptyState>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <figure className="flex flex-col gap-1" aria-label="Normalised price chart">
            <figcaption className="text-sm font-semibold">Price, rebased to 100 {prices?.base_date ? `(${prices.base_date})` : ""}</figcaption>
            {priceData.length === 0 ? (
              <p className="text-muted-foreground text-sm">No overlapping price history for these stocks.</p>
            ) : (
              <div className="h-64">
                <ResponsiveContainer>
                  <LineChart data={priceData}>
                    <CartesianGrid stroke="var(--viz-grid)" vertical={false} />
                    <XAxis dataKey="date" tick={axisTick} minTickGap={48} />
                    <YAxis tick={axisTick} width={40} domain={["auto", "auto"]} />
                    <Tooltip contentStyle={{ background: "var(--viz-surface)", borderColor: "var(--viz-grid)", fontSize: 12 }} formatter={(v) => num(Number(v), 1)} />
                    <Legend wrapperStyle={{ fontSize: 12 }} />
                    {syms.map((s, i) => (
                      <Line key={s} dataKey={s} stroke={COLORS[i]} dot={false} strokeWidth={2} connectNulls />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </figure>
          <figure className="flex flex-col gap-1" aria-label="Pillar radar">
            <figcaption className="text-sm font-semibold">Pillar scores</figcaption>
            <div className="h-64">
              <ResponsiveContainer>
                <RadarChart data={radar}>
                  <PolarGrid stroke="var(--viz-grid)" />
                  <PolarAngleAxis dataKey="pillar" tick={axisTick} />
                  <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
                  {ok.map(({ s }, i) => (
                    <Radar key={s} name={s} dataKey={s} stroke={COLORS[syms.indexOf(s)] ?? COLORS[i]} fill={COLORS[syms.indexOf(s)] ?? COLORS[i]} fillOpacity={0.12} />
                  ))}
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                </RadarChart>
              </ResponsiveContainer>
            </div>
          </figure>
        </div>
      )}
    </div>
  );
}
