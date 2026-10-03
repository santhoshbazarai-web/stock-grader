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
};

const TONE = {
  discount: "text-[var(--sem-discount)]",
  fair: "text-[var(--sem-fair)]",
  premium: "text-[var(--sem-premium)]",
  unknown: "",
};

export function MetricTable({ title, rows, className }: { title?: string; rows: MetricRow[]; className?: string }) {
  return (
    <section className={cn("flex flex-col gap-1.5", className)} aria-label={title}>
      {title && <h3 className="text-sm font-semibold">{title}</h3>}
      <dl className="divide-y rounded-lg border text-sm">
        {rows.map((r) => {
          const missing = r.value == null || r.value === "" || r.value === "—";
          return (
            <div key={r.label} className="flex items-baseline justify-between gap-3 px-3 py-1.5">
              <dt className="text-muted-foreground" title={r.hint}>
                <Term label={r.label} />
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
            </div>
          );
        })}
      </dl>
    </section>
  );
}
