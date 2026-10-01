"use client";

// Watchlist & alerts (SPEC §9). Alerts are in-app notifications evaluated by the intraday job
// (P14): they never place orders or broker GTTs (AGENTS.md rule 7).
import { Trash2 } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ActionBadge, Empty, ErrorText, GradeBadge, Page } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { inr, ZONE_LABEL } from "@/lib/format";
import type { Alert, AlertType, WatchlistItem } from "@/lib/types";

export const ALERT_TYPES: { value: AlertType; label: string }[] = [
  { value: "enters_buy_zone", label: "Price enters the buy zone" },
  { value: "crosses_fv", label: "Price crosses fair value" },
  { value: "crosses_top_band", label: "Price crosses the top band" },
  { value: "crosses_invalidation", label: "Price crosses the invalidation level" },
];
const SYMBOL_RE = /^[A-Za-z0-9&_.-]{1,32}$/;

const input = "border-input bg-background h-8 rounded-md border px-2 text-sm";

export function Watchlist() {
  const [items, setItems] = useState<WatchlistItem[] | null>(null);
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wSymbol, setWSymbol] = useState("");
  const [notes, setNotes] = useState("");
  const [aSymbol, setASymbol] = useState("");
  const [aType, setAType] = useState<AlertType>("enters_buy_zone");

  const load = useCallback(() => {
    api<WatchlistItem[]>("/watchlist").then(setItems).catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
    api<Alert[]>("/alerts").then(setAlerts).catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, []);
  useEffect(load, [load]);

  async function run(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "request failed");
    }
  }

  function addWatch(e: React.FormEvent) {
    e.preventDefault();
    if (!SYMBOL_RE.test(wSymbol.trim())) return setError("Enter an NSE symbol, e.g. TCS");
    run(async () => {
      await api("/watchlist", { method: "POST", body: JSON.stringify({ symbol: wSymbol.trim(), notes: notes || null }) });
      setWSymbol("");
      setNotes("");
    });
  }

  function addAlert(e: React.FormEvent) {
    e.preventDefault();
    if (!SYMBOL_RE.test(aSymbol.trim())) return setError("Enter an NSE symbol, e.g. TCS");
    run(async () => {
      await api("/alerts", { method: "POST", body: JSON.stringify({ symbol: aSymbol.trim(), alert_type: aType }) });
      setASymbol("");
    });
  }

  return (
    <Page>
      <h1 className="text-2xl font-semibold tracking-tight">Watchlist & alerts</h1>
      {error && <ErrorText>{error}</ErrorText>}
      <Card className="gap-4">
        <CardHeader>
          <CardTitle className="text-base">Watchlist</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <form onSubmit={addWatch} className="flex flex-wrap items-end gap-2" aria-label="Add to watchlist">
            <input aria-label="Symbol" placeholder="Symbol" value={wSymbol} onChange={(e) => setWSymbol(e.target.value)} className={`${input} w-32 uppercase`} />
            <input aria-label="Notes" placeholder="Notes (optional)" value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={2000} className={`${input} w-72`} />
            <Button size="sm" type="submit">
              Add
            </Button>
            <span className="text-muted-foreground text-xs">New symbols join the daily data jobs.</span>
          </form>
          {items == null ? (
            <p className="text-muted-foreground text-sm">Loading…</p>
          ) : items.length === 0 ? (
            <Empty>Your watchlist is empty.</Empty>
          ) : (
            <table className="w-full text-sm tabular-nums" aria-label="Watchlist">
              <thead className="text-muted-foreground text-xs">
                <tr>
                  <th className="px-2 py-1 text-left font-normal">Stock</th>
                  <th className="px-2 py-1 text-left font-normal">Grade</th>
                  <th className="px-2 py-1 text-left font-normal">Zone</th>
                  <th className="px-2 py-1 text-right font-normal">CMP</th>
                  <th className="px-2 py-1 text-left font-normal">Action</th>
                  <th className="px-2 py-1 text-left font-normal">Notes</th>
                  <th className="py-1" />
                </tr>
              </thead>
              <tbody>
                {items.map((w) => (
                  <tr key={w.symbol} className="border-t">
                    <td className="px-2 py-2">
                      <Link href={`/stocks/${w.symbol}`} className="font-medium hover:underline">
                        {w.symbol}
                      </Link>
                      <div className="text-muted-foreground text-xs">{w.name}</div>
                    </td>
                    <td className="px-2 py-2">
                      <GradeBadge label={w.grade ? w.grade.replace("_plus", "+") : null} />
                    </td>
                    <td className="px-2 py-2 text-xs">{w.zone ? ZONE_LABEL[w.zone] : "no report yet"}</td>
                    <td className="px-2 py-2 text-right">{inr(w.cmp)}</td>
                    <td className="px-2 py-2">
                      <ActionBadge action={w.action} />
                    </td>
                    <td className="text-muted-foreground max-w-64 px-2 py-2 text-xs">{w.notes}</td>
                    <td className="px-2 py-2 text-right">
                      <Button size="sm" variant="ghost" aria-label={`Remove ${w.symbol}`} onClick={() => run(() => api(`/watchlist/${w.symbol}`, { method: "DELETE" }))}>
                        <Trash2 />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <Card className="gap-4">
        <CardHeader>
          <CardTitle className="text-base">Alerts</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <form onSubmit={addAlert} className="flex flex-wrap items-end gap-2" aria-label="Create alert">
            <input aria-label="Alert symbol" placeholder="Symbol" value={aSymbol} onChange={(e) => setASymbol(e.target.value)} className={`${input} w-32 uppercase`} />
            <select aria-label="Alert type" value={aType} onChange={(e) => setAType(e.target.value as AlertType)} className={input}>
              {ALERT_TYPES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
            <Button size="sm" type="submit">
              Create alert
            </Button>
            <span className="text-muted-foreground text-xs">In-app notifications only — no orders are placed.</span>
          </form>
          {alerts == null ? (
            <p className="text-muted-foreground text-sm">Loading…</p>
          ) : alerts.length === 0 ? (
            <Empty>No alerts yet.</Empty>
          ) : (
            <table className="w-full text-sm tabular-nums" aria-label="Alerts">
              <thead className="text-muted-foreground text-xs">
                <tr>
                  <th className="px-2 py-1 text-left font-normal">Stock</th>
                  <th className="px-2 py-1 text-left font-normal">When</th>
                  <th className="px-2 py-1 text-left font-normal">Last triggered</th>
                  <th className="px-2 py-1 text-left font-normal">Active</th>
                  <th className="py-1" />
                </tr>
              </thead>
              <tbody>
                {alerts.map((a) => (
                  <tr key={a.id} className="border-t">
                    <td className="px-2 py-2">
                      <Link href={`/stocks/${a.symbol}`} className="font-medium hover:underline">
                        {a.symbol}
                      </Link>
                    </td>
                    <td className="px-2 py-2 text-xs">{ALERT_TYPES.find((t) => t.value === a.alert_type)?.label}</td>
                    <td className="text-muted-foreground px-2 py-2 text-xs">
                      {a.last_triggered_at
                        ? `${new Date(a.last_triggered_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })} at ${inr(a.last_triggered_price)}`
                        : "never"}
                    </td>
                    <td className="px-2 py-2">
                      <label className="inline-flex items-center gap-2 text-xs">
                        <input
                          type="checkbox"
                          checked={a.is_active}
                          onChange={(e) =>
                            run(() =>
                              api("/alerts", {
                                method: "POST",
                                body: JSON.stringify({ symbol: a.symbol, alert_type: a.alert_type, is_active: e.target.checked }),
                              }),
                            )
                          }
                        />
                        {a.is_active ? "on" : "paused"}
                      </label>
                    </td>
                    <td className="px-2 py-2 text-right">
                      <Button size="sm" variant="ghost" aria-label={`Delete alert ${a.id}`} onClick={() => run(() => api(`/alerts/${a.id}`, { method: "DELETE" }))}>
                        <Trash2 />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </Page>
  );
}
