"use client";

// DCF value per share over WACC (rows) x terminal growth (columns). Cells are coloured on the
// diverging pair by value vs CMP (blue = upside, red = downside, gray ≈ at price) and always
// carry the number, so colour is never the only channel. The base case has an ink ring.
import { inr, pct, signedPct } from "@/lib/format";
import type { Sensitivity } from "@/lib/types";

export const BINS = [
  { min: 0.2, bg: "var(--viz-div-neg-2)", label: "≥ +20% vs CMP", strong: true },
  { min: 0.05, bg: "var(--viz-div-neg-1)", label: "+5% to +20%", strong: false },
  { min: -0.05, bg: "var(--viz-div-mid)", label: "±5%", strong: false },
  { min: -0.2, bg: "var(--viz-div-pos-1)", label: "−20% to −5%", strong: false },
  { min: -Infinity, bg: "var(--viz-div-pos-2)", label: "≤ −20%", strong: true },
] as const;

export function binFor(value: number, cmp: number) {
  const up = value / cmp - 1;
  return BINS.find((b) => up >= b.min) ?? BINS[BINS.length - 1];
}

export function SensitivityHeatmap({ grid, cmp }: { grid: Sensitivity; cmp: number }) {
  const baseRow = grid.waccs.findIndex((w) => Math.abs(w - grid.base_wacc) < 1e-9);
  const baseCol = grid.terminal_growths.findIndex((g) => Math.abs(g - grid.base_g_terminal) < 1e-9);
  return (
    <div className="flex flex-col gap-2">
      <div className="overflow-x-auto">
        <table className="w-full border-separate border-spacing-[2px] text-xs tabular-nums" aria-label="DCF sensitivity">
          <thead>
            <tr>
              <th className="text-muted-foreground p-1 text-left font-normal">WACC ↓ / g →</th>
              {grid.terminal_growths.map((g) => (
                <th key={g} scope="col" className="text-muted-foreground p-1 font-normal">
                  {pct(g)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.waccs.map((w, i) => (
              <tr key={w}>
                <th scope="row" className="text-muted-foreground p-1 text-left font-normal">
                  {pct(w)}
                </th>
                {grid.values[i].map((v, j) => {
                  const base = i === baseRow && j === baseCol;
                  if (v == null) {
                    return (
                      <td key={j} className="text-muted-foreground rounded-sm p-1 text-center" title="WACC ≤ g: undefined">
                        n/a
                      </td>
                    );
                  }
                  const bin = binFor(v, cmp);
                  return (
                    <td
                      key={j}
                      title={`WACC ${pct(w)}, g ${pct(grid.terminal_growths[j])}: ${inr(v)} (${signedPct(v / cmp - 1)} vs CMP)${base ? " — base case" : ""}`}
                      className={`rounded-sm p-1 text-center ${base ? "font-semibold" : ""}`}
                      style={{
                        background: bin.bg,
                        color: bin.strong ? "#ffffff" : "var(--viz-ink)",
                        boxShadow: base ? "inset 0 0 0 2px var(--viz-ink)" : undefined,
                      }}
                    >
                      {Math.round(v).toLocaleString("en-IN")}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 text-[11px]" aria-label="Heatmap legend">
        {BINS.map((b) => (
          <li key={b.label} className="flex items-center gap-1">
            <span aria-hidden className="inline-block h-3 w-3 rounded-[2px]" style={{ background: b.bg }} />
            {b.label}
          </li>
        ))}
        <li>Ringed cell = base case · CMP {inr(cmp)}</li>
      </ul>
    </div>
  );
}
