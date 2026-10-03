"use client";

// Watchlists (SPEC §9): several named lists, add by search, CSV import, and per-stock grade,
// zone, discount, next results date and what changed since the previous report. Alerts live on
// /alerts ("Manage alerts"); they never place orders (AGENTS.md rule 7).
import { Trash2 } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ActionBadge, Empty, ErrorText, GradeBadge, Page } from "@/components/common";
import { Tabs } from "@/components/ds";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { inr, signedPct, ZONE_LABEL } from "@/lib/format";
import type { SearchHit, WatchlistInfo, WatchlistItem } from "@/lib/types";

const input = "border-input bg-background h-8 rounded-md border px-2 text-sm";
type ImportResult = { added: string[]; already_there: string[]; invalid: string[] };

function AddBySearch({ onPick }: { onPick: (symbol: string) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  useEffect(() => {
    const term = q.trim();
    if (!term) return setHits([]);
    const t = setTimeout(() => {
      api<SearchHit[]>(`/stocks/search?q=${encodeURIComponent(term)}&limit=6`).then((h) => setHits(h.filter((x) => x.symbol))).catch(() => setHits([]));
    }, 150);
    return () => clearTimeout(t);
  }, [q]);
  return (
    <div className="relative">
      <input aria-label="Search stock to add" placeholder="Search a stock to add…" value={q} onChange={(e) => setQ(e.target.value)} className={`${input} w-64`} />
      {hits.length > 0 && (
        <ul className="bg-popover absolute z-20 mt-1 w-80 rounded-md border shadow-lg" aria-label="Search results">
          {hits.map((h) => (
            <li key={h.symbol}>
              <button
                type="button"
                className="hover:bg-secondary flex w-full items-baseline gap-2 px-3 py-1.5 text-left text-sm"
                onClick={() => {
                  onPick(h.symbol as string);
                  setQ("");
                  setHits([]);
                }}
              >
                <span className="font-medium">{h.symbol}</span>
                <span className="text-muted-foreground truncate text-xs">{h.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Watchlist() {
  const [lists, setLists] = useState<WatchlistInfo[]>([]);
  const [listId, setListId] = useState<number | null>(null);
  const [items, setItems] = useState<WatchlistItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [newList, setNewList] = useState("");
  const [csv, setCsv] = useState("");

  const loadLists = useCallback(async () => {
    const l = await api<WatchlistInfo[]>("/watchlists");
    setLists(l);
    setListId((cur) => (cur != null && l.some((x) => x.id === cur) ? cur : (l[0]?.id ?? null)));
  }, []);
  useEffect(() => {
    loadLists().catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, [loadLists]);

  const loadItems = useCallback(() => {
    if (listId == null) return;
    api<WatchlistItem[]>(`/watchlist?list_id=${listId}`).then(setItems).catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, [listId]);
  useEffect(() => {
    setItems(null);
    loadItems();
  }, [loadItems]);

  async function run(fn: () => Promise<unknown>) {
    setError(null);
    setMsg(null);
    try {
      await fn();
      await loadLists();
      loadItems();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "request failed");
    }
  }
  const current = lists.find((l) => l.id === listId);

  return (
    <Page>
      <h1 className="text-2xl font-semibold tracking-tight">Watchlist</h1>
      {error && <ErrorText>{error}</ErrorText>}
      {msg && (
        <p role="status" className="text-sm">
          {msg}
        </p>
      )}
      <Card className="gap-4">
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div className="min-w-0 flex-1">
              <Tabs tabs={lists.map((l) => ({ id: String(l.id), label: `${l.name} (${l.count})` }))} value={String(listId ?? "")} onChange={(id) => setListId(Number(id))} label="Watchlists" />
            </div>
            <form
              className="flex items-center gap-1"
              aria-label="New list"
              onSubmit={(e) => {
                e.preventDefault();
                if (!newList.trim()) return;
                run(async () => {
                  const l = await api<WatchlistInfo>("/watchlists", { method: "POST", body: JSON.stringify({ name: newList.trim() }) });
                  setNewList("");
                  setListId(l.id);
                });
              }}
            >
              <input aria-label="New list name" placeholder="New list name" value={newList} onChange={(e) => setNewList(e.target.value)} className={`${input} w-36`} />
              <Button size="sm" variant="outline" type="submit">
                Create list
              </Button>
            </form>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <AddBySearch onPick={(symbol) => run(() => api("/watchlist", { method: "POST", body: JSON.stringify({ symbol, list_id: listId }) }))} />
            {current && current.name !== "Default" && (
              <Button size="sm" variant="ghost" onClick={() => run(() => api(`/watchlists/${current.id}`, { method: "DELETE" }))}>
                <Trash2 /> Delete list
              </Button>
            )}
          </div>
          <details className="text-sm">
            <summary className="cursor-pointer">Import from CSV</summary>
            <form
              className="mt-2 flex flex-col gap-2"
              aria-label="Import CSV"
              onSubmit={(e) => {
                e.preventDefault();
                if (!csv.trim()) return setError("Paste CSV text or choose a file first");
                run(async () => {
                  const r = await api<ImportResult>("/watchlist/import", { method: "POST", body: JSON.stringify({ csv, list_id: listId }) });
                  setCsv("");
                  setMsg(`Imported ${r.added.length}, already there ${r.already_there.length}${r.invalid.length ? `, skipped invalid: ${r.invalid.join(", ")}` : ""}`);
                });
              }}
            >
              <input type="file" accept=".csv,text/csv,text/plain" aria-label="CSV file" onChange={async (e) => setCsv((await e.target.files?.[0]?.text()) ?? "")} />
              <textarea aria-label="CSV text" placeholder={"Symbol\nTCS\nINFY"} value={csv} onChange={(e) => setCsv(e.target.value)} rows={4} className="bg-background rounded-md border px-2 py-1.5 font-mono text-xs" />
              <div>
                <Button size="sm" type="submit">
                  Import
                </Button>
                <span className="text-muted-foreground ml-2 text-xs">Symbol in the first column (or a “Symbol” column).</span>
              </div>
            </form>
          </details>
          {items == null ? (
            <p className="text-muted-foreground text-sm">Loading…</p>
          ) : items.length === 0 ? (
            <Empty>This list is empty.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm tabular-nums" aria-label="Watchlist">
                <thead className="text-muted-foreground text-xs">
                  <tr>
                    {["Stock", "Grade", "Zone", "CMP", "Discount", "Next results", "Change since last report", "Action", ""].map((h, i) => (
                      <th key={i} scope="col" className={`px-2 py-1 font-normal whitespace-nowrap ${h === "CMP" || h === "Discount" ? "text-right" : "text-left"}`}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {items.map((w) => (
                    <tr key={w.symbol} className="border-t align-top">
                      <td className="px-2 py-2">
                        <Link href={`/stocks/${w.symbol}`} className="font-medium hover:underline">
                          {w.symbol}
                        </Link>
                        <div className="text-muted-foreground text-xs">{w.name}</div>
                        {w.notes && <div className="text-muted-foreground max-w-48 text-xs italic">{w.notes}</div>}
                      </td>
                      <td className="px-2 py-2">
                        <GradeBadge label={w.grade ? w.grade.replace("_plus", "+") : null} />
                      </td>
                      <td className="px-2 py-2 text-xs">{w.zone ? ZONE_LABEL[w.zone] : "no report yet"}</td>
                      <td className="px-2 py-2 text-right">{inr(w.cmp)}</td>
                      <td className="px-2 py-2 text-right">{w.discount_pct == null ? "-" : signedPct(w.discount_pct / 100, 1)}</td>
                      <td className="px-2 py-2 text-xs whitespace-nowrap">{w.next_results_date ? new Date(w.next_results_date).toLocaleDateString("en-IN", { dateStyle: "medium" }) : "-"}</td>
                      <td className="max-w-56 px-2 py-2 text-xs">{w.since_last_report.length ? w.since_last_report.join("; ") : <span className="text-muted-foreground">no change</span>}</td>
                      <td className="px-2 py-2">
                        <ActionBadge action={w.action} />
                      </td>
                      <td className="px-2 py-2 text-right whitespace-nowrap">
                        <Link href={`/alerts?symbol=${w.symbol}`} className="text-primary mr-2 text-xs hover:underline" aria-label={`Manage alerts for ${w.symbol}`}>
                          Manage alerts{w.active_alerts ? ` (${w.active_alerts})` : ""}
                        </Link>
                        <Button size="sm" variant="ghost" aria-label={`Remove ${w.symbol}`} onClick={() => run(() => api(`/watchlist/${w.symbol}?list_id=${listId}`, { method: "DELETE" }))}>
                          <Trash2 />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </Page>
  );
}
