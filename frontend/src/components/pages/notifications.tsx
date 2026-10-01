"use client";

// Notification centre (P23): every in-app notification (price alerts, results changes,
// broker-token reminders, tests), filtered by read state, type and stock, paged, with each
// one's Telegram delivery (and a resend for a failed one), plus the read-only Telegram bot's
// status. Notifications come from one place (app.alerts.notify), which also sends to Telegram.
import { BellRing, CheckCheck, Send } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Page } from "@/components/common";
import { NOTIFICATIONS_CHANGED, timeAgo } from "@/components/notification-bell";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import type {
  Notification,
  NotificationsView,
  TelegramStatus,
} from "@/lib/types";

const GROUPS: { key: string; label: string; kinds: string[] }[] = [
  {
    key: "alerts",
    label: "Price alerts",
    kinds: [
      "enters_buy_zone",
      "crosses_fv",
      "crosses_top_band",
      "crosses_invalidation",
    ],
  },
  { key: "results", label: "Results changes", kinds: ["results"] },
  { key: "broker", label: "Broker tokens", kinds: ["broker_token"] },
  { key: "test", label: "Tests", kinds: ["test"] },
];
const PAGE = 30;

function when(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function Delivery({
  n,
  onResent,
}: {
  n: Notification;
  onResent: (n: Notification) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function resend() {
    setBusy(true);
    setErr(null);
    try {
      onResent(
        await api<Notification>(`/notifications/${n.id}/resend`, {
          method: "POST",
        }),
      );
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : "failed");
    } finally {
      setBusy(false);
    }
  }
  if (n.telegram === "sent") return <span>sent to Telegram</span>;
  if (n.telegram === "disabled") return <span>in-app only</span>;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <span style={{ color: "var(--viz-critical)" }}>
        Telegram failed ({n.telegram_error ?? "error"})
      </span>
      <Button
        size="sm"
        variant="outline"
        className="h-6 px-2 text-xs"
        disabled={busy}
        onClick={resend}
      >
        <Send className="size-3" /> Resend
      </Button>
      {err && <span className="text-destructive">{err}</span>}
    </span>
  );
}

function Row({
  n,
  onToggle,
  onResent,
}: {
  n: Notification;
  onToggle: (n: Notification) => void;
  onResent: (n: Notification) => void;
}) {
  return (
    <li
      className="flex gap-3 py-3"
      aria-label={`${n.read ? "Read" : "Unread"}: ${n.title}`}
    >
      <span
        aria-hidden
        className="mt-1.5 size-2 shrink-0 rounded-full"
        style={{
          background: n.read ? "transparent" : "var(--viz-s1)",
          border: "1px solid var(--viz-s1)",
        }}
      />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <p className={`text-sm break-words ${n.read ? "" : "font-semibold"}`}>
          {n.title}
        </p>
        <p className="text-muted-foreground text-sm break-words">{n.body}</p>
        <p className="text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          <time dateTime={n.created_at} title={when(n.created_at)}>
            {timeAgo(n.created_at)}
          </time>
          <span aria-hidden>·</span>
          <span>{n.kind.replaceAll("_", " ")}</span>
          {n.symbol && (
            <>
              <span aria-hidden>·</span>
              <Link href={`/stocks/${n.symbol}`} className="underline">
                {n.symbol}
              </Link>
            </>
          )}
          <span aria-hidden>·</span>
          <Delivery n={n} onResent={onResent} />
        </p>
      </div>
      <Button
        size="sm"
        variant="ghost"
        className="shrink-0 self-start text-xs"
        onClick={() => onToggle(n)}
      >
        {n.read ? "Mark unread" : "Mark read"}
      </Button>
    </li>
  );
}

function BotCard() {
  const [st, setSt] = useState<TelegramStatus | null>(null);
  useEffect(() => {
    api<TelegramStatus>("/notifications/telegram")
      .then(setSt)
      .catch(() => setSt(null));
  }, []);
  if (!st) return null;
  const state = !st.configured
    ? "not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env, then restart the worker"
    : !st.bot_enabled
      ? "delivery on, bot off (jobs.yaml → telegram_bot.enabled)"
      : st.state === "polling"
        ? `bot running · last poll ${st.last_poll_at ? timeAgo(st.last_poll_at) : "—"}`
        : st.state
          ? `bot ${st.state}`
          : "delivery on · bot not seen yet (is the worker running?)";
  return (
    <Card className="gap-3" role="region" aria-label="Telegram">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <BellRing className="size-4" aria-hidden /> Telegram
        </CardTitle>
        <CardDescription>
          Every notification below is also sent to Telegram when configured. The
          bot is read-only and answers only your chat (TELEGRAM_CHAT_ID), by
          long polling: nothing listens for inbound connections.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <p role="status">{state}</p>
        {st.last_error && (
          <p className="text-destructive text-xs">
            Last error
            {st.last_error_at ? ` (${timeAgo(st.last_error_at)})` : ""}:{" "}
            {st.last_error}
          </p>
        )}
        {st.ignored_messages > 0 && (
          <p className="text-muted-foreground text-xs">
            {st.ignored_messages} message(s) from other chats ignored (not on
            the allow-list).
          </p>
        )}
        <ul className="text-muted-foreground font-mono text-xs leading-relaxed">
          <li>/grade SYMBOL — grade, zone, FV and buy zone</li>
          <li>/buyzone — stocks in or near their buy zone</li>
          <li>/status — data freshness, jobs, broker tokens</li>
        </ul>
      </CardContent>
    </Card>
  );
}

export function NotificationCentre() {
  const [view, setView] = useState<NotificationsView | null>(null);
  const [items, setItems] = useState<Notification[]>([]);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [group, setGroup] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("");
  const [error, setError] = useState<string | null>(null);

  const query = useCallback(
    (before?: number | null) => {
      const p = new URLSearchParams({ limit: String(PAGE) });
      if (unreadOnly) p.set("unread_only", "true");
      for (const k of GROUPS.find((g) => g.key === group)?.kinds ?? [])
        p.append("kind", k);
      if (symbol.trim()) p.set("symbol", symbol.trim().toUpperCase());
      if (before) p.set("before_id", String(before));
      return `/notifications?${p}`;
    },
    [unreadOnly, group, symbol],
  );

  const load = useCallback(() => {
    api<NotificationsView>(query())
      .then((v) => {
        setView(v);
        setItems(v.items);
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, [query]);
  useEffect(load, [load]);

  async function more() {
    if (!view?.next_before_id) return;
    const v = await api<NotificationsView>(query(view.next_before_id));
    setView(v);
    setItems((cur) => [...cur, ...v.items]);
  }
  const changed = () => window.dispatchEvent(new Event(NOTIFICATIONS_CHANGED));
  async function toggle(n: Notification) {
    await api(`/notifications/${n.id}/${n.read ? "unread" : "read"}`, {
      method: "POST",
    }).catch(() => undefined);
    setItems((cur) =>
      cur.map((x) => (x.id === n.id ? { ...x, read: !n.read } : x)),
    );
    setView((v) => v && { ...v, unread: v.unread + (n.read ? 1 : -1) });
    changed();
  }
  async function readAll() {
    await api("/notifications/read-all", { method: "POST" }).catch(
      () => undefined,
    );
    changed();
    load();
  }
  const count = (kinds: string[]) =>
    kinds.reduce((a, k) => a + (view?.kinds?.[k] ?? 0), 0);
  const chip = (active: boolean) =>
    `rounded-full border px-3 py-1 text-xs ${active ? "bg-secondary border-transparent font-medium" : "text-muted-foreground"}`;

  return (
    <Page>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Notifications</h1>
          <p className="text-muted-foreground text-sm">
            {view ? `${view.unread} unread` : "Loading…"} · price alerts,
            results changes and broker reminders
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          disabled={!view?.unread}
          onClick={readAll}
        >
          <CheckCheck className="size-4" /> Mark all read
        </Button>
      </div>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_20rem]">
        <Card className="gap-3">
          <CardContent className="flex flex-col gap-3">
            <div
              className="flex flex-wrap items-center gap-2"
              role="group"
              aria-label="Filters"
            >
              <button
                type="button"
                className={chip(!unreadOnly)}
                aria-pressed={!unreadOnly}
                onClick={() => setUnreadOnly(false)}
              >
                All
              </button>
              <button
                type="button"
                className={chip(unreadOnly)}
                aria-pressed={unreadOnly}
                onClick={() => setUnreadOnly(true)}
              >
                Unread
              </button>
              <span className="bg-border mx-1 h-4 w-px" aria-hidden />
              {GROUPS.map((g) => (
                <button
                  key={g.key}
                  type="button"
                  className={chip(group === g.key)}
                  aria-pressed={group === g.key}
                  onClick={() =>
                    setGroup((cur) => (cur === g.key ? null : g.key))
                  }
                >
                  {g.label}{" "}
                  <span className="tabular-nums">{count(g.kinds)}</span>
                </button>
              ))}
              <input
                aria-label="Filter by symbol"
                placeholder="Symbol"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                className="border-input bg-background h-7 w-28 rounded-md border px-2 text-xs"
              />
            </div>
            {error && <p className="text-destructive text-sm">{error}</p>}
            {view && items.length === 0 ? (
              <p className="text-muted-foreground py-8 text-center text-sm">
                No notifications match.
              </p>
            ) : (
              <ul
                className="divide-border divide-y"
                aria-label="Notification list"
              >
                {items.map((n) => (
                  <Row
                    key={n.id}
                    n={n}
                    onToggle={toggle}
                    onResent={(r) =>
                      setItems((cur) => cur.map((x) => (x.id === r.id ? r : x)))
                    }
                  />
                ))}
              </ul>
            )}
            {view?.next_before_id && (
              <Button
                size="sm"
                variant="ghost"
                className="self-center"
                onClick={more}
              >
                Load older
              </Button>
            )}
          </CardContent>
        </Card>
        <div className="flex flex-col gap-6">
          <BotCard />
        </div>
      </div>
    </Page>
  );
}
