"use client";

// Broker connections (SPEC §3.3, §9): one card per broker, Connected (expires HH:MM) / Expired /
// Not connected / Not configured / Disabled. Connect sends the browser through the API's OAuth
// login (same tab), which redirects to Fyers / Zerodha and returns to /settings?broker=…&status=….
// Brokers are read-only (AGENTS.md rule 7): tokens are used for market data only.
import { CheckCircle2, CircleAlert, CircleSlash, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
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

function timeText(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
}

type State = "connected" | "expired" | "not_connected" | "not_configured" | "disabled";

export function brokerState(b: BrokerStatus): State {
  if (!b.enabled) return "disabled";
  if (!b.configured) return "not_configured";
  if (b.connected) return "connected";
  return b.expires_at ? "expired" : "not_connected";
}

function describe(b: BrokerStatus, state: State): { label: string; detail: string } {
  switch (state) {
    case "connected":
      return {
        label: `Connected (expires ${timeText(b.expires_at)})`,
        detail: `Token valid until ${expiryText(b.expires_at)}. Brokers expire tokens daily.`,
      };
    case "expired":
      return {
        label: "Expired",
        detail: `The token expired ${expiryText(b.expires_at)}. Prices fall back to the NSE bhavcopy and yfinance until you reconnect.`,
      };
    case "not_connected":
      return { label: "Not connected", detail: "Log in to fetch prices through this broker." };
    case "not_configured":
      return { label: "Not configured", detail: `Set ${ENV[b.broker]} in .env to enable.` };
    case "disabled":
      return {
        label: "Disabled",
        detail: `Off in config/providers.yaml (brokers.${b.broker}.enabled).${b.broker === "kite" ? " Kite historical data needs a paid subscription." : ""}`,
      };
  }
}

function Icon({ state }: { state: State }) {
  if (state === "connected")
    return <CheckCircle2 className="mt-0.5 size-4 shrink-0" style={{ color: "var(--viz-good)" }} aria-hidden />;
  if (state === "expired" || state === "not_connected")
    return <CircleAlert className="mt-0.5 size-4 shrink-0" style={{ color: "var(--viz-critical)" }} aria-hidden />;
  return <CircleSlash className="text-muted-foreground mt-0.5 size-4 shrink-0" aria-hidden />;
}

export function BrokerList({ statuses, connect }: { statuses: BrokerStatus[]; connect: boolean }) {
  return (
    <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2" aria-label="Broker connections">
      {statuses.map((b) => {
        const state = brokerState(b);
        const { label, detail } = describe(b, state);
        const canConnect = connect && b.enabled && b.configured;
        return (
          <li
            key={b.broker}
            aria-label={`${NAME[b.broker]}: ${label}`}
            className={`flex flex-col gap-3 rounded-lg border p-4 ${state === "disabled" ? "opacity-70" : ""}`}
          >
            <div className="flex items-start gap-2">
              <Icon state={state} />
              <div className="min-w-0">
                <p className="text-sm font-medium">
                  {NAME[b.broker]} <span className="text-muted-foreground font-normal">· {label}</span>
                </p>
                <p className="text-muted-foreground text-xs">{detail}</p>
              </div>
            </div>
            {canConnect && (
              <Button
                size="sm"
                className="self-start"
                variant={state === "connected" ? "outline" : "default"}
                onClick={() => window.location.assign(`/api/brokers/${b.broker}/login`)}
              >
                {state === "connected" || state === "expired" ? "Reconnect" : `Connect ${NAME[b.broker]}`}
              </Button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

const DISMISS_KEY = "sg-broker-banner-dismissed";

/** SPEC §3.3: a "Reconnect" banner on every page while an enabled, configured broker has no
 * valid token (hidden on Settings, where the cards show it; dismissible for this tab). */
export function ReconnectBanner() {
  const path = usePathname();
  const [stale, setStale] = useState<BrokerStatus[]>([]);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    try {
      setDismissed(sessionStorage.getItem(DISMISS_KEY) === "1");
    } catch {
      /* storage unavailable: show the banner */
    }
    let live = true;
    api<BrokerStatus[]>("/brokers/status")
      .then((all) => live && setStale(all.filter((b) => ["expired", "not_connected"].includes(brokerState(b)))))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  if (dismissed || stale.length === 0 || path.startsWith("/settings")) return null;
  const names = stale.map((b) => NAME[b.broker]).join(" and ");
  return (
    <aside
      aria-label="Broker token"
      className="bg-card flex flex-wrap items-center justify-between gap-2 rounded-lg border-l-4 px-4 py-2 text-sm shadow-sm"
      style={{ borderLeftColor: "var(--viz-critical)" }}
    >
      <span>
        {names} {stale.length === 1 ? "is" : "are"} not connected: prices use the NSE bhavcopy and yfinance fallback.
      </span>
      <span className="flex items-center gap-2">
        <Link href="/settings" className="font-medium underline">
          Reconnect
        </Link>
        <button
          type="button"
          aria-label="Dismiss"
          className="text-muted-foreground"
          onClick={() => {
            setDismissed(true);
            try {
              sessionStorage.setItem(DISMISS_KEY, "1");
            } catch {
              /* ignore */
            }
          }}
        >
          <X className="size-4" />
        </button>
      </span>
    </aside>
  );
}
