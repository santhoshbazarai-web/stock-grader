"use client";

// 10-year fundamentals (SPEC §9) as small multiples, one measure per chart on its own axis
// (never a dual axis): sales, EBITDA, PAT, CFO, FCF as bars; ROCE and CCC as lines. Plus the
// shareholding trend (4 series, with a legend). Missing years are gaps, never zeros. A table
// view carries every number.
import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { num } from "@/lib/format";
import type { FundamentalsHistory, YearPoint } from "@/lib/types";

type Metric = {
  key: keyof Omit<YearPoint, "fiscal_year">;
  title: string;
  unit: string;
  kind: "bar" | "line";
  scale: number; // display multiplier (ROCE fraction → %)
};

export const METRICS: Metric[] = [
  { key: "revenue", title: "Sales", unit: "₹ Cr", kind: "bar", scale: 1 },
  { key: "ebitda", title: "EBITDA", unit: "₹ Cr", kind: "bar", scale: 1 },
  { key: "pat", title: "PAT", unit: "₹ Cr", kind: "bar", scale: 1 },
  { key: "cfo", title: "Cash from operations", unit: "₹ Cr", kind: "bar", scale: 1 },
  { key: "fcf", title: "Free cash flow", unit: "₹ Cr", kind: "bar", scale: 1 },
  { key: "roce", title: "ROCE", unit: "%", kind: "line", scale: 100 },
  { key: "ccc_days", title: "Cash conversion cycle", unit: "days", kind: "line", scale: 1 },
];

const SHP = [
  { key: "promoter_pct", label: "Promoter", color: "var(--viz-s1)" },
  { key: "fii_pct", label: "FII", color: "var(--viz-s2)" },
  { key: "dii_pct", label: "DII (incl. MF)", color: "var(--viz-s3)" },
  { key: "public_pct", label: "Public", color: "var(--viz-s4)" },
] as const;

const axisTick = { fontSize: 11, fill: "var(--viz-muted)" };
const tooltipStyle = { background: "var(--viz-surface)", borderColor: "var(--viz-grid)", fontSize: 12 };

/** Y domain that never zooms into noise: at least ±5% of the magnitude (or ±1) around flat data. */
export function lineDomain(values: number[]): [number, number] {
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const minSpan = Math.max(Math.abs(lo), Math.abs(hi)) * 0.1 || 2;
  if (hi - lo >= minSpan) {
    const pad = (hi - lo) * 0.1;
    return [Math.floor(lo - pad), Math.ceil(hi + pad)];
  }
  const mid = (lo + hi) / 2;
  return [Math.floor(mid - minSpan / 2), Math.ceil(mid + minSpan / 2)];
}

export function seriesFor(years: YearPoint[], m: Metric) {
  return years.map((y) => ({
    fy: `FY${String(y.fiscal_year).slice(-2)}`,
    value: y[m.key] == null ? null : (y[m.key] as number) * m.scale,
  }));
}

function SmallMultiple({ years, m }: { years: YearPoint[]; m: Metric }) {
  const data = seriesFor(years, m);
  const values = data.map((d) => d.value).filter((v): v is number => v != null);
  const last = [...data].reverse().find((d) => d.value != null);
  const hasNegative = values.some((v) => v < 0);
  return (
    <figure className="flex flex-col gap-1">
      <figcaption className="flex items-baseline justify-between text-xs">
        <span className="font-medium">
          {m.title} <span className="text-muted-foreground font-normal">({m.unit})</span>
        </span>
        <span className="text-muted-foreground tabular-nums">
          {last ? `${last.fy} ${num(last.value, m.unit === "%" ? 1 : 0)}` : "no data"}
        </span>
      </figcaption>
      <div className="h-32 w-full">
        {values.length === 0 ? (
          <div className="text-muted-foreground flex h-full items-center justify-center text-xs">No data on file</div>
        ) : (
          <ResponsiveContainer>
            {m.kind === "bar" ? (
              <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }} barCategoryGap={2}>
                <CartesianGrid vertical={false} stroke="var(--viz-grid)" />
                <XAxis dataKey="fy" tick={axisTick} tickLine={false} axisLine={{ stroke: "var(--viz-axis)" }} interval="preserveStartEnd" />
                <YAxis tick={axisTick} tickLine={false} axisLine={false} width={48} tickFormatter={(v) => num(v, 0)} />
                {hasNegative && <ReferenceLine y={0} stroke="var(--viz-axis)" />}
                <Tooltip cursor={{ fill: "var(--viz-grid)" }} contentStyle={tooltipStyle} formatter={(v) => [num(Number(v), 0), m.title]} />
                <Bar dataKey="value" fill="var(--viz-s1)" radius={hasNegative ? 0 : [4, 4, 0, 0]} maxBarSize={28} isAnimationActive={false} />
              </BarChart>
            ) : (
              <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--viz-grid)" />
                <XAxis dataKey="fy" tick={axisTick} tickLine={false} axisLine={{ stroke: "var(--viz-axis)" }} interval="preserveStartEnd" />
                <YAxis tick={axisTick} tickLine={false} axisLine={false} width={48} tickFormatter={(v) => num(v, 0)} domain={lineDomain(values)} allowDecimals={false} />
                <Tooltip contentStyle={tooltipStyle} formatter={(v) => [`${num(Number(v), 1)} ${m.unit}`, m.title]} />
                <Line dataKey="value" stroke="var(--viz-s1)" strokeWidth={2} dot={{ r: 3 }} connectNulls={false} isAnimationActive={false} />
              </LineChart>
            )}
          </ResponsiveContainer>
        )}
      </div>
    </figure>
  );
}

function FundamentalsTable({ h }: { h: FundamentalsHistory }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs tabular-nums" aria-label="10-year fundamentals">
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-1 pr-2 text-left font-normal">Metric</th>
            {h.years.map((y) => (
              <th key={y.fiscal_year} className="px-1 py-1 text-right font-normal">
                FY{String(y.fiscal_year).slice(-2)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {METRICS.map((m) => (
            <tr key={m.key} className="border-t">
              <th scope="row" className="py-1 pr-2 text-left font-normal whitespace-nowrap">
                {m.title} ({m.unit})
              </th>
              {seriesFor(h.years, m).map((d) => (
                <td key={d.fy} className="px-1 py-1 text-right">
                  {num(d.value, m.unit === "%" ? 1 : 0)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Shareholding({ h, table }: { h: FundamentalsHistory; table: boolean }) {
  if (h.shareholding.length === 0) {
    return <p className="text-muted-foreground text-sm">No shareholding filings on file.</p>;
  }
  const data = h.shareholding.map((s) => ({ ...s, q: s.period_end.slice(0, 7) }));
  if (table) {
    return (
      <div className="overflow-x-auto">
        <table className="w-full text-xs tabular-nums" aria-label="Shareholding">
          <thead className="text-muted-foreground">
            <tr>
              <th className="py-1 text-left font-normal">Quarter</th>
              {SHP.map((s) => (
                <th key={s.key} className="py-1 text-right font-normal">
                  {s.label} %
                </th>
              ))}
              <th className="py-1 text-right font-normal">Pledged %</th>
            </tr>
          </thead>
          <tbody>
            {data.map((d) => (
              <tr key={d.period_end} className="border-t">
                <td className="py-1">{d.q}</td>
                {SHP.map((s) => (
                  <td key={s.key} className="py-1 text-right">
                    {num(d[s.key], 2)}
                  </td>
                ))}
                <td className="py-1 text-right">{num(d.promoter_pledge_pct, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  const lastPledge = data.at(-1)?.promoter_pledge_pct;
  return (
    <figure className="flex flex-col gap-1">
      <figcaption className="flex items-baseline justify-between text-xs">
        <span className="font-medium">
          Shareholding <span className="text-muted-foreground font-normal">(% of equity)</span>
        </span>
        <span className="text-muted-foreground">Promoter pledge: {lastPledge == null ? "—" : `${num(lastPledge, 2)}%`}</span>
      </figcaption>
      <div className="h-56 w-full">
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--viz-grid)" />
            <XAxis dataKey="q" tick={axisTick} tickLine={false} axisLine={{ stroke: "var(--viz-axis)" }} />
            <YAxis tick={axisTick} tickLine={false} axisLine={false} width={36} domain={[0, "auto"]} />
            <Tooltip contentStyle={tooltipStyle} formatter={(v, n) => [`${num(Number(v), 2)}%`, n]} />
            <Legend iconType="plainline" itemSorter={null} wrapperStyle={{ fontSize: 12 }} />
            {SHP.map((s) => (
              <Line key={s.key} dataKey={s.key} name={s.label} stroke={s.color} strokeWidth={2} dot={{ r: 3 }} isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

export function FundamentalsCharts({ symbol }: { symbol: string }) {
  const [h, setH] = useState<FundamentalsHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [table, setTable] = useState(false);

  useEffect(() => {
    let live = true;
    api<FundamentalsHistory>(`/stocks/${symbol}/fundamentals`)
      .then((d) => live && setH(d))
      .catch((e) => live && setError(e instanceof ApiError ? e.detail : "unavailable"));
    return () => {
      live = false;
    };
  }, [symbol]);

  if (error) return <p className="text-destructive text-sm">Fundamentals unavailable: {error}</p>;
  if (!h) return <p className="text-muted-foreground text-sm">Loading…</p>;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-2">
        <p className="text-muted-foreground text-xs">
          {h.statement_type ?? "no"} statements · source {h.source ?? "—"}
          {h.missing.length > 0 && ` · no data: ${h.missing.join(", ")}`}
        </p>
        <Button size="sm" variant="outline" onClick={() => setTable((t) => !t)} aria-pressed={table}>
          {table ? "Charts" : "Table"}
        </Button>
      </div>
      {h.years.length === 0 ? (
        <p className="text-muted-foreground text-sm">No annual financials on file (upload a Screener export).</p>
      ) : table ? (
        <FundamentalsTable h={h} />
      ) : (
        <div className="grid grid-cols-1 gap-x-6 gap-y-5 sm:grid-cols-2 lg:grid-cols-4">
          {METRICS.map((m) => (
            <SmallMultiple key={m.key} years={h.years} m={m} />
          ))}
        </div>
      )}
      <Shareholding h={h} table={table} />
    </div>
  );
}
