"use client";

// Fair Value tab: why the zone is what it is, a football-field chart (each method's range
// against the price), the method table, bear / base / bull scenarios, the sensitivity grid and
// an assumptions drawer. Ranges come from what is stored: DCF bear-to-bull, the justified P/B
// grid, and the overall fair-value band; a method with a single value is a point.
import { useEffect, useState } from "react";

import { EmptyState, Term } from "@/components/ds";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { crore, inr, METHOD_LABEL, num, pct, signedPct } from "@/lib/format";
import type { Sensitivity, StockReport } from "@/lib/types";

import { AssumptionsForm } from "./assumptions-form";
import { JustifiedPbGrid } from "./justified-pb-grid";
import { SensitivityHeatmap } from "./sensitivity-heatmap";
import { ZoneGauge } from "./zone-gauge";

type Bar = { label: string; low: number; high: number; point: boolean };

export function footballBars(report: StockReport): Bar[] {
  const v = report.valuation;
  const out: Bar[] = [];
  const dcf = v.dcf.map((d) => d.value_per_share).filter((x): x is number => x != null);
  for (const m of v.methods) {
    if (m.value == null) continue;
    let lo = m.value;
    let hi = m.value;
    if (/dcf/i.test(m.name) && dcf.length >= 2) [lo, hi] = [Math.min(...dcf), Math.max(...dcf)];
    const grid = v.justified_pb_grid.map((c) => c.value).filter((x): x is number => x != null);
    if (/^justified/i.test(m.name) && grid.length >= 2) [lo, hi] = [Math.min(...grid), Math.max(...grid)];
    out.push({ label: METHOD_LABEL[m.name] ?? m.name, low: lo, high: hi, point: lo === hi });
  }
  const l = report.levels;
  if (l.fair_value_low != null && l.fair_value_high != null) out.push({ label: "Fair value range", low: l.fair_value_low, high: l.fair_value_high, point: l.fair_value_low === l.fair_value_high });
  else if (l.fair_value != null) out.push({ label: "Fair value", low: l.fair_value, high: l.fair_value, point: true });
  if (l.baseline != null && l.top_band != null) out.push({ label: "Baseline to top band", low: l.baseline, high: l.top_band, point: false });
  return out;
}

export function FootballField({ report }: { report: StockReport }) {
  const bars = footballBars(report);
  if (bars.length === 0) return <p className="text-muted-foreground text-sm">No method produced a value.</p>;
  const all = [...bars.flatMap((b) => [b.low, b.high]), report.cmp];
  const lo = Math.min(...all) * 0.95;
  const hi = Math.max(...all) * 1.05;
  const W = 720;
  const L = 170;
  const rowH = 30;
  const H = bars.length * rowH + 34;
  const x = (v: number) => L + ((v - lo) / (hi - lo)) * (W - L - 150);
  return (
    <figure className="flex flex-col gap-1">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Football field: ${bars.map((b) => `${b.label} ${inr(b.low)} to ${inr(b.high)}`).join("; ")}; price ${inr(report.cmp)}`} className="w-full">
        {bars.map((b, i) => (
          <g key={b.label} transform={`translate(0 ${i * rowH + 8})`}>
            <text x={L - 8} y={14} textAnchor="end" fontSize={11} fill="var(--viz-muted)">
              {b.label}
            </text>
            {b.point ? (
              <rect x={x(b.low) - 5} y={6} width={10} height={10} transform={`rotate(45 ${x(b.low)} 11)`} fill="var(--viz-s1)" />
            ) : (
              <rect x={x(b.low)} y={5} width={Math.max(3, x(b.high) - x(b.low))} height={12} rx={3} fill="var(--viz-s1)" opacity={0.85} />
            )}
            <text x={x(b.high) + 8} y={15} fontSize={10} fill="var(--viz-muted)">
              {b.point ? inr(b.low) : `${inr(b.low)} – ${inr(b.high)}`}
            </text>
          </g>
        ))}
        <line x1={x(report.cmp)} x2={x(report.cmp)} y1={0} y2={H - 22} stroke="var(--viz-critical)" strokeDasharray="4 3" strokeWidth={1.5} />
        <text x={x(report.cmp)} y={H - 8} textAnchor="middle" fontSize={11} fill="var(--viz-critical)">
          Price {inr(report.cmp)}
        </text>
      </svg>
      <figcaption className="text-muted-foreground text-xs">Bars: range per method (DCF bear to bull, justified P/B grid, fair-value band). Diamonds: a single value. Dashed line: current price.</figcaption>
    </figure>
  );
}

function Scenarios({ report }: { report: StockReport }) {
  const v = report.valuation;
  const rows = ["bear", "base", "bull"].map((s) => v.dcf.find((d) => d.scenario === s));
  if (rows.every((r) => !r)) return <p className="text-muted-foreground text-sm">No scenarios for this valuation model{v.model === "bank" || v.model === "insurance" ? " (equity model: see the justified P/B grid)" : ""}.</p>;
  return (
    <table className="w-full text-sm tabular-nums" aria-label="Scenarios">
      <thead className="text-muted-foreground text-xs">
        <tr>
          <th className="py-1 text-left font-normal">Scenario</th>
          <th className="py-1 text-right font-normal">Value per share</th>
          <th className="py-1 text-right font-normal">vs price</th>
          <th className="py-1 text-right font-normal">Terminal share</th>
        </tr>
      </thead>
      <tbody>
        {(["bear", "base", "bull"] as const).map((s, i) => {
          const r = rows[i];
          const val = r?.value_per_share ?? null;
          return (
            <tr key={s} className="border-t align-top">
              <td className="py-1.5 capitalize">{s}</td>
              <td className="py-1.5 text-right">{val == null ? <span title={r?.reasons.at(-1) ?? "scenario not computed"}>-</span> : inr(val)}</td>
              <td className="py-1.5 text-right">{val == null ? "-" : signedPct(val / report.cmp - 1)}</td>
              <td className="py-1.5 text-right">{r?.terminal_share == null ? "-" : pct(r.terminal_share, 0)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function FairValueTab({ report, sectors, onReport }: { report: StockReport; sectors: string[]; onReport: (r: StockReport | null) => void }) {
  const v = report.valuation;
  const [grid, setGrid] = useState<Sensitivity | null>(null);
  const [gridNote, setGridNote] = useState<string | null>(null);
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    let live = true;
    setGridNote(null);
    api<Sensitivity>(`/stocks/${report.symbol}/valuation/sensitivity`)
      .then((g) => live && setGrid(g))
      .catch((e) => {
        if (!live) return;
        setGrid(null);
        setGridNote(e instanceof ApiError ? e.detail : "sensitivity unavailable");
      });
    return () => {
      live = false;
    };
  }, [report]);

  const why = [...report.reasons, ...(report.decision?.reasons ?? [])].filter((r, i, a) => a.indexOf(r) === i);
  const rd = v.reverse_dcf;
  return (
    <div className="flex flex-col gap-6">
      <section aria-label="Why this zone" className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">Why this zone</h3>
        <ZoneGauge report={report} />
        {why.length > 0 ? (
          <ul className="text-muted-foreground flex list-disc flex-col gap-1 pl-5 text-sm">
            {why.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        ) : (
          <p className="text-muted-foreground text-sm">No reasons recorded for this report.</p>
        )}
      </section>

      <section aria-label="Football field" className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">
          <Term label="Football-field chart" k="football_field" />
        </h3>
        <FootballField report={report} />
      </section>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section aria-label="Methods" className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold">
            Methods <span className="text-muted-foreground font-normal">· {v.sector} ({v.model} model)</span>
          </h3>
          <table className="w-full text-sm tabular-nums" aria-label="Valuation methods">
            <thead className="text-muted-foreground text-xs">
              <tr>
                <th className="py-1 text-left font-normal">Method</th>
                <th className="py-1 text-right font-normal">Value</th>
                <th className="py-1 text-right font-normal">vs price</th>
                <th className="py-1 text-right font-normal">Weight used</th>
              </tr>
            </thead>
            <tbody>
              {v.methods.map((m) => (
                <tr key={m.name} className="border-t align-top">
                  <td className="py-1.5">
                    {METHOD_LABEL[m.name] ?? m.name}
                    {m.reasons.length > 0 && <p className="text-muted-foreground text-xs">{m.reasons.at(-1)}</p>}
                  </td>
                  <td className="py-1.5 text-right">{m.value == null ? <span title={m.reasons.at(-1) ?? "not computed"}>-</span> : inr(m.value)}</td>
                  <td className="py-1.5 text-right">{m.value == null ? "-" : signedPct(m.value / report.cmp - 1)}</td>
                  <td className="py-1.5 text-right">{m.value == null ? "-" : pct(m.effective_weight, 0)}</td>
                </tr>
              ))}
              <tr className="border-t font-semibold">
                <td className="py-1.5">Fair value</td>
                <td className="py-1.5 text-right">{inr(report.levels.fair_value)}</td>
                <td className="py-1.5 text-right">{report.levels.fair_value == null ? "-" : signedPct(report.levels.fair_value / report.cmp - 1)}</td>
                <td className="text-muted-foreground py-1.5 text-right text-xs font-normal">confidence {report.levels.confidence ?? "-"}</td>
              </tr>
            </tbody>
          </table>
          <dl className="text-muted-foreground grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
            <div>
              <dt><Term label="WACC" k="wacc" /></dt>
              <dd className="text-foreground">{v.model === "bank" || v.model === "insurance" ? "not used (equity model)" : pct(v.wacc, 2)}</dd>
            </div>
            <div>
              <dt>Cost of equity</dt>
              <dd className="text-foreground">{pct(v.cost_of_equity, 2)}</dd>
            </div>
            <div>
              <dt>Beta</dt>
              <dd className="text-foreground">{num(v.beta)}</dd>
            </div>
            <div>
              <dt>Market cap</dt>
              <dd className="text-foreground">{crore(v.market_cap_cr)}</dd>
            </div>
          </dl>
        </section>

        <section aria-label="Scenarios" className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold">
            <Term label="Bear / base / bull" k="scenarios" />
          </h3>
          <Scenarios report={report} />
          {rd && (
            <p className="text-sm">
              <Term label="Reverse DCF" k="reverse_dcf" />: the price implies <strong>{pct(rd.implied_growth)}</strong> stage-1 growth vs <strong>{pct(rd.hist_growth)}</strong> historical (gap {signedPct(rd.gap)}).
            </p>
          )}
        </section>
      </div>

      <section aria-label="Sensitivity" className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">
          <Term label="Sensitivity grid" k="sensitivity" /> {v.justified_pb_grid.length > 0 ? "· justified P/B (₹ per share)" : "· DCF (₹ per share)"}
        </h3>
        {v.justified_pb_grid.length > 0 ? (
          <JustifiedPbGrid inputs={v.justified_pb_inputs} cells={v.justified_pb_grid} cmp={report.cmp} />
        ) : grid ? (
          <SensitivityHeatmap grid={grid} cmp={report.cmp} />
        ) : (
          <EmptyState title="No sensitivity grid">{gridNote ?? "Loading…"}</EmptyState>
        )}
      </section>

      <div>
        <Button variant="outline" size="sm" onClick={() => setDrawer(true)}>
          Edit assumptions
        </Button>
      </div>
      {drawer && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/30" role="presentation" onClick={() => setDrawer(false)}>
          <aside role="dialog" aria-label="Assumptions" aria-modal="true" onClick={(e) => e.stopPropagation()} className="bg-background flex h-full w-full max-w-md flex-col gap-3 overflow-y-auto border-l p-4 shadow-xl">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold">Assumptions</h3>
              <Button size="sm" variant="ghost" onClick={() => setDrawer(false)} aria-label="Close assumptions">
                Close
              </Button>
            </div>
            <AssumptionsForm report={report} sectors={sectors} onSaved={onReport} />
          </aside>
        </div>
      )}
    </div>
  );
}
