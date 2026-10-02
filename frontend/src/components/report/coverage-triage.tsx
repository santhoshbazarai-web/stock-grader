"use client";

// Triage of a coverage cell's annual-report values to review: each PDF value next to the value
// another source already stored (₹ crore), the difference, and a one-click "use this". The
// default follows the precedence exchange XBRL > Indian API > PDF unless the PDF value is
// confident enough; "Apply defaults" decides the whole cell. Decisions are saved.
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { num, pct } from "@/lib/format";
import type { TriageRow } from "@/lib/types";

export function CoverageTriage({
  symbol,
  fiscalYear,
  statement,
  onDone,
}: {
  symbol: string;
  fiscalYear: number;
  statement: string;
  onDone: () => void;
}) {
  const url = `/stocks/${encodeURIComponent(symbol)}/coverage/${fiscalYear}/${encodeURIComponent(statement)}/triage`;
  const [rows, setRows] = useState<TriageRow[] | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api<TriageRow[]>(url)
      .then((r) => live && setRows(r))
      .catch((e) => live && setMsg(e instanceof ApiError ? e.detail : "unavailable"));
    return () => {
      live = false;
    };
  }, [url]);

  async function send(body: object) {
    setBusy(true);
    setMsg(null);
    try {
      const left = await api<TriageRow[]>(url, { method: "POST", body: JSON.stringify(body) });
      setRows(left);
      if (left.length === 0) onDone();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.detail : "save failed");
    } finally {
      setBusy(false);
    }
  }

  if (!rows) return <p className="text-muted-foreground text-xs">{msg ?? "Loading values to review…"}</p>;
  return (
    <section className="flex flex-col gap-2 rounded border p-2" aria-label={`Review ${statement} FY${fiscalYear}`}>
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="font-medium">
          {statement} FY{fiscalYear}: {rows.length} value{rows.length === 1 ? "" : "s"} to review
        </span>
        <span className="flex gap-2">
          <Button size="sm" variant="outline" disabled={busy || rows.length === 0} onClick={() => send({ apply_defaults: true })}>
            Apply defaults
          </Button>
          <Button size="sm" variant="ghost" onClick={onDone}>
            Close
          </Button>
        </span>
      </div>
      {rows.length === 0 ? (
        <p className="text-muted-foreground text-xs">Nothing left to review in this cell.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs tabular-nums">
            <thead className="text-muted-foreground">
              <tr>
                <th className="py-1 text-left font-normal">Item</th>
                <th className="py-1 text-right font-normal">Annual report (₹ Cr)</th>
                <th className="py-1 text-right font-normal">Other source (₹ Cr)</th>
                <th className="py-1 text-right font-normal">Difference</th>
                <th className="py-1 text-left font-normal">Use</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.candidate_id} className="border-t align-top">
                  <td className="py-1">
                    {r.item_code.replace(/_/g, " ")}
                    <p className="text-muted-foreground">“{r.raw_label}” · confidence {pct(r.pdf_confidence, 0)}</p>
                  </td>
                  <td className="py-1 text-right">{num(r.pdf_value_cr, 2)}</td>
                  <td className="py-1 text-right">
                    {num(r.other_value_cr, 2)}
                    <p className="text-muted-foreground">{r.other_source ?? "none stored"}</p>
                  </td>
                  <td className="py-1 text-right">
                    {num(r.difference_cr, 2)}
                    {r.difference_pct != null && <p className="text-muted-foreground">{pct(r.difference_pct, 2)}</p>}
                  </td>
                  <td className="py-1">
                    <div className="flex flex-wrap gap-1">
                      <Button size="sm" variant={r.default === "pdf" ? "default" : "outline"} disabled={busy || r.pdf_value_cr == null}
                        onClick={() => send({ decisions: [{ candidate_id: r.candidate_id, use: "pdf" }] })}>
                        Use annual report
                      </Button>
                      {r.other_source && (
                        <Button size="sm" variant={r.default === "other" ? "default" : "outline"} disabled={busy}
                          onClick={() => send({ decisions: [{ candidate_id: r.candidate_id, use: "other" }] })}>
                          Use {r.other_source}
                        </Button>
                      )}
                    </div>
                    <p className="text-muted-foreground mt-0.5">default: {r.reason}</p>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {msg && <p className="text-destructive text-xs">{msg}</p>}
    </section>
  );
}
