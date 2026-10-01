"use client";

// Header search (SPEC §3.5): NSE symbol, BSE code, ISIN, company name, or a former name, all
// resolved by the fuzzy /api/stocks/search. A WAI-ARIA combobox: ↑/↓ move through the
// results, Enter opens the highlighted stock (or the first), Esc closes the list and a second
// Esc clears the box; "/" or Ctrl/⌘+K focuses it from anywhere on the page.
import { useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { SearchHit } from "@/lib/types";

const DEBOUNCE_MS = 150;

/** Why this result matched, when it is not plainly the symbol or the name. */
export function matchNote(h: SearchHit): string | null {
  switch (h.match) {
    case "former_name":
      return `formerly ${h.matched}`;
    case "former_symbol":
      return `former symbol ${h.matched}`;
    case "bse_code":
      return `BSE ${h.matched}`;
    case "bse_symbol":
    case "bse_name":
      return `BSE: ${h.matched}`;
    case "isin":
      return `ISIN ${h.matched}`;
    case "fyers_symbol":
      return h.matched;
    case "user":
      return `your alias “${h.matched}”`;
    default:
      return null;
  }
}

function isTypingTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  return (
    el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)
  );
}

export function SymbolSearch() {
  const router = useRouter();
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const seq = useRef(0);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const term = q.trim();
    if (!term) {
      setHits([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    const mine = ++seq.current;
    const t = setTimeout(() => {
      api<SearchHit[]>(`/stocks/search?q=${encodeURIComponent(term)}&limit=8`)
        .then((h) => {
          if (mine !== seq.current) return; // a newer query was typed meanwhile
          setHits(h);
          setActive(h.length ? 0 : -1);
          setLoading(false);
        })
        .catch(() => {
          if (mine !== seq.current) return;
          setHits([]);
          setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const k = e.key.toLowerCase();
      if (
        (k === "/" && !isTypingTarget(e.target)) ||
        (k === "k" && (e.metaKey || e.ctrlKey))
      ) {
        e.preventDefault();
        input.current?.focus();
        input.current?.select();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  function go(h: SearchHit | undefined) {
    if (!h?.symbol) return; // BSE-only: no stock page
    setQ("");
    setHits([]);
    setOpen(false);
    input.current?.blur();
    router.push(`/stocks/${encodeURIComponent(h.symbol)}`);
  }

  function move(delta: number) {
    if (!hits.length) return;
    setOpen(true);
    setActive((a) => (a + delta + hits.length) % hits.length);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        move(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        move(-1);
        break;
      case "Home":
        if (open && hits.length) {
          e.preventDefault();
          setActive(0);
        }
        break;
      case "End":
        if (open && hits.length) {
          e.preventDefault();
          setActive(hits.length - 1);
        }
        break;
      case "Enter": {
        e.preventDefault();
        const typed = q.trim().toUpperCase();
        if (hits.length) go(hits[active] ?? hits[0]);
        else if (typed && !loading)
          router.push(`/stocks/${encodeURIComponent(typed)}`);
        break;
      }
      case "Escape":
        if (open && q) setOpen(false);
        else {
          setQ("");
          setHits([]);
        }
        break;
      case "Tab":
        setOpen(false);
        break;
    }
  }

  const listId = `${id}-list`;
  const showList = open && q.trim().length > 0;
  return (
    <div role="search" className="relative w-full max-w-xs">
      <input
        ref={input}
        role="combobox"
        aria-label="Search stocks"
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={
          showList && active >= 0 ? `${id}-opt-${active}` : undefined
        }
        placeholder="Search symbol, name or BSE code  /"
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={onKeyDown}
        autoComplete="off"
        spellCheck={false}
        className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
      />
      {showList && (
        <ul
          id={listId}
          role="listbox"
          aria-label="Stocks"
          className="bg-popover absolute left-0 z-20 mt-1 max-h-96 w-[min(28rem,calc(100vw-2rem))] overflow-y-auto rounded-md border p-1 text-sm shadow-md sm:right-0 sm:left-auto"
        >
          {hits.map((h, i) => {
            const note = matchNote(h);
            const disabled = !h.symbol;
            return (
              <li
                key={`${h.symbol ?? h.isin}-${i}`}
                id={`${id}-opt-${i}`}
                role="option"
                aria-selected={i === active}
                aria-disabled={disabled || undefined}
                onMouseDown={(e) => e.preventDefault()} // keep focus in the input
                onMouseEnter={() => setActive(i)}
                onClick={() => go(h)}
                className={`flex cursor-pointer flex-col rounded px-2 py-1.5 ${i === active ? "bg-accent" : ""} ${disabled ? "cursor-default opacity-70" : ""}`}
              >
                <span className="flex items-baseline gap-2">
                  <span className="font-medium">{h.symbol ?? h.bse_code}</span>
                  <span className="text-muted-foreground truncate text-xs">
                    {h.name}
                  </span>
                </span>
                <span className="text-muted-foreground flex flex-wrap gap-x-2 text-[11px]">
                  {note && <span>{note}</span>}
                  {h.in_universe_index && <span>Nifty 500</span>}
                  {h.bse_code && h.match !== "bse_code" && h.symbol && (
                    <span>BSE {h.bse_code}</span>
                  )}
                  {disabled && <span>BSE only · no NSE data</span>}
                  {!h.active && <span>inactive</span>}
                  {h.is_index && <span>index</span>}
                </span>
              </li>
            );
          })}
          {!loading && hits.length === 0 && (
            <li
              role="option"
              aria-selected={false}
              aria-disabled
              className="text-muted-foreground px-2 py-1.5"
            >
              No matches. Enter opens “{q.trim().toUpperCase()}”.
            </li>
          )}
          {loading && hits.length === 0 && (
            <li
              role="option"
              aria-selected={false}
              aria-disabled
              className="text-muted-foreground px-2 py-1.5"
            >
              Searching…
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
