// PriceLadder: Baseline → Buy zone → Fair value → Top band on one price axis, with the
// current price marked. Positions are proportional; labels never overlap (alternate rows).
import { inr } from "@/lib/format";
import { cn } from "@/lib/utils";

type Level = { label: string; value: number; strong?: boolean };

export function PriceLadder({
  cmp,
  baseline,
  buyZone,
  fairValue,
  topBand,
  range,
}: {
  cmp: number;
  baseline: number | null;
  buyZone: { low: number; high: number } | null;
  fairValue: number | null;
  topBand: number | null;
  /** methods' fair-value range when they disagree */
  range?: { low: number; high: number } | null;
}) {
  const levels: Level[] = [
    baseline != null ? { label: "Baseline", value: baseline } : null,
    fairValue != null ? { label: "Fair value", value: fairValue, strong: true } : null,
    topBand != null ? { label: "Top band", value: topBand } : null,
  ].filter((l): l is Level => l !== null);
  const all = [cmp, ...levels.map((l) => l.value), ...(buyZone ? [buyZone.low, buyZone.high] : []), ...(range ? [range.low, range.high] : [])];
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  const pad = (hi - lo) * 0.06 || hi * 0.05;
  const pos = (v: number) => ((v - (lo - pad)) / (hi - lo + 2 * pad)) * 100;
  const x = (v: number) => `${pos(v)}%`;
  // labels near an edge anchor to it instead of spilling out of the card
  const anchor = (v: number) => (pos(v) < 18 ? "translateX(0)" : pos(v) > 82 ? "translateX(-100%)" : "translateX(-50%)");
  const ordered = [...levels].sort((a, b) => a.value - b.value);
  return (
    <figure aria-label="Price ladder" className="flex flex-col gap-1">
      <div className="relative h-5" aria-hidden>
        <span className="absolute text-xs font-semibold tnum" style={{ left: x(cmp), transform: anchor(cmp) }}>
          {inr(cmp)} ▼
        </span>
      </div>
      <div className="bg-muted relative h-3 rounded-full">
        {range && (
          <div className="bg-[var(--sem-fair-bg)] absolute top-0 h-full rounded-full" title="Fair value range of the methods" style={{ left: x(range.low), width: `calc(${x(range.high)} - ${x(range.low)})` }} />
        )}
        {buyZone && (
          <div className="absolute top-0 h-full rounded-full bg-[var(--sem-discount)] opacity-80" title={`Buy zone ${inr(buyZone.low)} – ${inr(buyZone.high)}`} style={{ left: x(buyZone.low), width: `calc(${x(buyZone.high)} - ${x(buyZone.low)})` }} />
        )}
        {ordered.map((l) => (
          <span key={l.label} className={cn("absolute top-[-3px] h-[18px] w-0.5", l.strong ? "bg-foreground" : "bg-muted-foreground")} style={{ left: x(l.value) }} />
        ))}
        <span className="bg-[var(--brand)] absolute top-[-4px] h-5 w-1 -translate-x-1/2 rounded" style={{ left: x(cmp) }} />
      </div>
      <figcaption className="relative h-10 text-[11px]">
        {ordered.map((l, i) => (
          <span key={l.label} className={cn("tnum absolute whitespace-nowrap", l.strong ? "font-semibold" : "text-muted-foreground")} style={{ left: x(l.value), top: (i % 2) * 16, transform: anchor(l.value) }}>
            {l.label} {inr(l.value)}
          </span>
        ))}
      </figcaption>
      <p className="text-muted-foreground text-[11px]">
        {buyZone ? `Buy zone ${inr(buyZone.low)} – ${inr(buyZone.high)}` : "No buy zone yet"}
        {range ? ` · methods range ${inr(range.low)} – ${inr(range.high)}` : ""}
      </p>
    </figure>
  );
}
