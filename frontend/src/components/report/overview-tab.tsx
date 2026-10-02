// Overview: the metric groups as two-column tables (banks get their own metrics, the
// inapplicable ones are not listed at all). A missing value shows "-" with the reason on hover.
import { MetricTable } from "@/components/ds";
import { GROUPS, metricsFor } from "@/lib/metrics";
import type { StockReport } from "@/lib/types";

export function OverviewTab({ report }: { report: StockReport }) {
  const all = metricsFor(report);
  const seen = new Set<string>();
  const groups = GROUPS.map((g) => ({
    title: g,
    rows: all
      .filter((m) => m.group === g)
      .filter((m) => {
        const key = `${g}:${m.label}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .map((m) => {
        const v = m.get(report);
        return { label: m.label, value: v.text, reason: v.reason, hint: m.hint, tone: v.tone };
      }),
  })).filter((g) => g.rows.length > 0);
  return (
    <div className="grid grid-cols-1 gap-x-6 gap-y-5 md:grid-cols-2" data-testid="overview-tables">
      {groups.map((g) => (
        <MetricTable key={g.title} title={g.title} rows={g.rows} />
      ))}
    </div>
  );
}
