"use client";

// Key Metrics tab: grouped tables (banks get bank groups); each metric shows the latest fiscal
// year, its 5-year median and where it sits in its own history, with a tooltip link to the
// Glossary. A missing value is "-" with the reason on hover, never 0.
import { useEffect, useState } from "react";

import { EmptyState, MetricTable, Skeleton } from "@/components/ds";
import type { MetricRow } from "@/components/ds/metric-table";
import { api, ApiError } from "@/lib/api";
import { crore, inr, num } from "@/lib/format";
import type { KeyMetric, KeyMetricsOut } from "@/lib/types";

function fmt(v: number | null, unit: KeyMetric["unit"]): string | null {
  if (v == null) return null;
  switch (unit) {
    case "pct":
      return `${num(v, 1)}%`;
    case "x":
      return `${num(v, 2)}x`;
    case "days":
      return `${num(v, 0)} days`;
    case "inr":
      return inr(v);
    default:
      return crore(v);
  }
}

function toRow(m: KeyMetric): MetricRow {
  return {
    label: m.label,
    glossaryKey: m.glossary_key,
    value: m.value == null ? null : `${fmt(m.value, m.unit)}${m.fiscal_year ? ` · FY${m.fiscal_year}` : ""}`,
    median: fmt(m.median_5y, m.unit),
    percentile: m.percentile == null ? null : `${num(m.percentile, 0)}th`,
    reason: m.reason,
  };
}

export function KeyMetricsTab({ symbol }: { symbol: string }) {
  const [data, setData] = useState<KeyMetricsOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api<KeyMetricsOut>(`/stocks/${encodeURIComponent(symbol)}/key-metrics`)
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e instanceof ApiError ? e.detail : "key metrics unavailable"));
    return () => {
      live = false;
    };
  }, [symbol]);
  if (error) return <EmptyState title="Key metrics unavailable">{error}</EmptyState>;
  if (!data) return <Skeleton className="h-64 w-full" />;
  return (
    <div className="flex flex-col gap-3">
      <p className="text-muted-foreground text-xs">
        Latest fiscal year · {data.model === "bank" ? "bank metrics (CAR, CASA and NPA need the bank's own disclosure)" : "5-year median and percentile in the stock's own history"}. Percentile: share of the last 10 years at or below the latest value.
      </p>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {data.groups.map((g) => (
          <MetricTable key={g.title} title={g.title} rows={g.metrics.map(toRow)} />
        ))}
      </div>
    </div>
  );
}
