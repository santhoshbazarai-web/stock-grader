"use client";

// Bell in the nav: unread count from triggered alerts (polled every minute), a dropdown of the
// latest notifications, click-through to the stock, and mark-as-read.
import { Bell } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { Notification, NotificationsView } from "@/lib/types";

const POLL_MS = 60_000;
/** Dispatch on window after creating/reading notifications elsewhere, to refresh the bell now. */
export const NOTIFICATIONS_CHANGED = "notifications:changed";

export function timeAgo(iso: string, now = Date.now()): string {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86_400)}d ago`;
}

export function NotificationBell() {
  const router = useRouter();
  const [view, setView] = useState<NotificationsView | null>(null);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  const load = useCallback(() => {
    api<NotificationsView>("/notifications?limit=10").then(setView).catch(() => undefined);
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    window.addEventListener(NOTIFICATIONS_CHANGED, load);
    return () => {
      clearInterval(t);
      window.removeEventListener(NOTIFICATIONS_CHANGED, load);
    };
  }, [load]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  async function openItem(n: Notification) {
    if (!n.read) await api(`/notifications/${n.id}/read`, { method: "POST" }).catch(() => undefined);
    setOpen(false);
    load();
    if (n.symbol) router.push(`/stocks/${n.symbol}`);
  }
  async function readAll() {
    await api("/notifications/read-all", { method: "POST" }).catch(() => undefined);
    load();
  }

  const unread = view?.unread ?? 0;
  return (
    <div ref={box} className="relative">
      <Button
        size="sm"
        variant="ghost"
        aria-label={unread ? `Notifications, ${unread} unread` : "Notifications"}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Bell />
        {unread > 0 && (
          <span
            aria-hidden
            className="absolute top-0.5 right-0.5 min-w-4 rounded-full px-1 text-center text-[10px] leading-4 font-semibold text-white"
            style={{ background: "var(--viz-critical)" }}
          >
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </Button>
      {open && (
        <div role="dialog" aria-label="Notifications" className="bg-popover absolute right-0 z-30 mt-1 w-80 rounded-md border p-2 shadow-md">
          <div className="flex items-center justify-between px-1 pb-1">
            <span className="text-sm font-medium">Notifications</span>
            <span className="flex items-center gap-1">
              <Button size="sm" variant="ghost" disabled={!unread} onClick={readAll}>
                Mark all read
              </Button>
              <Link href="/notifications" className="text-xs underline" onClick={() => setOpen(false)}>
                View all
              </Link>
            </span>
          </div>
          {!view || view.items.length === 0 ? (
            <p className="text-muted-foreground px-1 py-4 text-center text-sm">No notifications yet.</p>
          ) : (
            <ul className="max-h-96 overflow-y-auto">
              {view.items.map((n) => (
                <li key={n.id}>
                  <button type="button" onClick={() => openItem(n)} className="hover:bg-accent flex w-full gap-2 rounded px-2 py-1.5 text-left">
                    <span
                      aria-label={n.read ? "read" : "unread"}
                      className="mt-1.5 size-2 shrink-0 rounded-full"
                      style={{ background: n.read ? "transparent" : "var(--viz-s1)" }}
                    />
                    <span className="min-w-0">
                      <span className={`block text-sm ${n.read ? "" : "font-medium"}`}>{n.title}</span>
                      <span className="text-muted-foreground block text-xs">{n.body}</span>
                      <span className="text-muted-foreground block text-[11px]">
                        {timeAgo(n.created_at)}
                        {n.telegram === "sent" && " · sent to Telegram"}
                        {n.telegram === "failed" && ` · Telegram failed (${n.telegram_error ?? "error"})`}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
