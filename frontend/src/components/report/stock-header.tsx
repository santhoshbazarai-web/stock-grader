"use client";

// Sticky stock header: breadcrumb (sector › industry), name, NSE price and day change, grade,
// action, zone with the distance to fair value and confidence, data depth, watchlist, refresh.
import { RefreshCw, Star } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { ZoneBadge } from "@/components/ds";
import { ActionBadge, DurabilityBadge, GradeBadge } from "@/components/common";
import { AddAlertButton } from "@/components/add-alert";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { inr, signedPct, titleCase } from "@/lib/format";
import type { PipelineRun, StockReport, WatchlistItem } from "@/lib/types";

import { DepthBadge } from "./panels";

export function StockHeader({ report, onRun }: { report: StockReport; onRun?: (run: PipelineRun) => void }) {
  const [watching, setWatching] = useState<boolean | null>(null);
  const [note, setNote] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api<WatchlistItem[]>("/watchlist")
      .then((w) => live && setWatching(w.some((i) => i.symbol === report.symbol)))
      .catch(() => live && setWatching(null));
    return () => {
      live = false;
    };
  }, [report.symbol]);

  async function toggleWatch() {
    try {
      if (watching) await api(`/watchlist/${report.symbol}`, { method: "DELETE" });
      else await api("/watchlist", { method: "POST", body: JSON.stringify({ symbol: report.symbol, notes: null }) });
      setWatching(!watching);
    } catch (e) {
      setNote(e instanceof ApiError ? e.detail : "watchlist unavailable");
    }
  }
  async function refresh() {
    try {
      const r = await api<{ queued: boolean; run_id: number }>(`/stocks/${report.symbol}/refresh`, { method: "POST" });
      setNote(r.queued ? null : "Already running");
      onRun?.(await api<PipelineRun>(`/pipeline/${r.run_id}`));
    } catch (e) {
      setNote(e instanceof ApiError ? e.detail : "refresh failed");
    }
  }

  const change = report.day_change_pct ?? null;
  const changeColor = change == null ? undefined : change >= 0 ? "var(--viz-good)" : "var(--viz-critical)";
  const crumbs = [report.sector && report.sector !== "default" ? titleCase(report.sector) : null, report.industry].filter(Boolean);
  return (
    <header className="bg-background/95 sticky top-[3.25rem] z-20 -mx-4 flex flex-col gap-2 border-b px-4 py-3 backdrop-blur sm:-mx-6 sm:px-6">
      <nav aria-label="Breadcrumb" className="text-muted-foreground flex flex-wrap items-center gap-1 text-xs">
        <Link href="/screener" className="hover:underline">
          Stocks
        </Link>
        {crumbs.map((c) => (
          <span key={String(c)} className="flex items-center gap-1">
            <span aria-hidden>›</span>
            {c}
          </span>
        ))}
        <span aria-hidden>›</span>
        <span aria-current="page" className="text-foreground">
          {report.symbol}
        </span>
      </nav>
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h1 className="truncate text-xl font-semibold tracking-tight sm:text-2xl">
            {report.name ?? report.symbol} <span className="text-muted-foreground text-sm font-normal">NSE: {report.symbol}</span>
          </h1>
          <p className="flex flex-wrap items-baseline gap-2">
            <span className="tnum text-2xl font-semibold">{inr(report.cmp)}</span>
            <span className="tnum text-sm font-medium" style={{ color: changeColor }} aria-label={change == null ? "day change unavailable" : `day change ${signedPct(change, 2)}`}>
              {change == null ? "—" : `${change >= 0 ? "▲" : "▼"} ${signedPct(change, 2)}`}
            </span>
            <span className="text-muted-foreground text-xs">as of {report.as_of}</span>
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <GradeBadge label={report.grade_label} />
          <DurabilityBadge durability={report.durability} />
          <ActionBadge action={report.action} />
          <ZoneBadge zone={report.zone} cmp={report.cmp} fairValue={report.levels.fair_value} confidence={report.levels.confidence} />
          {report.data_depth && <DepthBadge depth={report.data_depth} />}
          <Button size="sm" variant={watching ? "secondary" : "outline"} onClick={toggleWatch} disabled={watching === null} aria-pressed={!!watching}>
            <Star className={watching ? "fill-current" : ""} /> {watching ? "In watchlist" : "Add to watchlist"}
          </Button>
          <AddAlertButton symbol={report.symbol} />
          <Button size="sm" variant="outline" onClick={refresh}>
            <RefreshCw /> Refresh data
          </Button>
          {note && (
            <span role="status" className="text-muted-foreground text-xs">
              {note}
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
