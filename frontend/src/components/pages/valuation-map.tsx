"use client";

// Valuation Map: treemap (market cap area, colour = discount / day change / grade), the zone ×
// grade distribution, per-zone symbol chips, and a note on stocks left out for thin data.
import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { Page } from "@/components/common";
import { EmptyState, Skeleton } from "@/components/ds";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { pct, ZONE_LABEL, zoneTone } from "@/lib/format";
import { TONE_TEXT } from "@/components/ds";
import {
  BANDS,
  buildTree,
  describe,
  distribution,
  legendFor,
  fillFor,
  textOn,
  type ColourBy,
  type GroupBy,
  type MapRow,
  type ValuationMapData,
  ZONES,
} from "@/lib/valuation-map";

const Treemap = dynamic(() => import("./valuation-treemap"), { ssr: false, loading: () => <Skeleton className="h-[460px]" /> });

const COLOUR_LABEL: Record<ColourBy, string> = { discount: "Discount / premium", day_change: "Day change", grade: "Grade" };

function Select<T extends string>({ label, value, onChange, options }: { label: string; value: T; onChange: (v: T) => void; options: Record<T, string> }) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value as T)} className="bg-background rounded-md border px-2 py-1">
        {(Object.keys(options) as T[]).map((k) => (
          <option key={k} value={k}>
            {options[k]}
          </option>
        ))}
      </select>
    </label>
  );
}

function Distribution({ rows }: { rows: MapRow[] }) {
  const { zones, atDiscountOrFairPct } = distribution(rows);
  const max = Math.max(1, ...zones.map((z) => z.total));
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm font-medium" data-testid="cheap-share">
        {atDiscountOrFairPct == null ? "No stocks to compare." : `${(atDiscountOrFairPct * 100).toFixed(0)}% of the universe is at discount or fair.`}
      </p>
      <ul className="flex flex-col gap-2" aria-label="Distribution by zone and grade">
        {zones.map((z) => (
          <li key={z.zone} className="grid grid-cols-[8.5rem_1fr_6rem] items-center gap-3 text-sm">
            <span className={TONE_TEXT[zoneTone(z.zone)]}>{z.label}</span>
            <span className="flex h-5 overflow-hidden rounded bg-muted" role="img" aria-label={`${z.label}: ${z.bands.map((b) => `${b.label} ${b.count}`).join(", ")}`}>
              {z.bands.filter((b) => b.count > 0).map((b) => (
                <span key={b.id} title={`${b.label}: ${b.count}`} className="grid place-items-center text-[10px] font-medium" style={{ width: `${(b.count / max) * 100}%`, background: b.fill, color: textOn(b.fill) }}>
                  {b.count}
                </span>
              ))}
            </span>
            <span className="tnum text-right text-xs">
              {z.total} · {pct(z.pct, 0)}
            </span>
          </li>
        ))}
      </ul>
      <ul className="text-muted-foreground flex flex-wrap gap-3 text-xs" aria-label="Grade bands">
        {BANDS.map((b) => (
          <li key={b.id} className="flex items-center gap-1">
            <span aria-hidden className="inline-block size-3 rounded-sm" style={{ background: b.fill }} />
            {b.label}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Chips({ rows }: { rows: MapRow[] }) {
  const [sort, setSort] = useState<"az" | "discount">("discount");
  const [all, setAll] = useState(false);
  const LIMIT = 24;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground text-xs">Sort</span>
        {(["discount", "az"] as const).map((s) => (
          <Button key={s} size="sm" variant={sort === s ? "default" : "outline"} aria-pressed={sort === s} onClick={() => setSort(s)}>
            {s === "az" ? "A-Z" : "By discount"}
          </Button>
        ))}
        <Button size="sm" variant="ghost" onClick={() => setAll((a) => !a)} aria-pressed={all}>
          {all ? "Show fewer" : "Show all"}
        </Button>
      </div>
      {ZONES.map((z) => {
        const inZone = rows.filter((r) => r.zone === z).sort((a, b) => (sort === "az" ? a.symbol.localeCompare(b.symbol) : a.discount_pct - b.discount_pct));
        const shown = all ? inZone : inZone.slice(0, LIMIT);
        return (
          <section key={z} aria-label={`${ZONE_LABEL[z]} stocks`} className="flex flex-col gap-1.5">
            <h3 className={`text-sm font-semibold ${TONE_TEXT[zoneTone(z)]}`}>
              {ZONE_LABEL[z]} <span className="text-muted-foreground font-normal">({inZone.length})</span>
            </h3>
            {inZone.length === 0 ? (
              <p className="text-muted-foreground text-xs">None.</p>
            ) : (
              <ul className="flex flex-wrap gap-1.5">
                {shown.map((r) => {
                  const fill = fillFor(r, "grade");
                  return (
                    <li key={r.symbol}>
                      <Link href={`/stocks/${r.symbol}`} title={`${r.name ?? r.symbol}: grade ${r.grade ?? "n/a"}, ${(r.discount_pct * 100).toFixed(0)}% vs fair value`} className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium" style={{ background: fill, color: textOn(fill) }}>
                        {r.symbol}
                        <span className="opacity-80">{r.grade ?? ""}</span>
                      </Link>
                    </li>
                  );
                })}
                {!all && inZone.length > LIMIT && <li className="text-muted-foreground self-center text-xs">+{inZone.length - LIMIT} more</li>}
              </ul>
            )}
          </section>
        );
      })}
    </div>
  );
}

export function ValuationMap() {
  const [data, setData] = useState<ValuationMapData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [colourBy, setColourBy] = useState<ColourBy>("discount");
  const [groupBy, setGroupBy] = useState<GroupBy>("sector");
  const [zoom, setZoom] = useState<string | null>(null);
  const [hover, setHover] = useState<string>("");

  useEffect(() => {
    let live = true;
    api<ValuationMapData>("/valuation-map?universe=NIFTY500")
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e instanceof ApiError ? e.detail : "unavailable"));
    return () => {
      live = false;
    };
  }, []);

  const { tree, noCap } = useMemo(() => buildTree(data?.rows ?? [], groupBy, zoom), [data, groupBy, zoom]);

  return (
    <Page>
      <h1 className="text-xl font-semibold">Valuation Map</h1>
      {error && <EmptyState title="Valuation map unavailable">{error}</EmptyState>}
      {!data && !error && <Skeleton className="h-[460px]" />}
      {data && data.rows.length === 0 && <EmptyState title="No stocks to map yet">Reports with at least provisional data appear here once they are built.</EmptyState>}
      {data && data.rows.length > 0 && (
        <>
          <Card>
            <CardHeader className="gap-3">
              <CardTitle className="text-base">
                {data.universe} by {groupBy} <span className="text-muted-foreground text-xs font-normal">· area = market cap · colour = {COLOUR_LABEL[colourBy].toLowerCase()}</span>
              </CardTitle>
              <div className="flex flex-wrap items-center gap-4">
                <Select label="Colour by" value={colourBy} onChange={setColourBy} options={COLOUR_LABEL} />
                <Select label="Group by" value={groupBy} onChange={(g) => { setGroupBy(g); setZoom(null); }} options={{ sector: "Sector", industry: "Industry", durability: "Durability (proxy)" }} />
                {zoom && (
                  <Button size="sm" variant="outline" onClick={() => setZoom(null)}>
                    Reset zoom ({zoom})
                  </Button>
                )}
              </div>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              <Treemap
                tree={tree}
                colourBy={colourBy}
                onZoom={setZoom}
                onHover={(r) => {
                  setHover(r ? describe(r) : "");
                }}
              />
              <p className="text-muted-foreground min-h-5 text-xs" aria-live="polite" data-testid="tile-readout">
                {hover || "Hover a tile for details; click a stock to open it, a group name to zoom."}
              </p>
              <ul className="flex flex-wrap gap-x-3 gap-y-1 text-xs" aria-label="Colour legend">
                {legendFor(colourBy).map((l) => (
                  <li key={l.label} className="flex items-center gap-1">
                    <span aria-hidden className="inline-block size-3 rounded-sm border" style={{ background: l.fill }} />
                    {l.label}
                  </li>
                ))}
                <li className="flex items-center gap-1">
                  <span aria-hidden className="inline-block size-3 rounded-sm border" style={{ background: "#b9b8b2" }} />
                  unknown
                </li>
              </ul>
            </CardContent>
          </Card>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Distribution</CardTitle>
              </CardHeader>
              <CardContent>
                <Distribution rows={data.rows} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">By zone</CardTitle>
              </CardHeader>
              <CardContent>
                <Chips rows={data.rows} />
              </CardContent>
            </Card>
          </div>
          <p className="text-muted-foreground text-xs" data-testid="map-footnote">
            {data.rows.length} of {data.total} stocks shown.
            {data.excluded > 0 && ` ${data.excluded} excluded for insufficient data (below provisional depth, or no fair value).`}
            {noCap > 0 && ` ${noCap} without a market cap are not drawn in the treemap.`}
            {data.universe_note ? ` ${data.universe_note}.` : ""}
          </p>
        </>
      )}
    </Page>
  );
}
