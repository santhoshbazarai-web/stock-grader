"use client";

// Screening Ideas (SPEC §9): preset screens from config/screen_presets.yaml. "Run scan" opens
// the screener with the preset's filters in the URL, where they stay editable.
import Link from "next/link";
import { useEffect, useState } from "react";

import { Page } from "@/components/common";
import { EmptyState, Skeleton } from "@/components/ds";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { paramsFromFilters, type Idea } from "@/lib/screens";

/** Simple abstract line icons, one per preset `icon` key. */
const ICONS: Record<string, React.ReactNode> = {
  trend: <path d="M3 17l6-6 4 4 8-9M15 6h6v6" />,
  coins: <><circle cx="9" cy="9" r="5" /><path d="M14 13a5 5 0 1 0 5-5" /></>,
  rocket: <path d="M5 19c0-6 4-12 14-14 0 10-6 14-12 14M5 19l-1 2M9 15l3 3" />,
  shield: <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" />,
  rebound: <path d="M3 6l6 8 4-4 8 8M21 12v6h-6" />,
  cycle: <path d="M4 12a8 8 0 0 1 14-5M20 12a8 8 0 0 1-14 5M18 3v4h-4M6 21v-4h4" />,
  seedling: <path d="M12 21v-9M12 12C12 7 8 5 4 5c0 4 3 7 8 7zM12 14c0-4 3-6 8-6 0 4-3 6-8 6z" />,
  scale: <path d="M12 4v16M6 20h12M5 8h14M5 8l-3 7h6zM19 8l-3 7h6z" />,
  gem: <path d="M6 4h12l4 6-10 11L2 10zM2 10h20M9 4l3 17M15 4l-3 17" />,
};

function Icon({ name }: { name: string }) {
  return (
    <svg aria-hidden viewBox="0 0 24 24" className="size-6" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      {ICONS[name] ?? ICONS.trend}
    </svg>
  );
}

export function ScreeningIdeas() {
  const [ideas, setIdeas] = useState<Idea[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<Idea[]>("/screener/ideas").then(setIdeas).catch((e) => setError(e instanceof ApiError ? e.detail : "ideas unavailable"));
  }, []);

  return (
    <Page>
      <header>
        <h1 className="text-2xl font-semibold">Screening Ideas</h1>
        <p className="text-muted-foreground text-sm">Starting points built on our own fields. Run one, then tweak the filters in the screener.</p>
      </header>
      {error ? (
        <EmptyState title="Could not load ideas">{error}</EmptyState>
      ) : !ideas ? (
        <Skeleton className="h-40 w-full" />
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {ideas.map((i) => (
            <li key={i.id}>
              <Card className="h-full">
                <CardContent className="flex h-full flex-col gap-3 p-4">
                  <span className="bg-secondary text-primary grid size-10 place-items-center rounded-lg">
                    <Icon name={i.icon} />
                  </span>
                  <h2 className="font-semibold">{i.name}</h2>
                  <p className="text-muted-foreground flex-1 text-sm">{i.description}</p>
                  <Link
                    href={`/screener?${paramsFromFilters(i.filters, { sort: i.sort, order: i.order }).toString()}`}
                    className="bg-primary text-primary-foreground w-fit rounded-md px-3 py-1.5 text-sm font-medium"
                    aria-label={`Run scan: ${i.name}`}
                  >
                    Run scan
                  </Link>
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </Page>
  );
}
