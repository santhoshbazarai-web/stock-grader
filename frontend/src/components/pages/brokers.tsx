"use client";

// Broker connection status (SPEC §9). Connect sends the browser through the API's OAuth login,
// which redirects to Fyers / Zerodha and comes back to /settings?broker=…&status=…
// Brokers are read-only (AGENTS.md rule 7): the tokens are used for market data only.
import { CheckCircle2, CircleAlert, CircleSlash } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { BrokerStatus } from "@/lib/types";

const NAME: Record<BrokerStatus["broker"], string> = { fyers: "Fyers", kite: "Zerodha Kite" };
const ENV: Record<BrokerStatus["broker"], string> = {
  fyers: "FYERS_APP_ID, FYERS_SECRET, FYERS_REDIRECT_URI",
  kite: "KITE_API_KEY, KITE_API_SECRET",
};

export function expiryText(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export function BrokerList({ statuses, connect }: { statuses: BrokerStatus[]; connect: boolean }) {
  return (
    <ul className="flex flex-col divide-y" aria-label="Broker connections">
      {statuses.map((b) => (
        <li key={b.broker} className="flex flex-wrap items-center justify-between gap-3 py-3">
          <div className="flex items-start gap-2">
            {b.connected ? (
              <CheckCircle2 className="mt-0.5 size-4" style={{ color: "var(--viz-good)" }} aria-hidden />
            ) : b.configured ? (
              <CircleAlert className="mt-0.5 size-4" style={{ color: "var(--viz-critical)" }} aria-hidden />
            ) : (
              <CircleSlash className="text-muted-foreground mt-0.5 size-4" aria-hidden />
            )}
            <div>
              <p className="text-sm font-medium">
                {NAME[b.broker]}{" "}
                <span className="text-muted-foreground font-normal">
                  · {b.connected ? "connected" : b.configured ? b.reason : "not configured"}
                </span>
              </p>
              <p className="text-muted-foreground text-xs">
                {b.connected
                  ? `Token valid until ${expiryText(b.expires_at)} (brokers expire tokens daily)`
                  : b.configured
                    ? "Log in to fetch prices through this broker."
                    : `Set ${ENV[b.broker]} in .env to enable.`}
              </p>
            </div>
          </div>
          {connect && b.configured && (
            <Button
              size="sm"
              variant={b.connected ? "outline" : "default"}
              onClick={() => window.location.assign(`/api/brokers/${b.broker}/login`)}
            >
              {b.connected ? "Reconnect" : `Connect ${NAME[b.broker]}`}
            </Button>
          )}
        </li>
      ))}
    </ul>
  );
}
