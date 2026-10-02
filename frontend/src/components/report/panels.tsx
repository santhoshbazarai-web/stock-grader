"use client";

// Header, decision + earned premium, and red flags / data gaps for the report page.
import { AlertTriangle, CheckCircle2, CircleHelp, Info, RefreshCw, XCircle } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { ACTION_LABEL, crore, inr, num, pct, ZONE_LABEL } from "@/lib/format";
import type { DataDepth, PipelineRun, StockReport } from "@/lib/types";

const BUYS = new Set(["strong_buy", "buy", "accumulate", "buy_on_pullback", "momentum_entry"]);

export function ReportHeader({ report, onRun }: { report: StockReport; onRun?: (run: PipelineRun) => void }) {
  const [queued, setQueued] = useState<string | null>(null);
  async function refresh() {
    try {
      const r = await api<{ queued: boolean; run_id: number }>(`/stocks/${report.symbol}/refresh`, { method: "POST" });
      setQueued(r.queued ? null : "Already running");
      onRun?.(await api<PipelineRun>(`/pipeline/${r.run_id}`));
    } catch (e) {
      setQueued(e instanceof ApiError ? e.detail : "refresh failed");
    }
  }
  const action = report.action;
  return (
    <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-semibold tracking-tight">{report.symbol}</h1>
          <Badge variant="outline" className="text-sm" aria-label={`Grade ${report.grade_label ?? "n/a"}${report.grade_confidence === "reduced" ? " (provisional)" : ""}`}>
            Grade {report.grade_label ?? "n/a"}
            {report.grade_confidence === "reduced" && <span className="text-muted-foreground ml-1 text-xs font-normal">provisional</span>}
          </Badge>
          <Badge
            variant={action && BUYS.has(action) ? "default" : action === "avoid" || action === "book_profits" ? "destructive" : "secondary"}
            className="text-sm"
          >
            {action ? ACTION_LABEL[action] ?? action : "No action"}
          </Badge>
          {report.zone && <Badge variant="secondary">{ZONE_LABEL[report.zone]}</Badge>}
          {report.data_depth && <DepthBadge depth={report.data_depth} />}
        </div>
        <p className="text-muted-foreground text-sm">{report.name}</p>
      </div>
      <div className="flex flex-col items-start gap-1 sm:items-end">
        <p className="text-3xl font-semibold tabular-nums">{inr(report.cmp)}</p>
        <p className="text-muted-foreground text-xs">
          as of {report.as_of} · prices {report.sources.prices ?? "—"} · fundamentals {report.sources.fundamentals ?? "—"}
          {report.sources.statement_type ? ` (${report.sources.statement_type})` : ""} · shareholding{" "}
          {report.sources.shareholding ?? "—"}
          {report.shareholding
            ? ` (${report.shareholding.period_end}${report.shareholding.filing_date ? `, filed ${report.shareholding.filing_date}` : ""})`
            : ""}
        </p>
        {report.shareholding?.note && <p className="text-muted-foreground text-xs">{report.shareholding.note}</p>}
        <div className="flex items-center gap-2">
          {queued && <span className="text-muted-foreground text-xs" role="status">{queued}</span>}
          <Button size="sm" variant="outline" onClick={refresh}>
            <RefreshCw /> Refresh data
          </Button>
        </div>
      </div>
    </header>
  );
}

const DEPTH_LABEL: Record<DataDepth["level"], string> = {
  full: "Full data",
  provisional: "Provisional data",
  technical_only: "Technical only",
};

function DepthBadge({ depth }: { depth: DataDepth }) {
  const color = depth.level === "full" ? "var(--viz-good)" : depth.level === "provisional" ? "var(--viz-s4)" : "var(--viz-critical)";
  return (
    <Badge variant="outline" className="text-xs" style={{ borderColor: color, color }} title={depth.reason} aria-label={`Data depth: ${DEPTH_LABEL[depth.level]}, ${depth.pl_years} years of P&L`}>
      {DEPTH_LABEL[depth.level]} · {depth.pl_years} yr
    </Badge>
  );
}

function Mark({ met }: { met: boolean | null }) {
  if (met === true) return <CheckCircle2 aria-label="met" className="size-4 shrink-0" style={{ color: "var(--viz-good)" }} />;
  if (met === false) return <XCircle aria-label="not met" className="text-muted-foreground size-4 shrink-0" />;
  return <CircleHelp aria-label="unknown" className="text-muted-foreground size-4 shrink-0" />;
}

export function DecisionPanel({ report }: { report: StockReport }) {
  const d = report.decision;
  const ep = report.earned_premium_detail;
  const t = report.technical;
  return (
    <div className="flex flex-col gap-5 text-sm">
      <section className="flex flex-col gap-1">
        <h3 className="font-semibold">Why this action</h3>
        {d.overridden_by_stage4 && (
          <p className="flex items-center gap-1.5" role="note">
            <AlertTriangle className="size-4" style={{ color: "var(--viz-critical)" }} aria-hidden />
            Stage 4 override: downtrend — wait for Stage 1 base
          </p>
        )}
        <ul className="text-muted-foreground list-disc pl-5 text-xs">
          {d.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
        {d.checklist.length > 0 && (
          <div className="mt-2 rounded-md border p-3">
            <p className="text-xs font-semibold">
              {d.rule === "value_trap_check" ? "Value-trap check" : "Why is it cheap?"} — check before acting
            </p>
            <ul className="mt-1 list-disc pl-5 text-xs">
              {d.checklist.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </div>
        )}
      </section>
      <section className="flex flex-col gap-1">
        <h3 className="font-semibold">
          Earned premium {ep.score}/{ep.out_of ?? 8}{" "}
          {ep.max_possible > ep.score && <span className="text-muted-foreground text-xs font-normal">(up to {ep.max_possible} with missing data)</span>}
        </h3>
        <ul className="flex flex-col gap-1 text-xs">
          {ep.conditions.map((c) => (
            <li key={c.code} className="flex items-start gap-2">
              <Mark met={c.met} />
              <span>{c.reason}</span>
            </li>
          ))}
        </ul>
      </section>
      <section className="flex flex-col gap-1">
        <h3 className="font-semibold">Technical</h3>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
          {[
            ["Stage", t.stage ?? "—"],
            ["Trend", t.trend ?? "—"],
            ["RS percentile", num(t.rs_percentile, 0)],
            ["Mansfield RS", num(t.mansfield_rs, 1)],
            ["RSI (weekly)", num(t.rsi, 0)],
            ["From 52-wk high", pct(t.from_52w_high)],
            ["ATR (weekly)", inr(t.atr)],
            ["Delivery ratio", num(t.delivery_ratio, 2)],
            ["VCP", t.vcp ? "detected" : "no"],
            ["R:R to FV", num(report.rr_to_fv, 2)],
            ["R:R to top band", num(report.rr_to_top, 2)],
            ["Market cap", crore(report.valuation.market_cap_cr)],
          ].map(([k, v]) => (
            <div key={String(k)}>
              <dt className="text-muted-foreground">{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  );
}

export function FlagsPanel({ report }: { report: StockReport }) {
  const ko = report.knockouts;
  return (
    <div className="flex flex-col gap-5 text-sm">
      <section className="flex flex-col gap-1">
        <h3 className="font-semibold">Red flags</h3>
        {report.red_flags.length === 0 ? (
          <p className="text-muted-foreground text-xs">None raised.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {report.red_flags.map((f) => (
              <li key={f} className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" style={{ color: "var(--viz-critical)" }} aria-label="red flag" />
                <span>{f}</span>
              </li>
            ))}
          </ul>
        )}
        <p className="text-muted-foreground text-xs">
          Knock-outs:{" "}
          {ko.triggered.length ? `${ko.triggered.join(", ")} → grade capped at ${ko.cap}` : "none triggered"}
          {ko.unknown.length > 0 && ` · not evaluated: ${ko.unknown.join(", ")}`}
        </p>
      </section>
      <section className="flex flex-col gap-1">
        <h3 className="font-semibold">
          Data gaps <span className="text-muted-foreground text-xs font-normal">({report.data_gaps.length})</span>
        </h3>
        {report.data_gaps.length === 0 ? (
          <p className="text-muted-foreground text-xs">No gaps: every input was available.</p>
        ) : (
          <details open={report.data_gaps.length <= 8}>
            <summary className="text-muted-foreground cursor-pointer text-xs">
              Inputs the report ran without (never defaulted to 0)
            </summary>
            <ul className="mt-1 flex flex-col gap-0.5 text-xs">
              {report.data_gaps.map((g) => (
                <li key={g} className="flex items-start gap-2">
                  <Info className="text-muted-foreground mt-0.5 size-3.5 shrink-0" aria-hidden />
                  <span>{g}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>
      {report.analyst_consensus && <AnalystConsensusNote c={report.analyst_consensus} />}
    </div>
  );
}

function AnalystConsensusNote({ c }: { c: NonNullable<StockReport["analyst_consensus"]> }) {
  const parts = Object.entries(c.ratings)
    .filter(([, n]) => n > 0)
    .map(([name, n]) => `${name} ${n}`);
  return (
    <section className="flex flex-col gap-1" aria-label="Analyst consensus">
      <h3 className="font-semibold">
        Analyst consensus <span className="text-muted-foreground text-xs font-normal">(informational, not scored)</span>
      </h3>
      <p className="text-xs">
        {c.recommendations} analysts
        {c.mean_rating != null && ` · mean ${c.mean_rating.toFixed(2)} (1 Strong Buy … 5 Strong Sell)`}
        {parts.length > 0 && ` · ${parts.join(", ")}`}
      </p>
      <p className="text-muted-foreground text-xs">
        {c.source}, {c.as_of}. Never part of the grade, zone or action.
      </p>
    </section>
  );
}
