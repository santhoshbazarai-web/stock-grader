"use client";

// Exchange results filings (XBRL): the primary source of fundamentals. The worker's
// results_backfill job lists and downloads each company's NSE filings; this card shows that
// ledger, retries failures, and takes XBRL documents uploaded by hand (e.g. from BSE).
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Empty, ErrorText } from "@/components/common";
import { Chips } from "@/components/pages/screener";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import type { FilingsSummary, FilingStatus, ResultFiling, XbrlUploadSummary } from "@/lib/types";

const SYMBOL_RE = /^[A-Za-z0-9&_.-]{1,32}$/;
const STATUSES: FilingStatus[] = ["failed", "pending", "parsed"];
const STATUS_LABEL: Record<FilingStatus, string> = { failed: "Failed", pending: "Pending", parsed: "Stored" };

const dateTime = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "—";

export function periodLabel(f: Pick<ResultFiling, "periods" | "period_end">): string {
  if (f.periods?.length) return f.periods.map((p) => p.replace("quarter", "Q").replace("year", "FY")).join(" + ");
  return f.period_end ? `period to ${f.period_end}` : "—";
}

function Summary({ s }: { s: FilingsSummary | null }) {
  if (!s) return null;
  return (
    <p className="text-muted-foreground text-sm" aria-label="Filings summary">
      <strong className="text-foreground">{s.parsed}</strong> {s.parsed === 1 ? "filing" : "filings"} stored for{" "}
      {s.symbols} {s.symbols === 1 ? "stock" : "stocks"} ·{" "}
      {s.pending} waiting to download · {s.failed} failed · last stored {dateTime(s.last_parsed_at)}
    </p>
  );
}

function Upload({ onDone }: { onDone: () => void }) {
  const [symbol, setSymbol] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<XbrlUploadSummary | null>(null);
  const [rebuild, setRebuild] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (files.length === 0) return setError("Choose one or more XBRL .xml files");
    if (!SYMBOL_RE.test(symbol.trim())) return setError("Enter the NSE symbol these filings are for");
    const body = new FormData();
    files.forEach((f) => body.append("files", f));
    body.set("symbol", symbol.trim());
    setBusy(true);
    setError(null);
    setResult(null);
    setRebuild(null);
    try {
      const summary = await api<XbrlUploadSummary>("/uploads/xbrl", { method: "POST", body });
      setResult(summary);
      if (summary.files.some((f) => f.status === "parsed")) {
        setRebuild("Rebuilding the report…");
        api(`/stocks/${summary.symbol}/report?rebuild=true`)
          .then(() => setRebuild("Report rebuilt with the new figures."))
          .catch((err) =>
            setRebuild(
              err instanceof ApiError && err.status === 404
                ? "No prices stored yet for this stock; the report is built once prices are fetched."
                : `Report not rebuilt: ${err instanceof ApiError ? err.detail : "error"}`,
            ),
          );
      }
      setFiles([]);
      if (input.current) input.current.value = "";
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "upload failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <form onSubmit={submit} className="flex flex-wrap items-end gap-3" aria-label="Upload XBRL filings">
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">Results XBRL (.xml, several allowed)</span>
          <input
            ref={input}
            type="file"
            multiple
            accept=".xml,application/xml,text/xml"
            onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
            className="text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">Symbol</span>
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="e.g. TCS"
            aria-label="XBRL symbol"
            className="border-input bg-background h-8 w-32 rounded-md border px-2 text-sm uppercase"
          />
        </label>
        <Button size="sm" type="submit" disabled={busy}>
          {busy ? "Importing…" : "Upload filings"}
        </Button>
      </form>
      {error && <ErrorText>{error}</ErrorText>}
      {result && (
        <div role="status" className="rounded-md border p-3 text-sm">
          <p>
            <strong>{result.symbol}</strong>: {result.files.filter((f) => f.status === "parsed").length} of{" "}
            {result.files.length} stored.{" "}
            <Link href={`/stocks/${result.symbol}`} className="underline">
              Open report
            </Link>
          </p>
          <ul className="mt-1 flex flex-col gap-0.5 text-xs">
            {result.files.map((f, i) => (
              <li key={`${f.filename}-${i}`} className={f.status === "failed" ? "text-destructive" : undefined}>
                {f.filename}:{" "}
                {f.status === "parsed"
                  ? `${periodLabel({ periods: f.periods, period_end: null })}, ${f.statement_type}, usable from ${f.announcement_date ?? "unknown date"}`
                  : `failed: ${f.error}`}
                {f.warnings.length > 0 && <span className="text-muted-foreground"> ({f.warnings.join("; ")})</span>}
              </li>
            ))}
          </ul>
          {rebuild && <p className="text-muted-foreground mt-1 text-xs">{rebuild}</p>}
        </div>
      )}
    </div>
  );
}

export function Filings() {
  const [summary, setSummary] = useState<FilingsSummary | null>(null);
  const [rows, setRows] = useState<ResultFiling[] | null>(null);
  const [status, setStatus] = useState<FilingStatus[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    const q = status.length === 1 ? `&status=${status[0]}` : "";
    api<FilingsSummary>("/filings/summary").then(setSummary).catch(() => setSummary(null));
    api<ResultFiling[]>(`/filings?limit=50${q}`)
      .then((r) => setRows(status.length > 1 ? r.filter((f) => status.includes(f.status)) : r))
      .catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, [status]);
  useEffect(load, [load]);

  async function retry(id: number) {
    try {
      await api(`/filings/${id}/retry`, { method: "POST" });
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "retry failed");
    }
  }

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle className="text-base">Results filings (exchange XBRL)</CardTitle>
        <CardDescription>
          Quarterly and annual results come from the XBRL filings companies make to NSE and BSE. The nightly
          results_backfill job downloads new ones and backfills ten years, a few hundred documents a night. Each
          filing is dated by when the exchange published it, so backtests only see results that were public at the
          time. For a company or period the job can’t fetch, upload its XBRL documents from the NSE or BSE
          website.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Summary s={summary} />
        <Upload onDone={load} />
        <div className="flex flex-col gap-2">
          <Chips label="Show" options={STATUSES} value={status} onChange={setStatus} render={(s) => STATUS_LABEL[s]} />
          {error && <ErrorText>{error}</ErrorText>}
          {rows == null ? (
            <p className="text-muted-foreground text-sm">Loading…</p>
          ) : rows.length === 0 ? (
            <Empty>{status.length ? "No filings with this status." : "No filings yet: the results_backfill job lists them on its first run."}</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm tabular-nums" aria-label="Results filings">
                <thead className="text-muted-foreground text-xs">
                  <tr>
                    <th className="py-1 pr-2 text-left font-normal">Stock</th>
                    <th className="py-1 pr-2 text-left font-normal">Period</th>
                    <th className="py-1 pr-2 text-left font-normal">Statements</th>
                    <th className="py-1 pr-2 text-left font-normal">Usable from</th>
                    <th className="py-1 pr-2 text-left font-normal">Source</th>
                    <th className="py-1 text-left font-normal">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((f) => (
                    <tr key={f.id} className="border-t align-top">
                      <td className="py-1.5 pr-2">
                        <Link href={`/stocks/${f.symbol}`} className="font-medium hover:underline">
                          {f.symbol}
                        </Link>
                      </td>
                      <td className="py-1.5 pr-2 text-xs whitespace-nowrap">{periodLabel(f)}</td>
                      <td className="py-1.5 pr-2 text-xs">
                        {f.statement_type ?? "—"}
                        {f.is_bank ? " · bank" : ""}
                      </td>
                      <td className="py-1.5 pr-2 text-xs whitespace-nowrap" title={f.disseminated_at ? `published ${dateTime(f.disseminated_at)}` : undefined}>
                        {f.announcement_date ?? "—"}
                      </td>
                      <td className="py-1.5 pr-2 text-xs">
                        {f.exchange === "nse" ? (
                          <a href={f.document} target="_blank" rel="noreferrer noopener" className="hover:underline">
                            NSE
                          </a>
                        ) : (
                          "upload"
                        )}
                      </td>
                      <td className="py-1.5 text-xs">
                        {f.status === "failed" ? (
                          <span className="flex flex-wrap items-center gap-2">
                            <span className="text-destructive">
                              Failed ({f.attempts}×): {f.error}
                            </span>
                            {f.exchange === "nse" && (
                              <Button size="sm" variant="outline" className="h-6 px-2 text-xs" onClick={() => retry(f.id)}>
                                Retry
                              </Button>
                            )}
                          </span>
                        ) : (
                          <span title={f.warnings?.join("; ") || undefined}>
                            {STATUS_LABEL[f.status]}
                            {f.warnings?.length ? " (with notes)" : ""}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
