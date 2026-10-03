"use client";

// Financials tab: annual / quarterly toggle, and for each statement a Recharts chart above a
// table of up to 12 columns (oldest -> newest) with YoY % rows and a source badge per column.
// A ⚑ under a column opens the reconciliation choice for that year and statement. Missing
// cells are "-" with the reason on hover, never 0.
import { Fragment, useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { EmptyState, Skeleton, Term } from "@/components/ds";
import { api, ApiError } from "@/lib/api";
import { num } from "@/lib/format";
import type { CoverageCell, CoverageGrid, StatementRow, StatementSection, Statements } from "@/lib/types";

import { CoverageTriage } from "./coverage-triage";

const SOURCE_BADGE: Record<string, string> = {
  nse: "XBRL",
  xbrl: "XBRL",
  indianapi: "API",
  screener: "Scr",
  yfinance: "yf",
  annual_report_pdf: "PDF",
  pdf: "PDF",
};
const STATEMENT: Record<StatementSection["id"], CoverageCell["statement"]> = { income: "P&L", balance: "BS", cashflow: "CF" };
const CHART_KEYS: Record<string, string[]> = {
  income: ["revenue", "ebitda", "pat", "nii"],
  balance: ["total_assets", "total_equity", "total_debt", "advances", "deposits"],
  cashflow: ["cfo", "fcf"],
};
const COLORS = ["var(--viz-s1)", "var(--viz-s2)", "var(--viz-s3)"];
const axisTick = { fontSize: 11, fill: "var(--viz-muted)" };

function fmtCell(v: number | null, unit: StatementRow["unit"]): string {
  if (v == null) return "-";
  return num(v, unit === "inr" ? 2 : 0);
}

export function yoyValues(values: (number | null)[], lag: number): (number | null)[] {
  return values.map((v, i) => {
    const prev = i >= lag ? values[i - lag] : null;
    return v != null && prev != null && prev > 0 ? (v / prev - 1) * 100 : null;
  });
}

function StatementChart({ section, columns }: { section: StatementSection; columns: Statements["columns"] }) {
  const rows = section.rows.filter((r) => (CHART_KEYS[section.id] ?? []).includes(r.key) && r.values.some((v) => v != null)).slice(0, 3);
  if (rows.length === 0) return null;
  const data = columns.map((c, i) => Object.fromEntries([["label", c.label], ...rows.map((r) => [r.key, r.values[i]])]));
  return (
    <div className="h-52 w-full" role="img" aria-label={`${section.title} chart: ${rows.map((r) => r.label).join(", ")}`}>
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--viz-grid)" vertical={false} />
          <XAxis dataKey="label" tick={axisTick} />
          <YAxis tick={axisTick} tickFormatter={(v: number) => num(v, 0)} width={56} />
          <Tooltip contentStyle={{ background: "var(--viz-surface)", borderColor: "var(--viz-grid)", fontSize: 12 }} formatter={(v) => (v == null ? "-" : num(Number(v), 0))} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          {rows.map((r, i) => (
            <Bar key={r.key} dataKey={r.key} name={`${r.label} (₹ Cr)`} fill={COLORS[i % COLORS.length]} radius={[2, 2, 0, 0]} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function StatementTable({
  section,
  columns,
  lag,
  flags,
  onFlag,
}: {
  section: StatementSection;
  columns: Statements["columns"];
  lag: number;
  flags: Map<number, number>;
  onFlag: (fy: number) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-xs tabular-nums" aria-label={section.title}>
        <thead className="text-muted-foreground">
          <tr>
            <th scope="col" className="bg-background sticky left-0 px-3 py-1.5 text-left font-normal">₹ Crore</th>
            {columns.map((c) => (
              <th key={c.period_end} scope="col" className="px-2 py-1.5 text-right font-medium whitespace-nowrap">
                {c.label}
                <div className="text-[10px] font-normal" title={c.derived ? "Summed from the year's four quarters" : c.vendor_reclassified ? "Vendor-reclassified (Indian API)" : `Source: ${c.source ?? "unknown"}`}>
                  <span className="bg-secondary rounded px-1">{c.derived ? "Σ Qtr" : c.vendor_reclassified ? "API" : (SOURCE_BADGE[c.source ?? ""] ?? c.source ?? "?")}</span>
                </div>
                {c.fiscal_year != null && (flags.get(c.fiscal_year) ?? 0) > 0 && (
                  <button type="button" onClick={() => onFlag(c.fiscal_year as number)} className="text-[10px] underline" aria-label={`${flags.get(c.fiscal_year)} value(s) to review for ${c.label}`}>
                    ⚑{flags.get(c.fiscal_year)}
                  </button>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {section.rows.map((r) => {
            const y = r.yoy ? yoyValues(r.values, lag) : null;
            return (
              <Fragment key={r.key}>
                <tr className="border-t">
                  <th scope="row" className="bg-background sticky left-0 px-3 py-1.5 text-left font-normal whitespace-nowrap">
                    <Term label={r.label} />
                  </th>
                  {r.values.map((v, i) => (
                    <td key={i} className="px-2 py-1.5 text-right">
                      {v == null ? (
                        <span tabIndex={0} title={r.reason ?? "not available"} aria-label={`${r.label} ${columns[i].label}: not available`} className="text-muted-foreground cursor-help">
                          -
                        </span>
                      ) : (
                        fmtCell(v, r.unit)
                      )}
                    </td>
                  ))}
                </tr>
                {y && (
                  <tr className="text-muted-foreground">
                    <th scope="row" className="bg-background sticky left-0 px-3 py-0.5 pl-6 text-left text-[11px] font-normal italic">
                      YoY %
                    </th>
                    {y.map((v, i) => (
                      <td key={i} className="px-2 py-0.5 text-right text-[11px] italic" title={v == null ? "needs a positive value in the comparison period" : undefined}>
                        {v == null ? "-" : `${v > 0 ? "+" : ""}${num(v, 1)}%`}
                      </td>
                    ))}
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function FinancialsTab({ symbol }: { symbol: string }) {
  const [period, setPeriod] = useState<"annual" | "quarterly">("annual");
  const [data, setData] = useState<Statements | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [grid, setGrid] = useState<CoverageGrid | null>(null);
  const [review, setReview] = useState<{ fy: number; statement: CoverageCell["statement"] } | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    setData(null);
    setError(null);
    api<Statements>(`/stocks/${encodeURIComponent(symbol)}/statements?period=${period}`)
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e instanceof ApiError ? e.detail : "statements unavailable"));
    return () => {
      live = false;
    };
  }, [symbol, period, reload]);
  useEffect(() => {
    api<CoverageGrid>(`/stocks/${encodeURIComponent(symbol)}/coverage`).then(setGrid).catch(() => setGrid(null));
  }, [symbol, reload]);

  const flags = useMemo(() => {
    const out: Record<string, Map<number, number>> = { "P&L": new Map(), BS: new Map(), CF: new Map() };
    const basis = grid?.bases.find((b) => b.basis === data?.basis) ?? grid?.bases[0];
    for (const c of basis?.cells ?? []) if (c.pending_review > 0) out[c.statement].set(c.fiscal_year, c.pending_review);
    return out;
  }, [grid, data]);

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div role="group" aria-label="Period" className="flex gap-1">
          {(["annual", "quarterly"] as const).map((p) => (
            <button key={p} type="button" aria-pressed={period === p} onClick={() => setPeriod(p)} className={`rounded-md border px-3 py-1 text-sm capitalize ${period === p ? "bg-primary text-primary-foreground" : ""}`}>
              {p}
            </button>
          ))}
        </div>
        {data && (
          <p className="text-muted-foreground text-xs">
            {data.basis ?? "no"} statements · {data.columns.length} {period === "annual" ? "years" : "quarters"}, oldest first · {data.model === "bank" ? "bank format" : "₹ crore unless noted"}
          </p>
        )}
      </div>
      {error ? (
        <EmptyState title="Statements unavailable">{error}</EmptyState>
      ) : !data ? (
        <Skeleton className="h-64 w-full" />
      ) : data.columns.length === 0 ? (
        <EmptyState title="No statements stored">Refresh the data or upload a Screener export.</EmptyState>
      ) : (
        data.sections.map((s) => (
          <section key={s.id} className="flex flex-col gap-2" aria-label={s.title}>
            <h3 className="text-sm font-semibold">{s.title}</h3>
            <StatementChart section={s} columns={data.columns} />
            <StatementTable section={s} columns={data.columns} lag={period === "annual" ? 1 : 4} flags={flags[STATEMENT[s.id]]} onFlag={(fy) => setReview({ fy, statement: STATEMENT[s.id] })} />
            {review && review.statement === STATEMENT[s.id] && (
              <CoverageTriage key={`${review.fy}-${review.statement}`} symbol={symbol} fiscalYear={review.fy} statement={review.statement} onDone={() => { setReview(null); setReload((n) => n + 1); }} />
            )}
          </section>
        ))
      )}
    </div>
  );
}
