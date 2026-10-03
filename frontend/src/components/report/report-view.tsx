"use client";

// Stock Report page (SPEC §9, §3.7): sticky header, a top row (My metrics, chart, pillars and
// price ladder), then tabs: Overview (metric tables), Financials, Key Metrics, Fair Value,
// Dividends, Technicals, Compare, Ownership & Events, AI Insights, Data quality. The pipeline is
// a collapsible strip. Saving assumptions swaps in the recomputed report.
import { useCallback, useEffect, useState } from "react";

import { AppNav } from "@/components/common";
import { CompareTab } from "./compare-tab";
import { DividendsTab } from "./dividends-tab";
import { FairValueTab } from "./fair-value-tab";
import { FinancialsTab } from "./financials-tab";
import { KeyMetricsTab } from "./key-metrics-tab";
import { NotesPanel } from "@/components/notes-panel";
import { EmptyState, MetricTable, PillarMiniChart, PriceLadder, Skeleton, Tabs, type TabDef } from "@/components/ds";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { friendlyMessage, inr, num, signedPct } from "@/lib/format";
import type { PipelineRun, PipelineStart, StockReport } from "@/lib/types";

import { CoverageGrid } from "./coverage-grid";
import { FundamentalsCharts } from "./fundamentals-charts";
import { KeyMetrics } from "./key-metrics";
import { EventsCard } from "./events-card";
import { MyMetricsCard } from "./my-metrics-card";
import { OverviewTab } from "./overview-tab";
import { DecisionPanel, FlagsPanel } from "./panels";
import { PipelineStrip } from "./pipeline-strip";
import { StockHeader } from "./stock-header";
import { PriceChart } from "./price-chart";
import { PriceAnomalyBanner } from "./price-anomaly-banner";
import { ReconciliationBanner } from "./reconciliation-banner";
import { Scorecard } from "./scorecard";
import { SourcesPanel } from "./sources-panel";
import { ThesisCard } from "./thesis-card";

function Section({
  title,
  children,
  className,
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card className={`gap-4 ${className ?? ""}`}>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

const TABS: TabDef[] = [
  { id: "overview", label: "Overview" },
  { id: "financials", label: "Financials" },
  { id: "key-metrics", label: "Key Metrics" },
  { id: "fair-value", label: "Fair Value" },
  { id: "dividends", label: "Dividends" },
  { id: "technicals", label: "Technicals" },
  { id: "compare", label: "Compare" },
  { id: "ownership", label: "Ownership & Events" },
  { id: "ai", label: "AI Insights" },
  { id: "notes", label: "Notes" },
  { id: "quality", label: "Data quality" },
];

function useTab(): [string, (id: string) => void] {
  const [tab, setTab] = useState("overview");
  useEffect(() => {
    const h = window.location.hash.slice(1);
    if (TABS.some((t) => t.id === h)) setTab(h);
  }, []);
  return [
    tab,
    (id) => {
      setTab(id);
      window.history.replaceState(null, "", `#${id}`);
    },
  ];
}

export function ReportView({ symbol }: { symbol: string }) {
  const [report, setReport] = useState<StockReport | null>(null);
  const [, setFundYears] = useState<number | null>(null);
  const [error, setError] = useState<{ status: number; detail: string } | null>(
    null,
  );
  const [sectors, setSectors] = useState<string[]>([]);
  const [run, setRun] = useState<PipelineRun | null>(null);
  const [tab, setTab] = useTab();

  // SPEC §3.7: a fresh stored report shows at once; otherwise a pipeline run updates it (the
  // stored report stays on screen meanwhile), or builds it for a stock seen for the first time.
  useEffect(() => {
    let live = true;
    setReport(null);
    setError(null);
    setRun(null);
    const startRun = () =>
      api<PipelineStart>("/pipeline", {
        method: "POST",
        body: JSON.stringify({ symbol }),
      })
        .then((p) => live && p.run && setRun(p.run))
        .catch(() => undefined);
    api<StockReport>(`/stocks/${symbol}/report`)
      .then((r) => {
        if (!live) return;
        setReport(r);
        startRun();
      })
      .catch((e) => {
        if (!live) return;
        setError(
          e instanceof ApiError
            ? { status: e.status, detail: e.detail }
            : { status: 0, detail: "failed" },
        );
        if (e instanceof ApiError && e.status === 404) startRun();
      });
    api<{ parsed: { sectors: Record<string, unknown> } }>("/config")
      .then((c) => live && setSectors(Object.keys(c.parsed.sectors).sort()))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [symbol]);

  const onFinished = useCallback(
    (r: PipelineRun) => {
      if (r.status !== "done") return;
      api<StockReport>(`/stocks/${symbol}/report`)
        .then((rep) => {
          setReport(rep);
          setError(null);
        })
        .catch(() => undefined);
    },
    [symbol],
  );

  const buyZone = report?.buy_zone?.status === "zone" && report.buy_zone.low != null && report.buy_zone.high != null
    ? { low: report.buy_zone.low, high: report.buy_zone.high }
    : null;
  const range = report?.levels.fair_value_low != null && report.levels.fair_value_high != null
    ? { low: report.levels.fair_value_low, high: report.levels.fair_value_high }
    : null;
  return (
    <main className="mx-auto flex max-w-7xl flex-col gap-4 p-4 sm:p-6">
      <AppNav />
      {report && <StockHeader report={report} onRun={setRun} />}
      {run && <PipelineStrip key={run.id} run={run} onFinished={onFinished} startOpen={!report} />}
      {error && !run && (
        <EmptyState title={error.status === 404 ? `No report for ${symbol.toUpperCase()}` : "Report unavailable"}>
          {error.status === 404 ? error.detail : `${friendlyMessage(error.detail)} Try Refresh data.`}
        </EmptyState>
      )}
      {!report && !error && (
        <div className="flex flex-col gap-3" aria-busy="true" aria-label="Building report">
          <Skeleton className="h-16 w-full" />
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[18rem_1fr_20rem]">
            <Skeleton className="h-72" />
            <Skeleton className="h-72" />
            <Skeleton className="h-72" />
          </div>
        </div>
      )}
      {report && (
        <>
          <ReconciliationBanner symbol={report.symbol} />
          <PriceAnomalyBanner symbol={report.symbol} />
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[17rem_minmax(0,1fr)_20rem]">
            <Section title="" className="order-2 lg:order-1">
              <MyMetricsCard report={report} />
            </Section>
            <Section title="Price" className="order-1 lg:order-2">
              <PriceChart report={report} />
            </Section>
            <Section title="Quality & price levels" className="order-3">
              <div className="flex flex-col gap-5">
                <PillarMiniChart pillars={report.pillars} />
                <PriceLadder cmp={report.cmp} baseline={report.levels.baseline} buyZone={buyZone} fairValue={report.levels.fair_value} topBand={report.levels.top_band} range={range} />
              </div>
            </Section>
          </div>
          <Tabs tabs={TABS} value={tab} onChange={setTab} label="Stock sections" />
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} className="flex flex-col gap-4">
            {tab === "overview" && (
              <>
                <OverviewTab report={report} />
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <Section title="Decision">
                    <DecisionPanel report={report} />
                  </Section>
                  <Section title="Red flags & data gaps">
                    <FlagsPanel report={report} />
                  </Section>
                </div>
              </>
            )}
            {tab === "financials" && <FinancialsTab symbol={report.symbol} />}
            {tab === "key-metrics" && (
              <>
                <KeyMetricsTab symbol={report.symbol} durability={report.durability} />
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <Section title="Scorecard">
                    <Scorecard report={report} />
                  </Section>
                  <Section title="Fundamentals notes">
                    <KeyMetrics report={report} />
                  </Section>
                </div>
              </>
            )}
            {tab === "fair-value" && <FairValueTab report={report} sectors={sectors} onReport={(r) => r && setReport(r)} />}
            {tab === "dividends" && <DividendsTab symbol={report.symbol} />}
            {tab === "technicals" && (
              <Section title="Technicals">
                <TechnicalsTab report={report} />
              </Section>
            )}
            {tab === "compare" && <CompareTab symbol={report.symbol} />}
            {tab === "ownership" && (
              <>
                <Section title="Shareholding">
                  <FundamentalsCharts symbol={report.symbol} onYears={setFundYears} shareholdingOnly />
                </Section>
                <Section title="Corporate events">
                  <EventsCard symbol={report.symbol} />
                </Section>
              </>
            )}
            {tab === "ai" && (
              <Section title="AI insights">
                <ThesisCard symbol={report.symbol} version={`${report.as_of}:${report.cmp}:${report.grade}:${report.action}`} />
              </Section>
            )}
            {tab === "notes" && <NotesPanel symbol={report.symbol} />}
            {tab === "quality" && (
              <>
                <Section title="Data coverage">
                  <CoverageGrid symbol={report.symbol} />
                </Section>
                <Section title="Data sources">
                  <SourcesPanel report={report} />
                </Section>
              </>
            )}
          </div>
        </>
      )}
    </main>
  );
}

function TechnicalsTab({ report }: { report: StockReport }) {
  const t = report.technical;
  const rows = [
    { label: "Weinstein stage", value: t.stage != null ? `Stage ${t.stage}` : null, reason: "needs 30 weeks of prices" },
    { label: "Trend", value: t.trend, reason: "needs swing points" },
    { label: "RSI (14)", value: t.rsi != null ? num(t.rsi, 0) : null, reason: "needs 15 sessions" },
    { label: "RS percentile", value: t.rs_percentile != null ? num(t.rs_percentile, 0) : null, reason: "RS snapshot not available" },
    { label: "Mansfield RS", value: t.mansfield_rs != null ? num(t.mansfield_rs, 1) : null, reason: "needs the benchmark index" },
    { label: "ATR (14)", value: t.atr != null ? inr(t.atr) : null, reason: "needs 15 sessions" },
    { label: "From 52-week high", value: t.from_52w_high != null ? signedPct(t.from_52w_high) : null, reason: "needs a year of prices" },
    { label: "Delivery ratio", value: t.delivery_ratio != null ? num(t.delivery_ratio, 2) : null, reason: "delivery data not on file" },
  ];
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      <MetricTable title="Trend & momentum" rows={rows} />
      <ul className="text-muted-foreground flex list-disc flex-col gap-1 pl-5 text-sm">
        {t.reasons.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
    </div>
  );
}
