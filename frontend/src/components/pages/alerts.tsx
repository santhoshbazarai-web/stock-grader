"use client";

// Alerts (SPEC §9): every alert with filters, plus a form to add one. Alerts notify in-app and
// on Telegram; they never place orders or broker GTTs (AGENTS.md rule 7).
import { Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AddAlertForm } from "@/components/add-alert";
import { Empty, ErrorText, Page } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { ALERT_TYPES } from "@/lib/alerts";
import type { Alert } from "@/lib/types";

const field = "border-input bg-background h-8 rounded-md border px-2 text-sm";
const when = (iso: string) => new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });

export function AlertsPage() {
  const router = useRouter();
  const params = useSearchParams();
  const symbol = params.get("symbol") ?? "";
  const type = params.get("type") ?? "";
  const status = params.get("status") ?? "";
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    const p = new URLSearchParams();
    if (symbol) p.set("symbol", symbol);
    if (type) p.set("alert_type", type);
    if (status) p.set("status", status);
    api<Alert[]>(`/alerts?${p}`).then(setAlerts).catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, [symbol, type, status]);
  useEffect(load, [load]);

  function setFilter(key: string, value: string) {
    const p = new URLSearchParams(params.toString());
    if (value) p.set(key, value);
    else p.delete(key);
    router.replace(`/alerts${p.size ? `?${p}` : ""}`);
  }
  async function run(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "request failed");
    }
  }

  return (
    <Page>
      <h1 className="text-2xl font-semibold tracking-tight">Alerts</h1>
      {error && <ErrorText>{error}</ErrorText>}
      <Card className="gap-4">
        <CardHeader>
          <CardTitle className="text-base">Add alert</CardTitle>
        </CardHeader>
        <CardContent>
          <AddAlertForm onCreated={load} />
        </CardContent>
      </Card>
      <Card className="gap-4">
        <CardHeader>
          <CardTitle className="text-base">Your alerts</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Alert filters">
            <input aria-label="Filter by symbol" placeholder="Symbol" defaultValue={symbol} onBlur={(e) => setFilter("symbol", e.target.value.trim().toUpperCase())} onKeyDown={(e) => e.key === "Enter" && setFilter("symbol", e.currentTarget.value.trim().toUpperCase())} className={`${field} w-28 uppercase`} />
            <select aria-label="Filter by type" value={type} onChange={(e) => setFilter("type", e.target.value)} className={field}>
              <option value="">All types</option>
              {ALERT_TYPES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
            <select aria-label="Filter by status" value={status} onChange={(e) => setFilter("status", e.target.value)} className={field}>
              <option value="">Any status</option>
              <option value="active">Active</option>
              <option value="triggered">Triggered</option>
              <option value="paused">Paused</option>
            </select>
          </div>
          {alerts == null ? (
            <p className="text-muted-foreground text-sm">Loading…</p>
          ) : alerts.length === 0 ? (
            <Empty>No alerts match.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm tabular-nums" aria-label="Alerts">
                <thead className="text-muted-foreground text-xs">
                  <tr>
                    {["Symbol", "Company", "Type", "Condition", "Created", "Last updated", "Status", "Actions"].map((h) => (
                      <th key={h} scope="col" className="px-2 py-1 text-left font-normal whitespace-nowrap">
                        {h}
                      </th>
                    ))}
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
                      <td className="text-muted-foreground px-2 py-2 text-xs">{a.company}</td>
                      <td className="px-2 py-2 text-xs">{ALERT_TYPES.find((t) => t.value === a.alert_type)?.label.replace("…", "")}</td>
                      <td className="px-2 py-2 text-xs">{a.condition}</td>
                      <td className="text-muted-foreground px-2 py-2 text-xs whitespace-nowrap">{when(a.created_at)}</td>
                      <td className="text-muted-foreground px-2 py-2 text-xs whitespace-nowrap">{when(a.last_triggered_at ?? a.updated_at)}</td>
                      <td className="px-2 py-2 text-xs capitalize">{a.status}</td>
                      <td className="px-2 py-2 whitespace-nowrap">
                        <label className="mr-2 inline-flex items-center gap-1 text-xs">
                          <input
                            type="checkbox"
                            aria-label={`${a.is_active ? "Pause" : "Resume"} alert ${a.id}`}
                            checked={a.is_active}
                            onChange={(e) => run(() => api("/alerts", { method: "POST", body: JSON.stringify({ symbol: a.symbol, alert_type: a.alert_type, threshold: a.threshold, is_active: e.target.checked }) }))}
                          />
                          on
                        </label>
                        <Button size="sm" variant="ghost" aria-label={`Delete alert ${a.id}`} onClick={() => run(() => api(`/alerts/${a.id}`, { method: "DELETE" }))}>
                          <Trash2 />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </Page>
  );
}
