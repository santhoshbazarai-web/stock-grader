"use client";

// Annual-report review queue (SPEC v0.2 §3.6 step 3). The worker's annual_reports job (or an
// upload here) reads balance sheets and cash flow statements from annual-report PDFs for the
// years the exchange XBRL lacks. Confident values are stored at once; the rest wait here to be
// accepted, corrected (₹ crore) or rejected in one click. Each row links to its page in the PDF.
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { AppNav, Empty, ErrorText } from "@/components/common";
import { Chips } from "@/components/pages/screener";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import type { AnnualReport, PdfCandidate, ReviewStatus, ReviewSummary } from "@/lib/types";

const SYMBOL_RE = /^[A-Za-z0-9&_.-]{1,32}$/;
const STATUSES: ReviewStatus[] = ["pending", "auto_accepted", "accepted", "corrected", "rejected"];
const STATUS_LABEL: Record<ReviewStatus, string> = {
  pending: "To review",
  auto_accepted: "Auto-accepted",
  accepted: "Accepted",
  corrected: "Corrected",
  rejected: "Rejected",
};
const STATEMENT_LABEL = { bs: "Balance sheet", cf: "Cash flow" } as const;
const cr = (v: number | null) =>
  v == null ? "—" : v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function Upload({ symbol: initial, onDone }: { symbol: string; onDone: () => void }) {
  const [symbol, setSymbol] = useState(initial);
  const [fy, setFy] = useState("");
  const [published, setPublished] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AnnualReport | null>(null);
  const input = useRef<HTMLInputElement>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return setError("Choose the annual report (.pdf, or the exchange's .zip)");
    if (!SYMBOL_RE.test(symbol.trim())) return setError("Enter the NSE symbol");
    if (!/^\d{4}$/.test(fy)) return setError("Enter the fiscal year the report covers, e.g. 2014 for FY2013-14");
    const body = new FormData();
    body.set("file", file);
    body.set("symbol", symbol.trim());
    body.set("fiscal_year", fy);
    if (published) body.set("published_on", published);
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api<AnnualReport>("/uploads/annual-report", { method: "POST", body }));
      setFile(null);
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
      <form onSubmit={submit} className="flex flex-wrap items-end gap-3" aria-label="Upload annual report">
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">Annual report (.pdf / .zip)</span>
          <input
            ref={input}
            type="file"
            accept=".pdf,.zip,application/pdf,application/zip"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">Symbol</span>
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="e.g. TCS"
            aria-label="Report symbol"
            className="border-input bg-background h-8 w-28 rounded-md border px-2 text-sm uppercase"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">Fiscal year (ending)</span>
          <input
            value={fy}
            onChange={(e) => setFy(e.target.value)}
            placeholder="2014"
            inputMode="numeric"
            aria-label="Fiscal year"
            className="border-input bg-background h-8 w-20 rounded-md border px-2 text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">Published on (for backtests)</span>
          <input
            type="date"
            value={published}
            onChange={(e) => setPublished(e.target.value)}
            aria-label="Published on"
            className="border-input bg-background h-8 rounded-md border px-2 text-sm"
          />
        </label>
        <Button size="sm" type="submit" disabled={busy}>
          {busy ? "Reading…" : "Read report"}
        </Button>
      </form>
      {error && <ErrorText>{error}</ErrorText>}
      {result && (
        <div role="status" className="rounded-md border p-3 text-sm">
          {result.status === "parsed" ? (
            <p>
              <strong>{result.symbol}</strong> FY{result.fiscal_year}: read{" "}
              {(result.statements ?? [])
                .map((s) => `${s.basis} ${STATEMENT_LABEL[s.statement].toLowerCase()} (p.${s.pages.join(",")})`)
                .join(", ")}
              . {result.candidates.auto_accepted ?? 0} values stored, {result.candidates.pending ?? 0} to review.{" "}
              <Link href={`/stocks/${result.symbol}`} className="underline">
                Open report
              </Link>
            </p>
          ) : (
            <p className="text-destructive">Not read: {result.error}</p>
          )}
          {(result.warnings ?? []).length > 0 && (
            <ul className="text-muted-foreground mt-1 list-disc pl-4 text-xs">
              {result.warnings!.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function Row({ c, onChanged }: { c: PdfCandidate; onChanged: (c: PdfCandidate) => void }) {
  const [value, setValue] = useState(c.value_cr == null ? "" : String(c.corrected_value_cr ?? c.value_cr));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  async function decide(action: "accept" | "correct" | "reject") {
    const body: Record<string, unknown> = { action };
    if (action === "correct") {
      const v = Number(value.replace(/,/g, ""));
      if (!value.trim() || !Number.isFinite(v)) return setError("enter a number (₹ crore)");
      body.value_cr = v;
    }
    setBusy(true);
    setError(null);
    try {
      onChanged(
        await api<PdfCandidate>(`/review/annual-reports/${c.id}`, { method: "POST", body: JSON.stringify(body) }),
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "failed");
    } finally {
      setBusy(false);
    }
  }

  const page = c.pages[0];
  return (
    <tr className="border-t align-top" aria-label={`${c.symbol} ${c.item_code} ${c.period_end}`}>
      <td className="py-1.5 pr-2">
        <Link href={`/stocks/${c.symbol}`} className="font-medium underline">
          {c.symbol}
        </Link>
        <div className="text-muted-foreground text-xs">FY{c.fiscal_year} report</div>
      </td>
      <td className="pr-2 text-xs">
        {c.period_end}
        <div className="text-muted-foreground">
          {c.basis} · {STATEMENT_LABEL[c.statement]}
        </div>
      </td>
      <td className="pr-2 font-mono text-xs">{c.item_code}</td>
      <td className="max-w-64 pr-2 text-xs">
        “{c.raw_label}”
        <div className="text-muted-foreground">
          printed {c.raw_value.toLocaleString("en-IN")} ·{" "}
          <a
            href={`/api/annual-reports/${c.annual_report_id}/document#page=${page}`}
            target="_blank"
            rel="noreferrer"
            className="underline"
          >
            page {c.pages.join(", ")}
          </a>
        </div>
      </td>
      <td className="pr-2 text-right text-xs tabular-nums">{cr(c.corrected_value_cr ?? c.value_cr)}</td>
      <td className="pr-2 text-xs">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="underline decoration-dotted"
        >
          {Math.round(c.confidence * 100)}%
        </button>
        {open && (
          <ul className="text-muted-foreground mt-1 list-disc pl-4">
            {c.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        )}
      </td>
      <td className="text-xs">
        {c.status === "pending" || c.status === "auto_accepted" ? (
          <div className="flex flex-wrap items-center gap-1">
            <Button size="sm" variant="secondary" disabled={busy || c.value_cr == null} onClick={() => decide("accept")}>
              Accept
            </Button>
            <input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && decide("correct")}
              aria-label={`Corrected value for ${c.item_code} ${c.period_end} (₹ crore)`}
              className="border-input bg-background h-7 w-24 rounded-md border px-1.5 text-right text-xs tabular-nums"
            />
            <Button size="sm" variant="outline" disabled={busy} onClick={() => decide("correct")}>
              Save
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => decide("reject")}>
              Reject
            </Button>
          </div>
        ) : (
          <span>
            {STATUS_LABEL[c.status]}
            {c.stored ? " · stored" : ""}
          </span>
        )}
        {c.note && <div className="text-muted-foreground">{c.note}</div>}
        {error && <div className="text-destructive">{error}</div>}
      </td>
    </tr>
  );
}

function Reports({ symbol, version }: { symbol: string; version: number }) {
  const [rows, setRows] = useState<AnnualReport[] | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const load = useCallback(() => {
    const q = symbol ? `&symbol=${encodeURIComponent(symbol)}` : "";
    api<AnnualReport[]>(`/annual-reports?limit=30${q}`)
      .then(setRows)
      .catch(() => setRows([]));
  }, [symbol]);
  useEffect(load, [load, version]);

  async function reread(id: number) {
    setBusy(id);
    await api(`/annual-reports/${id}/reparse`, { method: "POST" }).catch(() => undefined);
    setBusy(null);
    load();
  }

  if (!rows) return null;
  if (rows.length === 0) return <Empty>No annual reports read yet.</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[44rem] text-sm" aria-label="Annual reports">
        <thead className="text-muted-foreground text-left text-xs">
          <tr>
            <th className="py-1 pr-2 font-normal">Stock</th>
            <th className="pr-2 font-normal">FY</th>
            <th className="pr-2 font-normal">Source</th>
            <th className="pr-2 font-normal">Status</th>
            <th className="pr-2 font-normal">Statements</th>
            <th className="pr-2 font-normal">Values</th>
            <th className="font-normal" />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t align-top text-xs">
              <td className="py-1.5 pr-2 text-sm font-medium">{r.symbol}</td>
              <td className="pr-2">{r.fiscal_year ?? "—"}</td>
              <td className="pr-2">{r.exchange === "upload" ? "upload" : "NSE"}</td>
              <td className={`pr-2 ${r.status === "failed" ? "text-destructive" : ""}`}>
                {r.status === "parsed" ? "read" : r.status}
                {r.error && <div className="max-w-64">{r.error}</div>}
              </td>
              <td className="pr-2">
                {(r.statements ?? []).map((s) => (
                  <div key={`${s.statement}-${s.basis}`}>
                    {s.basis} {STATEMENT_LABEL[s.statement].toLowerCase()} p.{s.pages.join(",")}
                    {s.checks.includes("failed") && <span className="text-destructive"> · check failed</span>}
                  </div>
                ))}
              </td>
              <td className="pr-2">
                {Object.entries(r.candidates)
                  .map(([k, n]) => `${n} ${STATUS_LABEL[k as ReviewStatus].toLowerCase()}`)
                  .join(", ") || "—"}
              </td>
              <td className="whitespace-nowrap">
                {r.has_document && (
                  <>
                    <a href={`/api/annual-reports/${r.id}/document`} target="_blank" rel="noreferrer" className="underline">
                      PDF
                    </a>{" "}
                    <Button size="sm" variant="ghost" disabled={busy === r.id} onClick={() => reread(r.id)}>
                      {busy === r.id ? "Reading…" : "Re-read"}
                    </Button>
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Review() {
  const params = useSearchParams();
  const [symbol, setSymbol] = useState((params.get("symbol") ?? "").toUpperCase());
  const [status, setStatus] = useState<ReviewStatus[]>(["pending"]);
  const [rows, setRows] = useState<PdfCandidate[] | null>(null);
  const [summary, setSummary] = useState<ReviewSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  const load = useCallback(() => {
    const sym = SYMBOL_RE.test(symbol) ? `&symbol=${encodeURIComponent(symbol)}` : "";
    api<ReviewSummary>("/review/annual-reports/summary").then(setSummary).catch(() => setSummary(null));
    // one request per status chosen (none chosen: all of them)
    const requests = (status.length ? status : STATUSES).map((s) =>
      api<PdfCandidate[]>(`/review/annual-reports?limit=500${sym}&status=${s}`),
    );
    Promise.all(requests)
      .then((lists) => {
        setRows(lists.flat().sort((a, b) => a.confidence - b.confidence));
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : "failed to load"));
  }, [symbol, status]);
  useEffect(load, [load, version]);

  const replace = (c: PdfCandidate) => {
    setRows((rs) => (rs ?? []).map((r) => (r.id === c.id ? c : r)));
    api<ReviewSummary>("/review/annual-reports/summary").then(setSummary).catch(() => undefined);
  };

  return (
    <main className="mx-auto flex max-w-7xl flex-col gap-6 p-4 sm:p-6">
      <AppNav />
      <Card>
        <CardHeader>
          <CardTitle>Annual reports</CardTitle>
          <CardDescription>
            Balance sheets and cash flows for the years the exchange XBRL lacks, read from annual-report PDFs.
            The weekly <code>annual_reports</code> job fetches them from NSE; for BSE-only companies (or while NSE is
            unreachable) upload the PDF here. Values never replace exchange XBRL figures.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Upload symbol={symbol} onDone={() => setVersion((v) => v + 1)} />
          <Reports symbol={SYMBOL_RE.test(symbol) ? symbol : ""} version={version} />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Review queue</CardTitle>
          <CardDescription>
            {summary ? (
              <span aria-label="Review summary">
                <strong className="text-foreground">{summary.pending}</strong> value
                {summary.pending === 1 ? "" : "s"} to review · {summary.reports_parsed} reports read ·{" "}
                {summary.reports_failed} failed.{" "}
              </span>
            ) : null}
            Accept a value as read, type the right figure (₹ crore) and Save, or Reject it. Lowest confidence first;
            click a confidence to see why.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-end gap-4">
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">Symbol</span>
              <input
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                placeholder="all"
                aria-label="Filter symbol"
                className="border-input bg-background h-8 w-28 rounded-md border px-2 text-sm uppercase"
              />
            </label>
            <Chips label="Status" options={STATUSES} value={status} onChange={setStatus} render={(s) => STATUS_LABEL[s]} />
          </div>
          {error && <ErrorText>{error}</ErrorText>}
          {rows && rows.length === 0 && <Empty>Nothing to review.</Empty>}
          {rows && rows.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[56rem] text-sm" aria-label="Review queue">
                <thead className="text-muted-foreground text-left text-xs">
                  <tr>
                    <th className="py-1 pr-2 font-normal">Stock</th>
                    <th className="pr-2 font-normal">Period</th>
                    <th className="pr-2 font-normal">Item</th>
                    <th className="pr-2 font-normal">Read from</th>
                    <th className="pr-2 text-right font-normal">₹ crore</th>
                    <th className="pr-2 font-normal">Confidence</th>
                    <th className="font-normal">Decision</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => (
                    <Row key={c.id} c={c} onChanged={replace} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </main>
  );
}
