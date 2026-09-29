"use client";

// Stock Report page (SPEC §9): header, zone gauge, chart, valuation panel, scorecard,
// decision, red flags / data gaps and 10-year fundamentals. Saving assumptions swaps in the
// recomputed report, which re-fetches the chart overlays and sensitivity grid.
import Link from "next/link";
import { useEffect, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import type { StockReport } from "@/lib/types";

import { FundamentalsCharts } from "./fundamentals-charts";
import { DecisionPanel, FlagsPanel, ReportHeader } from "./panels";
import { PriceChart } from "./price-chart";
import { Scorecard } from "./scorecard";
import { SymbolSearch } from "./symbol-search";
import { ValuationPanel } from "./valuation-panel";
import { ZoneGauge } from "./zone-gauge";

function Section({ title, children, className }: { title: string; children: React.ReactNode; className?: string }) {
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
  const [error, setError] = useState<{ status: number; detail: string } | null>(null);
  const [sectors, setSectors] = useState<string[]>([]);

  useEffect(() => {
    let live = true;
    setReport(null);
    setError(null);
    api<StockReport>(`/stocks/${symbol}/report`)
      .then((r) => live && setReport(r))
      .catch((e) => live && setError(e instanceof ApiError ? { status: e.status, detail: e.detail } : { status: 0, detail: "failed" }));
    api<{ parsed: { sectors: Record<string, unknown> } }>("/config")
      .then((c) => live && setSectors(Object.keys(c.parsed.sectors).sort()))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [symbol]);

  return (
    <main className="mx-auto flex max-w-7xl flex-col gap-6 p-4 sm:p-6">
      <nav className="flex items-center justify-between gap-4">
        <Link href="/" className="text-sm font-semibold">
          Stock Grader
        </Link>
        <SymbolSearch />
      </nav>
      {error && (
        <Card>
          <CardContent className="text-sm">
            {error.status === 404
              ? `No report for ${symbol.toUpperCase()}: ${error.detail}. Queue a data refresh from the dashboard or upload fundamentals.`
              : `Report unavailable: ${error.detail}`}
          </CardContent>
        </Card>
      )}
      {!report && !error && <p className="text-muted-foreground text-sm">Building report…</p>}
      {report && (
        <>
          <ReportHeader report={report} />
          <Section title="Valuation zone">
            <ZoneGauge report={report} />
          </Section>
          <Section title="Chart">
            <PriceChart report={report} />
          </Section>
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Section title="Valuation">
              <ValuationPanel report={report} sectors={sectors} onReport={(r) => r && setReport(r)} />
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
          <Section title="Fundamentals (10 years)">
            <FundamentalsCharts symbol={report.symbol} />
          </Section>
        </>
      )}
    </main>
  );
}
