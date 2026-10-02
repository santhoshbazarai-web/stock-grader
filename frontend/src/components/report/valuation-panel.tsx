"use client";

import { useEffect, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { crore, inr, METHOD_LABEL, num, pct, signedPct } from "@/lib/format";
import type { Sensitivity, StockReport } from "@/lib/types";

import { AssumptionsForm } from "./assumptions-form";
import { JustifiedPbGrid } from "./justified-pb-grid";
import { SensitivityHeatmap } from "./sensitivity-heatmap";

export function ValuationPanel({
  report,
  sectors,
  onReport,
}: {
  report: StockReport;
  sectors: string[];
  onReport: (r: StockReport | null) => void;
}) {
  const v = report.valuation;
  const [grid, setGrid] = useState<Sensitivity | null>(null);
  const [gridNote, setGridNote] = useState<string | null>(null);

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

  const rd = v.reverse_dcf;
  return (
    <div className="flex flex-col gap-6">
      <section aria-labelledby="methods-h" className="flex flex-col gap-2">
        <h3 id="methods-h" className="text-sm font-semibold">
          Methods <span className="text-muted-foreground font-normal">· {v.sector} ({v.model} model)</span>
        </h3>
        <table className="w-full text-sm tabular-nums">
          <thead className="text-muted-foreground text-xs">
            <tr>
              <th className="py-1 text-left font-normal">Method</th>
              <th className="py-1 text-right font-normal">Value</th>
              <th className="py-1 text-right font-normal">Weight</th>
              <th className="py-1 text-right font-normal">Used</th>
            </tr>
          </thead>
          <tbody>
            {v.methods.map((m) => (
              <tr key={m.name} className="border-t align-top">
                <td className="py-1.5">
                  {METHOD_LABEL[m.name] ?? m.name}
                  {m.reasons.length > 0 && (
                    <p className="text-muted-foreground text-xs">{m.reasons.at(-1)}</p>
                  )}
                </td>
                <td className="py-1.5 text-right">{inr(m.value)}</td>
                <td className="py-1.5 text-right">{pct(m.weight, 0)}</td>
                <td className="py-1.5 text-right">{m.value == null ? "—" : pct(m.effective_weight, 0)}</td>
              </tr>
            ))}
            <tr className="border-t font-semibold">
              <td className="py-1.5">Fair value</td>
              <td className="py-1.5 text-right">{inr(report.levels.fair_value)}</td>
              <td colSpan={2} className="text-muted-foreground py-1.5 text-right text-xs font-normal">
                confidence {report.levels.confidence ?? "—"}
              </td>
            </tr>
            {report.levels.fair_value_low != null && report.levels.fair_value_high != null && (
              <tr>
                <td className="py-1 text-xs">Range (methods disagree)</td>
                <td className="py-1 text-right text-xs">
                  {inr(report.levels.fair_value_low)} – {inr(report.levels.fair_value_high)}
                </td>
                <td colSpan={2} className="text-muted-foreground py-1 text-right text-xs">
                  {report.valuation.reasons.find((r) => r.startsWith("methods disagree"))?.split(";")[0]}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <dl className="text-muted-foreground grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
          <div>
            <dt>WACC</dt>
            <dd className="text-foreground">
              {v.model === "bank" || v.model === "insurance" ? "not used (equity model)" : pct(v.wacc, 2)}
            </dd>
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

      <section aria-labelledby="rdcf-h" className="flex flex-col gap-2">
        <h3 id="rdcf-h" className="text-sm font-semibold">
          Reverse DCF & scenarios
        </h3>
        {rd ? (
          <p className="text-sm">
            The price implies <strong>{pct(rd.implied_growth)}</strong> stage-1 growth vs{" "}
            <strong>{pct(rd.hist_growth)}</strong> historical (gap {signedPct(rd.gap)}).
            <span className="text-muted-foreground block text-xs">{rd.reasons.join("; ")}</span>
          </p>
        ) : (
          <p className="text-muted-foreground text-sm">No reverse DCF for this model.</p>
        )}
        {v.dcf.length > 0 && (
          <ul className="flex gap-4 text-sm tabular-nums">
            {v.dcf.map((d) => (
              <li key={d.scenario}>
                <span className="text-muted-foreground text-xs capitalize">{d.scenario}</span>{" "}
                {inr(d.value_per_share)}
              </li>
            ))}
          </ul>
        )}
      </section>

      {v.justified_pb_grid.length > 0 ? (
        <section aria-labelledby="jpb-h" className="flex flex-col gap-2">
          <h3 id="jpb-h" className="text-sm font-semibold">
            Justified P/B sensitivity (₹ per share)
          </h3>
          <JustifiedPbGrid inputs={v.justified_pb_inputs} cells={v.justified_pb_grid} cmp={report.cmp} />
        </section>
      ) : (
        <section aria-labelledby="sens-h" className="flex flex-col gap-2">
          <h3 id="sens-h" className="text-sm font-semibold">
            DCF sensitivity (₹ per share)
          </h3>
          {grid ? (
            <SensitivityHeatmap grid={grid} cmp={report.cmp} />
          ) : (
            <p className="text-muted-foreground text-sm">{gridNote ?? "Loading…"}</p>
          )}
        </section>
      )}

      <section aria-labelledby="assume-h" className="flex flex-col gap-2">
        <h3 id="assume-h" className="text-sm font-semibold">
          Assumptions
        </h3>
        <AssumptionsForm report={report} sectors={sectors} onSaved={onReport} />
      </section>
    </div>
  );
}
