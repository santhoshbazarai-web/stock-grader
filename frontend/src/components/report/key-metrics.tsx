// Key fundamentals with the reason each missing one is not computed (and how per-share
// growth across a structural break was measured). Only metrics that apply to the sector
// model are in the report, so nothing here is "missing" merely for being inapplicable.
import { crore, num, pct } from "@/lib/format";
import type { StockReport } from "@/lib/types";

export function formatMetric(key: string, v: number | null): string {
  if (v == null) return "—";
  if (key.endsWith("_pct")) return `${num(v, 2)}%`;
  if (key.endsWith("_ttm") && !key.startsWith("opm")) return crore(v);
  if (key.endsWith("_days")) return `${num(v, 0)} days`;
  if (/cagr|roe_latest|roce|roic|opm|share|conversion|cfo_to/.test(key)) return pct(v, 1);
  return num(v, 2);
}

export function KeyMetrics({ report }: { report: StockReport }) {
  const notes = report.fundamentals_notes ?? {};
  const rows = Object.entries(report.fundamentals);
  if (rows.length === 0) return null;
  return (
    <table className="w-full text-xs tabular-nums" aria-label="Key metrics">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k} className="border-t align-top">
            <th scope="row" className="py-1 pr-2 text-left font-normal whitespace-nowrap">
              {k.replace(/_/g, " ")}
            </th>
            <td className="py-1 pr-2 text-right">{formatMetric(k, v)}</td>
            <td className="text-muted-foreground py-1">{notes[k] ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
