// Data sources panel (P25): where each part of the report came from, so a number can be traced.
import type { StockReport } from "@/lib/types";

const LABEL: Record<string, string> = {
  fyers: "Fyers (broker API)",
  kite: "Kite Connect (broker API)",
  nse: "NSE",
  bse: "BSE",
  yfinance: "Yahoo Finance (fallback)",
  screener: "Screener.in export (upload)",
  annual_report_pdf: "Annual-report PDF",
  derived: "Derived (FY summed from quarters)",
  manual: "Manual",
  demo: "Demo data (synthetic)",
  offline: "Offline exchange (synthetic)",
};

// Fundamentals stored with source "nse" are exchange-filed results XBRL (NSE or BSE filings).
const FUNDAMENTALS_LABEL: Record<string, string> = {
  nse: "Exchange results filings (XBRL)",
  offline: "Offline exchange XBRL (synthetic)",
};

const SYNTHETIC = new Set(["demo", "offline"]);

function label(
  source: string | null | undefined,
  overrides: Record<string, string> = {},
): string {
  if (!source) return "—";
  return overrides[source] ?? LABEL[source] ?? source;
}

export function SourcesPanel({ report }: { report: StockReport }) {
  const s = report.sources;
  const basis = s.statement_type;
  const rows: { name: string; value: string; note?: string }[] = [
    { name: "Prices", value: label(s.prices), note: `as of ${report.as_of}` },
    {
      name: "Fundamentals",
      value: label(s.fundamentals, FUNDAMENTALS_LABEL),
      note: basis
        ? basis === "standalone"
          ? "standalone (no consolidated statements filed)"
          : basis
        : undefined,
    },
    { name: "Shareholding", value: label(s.shareholding) },
  ];
  const synthetic = Object.values(s).some((v) => v != null && SYNTHETIC.has(v));
  return (
    <div className="flex flex-col gap-3 text-sm">
      <dl
        className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2"
        aria-label="Data sources"
      >
        {rows.map((r) => (
          <div key={r.name} className="contents">
            <dt className="text-muted-foreground">{r.name}</dt>
            <dd>
              <span data-source={r.name.toLowerCase()}>{r.value}</span>
              {r.note && (
                <span className="text-muted-foreground"> · {r.note}</span>
              )}
            </dd>
          </div>
        ))}
      </dl>
      {synthetic && (
        <p className="text-muted-foreground text-xs" role="note">
          Synthetic data for development and testing: not real market data.
        </p>
      )}
    </div>
  );
}
