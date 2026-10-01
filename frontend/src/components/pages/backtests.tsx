"use client";

// Backtests (SPEC §9, §11): choose rules (grade set x zone set x holding period) and a period,
// queue the run (the worker's `backtests` job picks it up within a minute), and follow its
// progress. Results open on /backtests/[id].
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Empty, ErrorText, Page } from "@/components/common";
import { Chips } from "@/components/pages/screener";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { signedPct, ZONE_LABEL } from "@/lib/format";
import { GRADES, ZONES } from "@/lib/screener";
import type { Backtest, BacktestParams, BacktestStatus, BacktestSummary, Grade, ZoneName } from "@/lib/types";

const input = "border-input bg-background h-8 rounded-md border px-2 text-sm";
const SYMBOL_RE = /^[A-Za-z0-9&_.-]{1,32}$/;
const POLL_MS = 3000;

export const gradeText = (g: string) => g.replace("_plus", "+");

export function rulesText(p: BacktestParams): string {
  const zones = p.zones.map((z) => ZONE_LABEL[z] ?? z).join(", ");
  return `${p.grades.map(gradeText).join(", ")} in ${zones} · hold ${p.holding_days} sessions`;
}

export function StatusText({ status, progress }: { status: BacktestStatus; progress?: { done: number; total: number } | null }) {
  if (status === "running" && progress && progress.total > 0) {
    return (
      <span>
        Running {progress.done}/{progress.total} months
      </span>
    );
  }
  const label = { queued: "Queued", running: "Running", done: "Done", failed: "Failed" }[status];
  return <span className={status === "failed" ? "text-destructive" : undefined}>{label}</span>;
}

function isoYearsAgo(years: number): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - years);
  return d.toISOString().slice(0, 10);
}

export function parseSymbols(text: string): { symbols: string[]; bad: string[] } {
  const parts = text
    .split(/[\s,]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  const symbols = [...new Set(parts)];
  return { symbols: symbols.filter((s) => SYMBOL_RE.test(s)), bad: symbols.filter((s) => !SYMBOL_RE.test(s)) };
}

function NewBacktest() {
  const router = useRouter();
  const [grades, setGrades] = useState<Grade[]>(["A_plus", "A"]);
  const [zones, setZones] = useState<ZoneName[]>(["deep_discount", "discount"]);
  const [holding, setHolding] = useState("250");
  const [start, setStart] = useState(isoYearsAgo(5));
  const [end, setEnd] = useState(new Date().toISOString().slice(0, 10));
  const [symbolsText, setSymbolsText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const days = Number(holding);
    if (grades.length === 0 || zones.length === 0) return setError("Pick at least one grade and one zone.");
    if (!Number.isInteger(days) || days < 5 || days > 2520) return setError("Holding period must be 5–2520 sessions.");
    if (!start || !end || start >= end) return setError("Start must be before end.");
    const { symbols, bad } = parseSymbols(symbolsText);
    if (bad.length) return setError(`Not a valid symbol: ${bad.join(", ")}`);
    const body: BacktestParams = { grades, zones, holding_days: days, start, end, ...(symbols.length ? { symbols } : {}) };
    setBusy(true);
    try {
      const b = await api<Backtest>("/backtests", { method: "POST", body: JSON.stringify(body) });
      router.push(`/backtests/${b.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not queue the backtest.");
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New backtest</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} noValidate className="flex flex-col gap-4" aria-label="New backtest">
          <p className="text-muted-foreground text-sm">
            Each month, buy every stock whose point-in-time grade and zone match the rules, and hold it for a fixed number of
            sessions. Every grade × zone cell is also tested, so the results table shows which cells carry the edge.
          </p>
          <div className="flex flex-wrap gap-6">
            <Chips label="Grades" options={GRADES} value={grades} onChange={setGrades} render={gradeText} />
            <Chips label="Zones" options={ZONES} value={zones} onChange={setZones} render={(z) => ZONE_LABEL[z]} />
          </div>
          <div className="flex flex-wrap items-end gap-4">
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">Holding period (sessions)</span>
              <input className={`${input} w-28 tabular-nums`} type="number" min={5} max={2520} value={holding} onChange={(e) => setHolding(e.target.value)} />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">Start</span>
              <input className={input} type="date" value={start} onChange={(e) => setStart(e.target.value)} />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">End</span>
              <input className={input} type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </label>
          </div>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-muted-foreground">
              Symbols (optional). Leave empty to use point-in-time Nifty 500 membership. A fixed list carries survivorship bias.
            </span>
            <textarea
              className="border-input bg-background min-h-16 rounded-md border px-2 py-1 font-mono text-sm"
              placeholder="e.g. TCS, INFY, HDFCBANK"
              value={symbolsText}
              onChange={(e) => setSymbolsText(e.target.value)}
            />
          </label>
          {error && <ErrorText>{error}</ErrorText>}
          <div>
            <Button type="submit" disabled={busy}>
              {busy ? "Queuing…" : "Run backtest"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function History() {
  const [rows, setRows] = useState<BacktestSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api<BacktestSummary[]>("/backtests")
      .then((r) => {
        setRows(r);
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, []);
  useEffect(load, [load]);
  const pending = rows?.some((r) => r.status === "queued" || r.status === "running");
  useEffect(() => {
    if (!pending) return;
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [pending, load]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>History</CardTitle>
      </CardHeader>
      <CardContent>
        {error && <ErrorText>{error}</ErrorText>}
        {rows === null ? (
          <Empty>Loading…</Empty>
        ) : rows.length === 0 ? (
          <Empty>No backtests yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm" aria-label="Backtest history">
              <thead className="text-muted-foreground text-xs">
                <tr>
                  <th className="py-1 pr-2 text-left font-normal">#</th>
                  <th className="py-1 pr-2 text-left font-normal">Rules</th>
                  <th className="py-1 pr-2 text-left font-normal">Period</th>
                  <th className="py-1 pr-2 text-left font-normal">Status</th>
                  <th className="py-1 pr-2 text-right font-normal">CAGR</th>
                  <th className="py-1 text-right font-normal">Nifty 500</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-t">
                    <td className="py-1.5 pr-2">
                      <Link href={`/backtests/${r.id}`} className="underline-offset-2 hover:underline">
                        {r.id}
                      </Link>
                    </td>
                    <td className="py-1.5 pr-2">
                      <Link href={`/backtests/${r.id}`} className="underline-offset-2 hover:underline">
                        {rulesText(r.params)}
                      </Link>
                      {r.params.symbols?.length ? (
                        <span className="text-muted-foreground text-xs"> · {r.params.symbols.length} symbols</span>
                      ) : null}
                    </td>
                    <td className="py-1.5 pr-2 whitespace-nowrap tabular-nums">
                      {r.params.start} → {r.params.end}
                    </td>
                    <td className="py-1.5 pr-2" title={r.error ?? undefined}>
                      <StatusText status={r.status} progress={r.progress} />
                    </td>
                    <td className="py-1.5 pr-2 text-right tabular-nums">
                      {r.trades === 0 ? <span className="text-muted-foreground">no trades</span> : signedPct(r.cagr)}
                    </td>
                    <td className="py-1.5 text-right tabular-nums">{signedPct(r.benchmark_cagr)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function Backtests() {
  return (
    <Page>
      <h1 className="text-xl font-semibold">Backtests</h1>
      <NewBacktest />
      <History />
    </Page>
  );
}
