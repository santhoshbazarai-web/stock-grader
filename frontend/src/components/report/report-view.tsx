"use client";

// Stock Report page (SPEC §9, §3.7): pipeline progress, header, zone gauge, chart, valuation panel, scorecard,
// decision, red flags / data gaps, 10-year fundamentals and data coverage (§3.6 step 4). Saving assumptions swaps in the
// recomputed report, which re-fetches the chart overlays and sensitivity grid.
import { useCallback, useEffect, useState } from "react";

import { AppNav } from "@/components/common";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import type { PipelineRun, PipelineStart, StockReport } from "@/lib/types";

import { CoverageGrid } from "./coverage-grid";
import { FundamentalsCharts } from "./fundamentals-charts";
import { EventsCard } from "./events-card";
import { DecisionPanel, FlagsPanel, ReportHeader } from "./panels";
import { PipelineProgress } from "./pipeline-progress";
import { PriceChart } from "./price-chart";
import { PriceAnomalyBanner } from "./price-anomaly-banner";
import { ReconciliationBanner } from "./reconciliation-banner";
import { Scorecard } from "./scorecard";
import { SourcesPanel } from "./sources-panel";
import { ThesisCard } from "./thesis-card";
import { ValuationPanel } from "./valuation-panel";
import { ZoneGauge } from "./zone-gauge";

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

export function ReportView({ symbol }: { symbol: string }) {
  const [report, setReport] = useState<StockReport | null>(null);
  const [error, setError] = useState<{ status: number; detail: string } | null>(
    null,
  );
  const [sectors, setSectors] = useState<string[]>([]);
  const [run, setRun] = useState<PipelineRun | null>(null);

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

  return (
    <main className="mx-auto flex max-w-7xl flex-col gap-6 p-4 sm:p-6">
      <AppNav />
      {run && (
        <Card>
          <CardContent>
            <PipelineProgress
              key={run.id}
              run={run}
              onFinished={onFinished}
              compact={!!report}
            />
          </CardContent>
        </Card>
      )}
      {error && !run && (
        <Card>
          <CardContent className="text-sm">
            {error.status === 404
              ? `No report for ${symbol.toUpperCase()}: ${error.detail}.`
              : `Report unavailable: ${error.detail}`}
          </CardContent>
        </Card>
      )}
      {!report && !error && (
        <p className="text-muted-foreground text-sm">Building report…</p>
      )}
      {report && (
        <>
          <ReportHeader report={report} onRun={setRun} />
          <ReconciliationBanner symbol={report.symbol} />
          <PriceAnomalyBanner symbol={report.symbol} />
          <Section title="Valuation zone">
            <ZoneGauge report={report} />
          </Section>
          <Section title="Chart">
            <PriceChart report={report} />
          </Section>
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Section title="Valuation">
              <ValuationPanel
                report={report}
                sectors={sectors}
                onReport={(r) => r && setReport(r)}
              />
            </Section>
            <Section title="Scorecard">
              <Scorecard report={report} />
            </Section>
          </div>
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Section title="Decision">
              <DecisionPanel report={report} />
            </Section>
            <Section title="Red flags & data gaps">
              <FlagsPanel report={report} />
            </Section>
          </div>
          <Section title="Thesis">
            <ThesisCard
              symbol={report.symbol}
              version={`${report.as_of}:${report.cmp}:${report.grade}:${report.action}`}
            />
          </Section>
          <Section title="Corporate events">
            <EventsCard symbol={report.symbol} />
          </Section>
          <Section title="Fundamentals (10 years)">
            <FundamentalsCharts symbol={report.symbol} />
          </Section>
          <Section title="Data coverage">
            <CoverageGrid symbol={report.symbol} />
          </Section>
          <Section title="Data sources">
            <SourcesPanel report={report} />
          </Section>
        </>
      )}
    </main>
  );
}
