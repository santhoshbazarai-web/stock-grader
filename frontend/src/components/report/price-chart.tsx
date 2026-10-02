"use client";

// Weekly (or daily) candles with the SPEC §9 overlays: demand/supply zone rectangles, the
// buy zone, 30-week SMA, AVWAP lines and the valuation level lines (Baseline, FV, Top band),
// POC and invalidation. Overlays are computed on weekly bars (SPEC §6) in both views.
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type Time,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { AVWAP_LABEL, cssVar, inr } from "@/lib/format";
import { useThemeVersion } from "@/lib/hooks";
import type { Bar, StockReport, TechnicalDebug } from "@/lib/types";

import { BoxesPrimitive, type Box } from "./zones-primitive";

// Colour follows the entity: each AVWAP anchor keeps its slot whatever else is shown.
const AVWAP_SLOT: Record<string, string> = {
  low_52w: "--viz-s2",
  last_results_date: "--viz-s3",
  last_major_swing_low: "--viz-s4",
};

type Readout = { time: string; bar: Bar | null; lines: { label: string; value: number | null }[] };

function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.replace(/./g, (c) => c + c) : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export function debugQuery(r: StockReport): string {
  const q = new URLSearchParams({ include_daily: "true" });
  const { baseline, fair_value, top_band, mos_pct } = r.levels;
  if (fair_value != null) {
    q.set("fair_value", String(fair_value));
    if (baseline != null) q.set("baseline", String(baseline));
    if (top_band != null) q.set("top_band", String(top_band));
    if (mos_pct != null) q.set("mos", String(mos_pct));
    if (r.grade) q.set("grade", r.grade);
  }
  return q.toString();
}

type Range = "1M" | "6M" | "1Y" | "5Y" | "Max";
const RANGES: Range[] = ["1M", "6M", "1Y", "5Y", "Max"];
const RANGE_BARS: Record<Range, number | null> = { "1M": 22, "6M": 126, "1Y": 252, "5Y": 260, Max: null };
type Layer = "sma" | "avwap" | "zones" | "levels";
const LAYER_LABEL: Record<Layer, string> = { sma: "30-wk SMA", avwap: "AVWAP", zones: "Zones & buy zone", levels: "Valuation levels" };

export function PriceChart({ report }: { report: StockReport }) {
  const host = useRef<HTMLDivElement>(null);
  const [range, setRange] = useState<Range>("1Y");
  const [layers, setLayers] = useState<Record<Layer, boolean>>({ sma: true, avwap: false, zones: true, levels: true });
  // 1M-1Y read best on daily candles; 5Y / Max on weekly ones
  const tf = range === "5Y" || range === "Max" ? "weekly" : "daily";
  const [data, setData] = useState<TechnicalDebug | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [readout, setReadout] = useState<Readout | null>(null);
  const theme = useThemeVersion();
  const query = useMemo(() => debugQuery(report), [report]);

  useEffect(() => {
    let live = true;
    setError(null);
    api<TechnicalDebug>(`/stocks/${report.symbol}/technical/debug?${query}`)
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e instanceof ApiError ? e.detail : "chart data unavailable"));
    return () => {
      live = false;
    };
  }, [report.symbol, query]);

  useEffect(() => {
    const el = host.current;
    if (!el || !data) return;
    const c = (name: string) => cssVar(name);
    const chart: IChartApi = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: c("--viz-surface") },
        textColor: c("--viz-muted"),
        fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif",
        attributionLogo: false,
      },
      grid: { vertLines: { visible: false }, horzLines: { color: c("--viz-grid") } },
      rightPriceScale: { borderColor: c("--viz-axis") },
      timeScale: { borderColor: c("--viz-axis") },
      crosshair: { mode: CrosshairMode.Normal },
    });
    const bars = tf === "daily" && data.daily_bars ? data.daily_bars : data.bars;
    const candles: ISeriesApi<"Candlestick"> = chart.addSeries(CandlestickSeries, {
      upColor: c("--viz-good"),
      downColor: c("--viz-critical"),
      wickUpColor: c("--viz-good"),
      wickDownColor: c("--viz-critical"),
      borderVisible: false,
      priceLineVisible: false,
    });
    candles.setData(bars.map((b) => ({ time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close })));

    const line = (color: string) =>
      chart.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
    const sma = line(c("--viz-s1"));
    if (layers.sma) sma.setData(data.sma_30w.map((p) => ({ time: p.time as Time, value: p.value })));
    const avwapSeries = (layers.avwap ? data.avwaps : []).map((a) => {
      const s = line(c(AVWAP_SLOT[a.anchor] ?? "--viz-muted"));
      s.setData(a.series.map((p) => ({ time: p.time as Time, value: p.value })));
      return { label: AVWAP_LABEL[a.anchor] ?? a.anchor, series: s };
    });

    // Valuation levels: dashed reference lines, labelled on the price axis.
    const lv = report.levels;
    const priceLine = (price: number | null, title: string, color: string, style = LineStyle.Dashed) => {
      if (price == null || !layers.levels) return;
      candles.createPriceLine({ price, title, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true });
    };
    priceLine(lv.baseline, "Baseline", c("--viz-ink-2"));
    priceLine(lv.fair_value, "FV", c("--viz-ink"));
    priceLine(lv.top_band, "Top band", c("--viz-ink-2"));
    priceLine(data.volume_profile?.poc ?? null, "POC", c("--viz-muted"), LineStyle.Dotted);
    priceLine(report.invalidation, "Invalidation", c("--viz-critical"), LineStyle.Dotted);

    // Zones (unbroken only; fresh ones stronger) and the buy zone.
    const good = c("--viz-good");
    const bad = c("--viz-critical");
    const boxes: Box[] = (layers.zones ? data.zones : [])
      .filter((z) => !z.broken)
      .map((z) => {
        const base = z.side === "demand" ? good : bad;
        return {
          from: z.start as Time,
          to: null,
          top: z.top,
          bottom: z.bottom,
          fill: withAlpha(base, z.fresh ? 0.16 : 0.07),
          stroke: withAlpha(base, z.fresh ? 0.55 : 0.25),
        };
      });
    const bz = report.buy_zone;
    if (layers.zones && bz?.status === "zone" && bz.low != null && bz.high != null) {
      const from = bars[Math.max(0, bars.length - (tf === "daily" ? 130 : 26))].time as Time;
      boxes.push({
        from,
        to: null,
        top: bz.high,
        bottom: bz.low,
        fill: withAlpha(c("--viz-s1"), 0.22),
        stroke: withAlpha(c("--viz-s1"), 0.9),
      });
    }
    candles.attachPrimitive(new BoxesPrimitive(boxes));
    const want = RANGE_BARS[range];
    if (want != null) {
      const n = bars.length;
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - want), to: n + 3 });
    } else {
      chart.timeScale().fitContent();
    }

    const lastReadout = (): Readout => ({
      time: bars.at(-1)?.time ?? "",
      bar: bars.at(-1) ?? null,
      lines: [
        { label: "30-wk SMA", value: data.sma_30w.at(-1)?.value ?? null },
        ...data.avwaps.map((a) => ({ label: AVWAP_LABEL[a.anchor] ?? a.anchor, value: a.series.at(-1)?.value ?? null })),
      ],
    });
    setReadout(lastReadout());
    const onMove = (p: MouseEventParams<Time>) => {
      if (!p.time) return setReadout(lastReadout());
      const bar = p.seriesData.get(candles) as Bar | undefined;
      const val = (s: ISeriesApi<"Line">) => (p.seriesData.get(s) as { value?: number } | undefined)?.value ?? null;
      setReadout({
        time: String(p.time),
        bar: bar ?? null,
        lines: [{ label: "30-wk SMA", value: val(sma) }, ...avwapSeries.map((a) => ({ label: a.label, value: val(a.series) }))],
      });
    };
    chart.subscribeCrosshairMove(onMove);
    return () => {
      chart.unsubscribeCrosshairMove(onMove);
      chart.remove();
    };
  }, [data, tf, range, layers, theme, report.levels, report.invalidation, report.buy_zone]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Legend data={data} />
        <div className="flex gap-1" role="group" aria-label="Range">
          {RANGES.map((r) => (
            <Button key={r} size="sm" variant={range === r ? "default" : "outline"} onClick={() => setRange(r)} aria-pressed={range === r}>
              {r}
            </Button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs" role="group" aria-label="Chart layers">
        {(Object.keys(LAYER_LABEL) as Layer[]).map((l) => (
          <label key={l} className="inline-flex items-center gap-1.5">
            <input type="checkbox" checked={layers[l]} onChange={(e) => setLayers((s) => ({ ...s, [l]: e.target.checked }))} />
            {LAYER_LABEL[l]}
          </label>
        ))}
      </div>
      {readout && (
        <p className="text-muted-foreground text-xs tabular-nums" aria-live="polite">
          <span className="text-foreground font-medium">{readout.time}</span>
          {readout.bar && (
            <>
              {" "}O {inr(readout.bar.open)} H {inr(readout.bar.high)} L {inr(readout.bar.low)} C{" "}
              <span className="text-foreground">{inr(readout.bar.close)}</span>
            </>
          )}
          {readout.lines.map((l) => (
            <span key={l.label}>
              {" · "}
              {l.label} {inr(l.value)}
            </span>
          ))}
        </p>
      )}
      {error ? (
        <p className="text-destructive text-sm">Chart unavailable: {error}</p>
      ) : (
        <div ref={host} className="h-[420px] w-full" data-testid="price-chart" />
      )}
    </div>
  );
}

function Swatch({ color, kind }: { color: string; kind: "line" | "box" | "dash" }) {
  if (kind === "box") {
    return <span aria-hidden className="inline-block h-3 w-4 rounded-[2px] border" style={{ background: color, borderColor: color, opacity: 0.6 }} />;
  }
  return (
    <span
      aria-hidden
      className="inline-block w-4"
      style={{ borderTop: `2px ${kind === "dash" ? "dashed" : "solid"} ${color}` }}
    />
  );
}

function Legend({ data }: { data: TechnicalDebug | null }) {
  const items: { label: string; color: string; kind: "line" | "box" | "dash" }[] = [
    { label: "30-wk SMA", color: "var(--viz-s1)", kind: "line" },
    ...(data?.avwaps ?? []).map((a) => ({
      label: AVWAP_LABEL[a.anchor] ?? a.anchor,
      color: `var(${AVWAP_SLOT[a.anchor] ?? "--viz-muted"})`,
      kind: "line" as const,
    })),
    { label: "Demand zone", color: "var(--viz-good)", kind: "box" },
    { label: "Supply zone", color: "var(--viz-critical)", kind: "box" },
    { label: "Buy zone", color: "var(--viz-s1)", kind: "box" },
    { label: "Baseline / FV / Top band", color: "var(--viz-ink-2)", kind: "dash" },
  ];
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs" aria-label="Chart legend">
      {items.map((i) => (
        <li key={i.label} className="text-muted-foreground flex items-center gap-1.5">
          <Swatch color={i.color} kind={i.kind} />
          {i.label}
        </li>
      ))}
    </ul>
  );
}
