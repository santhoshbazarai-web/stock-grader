"use client";

// Six-pillar scorecard (SPEC §9): a radar (one series, so no legend; the title names it) and
// a table of pillars whose rows expand to the sub-metrics, each with its reason. A pillar
// without enough data has no score: it is plotted at 0 but labelled "n/a", never averaged in.
import { PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer, Tooltip } from "recharts";

import { num, titleCase } from "@/lib/format";
import type { Pillar, StockReport } from "@/lib/types";

const ORDER = ["quality", "growth", "valuation", "health", "governance", "technical"];

export function radarData(pillars: Pillar[]) {
  return ORDER.map((name) => {
    const p = pillars.find((x) => x.pillar === name);
    return { pillar: titleCase(name), score: p?.score ?? 0, missing: p?.score == null };
  });
}

function fmtValue(v: number | string | boolean | null): string {
  if (v == null) return "—";
  if (typeof v === "number") return num(v, 3);
  return String(v);
}

export function Scorecard({ report }: { report: StockReport }) {
  const data = radarData(report.pillars);
  const pillars = ORDER.map((n) => report.pillars.find((p) => p.pillar === n)).filter((p): p is Pillar => !!p);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-baseline justify-between">
        <p className="text-sm">
          Total <strong className="text-lg tabular-nums">{num(report.scores.total, 1)}</strong>
          <span className="text-muted-foreground"> / 100</span>
        </p>
        <p className="text-muted-foreground text-xs">
          provisional {report.provisional_grade?.replace("_plus", "+") ?? "—"} → final {report.grade_label ?? "—"}
          {report.knockouts.cap && ` (capped at ${report.knockouts.cap})`}
        </p>
      </div>
      <div className="h-64 w-full" role="img" aria-label="Pillar scores radar">
        <ResponsiveContainer>
          <RadarChart data={data} outerRadius="68%" margin={{ top: 16, right: 36, bottom: 16, left: 36 }}>
            <PolarGrid stroke="var(--viz-grid)" />
            <PolarAngleAxis
              dataKey="pillar"
              tick={({ payload, x, y, textAnchor, index }) => {
                const row = data.find((d) => d.pillar === payload.value);
                const score = row?.missing ? "n/a" : String(Math.round(row?.score ?? 0));
                if (index === 0) {
                  // Top axis: one line, lifted clear of a point at 100.
                  return (
                    <text x={x} y={y} dy={-8} textAnchor="middle" fontSize={11} fill="var(--viz-ink-2)">
                      {payload.value} <tspan fontWeight={600} fill="var(--viz-ink)">{score}</tspan>
                    </text>
                  );
                }
                return (
                  <text x={x} y={y} textAnchor={textAnchor} fontSize={11} fill="var(--viz-ink-2)">
                    <tspan x={x}>{payload.value}</tspan>
                    <tspan x={x} dy={13} fontWeight={600} fill="var(--viz-ink)">
                      {score}
                    </tspan>
                  </text>
                );
              }}
            />
            <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
            <Radar dataKey="score" stroke="var(--viz-s1)" strokeWidth={2} fill="var(--viz-s1)" fillOpacity={0.18} dot={{ r: 3, fill: "var(--viz-s1)" }} />
            <Tooltip
              formatter={(v, _n, item) => [(item?.payload as { missing?: boolean })?.missing ? "n/a" : num(Number(v), 1), "Score"]}
              contentStyle={{ background: "var(--viz-surface)", borderColor: "var(--viz-grid)", fontSize: 12 }}
            />
          </RadarChart>
        </ResponsiveContainer>
      </div>
      <ul className="divide-y text-sm">
        {pillars.map((p) => (
          <li key={p.pillar}>
            <details className="group py-2">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2">
                <span className="flex items-center gap-2">
                  <span aria-hidden className="text-muted-foreground text-xs transition group-open:rotate-90">▶</span>
                  {titleCase(p.pillar)}
                  <span className="text-muted-foreground text-xs">weight {p.weight}</span>
                </span>
                <span className="tabular-nums">
                  {p.score == null ? <span className="text-muted-foreground">n/a</span> : num(p.score, 0)}
                  <ScoreBar score={p.score} />
                </span>
              </summary>
              <table className="mt-2 w-full text-xs tabular-nums">
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="text-left font-normal">Sub-metric</th>
                    <th className="text-right font-normal">Value</th>
                    <th className="text-right font-normal">Score</th>
                  </tr>
                </thead>
                <tbody>
                  {p.subs.map((s) => (
                    <tr key={s.name} className="border-t align-top">
                      <td className="py-1">
                        {titleCase(s.name)}
                        <p className="text-muted-foreground">{s.reason}</p>
                      </td>
                      <td className="py-1 text-right">{fmtValue(s.value)}</td>
                      <td className="py-1 text-right">{s.score == null ? "—" : num(s.score, 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {p.reasons.length > 0 && <p className="text-muted-foreground mt-1 text-xs">{p.reasons.at(-1)}</p>}
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ScoreBar({ score }: { score: number | null }) {
  return (
    <span aria-hidden className="bg-muted ml-2 inline-block h-1.5 w-16 overflow-hidden rounded-full align-middle">
      {score != null && <span className="block h-full rounded-full" style={{ width: `${score}%`, background: "var(--viz-s1)" }} />}
    </span>
  );
}
