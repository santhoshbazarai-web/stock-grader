"use client";

// Dividends tab: DPS history with payout ratio, yield, 3/5/10-year DPS CAGR, bonus / split
// history, upcoming ex-dates and buybacks. Built from the stored dividend actions, so "no
// records" is shown as such ("-" with the reason), never as a zero yield.
import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { EmptyState, MetricTable, Skeleton } from "@/components/ds";
import { api, ApiError } from "@/lib/api";
import { inr, num, pct } from "@/lib/format";
import type { ActionRow, DividendsOut } from "@/lib/types";

const axisTick = { fontSize: 11, fill: "var(--viz-muted)" };
const tip = { background: "var(--viz-surface)", borderColor: "var(--viz-grid)", fontSize: 12 };
const day = (d: string) => new Date(d).toLocaleDateString("en-IN", { dateStyle: "medium" });

function Actions({ rows, title, empty }: { rows: ActionRow[]; title: string; empty: string }) {
  return (
    <section aria-label={title} className="flex flex-col gap-1.5">
      <h3 className="text-sm font-semibold">{title}</h3>
      {rows.length === 0 ? (
        <p className="text-muted-foreground text-sm">{empty}</p>
      ) : (
        <table className="w-full text-sm tabular-nums" aria-label={title}>
          <thead className="text-muted-foreground text-xs">
            <tr>
              <th className="py-1 text-left font-normal">Ex-date</th>
              <th className="py-1 text-left font-normal">Type</th>
              <th className="py-1 text-right font-normal">Detail</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.ex_date}-${r.action_type}`} className="border-t">
                <td className="py-1.5">{day(r.ex_date)}</td>
                <td className="py-1.5 capitalize">{r.action_type}</td>
                <td className="py-1.5 text-right">
                  {r.action_type === "dividend" ? (r.dividend_per_share == null ? "-" : `${inr(r.dividend_per_share)} per share`) : r.ratio_new != null && r.ratio_old != null ? `${num(r.ratio_new, 2)} for ${num(r.ratio_old, 2)}` : (r.description ?? "-")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function DividendsTab({ symbol }: { symbol: string }) {
  const [d, setD] = useState<DividendsOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api<DividendsOut>(`/stocks/${encodeURIComponent(symbol)}/dividends`)
      .then((x) => live && setD(x))
      .catch((e) => live && setError(e instanceof ApiError ? e.detail : "dividends unavailable"));
    return () => {
      live = false;
    };
  }, [symbol]);
  if (error) return <EmptyState title="Dividends unavailable">{error}</EmptyState>;
  if (!d) return <Skeleton className="h-64 w-full" />;
  const cg = (k: "3y" | "5y" | "10y") => (d.dps_cagr[k] == null ? null : pct(d.dps_cagr[k], 1));
  return (
    <div className="flex flex-col gap-6">
      {d.note && <p className="text-muted-foreground text-sm">{d.note}</p>}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <MetricTable
          title="Yield and growth"
          rows={[
            { label: "Dividend yield", glossaryKey: "dividend_yield", value: d.yield_pct == null ? null : `${num(d.yield_pct, 2)}%`, reason: d.yield_reason },
            { label: "DPS (last 12 months)", glossaryKey: "dps", value: d.ttm_dps == null ? null : inr(d.ttm_dps), reason: d.yield_reason },
            { label: "DPS CAGR 3y", glossaryKey: "cagr", value: cg("3y"), reason: d.cagr_reason },
            { label: "DPS CAGR 5y", glossaryKey: "cagr", value: cg("5y"), reason: d.cagr_reason },
            { label: "DPS CAGR 10y", glossaryKey: "cagr", value: cg("10y"), reason: d.cagr_reason },
          ]}
        />
        <section aria-label="Buybacks" className="flex flex-col gap-1.5">
          <h3 className="text-sm font-semibold">Buybacks</h3>
          {d.buybacks.length === 0 ? (
            <p className="text-muted-foreground text-sm">{d.buyback_note}</p>
          ) : (
            <ul className="flex flex-col gap-1 text-sm">
              {d.buybacks.map((b, i) => (
                <li key={i}>
                  {b.date ? `${day(b.date)}: ` : ""}
                  {b.title}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {d.history.length > 0 && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <figure className="flex flex-col gap-1" aria-label="DPS history">
            <figcaption className="text-sm font-semibold">Dividend per share (₹)</figcaption>
            <div className="h-48">
              <ResponsiveContainer>
                <BarChart data={d.history.map((h) => ({ ...h, label: `FY${h.fiscal_year}` }))}>
                  <CartesianGrid stroke="var(--viz-grid)" vertical={false} />
                  <XAxis dataKey="label" tick={axisTick} />
                  <YAxis tick={axisTick} width={44} />
                  <Tooltip contentStyle={tip} />
                  <Bar dataKey="dps" name="DPS (₹)" fill="var(--viz-s1)" radius={[2, 2, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </figure>
          <figure className="flex flex-col gap-1" aria-label="Payout ratio">
            <figcaption className="text-sm font-semibold">Payout ratio (%)</figcaption>
            <div className="h-48">
              <ResponsiveContainer>
                <LineChart data={d.history.map((h) => ({ label: `FY${h.fiscal_year}`, payout: h.payout_pct }))}>
                  <CartesianGrid stroke="var(--viz-grid)" vertical={false} />
                  <XAxis dataKey="label" tick={axisTick} />
                  <YAxis tick={axisTick} width={44} />
                  <Tooltip contentStyle={tip} />
                  <Line dataKey="payout" name="Payout %" stroke="var(--viz-s2)" strokeWidth={2} dot connectNulls={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </figure>
        </div>
      )}
      {d.history.length > 0 && (
        <table className="w-full text-sm tabular-nums" aria-label="DPS by fiscal year">
          <thead className="text-muted-foreground text-xs">
            <tr>
              <th className="py-1 text-left font-normal">Fiscal year</th>
              <th className="py-1 text-right font-normal">DPS</th>
              <th className="py-1 text-right font-normal">EPS</th>
              <th className="py-1 text-right font-normal">Payout</th>
            </tr>
          </thead>
          <tbody>
            {[...d.history].reverse().map((h) => (
              <tr key={h.fiscal_year} className="border-t">
                <td className="py-1.5">FY{h.fiscal_year}</td>
                <td className="py-1.5 text-right">{inr(h.dps)}</td>
                <td className="py-1.5 text-right">{h.eps == null ? <span title="EPS not stored for this year">-</span> : inr(h.eps)}</td>
                <td className="py-1.5 text-right">{h.payout_pct == null ? <span title="needs a positive EPS">-</span> : `${num(h.payout_pct, 1)}%`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Actions rows={d.upcoming} title="Upcoming ex-dates" empty="No upcoming ex-dates are stored." />
        <Actions rows={d.corporate_actions} title="Bonus and split history" empty="No bonus or split is stored for this stock." />
      </div>
    </div>
  );
}
