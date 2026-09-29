"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { InstrumentHit } from "@/lib/types";

export function SymbolSearch() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<InstrumentHit[]>([]);

  useEffect(() => {
    const term = q.trim();
    if (!term) {
      setHits([]);
      return;
    }
    const t = setTimeout(() => {
      api<InstrumentHit[]>(`/stocks/search?q=${encodeURIComponent(term)}&limit=8`)
        .then(setHits)
        .catch(() => setHits([]));
    }, 200);
    return () => clearTimeout(t);
  }, [q]);

  function go(symbol: string) {
    setQ("");
    setHits([]);
    router.push(`/stocks/${encodeURIComponent(symbol)}`);
  }

  return (
    <form
      role="search"
      className="relative w-full max-w-xs"
      onSubmit={(e) => {
        e.preventDefault();
        if (q.trim()) go(hits[0]?.symbol ?? q.trim().toUpperCase());
      }}
    >
      <input
        aria-label="Search symbol"
        placeholder="Search symbol or name"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
      />
      {hits.length > 0 && (
        <ul className="bg-popover absolute z-20 mt-1 w-full rounded-md border p-1 text-sm shadow-md">
          {hits.map((h) => (
            <li key={h.symbol}>
              <button type="button" onClick={() => go(h.symbol)} className="hover:bg-accent w-full rounded px-2 py-1 text-left">
                <span className="font-medium">{h.symbol}</span>{" "}
                <span className="text-muted-foreground text-xs">{h.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </form>
  );
}
