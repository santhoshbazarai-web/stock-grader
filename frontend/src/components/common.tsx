"use client";

// Small shared pieces for the app pages: navigation, grade/action badges, empty states.
import { LogOut } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { NotificationBell } from "@/components/notification-bell";
import { SymbolSearch } from "@/components/report/symbol-search";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { ACTION_LABEL, ZONE_LABEL } from "@/lib/format";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/screener", label: "Screener" },
  { href: "/watchlist", label: "Watchlist & alerts" },
  { href: "/backtests", label: "Backtests" },
  { href: "/review", label: "Review" },
  { href: "/settings", label: "Settings" },
];

export function AppNav() {
  const path = usePathname();
  const router = useRouter();
  async function logout() {
    await api("/auth/logout", { method: "POST" }).catch(() => undefined);
    router.replace("/login");
  }
  return (
    <nav className="flex flex-wrap items-center justify-between gap-3 border-b pb-3" aria-label="Main">
      <div className="flex flex-wrap items-center gap-1">
        <Link href="/" className="mr-3 text-sm font-semibold">
          Stock Grader
        </Link>
        {LINKS.map((l) => {
          const active = l.href === "/" ? path === "/" : path.startsWith(l.href);
          return (
            <Link
              key={l.href}
              href={l.href}
              aria-current={active ? "page" : undefined}
              className={`rounded-md px-2.5 py-1.5 text-sm ${active ? "bg-secondary font-medium" : "text-muted-foreground hover:text-foreground"}`}
            >
              {l.label}
            </Link>
          );
        })}
      </div>
      <div className="flex items-center gap-2">
        <SymbolSearch />
        <NotificationBell />
        <Button size="sm" variant="ghost" onClick={logout} aria-label="Sign out">
          <LogOut />
        </Button>
      </div>
    </nav>
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
