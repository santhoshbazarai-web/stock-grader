// Small design-system pieces: Stat, ZoneBadge, Skeleton, EmptyState, Sparkline, Tabs.
"use client";

import { useRef } from "react";

import { Badge } from "@/components/ui/badge";
import { ZONE_LABEL, pct, zoneTone, type Tone } from "@/lib/format";
import { cn } from "@/lib/utils";

import { TONE_CHIP, TONE_TEXT, TONE_VAR } from "./tone";

export function Stat({
  label,
  value,
  sub,
  tone,
  className,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: Tone;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-0.5", className)}>
      <span className="text-muted-foreground text-xs">{label}</span>
      <span className={cn("tnum text-lg leading-tight font-semibold", tone && TONE_TEXT[tone])}>{value}</span>
      {sub != null && <span className="text-muted-foreground tnum text-xs">{sub}</span>}
    </div>
  );
}

/** Zone chip with the distance to fair value ("12% below fair value") and the confidence. */
export function ZoneBadge({
  zone,
  cmp,
  fairValue,
  confidence,
}: {
  zone: string | null;
  cmp: number;
  fairValue: number | null;
  confidence?: string | null;
}) {
  const tone = zoneTone(zone);
  const gap = fairValue ? cmp / fairValue - 1 : null;
  const text =
    gap == null
      ? "no fair value"
      : `${pct(Math.abs(gap), 0)} ${gap <= 0 ? "below" : "above"} fair value`;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span className={cn("rounded-md px-2 py-0.5 text-xs font-semibold", TONE_CHIP[tone])}>
        {zone ? ZONE_LABEL[zone] : "No zone"}
      </span>
      <span className={cn("text-xs", TONE_TEXT[tone])}>{text}</span>
      {confidence && (
        <Badge variant="outline" className="text-[10px] uppercase" aria-label={`${confidence} confidence`}>
          {confidence} confidence
        </Badge>
      )}
    </span>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("bg-muted animate-pulse rounded-md", className ?? "h-4 w-full")} />;
}

export function EmptyState({
  title,
  children,
  icon,
}: {
  title: string;
  children?: React.ReactNode;
  icon?: React.ReactNode;
}) {
  return (
    <div role="status" className="text-muted-foreground flex flex-col items-center gap-2 rounded-xl border border-dashed px-6 py-12 text-center">
      {icon}
      <p className="text-foreground text-base font-medium">{title}</p>
      {children && <p className="max-w-md text-sm">{children}</p>}
    </div>
  );
}

/** Tiny trend line; the last point is marked. */
export function Sparkline({
  values,
  tone = "unknown",
  width = 96,
  height = 28,
  label,
}: {
  values: (number | null)[];
  tone?: Tone;
  width?: number;
  height?: number;
  label?: string;
}) {
  const v = values.filter((x): x is number => x != null && Number.isFinite(x));
  if (v.length < 2) return <span className="text-muted-foreground text-xs">—</span>;
  const lo = Math.min(...v);
  const hi = Math.max(...v);
  const span = hi - lo || 1;
  const pts = v.map((y, i) => [(i / (v.length - 1)) * (width - 4) + 2, height - 3 - ((y - lo) / span) * (height - 6)]);
  const color = TONE_VAR[tone];
  return (
    <svg width={width} height={height} role="img" aria-label={label ?? "trend"} viewBox={`0 0 ${width} ${height}`}>
      <polyline fill="none" stroke={color} strokeWidth={1.75} strokeLinejoin="round" points={pts.map((p) => p.join(",")).join(" ")} />
      <circle cx={pts.at(-1)![0]} cy={pts.at(-1)![1]} r={2.5} fill={color} />
    </svg>
  );
}

export type TabDef = { id: string; label: string; soon?: boolean };

/** Accessible tabs: arrow keys move, Home/End jump; the panel is rendered by the caller. */
export function Tabs({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: TabDef[];
  value: string;
  onChange: (id: string) => void;
  label: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  function onKey(e: React.KeyboardEvent, i: number) {
    const n = tabs.length;
    const next = e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i + n - 1) % n : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : null;
    if (next == null) return;
    e.preventDefault();
    refs.current[next]?.focus();
    onChange(tabs[next].id);
  }
  return (
    <div role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto border-b">
      {tabs.map((t, i) => {
        const active = t.id === value;
        return (
          <button
            key={t.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={active}
            aria-controls={`panel-${t.id}`}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(t.id)}
            onKeyDown={(e) => onKey(e, i)}
            className={cn(
              "-mb-px shrink-0 border-b-2 px-3 py-2 text-sm whitespace-nowrap transition-colors",
              active ? "border-[var(--brand)] font-medium text-foreground" : "text-muted-foreground hover:text-foreground border-transparent",
            )}
          >
            {t.label}
            {t.soon && <span className="text-muted-foreground ml-1 text-[10px]">soon</span>}
          </button>
        );
      })}
    </div>
  );
}
