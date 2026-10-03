"use client";

// The glossary (config/glossary.yaml) is fetched once and shared; labels look themselves up by
// glossary key or by term.
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { GlossaryEntry } from "@/lib/types";

export type GlossaryIndex = { byKey: Map<string, GlossaryEntry>; byTerm: Map<string, GlossaryEntry> };

let cache: Promise<GlossaryIndex> | null = null;

function load(): Promise<GlossaryIndex> {
  cache ??= api<GlossaryEntry[]>("/glossary")
    .then((entries) => ({
      byKey: new Map(entries.map((e) => [e.key, e])),
      byTerm: new Map(entries.map((e) => [e.term.toLowerCase(), e])),
    }))
    .catch(() => {
      cache = null; // retry on the next label
      return { byKey: new Map(), byTerm: new Map() };
    });
  return cache;
}

export function useGlossary(): GlossaryIndex | null {
  const [g, setG] = useState<GlossaryIndex | null>(null);
  useEffect(() => {
    let live = true;
    load().then((x) => live && setG(x));
    return () => {
      live = false;
    };
  }, []);
  return g;
}
