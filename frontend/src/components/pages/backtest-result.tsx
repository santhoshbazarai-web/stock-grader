"use client";

// One backtest (SPEC §11): progress while it runs; then headline metrics against the
// Nifty 500, the equity curve (both rebased to 100, one axis), and the grade x zone table.
// Each cell is its own portfolio of that cell's signals, coloured on the diverging pair by
// CAGR minus the benchmark's (blue = beat it, red = lagged it). Every cell carries its
// numbers, so colour is never the only channel.
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Empty, ErrorText, Page } from "@/components/common";
import { gradeText, rulesText, StatusText } from "@/components/pages/backtests";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { DASH, num, pct, signedPct, ZONE_LABEL } from "@/lib/format";
import { GRADES, ZONES } from "@/lib/screener";
import type { Backtest, BacktestCell, BacktestResults } from "@/lib/types";

const POLL_MS = 3000;
const axisTick = { fontSize: 11, fill: "var(--viz-muted)" };
const tooltipStyle = { background: "var(--viz-surface)", borderColor: "var(--viz-grid)", fontSize: 12 };

/** "NIFTY500 (price index; TRI not stored)" → "NIFTY500"; the full label is in the caveats. */
export const benchmarkShort = (label: string) => label.split(" (")[0];
/** Portfolio first in legends and tooltips (Recharts sorts by name otherwise). */
const portfolioFirst = (item: { dataKey?: unknown }) => (item.dataKey === "portfolio" ? 0 : 1);

/** Excess CAGR (cell minus benchmark, in fractions) → diverging bin. */
export const EXCESS_BINS = [
  { min: 0.05, bg: "var(--viz-div-neg-2)", label: "≥ 5 pts ahead", strong: true },
  { min: 0.01, bg: "var(--viz-div-neg-1)", label: "1–5 pts ahead", strong: false },
  { min: -0.01, bg: "var(--viz-div-mid)", label: "within ±1 pt", strong: false },
  { min: -0.05, bg: "var(--viz-div-pos-1)", label: "1–5 pts behind", strong: false },
  { min: -Infinity, bg: "var(--viz-div-pos-2)", label: "≥ 5 pts behind", strong: true },
] as const;

export function excessBin(cagr: number, benchmark: number) {
  const x = cagr - benchmark;
  return EXCESS_BINS.find((b) => x >= b.min) ?? EXCESS_BINS[EXCESS_BINS.length - 1];
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-lg border p-3">
      <span className="text-muted-foreground text-xs">{label}</span>
      <span className="text-xl font-semibold tabular-nums">{value}</span>
      {sub && <span className="text-muted-foreground text-xs tabular-nums">{sub}</span>}
    </div>
  );
}

function Headline({ r }: { r: BacktestResults }) {
  const p = r.portfolio;
  const b = r.benchmark;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6" aria-label="Headline metrics">
      <Tile label="CAGR" value={signedPct(p.cagr)} sub={`Nifty 500 ${signedPct(b.cagr)}`} />
      <Tile label="Total return" value={signedPct(p.total_return)} sub={`Nifty 500 ${signedPct(b.total_return)}`} />
      <Tile label="Max drawdown" value={pct(p.max_drawdown)} sub={`Nifty 500 ${pct(b.max_drawdown)}`} />
      <Tile label="Hit rate" value={pct(p.hit_rate, 0)} sub={`avg trade ${signedPct(p.avg_trade_return)}`} />
      <Tile label="Avg holding" value={p.avg_holding_days == null ? DASH : `${num(p.avg_holding_days, 0)} sessions`} />
      <Tile label="Trades" value={num(p.trades, 0)} sub={`avg invested ${pct(p.exposure, 0)}`} />
    </div>
  );
}

function EquityCurve({ r }: { r: BacktestResults }) {
  const [table, setTable] = useState(false);
  const hasBench = r.equity.some((e) => e.benchmark != null);
  const benchName = benchmarkShort(r.benchmark.label);
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Equity curve vs Nifty 500 (rebased to 100)</CardTitle>
        <Button size="sm" variant="outline" onClick={() => setTable((t) => !t)} aria-pressed={table}>
          {table ? "Chart" : "Table"}
        </Button>
      </CardHeader>
      <CardContent>
        {r.equity.length === 0 ? (
          <Empty>No equity points.</Empty>
        ) : table ? (
          <div className="max-h-80 overflow-auto">
            <table className="w-full text-xs tabular-nums" aria-label="Equity curve">
              <thead className="text-muted-foreground bg-background sticky top-0">
                <tr>
                  <th className="py-1 text-left font-normal">Week</th>
                  <th className="py-1 text-right font-normal">Portfolio</th>
                  <th className="py-1 text-right font-normal">{benchmarkShort(r.benchmark.label)}</th>
                </tr>
              </thead>
              <tbody>
                {r.equity.map((e) => (
                  <tr key={e.time} className="border-t">
                    <td className="py-0.5">{e.time}</td>
                    <td className="py-0.5 text-right">{num(e.portfolio, 2)}</td>
                    <td className="py-0.5 text-right">{num(e.benchmark, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <figure className="flex flex-col gap-1">
            <div className="h-72 w-full" role="img" aria-label="Equity curve chart: portfolio vs Nifty 500">
              <ResponsiveContainer>
                <LineChart data={r.equity} margin={{ top: 4, right: 12, bottom: 0, left: 0 }}>
                  <CartesianGrid vertical={false} stroke="var(--viz-grid)" />
                  <XAxis dataKey="time" tick={axisTick} tickLine={false} axisLine={{ stroke: "var(--viz-axis)" }} minTickGap={48} tickFormatter={(t: string) => t.slice(0, 7)} />
                  <YAxis tick={axisTick} tickLine={false} axisLine={false} width={44} domain={["auto", "auto"]} tickFormatter={(v) => num(v, 0)} />
                  <ReferenceLine y={100} stroke="var(--viz-axis)" strokeDasharray="3 3" />
                  <Tooltip contentStyle={tooltipStyle} itemSorter={portfolioFirst} formatter={(v, name) => [num(Number(v), 2), name]} />
                  <Legend wrapperStyle={{ fontSize: 12 }} itemSorter={portfolioFirst} />
                  <Line name="Portfolio" dataKey="portfolio" stroke="var(--viz-s1)" strokeWidth={2} dot={false} isAnimationActive={false} />
                  {hasBench && (
                    <Line name={benchName} dataKey="benchmark" stroke="var(--viz-s2)" strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
                  )}
                </LineChart>
              </ResponsiveContainer>
            </div>
            {!hasBench && <figcaption className="text-muted-foreground text-xs">No benchmark prices stored for this period.</figcaption>}
          </figure>
        )}
      </CardContent>
    </Card>
  );
}

function CellView({ c, bench }: { c: BacktestCell; bench: number | null }) {
  const label = `${gradeText(c.grade)} × ${ZONE_LABEL[c.zone]}`;
  if (c.trades === 0 || c.cagr == null) {
    return (
      <td className="text-muted-foreground rounded-sm border border-dashed p-2 text-center text-xs" title={`${label}: no trades`}>
        {c.signals ? `${c.signals} signals, no trades` : "no signals"}
      </td>
    );
  }
  const ref = bench ?? 0;
  const bin = excessBin(c.cagr, ref);
  return (
    <td
      className="rounded-sm p-2 text-center"
      title={`${label}: CAGR ${signedPct(c.cagr)} (${signedPct(c.cagr - ref)} vs ${bench == null ? "zero" : "Nifty 500"}), hit rate ${pct(c.hit_rate, 0)}, max DD ${pct(c.max_drawdown)}, ${c.trades} trades, avg hold ${num(c.avg_holding_days, 0)} sessions${c.selected ? " — in your rules" : ""}`}
      style={{
        background: bin.bg,
        color: bin.strong ? "#ffffff" : "var(--viz-ink)",
        boxShadow: c.selected ? "inset 0 0 0 2px var(--viz-ink)" : undefined,
      }}
    >
      <div className="text-sm font-semibold tabular-nums">{signedPct(c.cagr)}</div>
      <div className="text-[11px] tabular-nums opacity-90">
        hit {pct(c.hit_rate, 0)} · {c.trades} tr
      </div>
      <div className="text-[11px] tabular-nums opacity-90">
        DD {pct(c.max_drawdown, 0)} · {num(c.avg_holding_days, 0)}d
      </div>
    </td>
  );
}

function CellTable({ r }: { r: BacktestResults }) {
  const byKey = new Map(r.cells.map((c) => [`${c.grade}|${c.zone}`, c]));
  const bench = r.benchmark.cagr;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Results by grade × zone</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <p className="text-muted-foreground text-xs">
          Each cell is a separate portfolio of that cell&apos;s monthly signals under the same holding period and costs: CAGR, hit
          rate, trades, max drawdown and average holding period. Ringed cells are in your rules.
        </p>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] border-separate border-spacing-[3px]" aria-label="Results by grade and zone">
            <thead>
              <tr>
                <th className="text-muted-foreground p-1 text-left text-xs font-normal">Grade ↓ / Zone →</th>
                {ZONES.map((z) => (
                  <th key={z} scope="col" className="text-muted-foreground p-1 text-xs font-normal">
                    {ZONE_LABEL[z]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {GRADES.map((g) => (
                <tr key={g}>
                  <th scope="row" className="p-1 text-left text-sm font-medium">
                    {gradeText(g)}
                  </th>
                  {ZONES.map((z) => {
                    const c = byKey.get(`${g}|${z}`);
                    return c ? <CellView key={z} c={c} bench={bench} /> : <td key={z} />;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ul className="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 text-[11px]" aria-label="Cell colour legend">
          <li>CAGR vs {bench == null ? "zero (no benchmark)" : `Nifty 500 (${signedPct(bench)})`}:</li>
          {EXCESS_BINS.map((b) => (
            <li key={b.label} className="flex items-center gap-1">
              <span aria-hidden className="inline-block h-3 w-3 rounded-[2px]" style={{ background: b.bg }} />
              {b.label}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function Trades({ r }: { r: BacktestResults }) {
  if (r.trades_sample.length === 0) return null;
  return (
    <details className="rounded-lg border p-3">
      <summary className="cursor-pointer text-sm font-medium">Trades (last {r.trades_sample.length})</summary>
      <div className="mt-2 max-h-80 overflow-auto">
        <table className="w-full text-xs tabular-nums" aria-label="Trades">
          <thead className="text-muted-foreground bg-background sticky top-0">
            <tr>
              <th className="py-1 text-left font-normal">Symbol</th>
              <th className="py-1 text-left font-normal">Entry</th>
              <th className="py-1 text-left font-normal">Exit</th>
              <th className="py-1 text-right font-normal">Sessions</th>
              <th className="py-1 text-right font-normal">Net return</th>
              <th className="py-1 pl-2 text-left font-normal">Closed by</th>
            </tr>
          </thead>
          <tbody>
            {r.trades_sample.map((t, i) => (
              <tr key={`${t.symbol}-${t.entry}-${i}`} className="border-t">
                <td className="py-0.5">
                  <Link href={`/stocks/${encodeURIComponent(t.symbol)}`} className="hover:underline">
                    {t.symbol}
                  </Link>
                </td>
                <td className="py-0.5">{t.entry}</td>
                <td className="py-0.5">{t.exit}</td>
                <td className="py-0.5 text-right">{t.days}</td>
                <td className="py-0.5 text-right">{signedPct(t.return)}</td>
                <td className="py-0.5 pl-2">{t.closed_by.replace("_", " ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function Caveats({ r }: { r: BacktestResults }) {
  const failures = Object.entries(r.failures);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Method and caveats</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <p className="text-muted-foreground">
          {r.period.rebalances} monthly rebalances from {r.period.start} to {r.period.end}; universe{" "}
          {r.universe.source === "symbols" ? "a fixed symbol list" : "point-in-time index membership"}, on average{" "}
          {num(r.universe.avg_size, 0)} stocks ({r.universe.stocks_with_data} with price data). Fundamentals are used only after
          their announcement date; prices are split/bonus adjusted.
        </p>
        <ul className="list-disc pl-5" aria-label="Caveats">
          {r.caveats.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
        {(r.notes.length > 0 || failures.length > 0) && (
          <details>
            <summary className="text-muted-foreground cursor-pointer">
              Data notes ({r.notes.length}) and failures ({failures.length})
            </summary>
            <ul className="text-muted-foreground mt-1 list-disc pl-5 text-xs">
              {r.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
              {failures.map(([k, v]) => (
                <li key={k}>
                  {k}: {v}
                </li>
              ))}
            </ul>
          </details>
        )}
      </CardContent>
    </Card>
  );
}

function Progress({ b }: { b: Backtest }) {
  const p = b.results?.progress;
  const frac = p && p.total > 0 ? p.done / p.total : null;
  return (
    <Card>
      <CardContent className="flex flex-col gap-2 py-6">
        <p className="text-sm">
          <StatusText status={b.status} progress={p} />
          {b.status === "queued" && <span className="text-muted-foreground"> — the worker picks it up within a minute.</span>}
        </p>
        <div
          className="bg-secondary h-2 w-full overflow-hidden rounded-full"
          role="progressbar"
          aria-label="Backtest progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={frac == null ? undefined : Math.round(frac * 100)}
        >
          <div className="bg-primary h-full transition-all" style={{ width: `${Math.round((frac ?? 0) * 100)}%` }} />
        </div>
      </CardContent>
    </Card>
  );
}

function isDone(b: Backtest): b is Backtest & { results: BacktestResults } {
  return b.status === "done" && !!b.results && Array.isArray(b.results.cells);
}

export function BacktestResult({ id }: { id: string }) {
  const [b, setB] = useState<Backtest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api<Backtest>(`/backtests/${encodeURIComponent(id)}`)
      .then((x) => {
        setB(x);
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, [id]);
  useEffect(load, [load]);
  const pending = b?.status === "queued" || b?.status === "running";
  useEffect(() => {
    if (!pending) return;
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [pending, load]);

  return (
    <Page>
      <div className="flex flex-col gap-1">
        <Link href="/backtests" className="text-muted-foreground text-xs hover:underline">
          ← All backtests
        </Link>
        <h1 className="text-xl font-semibold">Backtest #{id}</h1>
        {b && (
          <p className="text-muted-foreground text-sm">
            {rulesText(b.params)} · {b.params.start} → {b.params.end}
            {b.params.symbols?.length ? ` · ${b.params.symbols.length} symbols` : " · Nifty 500 (point-in-time)"}
          </p>
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      {!b ? (
        !error && <Empty>Loading…</Empty>
      ) : b.status === "failed" ? (
        <ErrorText>Backtest failed: {b.error ?? "unknown error"}</ErrorText>
      ) : isDone(b) ? (
        <>
          <Headline r={b.results} />
          <EquityCurve r={b.results} />
          <CellTable r={b.results} />
          <Trades r={b.results} />
          <Caveats r={b.results} />
        </>
      ) : (
        <Progress b={b} />
      )}
    </Page>
  );
}
