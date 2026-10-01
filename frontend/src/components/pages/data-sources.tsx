"use client";

// Settings → Data sources: the last nse-diagnose / bse-diagnose result per endpoint (OK or
// blocked), with a Re-check button that runs both in the background (GET/POST /data-sources).
import { Loader2, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { BlockedNotice } from "@/components/blocked-notice";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import type { DataSources as DataSourcesT, SiteDiag } from "@/lib/types";

const POLL_MS = 3000;

function verdictBadge(verdict: string) {
  const ok = verdict === "OK" || verdict === "empty";
  const blocked = verdict.startsWith("blocked");
  return (
    <Badge
      variant={ok ? "secondary" : blocked ? "destructive" : "outline"}
      className="font-normal"
    >
      {verdict === "OK"
        ? "OK"
        : verdict === "empty"
          ? "OK (no rows)"
          : blocked
            ? "blocked"
            : verdict}
    </Badge>
  );
}

function when(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function Site({ label, d }: { label: "NSE" | "BSE"; d: SiteDiag | null }) {
  if (!d)
    return (
      <p className="text-muted-foreground text-sm">
        {label}: not checked yet. Use Re-check now, or run{" "}
        <code>python -m app.jobs {label.toLowerCase()}-diagnose</code>.
      </p>
    );
  const endpoints = Object.entries(d.summary);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline gap-2 text-sm">
        <span className="font-medium">{label}</span>
        <span className="text-muted-foreground text-xs">
          checked {when(d.checked_at)} ·{" "}
          {d.working_method
            ? `working method: ${d.working_method}`
            : "no method gets through"}
        </span>
      </div>
      {!d.working_method && <BlockedNotice site={label} compact />}
      <table className="w-full text-sm" aria-label={`${label} endpoints`}>
        <thead className="text-muted-foreground text-xs">
          <tr>
            <th className="py-1 pr-2 text-left font-normal">Endpoint</th>
            <th className="py-1 pr-2 text-left font-normal">Status</th>
            <th className="py-1 text-left font-normal">Method</th>
          </tr>
        </thead>
        <tbody>
          {endpoints.map(([name, s]) => (
            <tr key={name} className="border-t">
              <td className="py-1 pr-2">{name}</td>
              <td className="py-1 pr-2">{verdictBadge(s.verdict)}</td>
              <td className="text-muted-foreground py-1 text-xs">{s.method}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <details className="text-xs">
        <summary className="text-muted-foreground cursor-pointer">
          Every request ({d.rows.length})
        </summary>
        <div className="overflow-x-auto">
          <table className="mt-1 w-full tabular-nums">
            <thead className="text-muted-foreground">
              <tr>
                <th className="pr-2 text-left font-normal">Method</th>
                <th className="pr-2 text-left font-normal">Endpoint</th>
                <th className="pr-2 text-left font-normal">HTTP</th>
                <th className="pr-2 text-left font-normal">Server</th>
                <th className="pr-2 text-left font-normal">Cookies set</th>
                <th className="pr-2 text-right font-normal">Bytes</th>
                <th className="text-left font-normal">Verdict</th>
              </tr>
            </thead>
            <tbody>
              {d.rows.map((r, i) => (
                <tr key={i} className="border-t align-top" title={r.url}>
                  <td className="pr-2">{r.method}</td>
                  <td className="pr-2">{r.endpoint}</td>
                  <td className="pr-2">{r.status ?? "—"}</td>
                  <td className="pr-2">{r.server ?? "—"}</td>
                  <td className="pr-2">{r.cookie_names.join(", ") || "—"}</td>
                  <td className="pr-2 text-right">
                    {r.length.toLocaleString()}
                  </td>
                  <td>{r.verdict}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

export function DataSourcesCard() {
  const [data, setData] = useState<DataSourcesT | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api<DataSourcesT>("/data-sources")
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, []);
  useEffect(load, [load]);
  useEffect(() => {
    if (!data?.running) return;
    const t = setTimeout(load, POLL_MS);
    return () => clearTimeout(t);
  }, [data, load]);

  async function recheck() {
    try {
      setData(
        await api<DataSourcesT>("/data-sources/check", { method: "POST" }),
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "re-check failed");
    }
  }

  return (
    <Card className="gap-4" id="data-sources">
      <CardHeader>
        <CardTitle className="text-base">Data sources</CardTitle>
        <CardDescription>
          Whether NSE and BSE answer this connection, per endpoint the app uses
          (results filings, corporate actions, shareholding, board meetings,
          announcements, BSE scrip master). Each session method is tried in
          turn: a Chrome-like client, a headless browser, then plain requests.
          Exchanges change their protection without notice; when they block
          every method, the manual uploads below keep working.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={recheck}
            disabled={!!data?.running}
          >
            {data?.running ? (
              <Loader2 className="animate-spin" aria-hidden />
            ) : (
              <RefreshCw aria-hidden />
            )}
            Re-check now
          </Button>
          {data?.running && (
            <span className="text-muted-foreground text-xs" role="status">
              Checking at 1 request per second; this takes a minute or two…
            </span>
          )}
          {error && (
            <span className="text-destructive text-xs" role="alert">
              {error}
            </span>
          )}
        </div>
        {data && (
          <>
            <Site label="NSE" d={data.nse} />
            <Site label="BSE" d={data.bse} />
          </>
        )}
      </CardContent>
    </Card>
  );
}
