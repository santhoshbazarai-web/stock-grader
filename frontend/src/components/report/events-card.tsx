"use client";

// Corporate events (SPEC v0.2 §3.8, §10 events): upcoming board meetings and the latest
// announcements, results filings, pledge / SAST / insider-trading disclosures and bulk / block
// deals for the stock. Red-flag categories (auditor resignation, rating downgrade, default,
// pledge invocation) are marked.
import { AlertTriangle, CalendarClock, ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { api, ApiError } from "@/lib/api";
import type { EventKind, StockEvent, StockEvents } from "@/lib/types";

const KIND: Record<EventKind, string> = {
  announcement: "Announcement",
  board_meeting: "Board meeting",
  results: "Results",
  pledge: "Pledge",
  sast: "SAST",
  insider_trade: "Insider trade",
  bulk_deal: "Bulk deal",
  block_deal: "Block deal",
};
const SHOWN = 12;

function day(e: StockEvent): string {
  const d = e.event_date ?? e.disseminated_at?.slice(0, 10);
  if (!d) return "—";
  return new Date(`${d}T00:00:00`).toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function Row({ e }: { e: StockEvent }) {
  return (
    <li className="flex items-start gap-3 py-1.5">
      <span className="text-muted-foreground w-24 shrink-0 text-xs whitespace-nowrap tabular-nums">
        {day(e)}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant="outline" className="text-[10px]">
            {KIND[e.kind] ?? e.kind}
          </Badge>
          {e.red_flag && (
            <span
              className="flex items-center gap-1 text-xs font-medium"
              style={{ color: "var(--viz-critical)" }}
            >
              <AlertTriangle className="size-3.5" aria-hidden /> red flag
            </span>
          )}
          <span className="text-muted-foreground text-[10px] uppercase">
            {e.exchange}
          </span>
        </div>
        <p className="break-words">
          {e.title}
          {e.url && (
            <a
              href={e.url}
              target="_blank"
              rel="noreferrer"
              className="text-muted-foreground ml-1 inline-flex align-middle"
              aria-label="Open the filing"
            >
              <ExternalLink className="size-3.5" />
            </a>
          )}
        </p>
        {e.detail && (
          <p className="text-muted-foreground line-clamp-2 text-xs">
            {e.detail}
          </p>
        )}
      </div>
    </li>
  );
}

export function EventsCard({ symbol }: { symbol: string }) {
  const [data, setData] = useState<StockEvents | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [all, setAll] = useState(false);

  useEffect(() => {
    let live = true;
    api<StockEvents>(`/stocks/${encodeURIComponent(symbol)}/events`)
      .then((d) => live && setData(d))
      .catch(
        (e) => live && setError(e instanceof ApiError ? e.detail : "failed"),
      );
    return () => {
      live = false;
    };
  }, [symbol]);

  if (error)
    return (
      <p className="text-muted-foreground text-sm">
        Events unavailable: {error}
      </p>
    );
  if (!data)
    return <p className="text-muted-foreground text-sm">Loading events…</p>;
  const shown = all ? data.events : data.events.slice(0, SHOWN);
  return (
    <div className="flex flex-col gap-3 text-sm">
      {data.upcoming.length > 0 && (
        <ul
          aria-label="Upcoming board meetings"
          className="flex flex-col gap-1"
        >
          {data.upcoming.map((e) => (
            <li key={e.id} className="flex items-center gap-2">
              <CalendarClock
                className="text-muted-foreground size-4 shrink-0"
                aria-hidden
              />
              <span className="whitespace-nowrap tabular-nums">{day(e)}</span>
              <span className="min-w-0 break-words">{e.title}</span>
            </li>
          ))}
        </ul>
      )}
      {data.events.length === 0 ? (
        <p className="text-muted-foreground text-xs">
          No corporate events on file for the last year (the events and
          results_watch jobs read the NSE and BSE feeds).
        </p>
      ) : (
        <ul
          aria-label="Corporate events"
          className="divide-border flex flex-col divide-y"
        >
          {shown.map((e) => (
            <Row key={e.id} e={e} />
          ))}
        </ul>
      )}
      {data.events.length > SHOWN && (
        <button
          type="button"
          className="text-muted-foreground self-start text-xs underline"
          onClick={() => setAll((v) => !v)}
        >
          {all ? "Show fewer" : `Show all ${data.events.length}`}
        </button>
      )}
    </div>
  );
}
