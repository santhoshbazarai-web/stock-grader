// PillarMiniChart: the six pillar scores (0-100) as dots on a line, one row each. A missing
// score is an empty ring with "n/a"; the 50 line is the neutral midpoint.
import type { Pillar } from "@/lib/types";
import { cn } from "@/lib/utils";

const ORDER = ["quality", "growth", "valuation", "health", "governance", "technical"];

function color(score: number): string {
  return score >= 70 ? "var(--sem-discount)" : score >= 50 ? "var(--sem-fair)" : "var(--sem-premium)";
}

export function PillarMiniChart({ pillars, className }: { pillars: Pillar[]; className?: string }) {
  const by = new Map(pillars.map((p) => [p.pillar, p]));
  return (
    <ul className={cn("flex flex-col gap-1.5", className)} aria-label="Pillar scores">
      {ORDER.map((name) => {
        const p = by.get(name);
        const s = p?.score ?? null;
        return (
          <li key={name} className="grid grid-cols-[5.5rem_1fr_2.5rem] items-center gap-2 text-xs">
            <span className="capitalize">{name}</span>
            <span className="bg-muted relative h-1.5 rounded-full" role="img" aria-label={`${name} ${s == null ? "not available" : Math.round(s)} of 100`}>
              <span className="bg-border absolute top-[-2px] left-1/2 h-[10px] w-px" aria-hidden />
              {s != null ? (
                <span className="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-[var(--viz-surface)]" style={{ left: `${Math.min(100, Math.max(0, s))}%`, background: color(s) }} />
              ) : (
                <span className="absolute top-1/2 left-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-dashed" />
              )}
            </span>
            <span className="tnum text-right font-medium" title={p?.confidence === "reduced" ? "reduced confidence: inputs missing" : undefined}>
              {s == null ? "n/a" : Math.round(s)}
              {p?.confidence === "reduced" && <sup aria-hidden>*</sup>}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
