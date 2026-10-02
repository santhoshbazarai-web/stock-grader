"use client";

// Zone gauge (SPEC §9): a horizontal bar of the five valuation zones with Baseline, FV and
// Top band ticks, the technical buy zone on its own track, and a CMP marker. Zones use the
// diverging pair (cheap = blue ↔ expensive = red, Fair = neutral), so the bar reads as
// "how far from fair" at a glance; every zone and marker is also labelled in text.
import { useEffect, useRef, useState } from "react";

import type { StockReport } from "@/lib/types";
import { inr, ZONE_LABEL } from "@/lib/format";

type Segment = { zone: string; from: number; to: number; color: string };
type Marker = { label: string; value: number; strong?: boolean };

const ZONE_COLOR: Record<string, string> = {
  deep_discount: "var(--viz-div-neg-2)",
  discount: "var(--viz-div-neg-1)",
  fair: "var(--viz-div-mid)",
  premium: "var(--viz-div-pos-1)",
  extreme_premium: "var(--viz-div-pos-2)",
};

export function gaugeDomain(values: (number | null | undefined)[]): [number, number] {
  const xs = values.filter((v): v is number => v != null && Number.isFinite(v) && v > 0);
  const lo = Math.min(...xs);
  const hi = Math.max(...xs);
  const pad = (hi - lo) * 0.08 || hi * 0.05;
  return [Math.max(0, lo - pad), hi + pad];
}

export function segments(r: StockReport, lo: number, hi: number): Segment[] {
  const { baseline, discount_edge, fair_upper, top_band } = r.levels;
  const bounds: [string, number | null, number | null][] = [
    ["deep_discount", lo, baseline],
    ["discount", baseline ?? lo, discount_edge],
    ["fair", discount_edge, fair_upper],
    ["premium", fair_upper, top_band ?? hi],
    ["extreme_premium", top_band, hi],
  ];
  return bounds
    .filter(([, a, b]) => a != null && b != null && b > a)
    .map(([zone, a, b]) => ({
      zone,
      from: Math.max(lo, a as number),
      to: Math.min(hi, b as number),
      color: ZONE_COLOR[zone],
    }))
    .filter((s) => s.to > s.from);
}

/** Horizontal anchoring that keeps a label inside the bar near either edge. */
export function edgeShift(x: number): string {
  if (x < 0.12) return "translateX(0)";
  if (x > 0.88) return "translateX(-100%)";
  return "translateX(-50%)";
}

/** Put labels whose positions are within `minGap` (fraction of width) on alternating rows. */
export function stagger<T extends { x: number }>(items: T[], minGap = 0.22): (T & { row: number })[] {
  const sorted = [...items].sort((a, b) => a.x - b.x);
  const out: (T & { row: number })[] = [];
  sorted.forEach((it, i) => {
    const prev = out[i - 1];
    const row = prev && it.x - prev.x < minGap ? (prev.row + 1) % 2 : 0;
    out.push({ ...it, row });
  });
  return out;
}

// Widest level label ("Top band ₹12,34,567.89") in px at 11px; used to stagger by pixels.
const LABEL_PX = 130;

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(1000);
  useEffect(() => {
    if (!ref.current) return;
    const obs = new ResizeObserver(([e]) => setW(e.contentRect.width));
    obs.observe(ref.current);
    return () => obs.disconnect();
  }, []);
  return [ref, w];
}

export function ZoneGauge({ report }: { report: StockReport }) {
  const [box, width] = useWidth<HTMLElement>();
  const { levels, cmp, buy_zone: bz } = report;
  if (levels.fair_value == null) {
    return (
      <p className="text-muted-foreground text-sm">
        No valuation levels yet: {report.valuation.reasons.at(-1) ?? "fair value unavailable"}.
      </p>
    );
  }
  const zoneBz = bz?.status === "zone" && bz.low != null && bz.high != null ? bz : null;
  const [lo, hi] = gaugeDomain([
    levels.baseline,
    levels.fair_value,
    levels.fair_value_low,
    levels.fair_value_high,
    levels.top_band,
    cmp,
    zoneBz?.low,
    zoneBz?.high,
  ]);
  const x = (v: number) => ((v - lo) / (hi - lo)) * 100;
  const segs = segments(report, lo, hi);
  const markers: Marker[] = [
    { label: "Baseline", value: levels.baseline ?? NaN },
    { label: "FV", value: levels.fair_value, strong: true },
    { label: "Top band", value: levels.top_band ?? NaN },
  ].filter((m) => Number.isFinite(m.value));
  const placed = stagger(
    markers.map((m) => ({ ...m, x: x(m.value) / 100 })),
    LABEL_PX / Math.max(width, 1),
  );

  return (
    <figure ref={box} className="flex flex-col gap-1" aria-label="Valuation zone gauge">
      {/* CMP marker */}
      <div className="relative h-9">
        <div
          className={`absolute bottom-0 flex flex-col ${x(cmp) > 88 ? "items-end" : x(cmp) < 12 ? "items-start" : "items-center"}`}
          style={{ left: `${x(cmp)}%`, transform: edgeShift(x(cmp) / 100) }}
        >
          <span className="text-xs font-semibold whitespace-nowrap">CMP {inr(cmp)}</span>
          <span aria-hidden className="text-[10px] leading-none">▼</span>
        </div>
      </div>
      {/* zone bar: 2px surface gaps between segments */}
      <div className="relative flex h-7 w-full gap-[2px] overflow-hidden rounded-md">
        {segs.map((s) => {
          const width = ((s.to - s.from) / (hi - lo)) * 100;
          const current = report.zone === s.zone;
          return (
            <div
              key={s.zone}
              title={`${ZONE_LABEL[s.zone]}: ${inr(s.from)} – ${inr(s.to)}`}
              className="flex h-full min-w-0 items-center justify-center overflow-hidden"
              style={{ width: `${width}%`, background: s.color }}
            >
              {width > 11 && (
                <span
                  className={`truncate px-1 text-[11px] ${current ? "font-bold" : ""}`}
                  style={{ color: readableOn(s.zone) }}
                >
                  {ZONE_LABEL[s.zone]}
                </span>
              )}
            </div>
          );
        })}
        <div
          aria-hidden
          className="absolute top-0 h-full w-[2px]"
          style={{ left: `${x(cmp)}%`, background: "var(--viz-ink)" }}
        />
      </div>
      {/* buy zone track (and the methods' fair value range when they disagree) */}
      <div className="bg-muted relative h-3 w-full rounded-sm">
        {levels.fair_value_low != null && levels.fair_value_high != null && (
          <div
            className="absolute top-1 h-1 rounded-sm"
            title={`Fair value range ${inr(levels.fair_value_low)} – ${inr(levels.fair_value_high)}`}
            style={{
              left: `${x(levels.fair_value_low)}%`,
              width: `${Math.max(0.6, x(levels.fair_value_high) - x(levels.fair_value_low))}%`,
              background: "var(--viz-ink-2)",
            }}
          />
        )}
        {zoneBz && (
          <div
            className="absolute top-0 h-full rounded-sm"
            title={`Buy zone ${inr(zoneBz.low)} – ${inr(zoneBz.high)}`}
            style={{
              left: `${x(zoneBz.low!)}%`,
              width: `${Math.max(0.6, x(zoneBz.high!) - x(zoneBz.low!))}%`,
              background: "var(--viz-s1)",
            }}
          />
        )}
      </div>
      {/* level ticks */}
      <div className="relative h-10 w-full">
        {placed.map((m) => (
          <div
            key={m.label}
            className={`absolute flex flex-col ${m.x > 0.88 ? "items-end" : m.x < 0.12 ? "items-start" : "items-center"}`}
            style={{ left: `${m.x * 100}%`, top: m.row * 18, transform: edgeShift(m.x) }}
          >
            <span aria-hidden className="h-1.5 w-px" style={{ background: "var(--viz-ink-2)" }} />
            <span className={`text-[11px] whitespace-nowrap ${m.strong ? "font-semibold" : "text-muted-foreground"}`}>
              {m.label} {inr(m.value)}
            </span>
          </div>
        ))}
      </div>
      <figcaption className="text-muted-foreground flex flex-wrap gap-x-4 gap-y-1 text-xs">
        <span>
          Zone: <strong className="text-foreground">{report.zone ? ZONE_LABEL[report.zone] : "—"}</strong>
        </span>
        <span>
          Buy zone:{" "}
          <strong className="text-foreground">
            {zoneBz ? `${inr(zoneBz.low)} – ${inr(zoneBz.high)}` : bz?.status === "suppressed" ? "suppressed (Stage 4)" : "none yet"}
          </strong>
        </span>
        <span>Invalidation: {inr(report.invalidation)}</span>
        {levels.fair_value_low != null && levels.fair_value_high != null && (
          <span className="text-amber-700 dark:text-amber-400">
            Methods disagree: fair value range{" "}
            <strong>
              {inr(levels.fair_value_low)} – {inr(levels.fair_value_high)}
            </strong>{" "}
            (blend {inr(levels.fair_value)})
          </span>
        )}
        <span>
          MoS {levels.mos_pct != null ? `${(levels.mos_pct * 100).toFixed(1)}%` : "—"} · confidence{" "}
          {levels.confidence ?? "—"}
        </span>
      </figcaption>
    </figure>
  );
}

function readableOn(zone: string): string {
  // Saturated ends carry light text; the pale inner zones carry ink.
  return zone === "deep_discount" || zone === "extreme_premium" ? "#ffffff" : "var(--viz-ink)";
}
