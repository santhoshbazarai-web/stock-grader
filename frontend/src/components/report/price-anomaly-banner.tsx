"use client";

// Price data-quality banner (SPEC §3.2 "price adjustment"): adjusted closes that move like a
// split / bonus that is missing from the corporate actions, applied twice, or not applied.
// Each confirmed one has a one-click suggested fix (it changes the corporate actions and
// re-adjusts the prices; Refresh rebuilds the report). A genuine move can be dismissed.
import { AlertTriangle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import type { PriceAnomalies, PriceAnomaly, PriceFix } from "@/lib/types";

const KIND: Record<PriceAnomaly["kind"], string> = {
  missing_action: "split / bonus missing",
  double_adjusted: "adjusted twice",
  not_applied: "split / bonus not applied",
};

export function PriceAnomalyBanner({ symbol }: { symbol: string }) {
  const [data, setData] = useState<PriceAnomalies | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(() => {
    api<PriceAnomalies>(`/stocks/${encodeURIComponent(symbol)}/price-anomalies`)
      .then(setData)
      .catch(() => setData(null));
  }, [symbol]);
  useEffect(load, [load]);

  if (!data || (data.open.length === 0 && !message)) return null;

  const act = async (a: PriceAnomaly, what: "apply" | "dismiss") => {
    setBusy(a.id);
    setMessage(null);
    try {
      const path = `/stocks/${encodeURIComponent(symbol)}/price-anomalies/${a.id}/${what}`;
      if (what === "apply") {
        const fix = await api<PriceFix>(path, { method: "POST" });
        setMessage(
          `${fix.done}; ${fix.readjusted_bars} bars re-adjusted. Refresh to rebuild the report.`,
        );
      } else {
        await api(path, { method: "POST" });
      }
      load();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.detail : `could not ${what}`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section
      role="alert"
      aria-label="Price data anomalies"
      className="bg-card flex flex-col gap-2 rounded-lg border-l-4 p-4 text-sm shadow-sm"
      style={{ borderLeftColor: "var(--viz-s4)" }}
    >
      {data.open.length > 0 && (
        <h2 className="flex items-center gap-2 font-semibold">
          <AlertTriangle
            className="size-4 shrink-0"
            style={{ color: "var(--viz-s4)" }}
            aria-hidden
          />
          Price data: {data.open.length} move
          {data.open.length === 1 ? "" : "s"} look like a split / bonus problem
        </h2>
      )}
      <ul className="flex flex-col gap-2">
        {data.open.map((a) => (
          <li
            key={a.id}
            className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between sm:gap-4"
          >
            <div className="min-w-0">
              <p className="font-medium">
                {a.day}: {KIND[a.kind]}
              </p>
              <p className="text-muted-foreground text-xs break-words">
                {a.text}
              </p>
            </div>
            <div className="flex shrink-0 gap-2 self-start">
              {a.volume_confirmed && (
                <Button
                  size="sm"
                  disabled={busy === a.id}
                  onClick={() => act(a, "apply")}
                >
                  Apply suggested fix
                </Button>
              )}
              <Button
                size="sm"
                variant="outline"
                disabled={busy === a.id}
                onClick={() => act(a, "dismiss")}
              >
                Dismiss
              </Button>
            </div>
          </li>
        ))}
      </ul>
      {message && (
        <p className="text-muted-foreground text-xs" role="status">
          {message}
        </p>
      )}
    </section>
  );
}
