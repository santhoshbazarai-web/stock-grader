"use client";

// Dashboard (SPEC §9): broker connection status, data freshness, top A-grade stocks at their
// buy zone, triggered alerts.
import Link from "next/link";
import { useEffect, useState } from "react";

import { ActionBadge, Empty, ErrorText, GradeBadge, Page } from "@/components/common";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { inr, pct } from "@/lib/format";
import type { Alert, BrokerStatus, JobsView, ScreenerRow } from "@/lib/types";

import { BrokerList } from "./brokers";

const NEAR_ZONE = 0.05; // "near" = within 5% above the buy zone (display filter only)
const ALERT_LABEL: Record<string, string> = {
  enters_buy_zone: "entered buy zone",
  crosses_fv: "crossed fair value",
  crosses_top_band: "crossed top band",
  crosses_invalidation: "crossed invalidation",
};

function useLoad<T>(path: string): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api<T>(path)
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e instanceof ApiError ? e.detail : "failed to load"));
    return () => {
      live = false;
    };
  }, [path]);
  return { data, error };
}

export function daysOld(iso: string | null, now = new Date()): number | null {
  if (!iso) return null;
  return Math.floor((now.getTime() - new Date(iso).getTime()) / 86_400_000);
}

function Freshness({ jobs }: { jobs: JobsView }) {
  const f = jobs.freshness;
  const rows: [string, string | null][] = [
    ["Prices", f.prices],
    ["Delivery %", f.delivery],
    ["Technicals", f.technicals],
    ["Reports", f.reports],
    ["Shareholding (period)", f.shareholding_period],
    ["Fundamentals fetched", f.fundamentals_fetched],
  ];
  const failed = jobs.runs.filter((r) => r.status === "failed").slice(0, 3);
  return (
    <div className="flex flex-col gap-3 text-sm">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
        {rows.map(([k, v]) => {
          const age = daysOld(v);
          return (
            <div key={k}>
              <dt className="text-muted-foreground text-xs">{k}</dt>
              <dd className="tabular-nums">
                {v ? v.slice(0, 10) : "never"}
                {age != null && <span className="text-muted-foreground text-xs"> · {age}d ago</span>}
              </dd>
            </div>
          );
        })}
      </dl>
      <p className="text-muted-foreground text-xs">
        {jobs.open_data_gaps} open data gaps
        {jobs.refresh_queue.length > 0 && ` · refresh queued: ${jobs.refresh_queue.join(", ")}`}
      </p>
      {failed.length > 0 && (
        <ul className="flex flex-col gap-1 text-xs" aria-label="Recent failed jobs">
          {failed.map((r) => (
            <li key={r.id}>
              <span className="font-medium" style={{ color: "var(--viz-critical)" }}>
                {r.job_name} failed
              </span>{" "}
              <span className="text-muted-foreground">
                {r.started_at.slice(0, 16).replace("T", " ")} — {r.error?.slice(0, 120)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function BuyZoneList({ rows }: { rows: ScreenerRow[] }) {
  const sorted = [...rows]
    .filter((r) => r.pct_to_buy_zone != null && r.pct_to_buy_zone >= 0)
    .sort((a, b) => (a.pct_to_buy_zone ?? 0) - (b.pct_to_buy_zone ?? 0) || (b.total_score ?? 0) - (a.total_score ?? 0));
  if (sorted.length === 0) return <Empty>No A-grade stock is at or within {pct(NEAR_ZONE, 0)} of its buy zone.</Empty>;
  return (
    <table className="w-full text-sm tabular-nums">
      <thead className="text-muted-foreground text-xs">
        <tr>
          <th className="py-1 text-left font-normal">Stock</th>
          <th className="py-1 text-left font-normal">Grade</th>
          <th className="py-1 text-right font-normal">CMP</th>
          <th className="py-1 text-right font-normal">Buy zone</th>
          <th className="py-1 text-right font-normal">Distance</th>
          <th className="py-1 text-right font-normal">Action</th>
        </tr>
      </thead>
      <tbody>
        {sorted.slice(0, 10).map((r) => (
          <tr key={r.symbol} className="border-t">
            <td className="py-1.5">
              <Link href={`/stocks/${r.symbol}`} className="font-medium hover:underline">
                {r.symbol}
              </Link>
            </td>
            <td className="py-1.5">
              <GradeBadge label={r.grade_label} />
            </td>
            <td className="py-1.5 text-right">{inr(r.cmp)}</td>
            <td className="py-1.5 text-right">
              {inr(r.buy_zone_low)} – {inr(r.buy_zone_high)}
            </td>
            <td className="py-1.5 text-right">{r.pct_to_buy_zone === 0 ? "in zone" : `${pct(r.pct_to_buy_zone)} above`}</td>
            <td className="py-1.5 text-right">
              <ActionBadge action={r.action} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TriggeredAlerts({ alerts }: { alerts: Alert[] }) {
  const hits = alerts
    .filter((a) => a.last_triggered_at)
    .sort((a, b) => (b.last_triggered_at ?? "").localeCompare(a.last_triggered_at ?? ""))
    .slice(0, 10);
  if (hits.length === 0) {
    return <Empty>No alerts have triggered yet ({alerts.filter((a) => a.is_active).length} active).</Empty>;
  }
  return (
    <ul className="flex flex-col divide-y text-sm">
      {hits.map((a) => (
        <li key={a.id} className="flex justify-between gap-2 py-1.5">
          <span>
            <Link href={`/stocks/${a.symbol}`} className="font-medium hover:underline">
              {a.symbol}
            </Link>{" "}
            {ALERT_LABEL[a.alert_type]} at {inr(a.last_triggered_price)}
          </span>
          <span className="text-muted-foreground text-xs tabular-nums">
            {new Date(a.last_triggered_at!).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function Dashboard() {
  const brokers = useLoad<BrokerStatus[]>("/brokers/status");
  const jobs = useLoad<JobsView>("/jobs?limit=50");
  const near = useLoad<ScreenerRow[]>(
    `/screener?grade=A_plus&grade=A&max_distance_to_buy_zone=${NEAR_ZONE}&sort=pct_to_buy_zone&order=asc`,
  );
  const alerts = useLoad<Alert[]>("/alerts");
  const box = (title: string, body: React.ReactNode, extra?: React.ReactNode) => (
    <Card className="gap-4">
      <CardHeader className="flex flex-row items-baseline justify-between">
        <CardTitle className="text-base">{title}</CardTitle>
        {extra}
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  );
  const show = <T,>(s: { data: T | null; error: string | null }, render: (d: T) => React.ReactNode) =>
    s.error ? <ErrorText>{s.error}</ErrorText> : s.data ? render(s.data) : <p className="text-muted-foreground text-sm">Loading…</p>;

  return (
    <Page>
      <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {box(
          "Brokers",
          show(brokers, (d) => <BrokerList statuses={d} connect={false} />),
          <Link href="/settings" className="text-muted-foreground text-xs hover:underline">
            Manage
          </Link>,
        )}
        {box("Data freshness", show(jobs, (d) => <Freshness jobs={d} />))}
      </div>
      {box(
        "A-grade stocks at their buy zone",
        show(near, (d) => <BuyZoneList rows={d} />),
        <Link href="/screener?grade=A_plus&grade=A" className="text-muted-foreground text-xs hover:underline">
          Open in screener
        </Link>,
      )}
      {box(
        "Triggered alerts",
        show(alerts, (d) => <TriggeredAlerts alerts={d} />),
        <Link href="/watchlist" className="text-muted-foreground text-xs hover:underline">
          Manage alerts
        </Link>,
      )}
    </Page>
  );
}
