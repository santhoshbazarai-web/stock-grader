"use client";

// Reconciliation banner (SPEC v0.2 §3.9): the stock's filed figures disagree across sources
// (NSE XBRL, annual-report PDF, Market Lens, yfinance) by more than the tolerance. Open issues
// lower the valuation confidence; each can be ignored once explained (it then stops counting
// from the next report build).
import { AlertTriangle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import type { Reconciliation, ReconciliationIssue } from "@/lib/types";

const CAUSE: Record<NonNullable<ReconciliationIssue["cause"]>, string> = {
  units: "units mix-up",
  basis: "consolidated / standalone mix-up",
  restatement: "restatement",
};

export function ReconciliationBanner({
  symbol,
  onChange,
}: {
  symbol: string;
  onChange?: () => void;
}) {
  const [data, setData] = useState<Reconciliation | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api<Reconciliation>(`/stocks/${encodeURIComponent(symbol)}/reconciliation`)
      .then(setData)
      .catch(() => setData(null));
  }, [symbol]);
  useEffect(load, [load]);

  if (!data || data.open.length === 0) return null;

  const ignore = async (issue: ReconciliationIssue) => {
    setBusy(issue.id);
    setError(null);
    try {
      await api(
        `/stocks/${encodeURIComponent(symbol)}/reconciliation/${issue.id}/ignore`,
        { method: "POST" },
      );
      load();
      onChange?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "could not ignore");
    } finally {
      setBusy(null);
    }
  };

  const n = data.open.length;
  return (
    <section
      role="alert"
      aria-label="Reconciliation issues"
      className="bg-card flex flex-col gap-2 rounded-lg border-l-4 p-4 text-sm shadow-sm"
      style={{ borderLeftColor: "var(--viz-critical)" }}
    >
      <h2 className="flex items-center gap-2 font-semibold">
        <AlertTriangle
          className="size-4 shrink-0"
          style={{ color: "var(--viz-critical)" }}
          aria-hidden
        />
        Sources disagree on {n} figure{n === 1 ? "" : "s"}: valuation confidence
        lowered
      </h2>
      <p className="text-muted-foreground text-xs">
        Differences above {Math.round(data.tolerance_rel * 100)}% between the
        exchange filing and other sources. Check the cause before relying on the
        numbers; ignore one once it is explained.
      </p>
      <ul className="flex flex-col gap-2">
        {data.open.map((i) => (
          <li
            key={i.id}
            className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between sm:gap-4"
          >
            <div className="min-w-0">
              <p className="break-words">{i.reasons[0]}</p>
              <p className="text-muted-foreground text-xs">
                {i.cause ? (
                  <>
                    Likely cause:{" "}
                    <span className="text-foreground font-medium">
                      {CAUSE[i.cause]}
                    </span>{" "}
                    — {i.reasons[1]}
                  </>
                ) : (
                  (i.reasons[1] ?? "no cause found")
                )}
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              className="shrink-0 self-start"
              disabled={busy === i.id}
              onClick={() => ignore(i)}
            >
              Ignore
            </Button>
          </li>
        ))}
      </ul>
      {error && <p className="text-destructive text-xs">{error}</p>}
    </section>
  );
}
