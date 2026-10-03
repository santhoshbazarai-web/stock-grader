"use client";

// A metric label that links to its glossary entry and shows the definition on hover / focus.
// Looks the entry up by glossary key (k) or by the label text; an unknown label is plain text.
import Link from "next/link";

import { useGlossary } from "@/lib/glossary";

export function Term({ label, k }: { label: string; k?: string }) {
  const g = useGlossary();
  const entry = g ? (k ? g.byKey.get(k) : g.byTerm.get(label.toLowerCase())) : undefined;
  if (!entry) return <>{label}</>;
  return (
    <Link href={`/glossary#${entry.key}`} title={`${entry.definition} Why it matters: ${entry.why}`} className="decoration-muted-foreground/60 underline decoration-dotted underline-offset-2">
      {label}
    </Link>
  );
}
