"use client";

// Glossary (SPEC §9): every metric, score, zone and level, A-Z, with search. Entries come from
// config/glossary.yaml; /glossary#key jumps to one (labels across the app link here).
import { useEffect, useMemo, useState } from "react";

import { Page } from "@/components/common";
import { EmptyState, Skeleton } from "@/components/ds";
import { api, ApiError } from "@/lib/api";
import type { GlossaryEntry } from "@/lib/types";

export function Glossary() {
  const [entries, setEntries] = useState<GlossaryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  useEffect(() => {
    api<GlossaryEntry[]>("/glossary").then(setEntries).catch((e) => setError(e instanceof ApiError ? e.detail : "glossary unavailable"));
  }, []);
  useEffect(() => {
    if (entries && window.location.hash) document.getElementById(window.location.hash.slice(1))?.scrollIntoView();
  }, [entries]);

  const shown = useMemo(() => {
    const t = q.trim().toLowerCase();
    return (entries ?? []).filter((e) => !t || `${e.term} ${e.key} ${e.group} ${e.definition}`.toLowerCase().includes(t));
  }, [entries, q]);
  const letters = useMemo(() => [...new Set(shown.map((e) => e.term[0].toUpperCase()))].sort(), [shown]);
  const all = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("");

  return (
    <Page>
      <header>
        <h1 className="text-2xl font-semibold">Glossary</h1>
        <p className="text-muted-foreground text-sm">Plain-English definitions, formulas and why each number matters.</p>
      </header>
      <input aria-label="Search glossary" placeholder="Search terms (ROE, margin of safety…)" value={q} onChange={(e) => setQ(e.target.value)} className="bg-background w-full max-w-md rounded-md border px-3 py-2 text-sm" />
      <nav aria-label="A to Z index" className="flex flex-wrap gap-1 text-sm">
        {all.map((l) =>
          letters.includes(l) ? (
            <a key={l} href={`#letter-${l}`} className="bg-secondary rounded px-2 py-0.5 font-medium">
              {l}
            </a>
          ) : (
            <span key={l} className="text-muted-foreground/50 px-2 py-0.5">
              {l}
            </span>
          ),
        )}
      </nav>
      {error ? (
        <EmptyState title="Glossary unavailable">{error}</EmptyState>
      ) : !entries ? (
        <Skeleton className="h-40 w-full" />
      ) : shown.length === 0 ? (
        <EmptyState title="No matching terms">Try another word.</EmptyState>
      ) : (
        <div className="flex flex-col gap-6">
          {letters.map((l) => (
            <section key={l} id={`letter-${l}`} aria-label={`Terms starting with ${l}`}>
              <h2 className="text-muted-foreground mb-2 border-b text-lg font-semibold">{l}</h2>
              <dl className="flex flex-col gap-4">
                {shown
                  .filter((e) => e.term[0].toUpperCase() === l)
                  .map((e) => (
                    <div key={e.key} id={e.key} className="target:bg-secondary scroll-mt-20 rounded-lg p-2">
                      <dt className="font-semibold">
                        {e.term} <span className="text-muted-foreground text-xs font-normal">· {e.group}</span>
                      </dt>
                      <dd className="text-sm">
                        <p>{e.definition}</p>
                        {e.formula && (
                          <p className="text-muted-foreground mt-1">
                            <span className="font-medium">Formula:</span> <code>{e.formula}</code>
                          </p>
                        )}
                        <p className="text-muted-foreground mt-1">
                          <span className="font-medium">Why it matters:</span> {e.why}
                        </p>
                      </dd>
                    </div>
                  ))}
              </dl>
            </section>
          ))}
        </div>
      )}
    </Page>
  );
}
