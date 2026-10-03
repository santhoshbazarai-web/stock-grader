// MetricTable: label / value rows for one topic (the stock page's two-column overview).
// A missing value shows "-" and the reason on hover / focus (and to screen readers).
import { cn } from "@/lib/utils";

import { Term } from "./term";

export type MetricRow = {
  label: string;
  value: string | number | null | undefined;
  /** why the value is missing (or how it was computed) */
  reason?: string | null;
  hint?: string;
  tone?: "discount" | "fair" | "premium" | "unknown";
  /** glossary key for the label tooltip (defaults to a lookup by the label text) */
  glossaryKey?: string;
  /** own-history columns (Key Metrics tab): shown when any row has one */
  median?: string | null;
  percentile?: string | null;
};

const TONE = {
  discount: "text-[var(--sem-discount)]",
  fair: "text-[var(--sem-fair)]",
  premium: "text-[var(--sem-premium)]",
  unknown: "",
};

export function MetricTable({ title, rows, className }: { title?: string; rows: MetricRow[]; className?: string }) {
  const hist = rows.some((r) => r.median !== undefined || r.percentile !== undefined);
  return (
    <section className={cn("flex flex-col gap-1.5", className)} aria-label={title}>
      {title && <h3 className="text-sm font-semibold">{title}</h3>}
      {hist && (
        <div className="text-muted-foreground flex justify-end gap-6 px-3 text-xs" aria-hidden>
          <span>Latest</span>
          <span className="w-20 text-right">5-yr median</span>
          <span className="w-20 text-right">History pctile</span>
        </div>
      )}
      <dl className="divide-y rounded-lg border text-sm">
        {rows.map((r) => {
          const missing = r.value == null || r.value === "" || r.value === "—";
          return (
            <div key={r.label} className="flex items-baseline justify-between gap-3 px-3 py-1.5">
              <dt className="text-muted-foreground mr-auto" title={r.hint}>
                <Term label={r.label} k={r.glossaryKey} />
              </dt>
              <dd className={cn("tnum text-right font-medium", !missing && r.tone && TONE[r.tone])}>
                {missing ? (
                  <span
                    tabIndex={0}
                    title={r.reason ?? "not available"}
                    aria-label={`${r.label}: not available${r.reason ? `, ${r.reason}` : ""}`}
                    className="text-muted-foreground cursor-help border-b border-dotted"
                  >
                    -
                  </span>
                ) : (
                  r.value
                )}
              </dd>
              {hist && (
                <>
                  <dd className="tnum text-muted-foreground w-20 text-right" title={r.median ? undefined : (r.reason ?? "needs at least 3 years of history")}>
                    {r.median ?? "-"}
                  </dd>
                  <dd className="tnum text-muted-foreground w-20 text-right" title={r.percentile ? undefined : (r.reason ?? "needs at least 3 years of history")}>
                    {r.percentile ?? "-"}
                  </dd>
                </>
              )}
            </div>
          );
        })}
      </dl>
    </section>
  );
}
