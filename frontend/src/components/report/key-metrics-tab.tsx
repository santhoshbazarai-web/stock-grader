"use client";

// Key Metrics tab: grouped tables (banks get bank groups); each metric shows the latest fiscal
// year, its 5-year median and where it sits in its own history, with a tooltip link to the
// Glossary. A missing value is "-" with the reason on hover, never 0.
import { useEffect, useState } from "react";

import { EmptyState, MetricTable, Skeleton, Term } from "@/components/ds";
import type { MetricRow } from "@/components/ds/metric-table";
import { api, ApiError } from "@/lib/api";
import { crore, inr, num } from "@/lib/format";
import type { Durability, KeyMetric, KeyMetricsOut } from "@/lib/types";

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

function DurabilityCard({ d }: { d: Durability }) {
  return (
    <section aria-label="Durability" className="flex flex-col gap-1.5 rounded-lg border p-3">
      <h3 className="text-sm font-semibold">
        <Term label="Durability (proxy)" k="durability" /> · {d.rating ? `${d.rating[0].toUpperCase()}${d.rating.slice(1)}` : "no rating"}
        {d.confidence && <span className="text-muted-foreground font-normal"> · {d.confidence} confidence</span>}
      </h3>
      <p className="text-muted-foreground text-xs">{d.label}. Tests with no history are left out of the score and lower the confidence.</p>
      <ul className="grid grid-cols-1 gap-x-6 gap-y-0.5 text-xs md:grid-cols-2">
        {d.tests.map((t) => (
          <li key={t.key} title={t.detail}>
            <span aria-hidden>{t.passed === true ? "✓" : t.passed === false ? "✗" : "-"}</span> <span className="sr-only">{t.passed === true ? "pass" : t.passed === false ? "fail" : "no data"}: </span>
            {t.label}
            <span className="text-muted-foreground"> · {t.detail}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function KeyMetricsTab({ symbol, durability }: { symbol: string; durability?: Durability | null }) {
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
      {durability && <DurabilityCard d={durability} />}
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
