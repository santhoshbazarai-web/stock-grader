"use client";

// Research notes (markdown, timestamped). With ``symbol`` it is the stock page's Notes tab;
// without, the global "My Notes" list with a search box and a symbol field for new notes.
import { Trash2 } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { EmptyState } from "@/components/ds";
import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import type { Note } from "@/lib/types";

const when = (iso: string) => new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });

export function NotesPanel({ symbol }: { symbol?: string }) {
  const [notes, setNotes] = useState<Note[] | null>(null);
  const [q, setQ] = useState("");
  const [sym, setSym] = useState("");
  const [body, setBody] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    const p = new URLSearchParams();
    if (symbol) p.set("symbol", symbol);
    if (q.trim()) p.set("q", q.trim());
    api<Note[]>(`/notes?${p}`).then(setNotes).catch((e) => setError(e instanceof ApiError ? e.detail : "notes unavailable"));
  }, [symbol, q]);
  useEffect(load, [load]);

  async function run(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "request failed");
    }
  }
  const target = (symbol ?? sym).trim();

  return (
    <section className="flex flex-col gap-4" aria-label="Notes">
      <form
        className="flex flex-col gap-2"
        aria-label="Add note"
        onSubmit={(e) => {
          e.preventDefault();
          if (!target || !body.trim()) return setError(symbol ? "Write a note first" : "Enter a symbol and a note");
          run(async () => {
            await api("/notes", { method: "POST", body: JSON.stringify({ symbol: target, body }) });
            setBody("");
          });
        }}
      >
        {!symbol && <input aria-label="Note symbol" placeholder="Symbol, e.g. TCS" value={sym} onChange={(e) => setSym(e.target.value)} className="bg-background w-40 rounded-md border px-2 py-1.5 text-sm uppercase" />}
        <textarea aria-label="Note" placeholder="Write a note (markdown: **bold**, - lists, # headings)" value={body} onChange={(e) => setBody(e.target.value)} rows={3} className="bg-background rounded-md border px-2 py-1.5 text-sm" />
        <div>
          <Button size="sm" type="submit">
            Add note
          </Button>
        </div>
      </form>
      {!symbol && <input aria-label="Search notes" placeholder="Search notes" value={q} onChange={(e) => setQ(e.target.value)} className="bg-background w-full max-w-sm rounded-md border px-2 py-1.5 text-sm" />}
      {error && <p role="alert" className="text-destructive text-sm">{error}</p>}
      {notes == null ? null : notes.length === 0 ? (
        <EmptyState title="No notes yet">Jot down why you like (or avoid) a stock.</EmptyState>
      ) : (
        <ul className="flex flex-col gap-3">
          {notes.map((n) => (
            <li key={n.id} className="bg-card rounded-lg border p-3">
              <div className="text-muted-foreground mb-2 flex items-center justify-between gap-2 text-xs">
                <span>
                  {!symbol && (
                    <Link href={`/stocks/${n.symbol}#notes`} className="text-foreground font-medium hover:underline">
                      {n.symbol}
                    </Link>
                  )}{" "}
                  {when(n.created_at)}
                  {n.updated_at !== n.created_at && ` · edited ${when(n.updated_at)}`}
                </span>
                <span className="flex gap-1">
                  <Button size="sm" variant="ghost" onClick={() => setEditing(editing === n.id ? null : n.id)}>
                    {editing === n.id ? "Cancel" : "Edit"}
                  </Button>
                  <Button size="sm" variant="ghost" aria-label={`Delete note ${n.id}`} onClick={() => run(() => api(`/notes/${n.id}`, { method: "DELETE" }))}>
                    <Trash2 />
                  </Button>
                </span>
              </div>
              {editing === n.id ? (
                <form
                  className="flex flex-col gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const v = new FormData(e.currentTarget).get("body") as string;
                    run(async () => {
                      await api(`/notes/${n.id}`, { method: "PUT", body: JSON.stringify({ body: v }) });
                      setEditing(null);
                    });
                  }}
                >
                  <textarea name="body" aria-label="Edit note" defaultValue={n.body} rows={4} className="bg-background rounded-md border px-2 py-1.5 text-sm" />
                  <div>
                    <Button size="sm" type="submit">
                      Save
                    </Button>
                  </div>
                </form>
              ) : (
                <Markdown source={n.body} />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
