"use client";

// Data coverage (SPEC v0.2 §3.6 step 4): fiscal years × statements (P&L / BS / CF) per basis,
// each cell coloured and labelled by where its figures come from: exchange XBRL, an
// annual-report PDF, a year summed from quarters, or a Screener / yfinance row. An empty cell
// is a gap. Values waiting in the review queue are flagged with a link to it.
import Link from "next/link";
import { useEffect, useState } from "react";

import { api, ApiError } from "@/lib/api";
import type {
  CoverageCell,
  CoverageGrid as Grid,
  CoverageSource,
} from "@/lib/types";

export const SOURCE_STYLE: Record<
  CoverageSource,
  { label: string; short: string; color: string }
> = {
  xbrl: { label: "Exchange XBRL", short: "XBRL", color: "var(--viz-s3)" },
  pdf: { label: "Annual-report PDF", short: "PDF", color: "var(--viz-s1)" },
  derived: {
    label: "Summed from quarters",
    short: "Σ Qtr",
    color: "var(--viz-s4)",
  },
  indianapi: {
    label: "Indian API (vendor-reclassified)",
    short: "API",
    color: "var(--viz-div-neg-3)",
  },
  screener: { label: "Screener upload", short: "Scr", color: "var(--viz-s2)" },
  yfinance: { label: "yfinance", short: "yf", color: "var(--viz-s2)" },
  nse: { label: "Exchange (wide row)", short: "NSE", color: "var(--viz-s3)" },
};
const STATEMENTS: CoverageCell["statement"][] = ["P&L", "BS", "CF"];

function Cell({
  cell,
  symbol,
}: {
  cell: CoverageCell | undefined;
  symbol: string;
}) {
  if (!cell) return <td />;
  const [first, ...rest] = cell.sources;
  const style = first ? SOURCE_STYLE[first] : null;
  const what = style
    ? `${cell.statement} FY${cell.fiscal_year}: ${cell.sources.map((s) => SOURCE_STYLE[s]?.label ?? s).join(" + ")}` +
      (cell.items ? ` (${cell.items} items)` : "")
    : `${cell.statement} FY${cell.fiscal_year}: ${cell.note ? `gap, ${cell.note}` : "gap"}`;
  return (
    <td className="p-0.5">
      <div
        title={
          what +
          (cell.pending_review
            ? `; ${cell.pending_review} value(s) to review`
            : "")
        }
        aria-label={what}
        className={`flex h-8 min-w-11 flex-col items-center justify-center rounded text-[10px] leading-tight ${
          style ? "text-white" : "text-muted-foreground border border-dashed"
        }`}
        style={style ? { background: style.color } : undefined}
      >
        <span className="font-medium">
          {style ? style.short : cell.note ? "AR" : "—"}
        </span>
        {rest.length > 0 && (
          <span>+{rest.map((s) => SOURCE_STYLE[s]?.short ?? s).join("+")}</span>
        )}
        {cell.pending_review > 0 && (
          <Link
            href={`/review?symbol=${encodeURIComponent(symbol)}`}
            className={`underline ${style ? "text-white" : "text-foreground"}`}
            aria-label={`${cell.pending_review} value(s) to review`}
          >
            ⚑{cell.pending_review}
          </Link>
        )}
      </div>
    </td>
  );
}

export function CoverageGrid({ symbol }: { symbol: string }) {
  const [grid, setGrid] = useState<Grid | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api<Grid>(`/stocks/${encodeURIComponent(symbol)}/coverage`)
      .then((g) => live && setGrid(g))
      .catch(
        (e) => live && setError(e instanceof ApiError ? e.detail : "failed"),
      );
    return () => {
      live = false;
    };
  }, [symbol]);

  if (error)
    return (
      <p className="text-muted-foreground text-sm">
        Coverage unavailable: {error}
      </p>
    );
  if (!grid)
    return <p className="text-muted-foreground text-sm">Loading coverage…</p>;
  const used = new Set(
    grid.bases.flatMap((b) => b.cells.flatMap((c) => c.sources)),
  );
  return (
    <div className="flex flex-col gap-4">
      {grid.bases.map((b) => {
        const byKey = new Map(
          b.cells.map((c) => [`${c.fiscal_year}-${c.statement}`, c]),
        );
        const gaps = b.cells.filter((c) => c.sources.length === 0).length;
        // a mostly empty basis next to a fuller one is not what the report uses: fold it
        const folded =
          grid.bases.length > 1 &&
          gaps * 2 > b.cells.length &&
          grid.bases.some(
            (o) =>
              o !== b &&
              o.cells.filter((c) => c.sources.length === 0).length < gaps,
          );
        const table = (
          <div className="overflow-x-auto">
            <table className="text-xs" aria-label={`Data coverage, ${b.basis}`}>
              <caption className="text-muted-foreground mb-1 text-left">
                <span className="text-foreground font-medium capitalize">
                  {b.basis}
                </span>{" "}
                · {gaps} of {b.cells.length} cells missing
              </caption>
              <thead>
                <tr>
                  <th className="pr-2 text-left font-normal" />
                  {grid.years.map((y) => (
                    <th
                      key={y}
                      scope="col"
                      className="text-muted-foreground px-0.5 font-normal"
                    >
                      FY{String(y).slice(2)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {STATEMENTS.map((s) => (
                  <tr key={s}>
                    <th scope="row" className="pr-2 text-left font-medium">
                      {s}
                    </th>
                    {grid.years.map((y) => (
                      <Cell
                        key={y}
                        cell={byKey.get(`${y}-${s}`)}
                        symbol={grid.symbol}
                      />
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
        return folded ? (
          <details key={b.basis}>
            <summary className="text-muted-foreground cursor-pointer text-xs">
              <span className="capitalize">{b.basis}</span> · {gaps} of{" "}
              {b.cells.length} cells missing (not used by the report)
            </summary>
            {table}
          </details>
        ) : (
          <div key={b.basis}>{table}</div>
        );
      })}
      <ul
        className="text-muted-foreground flex flex-wrap gap-3 text-xs"
        aria-label="Coverage legend"
      >
        {(Object.keys(SOURCE_STYLE) as CoverageSource[])
          .filter((s) => used.has(s))
          .map((s) => (
            <li key={s} className="flex items-center gap-1">
              <span
                className="inline-block size-3 rounded-sm"
                style={{ background: SOURCE_STYLE[s].color }}
              />
              {SOURCE_STYLE[s].short} = {SOURCE_STYLE[s].label}
            </li>
          ))}
        <li className="flex items-center gap-1">
          <span className="inline-block size-3 rounded-sm border border-dashed" />{" "}
          — = gap
        </li>
        <li>AR = not in XBRL: use the annual report</li>
        <li>
          ⚑ = values to review ·{" "}
          <Link
            href={`/review?symbol=${encodeURIComponent(grid.symbol)}`}
            className="underline"
          >
            add an annual report
          </Link>
        </li>
      </ul>
    </div>
  );
}
