"use client";

// Small shared pieces for the app pages: navigation, grade/action badges, empty states.
import { LogOut, Monitor, Moon, Sun } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { EmptyState } from "@/components/ds";
import { brokerState } from "@/components/pages/brokers";
import type { BrokerStatus } from "@/lib/types";

import { NotificationBell } from "@/components/notification-bell";
import { ReconnectBanner } from "@/components/pages/brokers";
import { SymbolSearch } from "@/components/report/symbol-search";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { ACTION_LABEL, ZONE_LABEL } from "@/lib/format";
import type { Durability } from "@/lib/types";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/screener", label: "Screener" },
  { href: "/valuation-map", label: "Valuation Map" },
  { href: "/watchlist", label: "Watchlist" },
  { href: "/portfolio", label: "Portfolio" },
  { href: "/alerts", label: "Alerts" },
];
const MORE = [
  { href: "/screening-ideas", label: "Screening Ideas" },
  { href: "/notes", label: "My Notes" },
  { href: "/glossary", label: "Glossary" },
  { href: "/backtests", label: "Backtests" },
  { href: "/review", label: "Review" },
  { href: "/settings", label: "Settings" },
];

/** Light / dark / system, remembered in localStorage (the layout script applies it first). */
function ThemeToggle() {
  const [mode, setMode] = useState<"system" | "light" | "dark">("system");
  useEffect(() => {
    try {
      const m = localStorage.getItem("theme");
      if (m === "light" || m === "dark") setMode(m);
    } catch {
      /* storage unavailable */
    }
  }, []);
  function cycle() {
    const next = mode === "system" ? "light" : mode === "light" ? "dark" : "system";
    setMode(next);
    try {
      if (next === "system") localStorage.removeItem("theme");
      else localStorage.setItem("theme", next);
    } catch {
      /* ignore */
    }
    const dark = next === "dark" || (next === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.classList.toggle("dark", dark);
  }
  const Icon = mode === "dark" ? Moon : mode === "light" ? Sun : Monitor;
  return (
    <Button size="sm" variant="ghost" onClick={cycle} aria-label={`Theme: ${mode} (click to change)`} title={`Theme: ${mode}`}>
      <Icon />
    </Button>
  );
}

/** "Fyers connected" / "Reconnect Fyers": the live-price broker at a glance. */
function BrokerChip() {
  const [b, setB] = useState<BrokerStatus | null>(null);
  useEffect(() => {
    let live = true;
    api<BrokerStatus[]>("/brokers/status")
      .then((all) => live && setB(all.find((x) => x.broker === "fyers" && x.enabled) ?? null))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  if (!b) return null;
  const ok = brokerState(b) === "connected";
  return (
    <Link
      href="/settings"
      title={b.reason}
      aria-label={ok ? "Fyers connected" : "Fyers not connected: reconnect"}
      className="hidden items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs sm:inline-flex"
    >
      <span aria-hidden className="size-2 rounded-full" style={{ background: ok ? "var(--sem-discount)" : "var(--sem-premium)" }} />
      Fyers {ok ? "live" : <span className="font-medium underline">Reconnect</span>}
    </Link>
  );
}

export function AppNav() {
  const path = usePathname();
  const router = useRouter();
  // Ctrl/Cmd+K focuses the global search from anywhere
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        document.querySelector<HTMLInputElement>('input[aria-label="Search stocks"]')?.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  async function logout() {
    await api("/auth/logout", { method: "POST" }).catch(() => undefined);
    router.replace("/login");
  }
  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href));
  const moreActive = MORE.some((l) => active(l.href));
  return (
    <>
      <nav className="bg-background/90 sticky top-0 z-30 -mx-4 flex flex-wrap items-center justify-between gap-3 border-b px-4 py-2 backdrop-blur sm:-mx-6 sm:px-6" aria-label="Main">
        <div className="flex flex-wrap items-center gap-1">
          <Link href="/" className="mr-3 flex items-center gap-2 text-sm font-semibold">
            <span aria-hidden className="grid size-6 place-items-center rounded-md bg-[var(--brand)] text-xs text-white">
              SG
            </span>
            Stock Grader
          </Link>
          {LINKS.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              aria-current={active(l.href) ? "page" : undefined}
              className={`rounded-md px-2.5 py-1.5 text-sm ${active(l.href) ? "bg-secondary font-medium" : "text-muted-foreground hover:text-foreground"}`}
            >
              {l.label}
            </Link>
          ))}
          <details className="relative">
            <summary className={`cursor-pointer list-none rounded-md px-2.5 py-1.5 text-sm ${moreActive ? "bg-secondary font-medium" : "text-muted-foreground hover:text-foreground"}`}>
              More ▾
            </summary>
            <div className="bg-popover absolute left-0 z-40 mt-1 flex min-w-40 flex-col rounded-lg border p-1 shadow-[var(--shadow-pop)]">
              {MORE.map((l) => (
                <Link key={l.href} href={l.href} aria-current={active(l.href) ? "page" : undefined} className="hover:bg-secondary rounded-md px-3 py-1.5 text-sm">
                  {l.label}
                </Link>
              ))}
            </div>
          </details>
        </div>
        <div className="flex items-center gap-2">
          <BrokerChip />
          <span className="flex items-center gap-1">
            <SymbolSearch />
            <kbd className="text-muted-foreground hidden rounded border px-1 text-[10px] lg:inline">Ctrl K</kbd>
          </span>
          <NotificationBell />
          <ThemeToggle />
          <Button size="sm" variant="ghost" onClick={logout} aria-label="Sign out">
            <LogOut />
          </Button>
        </div>
      </nav>
      <ReconnectBanner />
    </>
  );
}

/** A page that is planned but not built yet: never a 404. */
export function ComingSoon({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <Page>
      <h1 className="text-xl font-semibold">{title}</h1>
      <EmptyState title="Coming soon">{children ?? `${title} is planned and not built yet.`}</EmptyState>
    </Page>
  );
}

export function Page({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex max-w-7xl flex-col gap-6 p-4 sm:p-6">
      <AppNav />
      {children}
    </main>
  );
}

const BUYS = new Set(["strong_buy", "buy", "accumulate", "buy_on_pullback", "momentum_entry"]);

export function GradeBadge({ label }: { label: string | null }) {
  return <Badge variant="outline">{label ?? "n/a"}</Badge>;
}

const DUR_CLASS: Record<string, string> = {
  strong: "border-[var(--sem-discount)] text-[var(--sem-discount)]",
  moderate: "border-[var(--sem-fair)] text-[var(--sem-fair)]",
  weak: "border-[var(--sem-premium)] text-[var(--sem-premium)]",
};

/** Durability proxy badge: always worded as a proxy, never as an analyst moat rating. */
export function DurabilityBadge({ durability }: { durability: Durability | null | undefined }) {
  if (!durability) return null;
  const r = durability.rating;
  const tip = `${durability.label}. ${durability.reasons.at(-1) ?? ""}`;
  return (
    <Badge variant="outline" title={tip} aria-label={r ? `Durability ${r}, proxy, ${durability.confidence} confidence` : "Durability: not enough data for a rating"} className={r ? DUR_CLASS[r] : "text-muted-foreground"}>
      Durability: {r ? `${r[0].toUpperCase()}${r.slice(1)}` : "n/a"}
      {r && durability.confidence !== "high" ? ` · ${durability.confidence} conf.` : ""}
    </Badge>
  );
}

export function ActionBadge({ action }: { action: string | null }) {
  if (!action) return <span className="text-muted-foreground text-xs">—</span>;
  const variant = BUYS.has(action) ? "default" : action === "avoid" || action === "book_profits" ? "destructive" : "secondary";
  return <Badge variant={variant}>{ACTION_LABEL[action] ?? action}</Badge>;
}

export function ZoneText({ zone }: { zone: string | null }) {
  return <span>{zone ? ZONE_LABEL[zone] : "—"}</span>;
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-muted-foreground py-6 text-center text-sm">{children}</p>;
}

export function ErrorText({ children }: { children: React.ReactNode }) {
  return (
    <p role="alert" className="text-destructive text-sm">
      {children}
    </p>
  );
}
