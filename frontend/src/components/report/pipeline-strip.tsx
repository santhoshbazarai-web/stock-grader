"use client";

// The pipeline as a slim, colour-coded strip: blue while running, green when done, amber when
// optional steps warned or were skipped, red on failure. Click to expand the steps.
import { useState } from "react";

import type { PipelineRun } from "@/lib/types";

import { PipelineProgress } from "./pipeline-progress";

export function stripTone(run: PipelineRun): { color: string; label: string } {
  if (run.status === "failed") return { color: "var(--sem-premium)", label: "Update failed" };
  if (run.status === "queued" || run.status === "running") return { color: "var(--brand)", label: "Updating data…" };
  const warned = run.steps.some((s) => s.status === "warning" || s.status === "skipped");
  return warned ? { color: "var(--sem-fair)", label: "Updated with notes" } : { color: "var(--sem-discount)", label: "Up to date" };
}

export function PipelineStrip({
  run,
  onFinished,
  startOpen = false,
}: {
  run: PipelineRun;
  onFinished?: (run: PipelineRun) => void;
  startOpen?: boolean;
}) {
  const [live, setLive] = useState(run);
  const [open, setOpen] = useState(startOpen);
  const tone = stripTone(live);
  return (
    <section aria-label="Data update" className="rounded-lg border-l-4 bg-card text-sm shadow-[var(--shadow-card)]" style={{ borderLeftColor: tone.color }}>
      <button type="button" className="flex w-full items-center justify-between gap-3 px-4 py-2 text-left" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="flex items-center gap-2 font-medium">
          <span aria-hidden className="size-2 rounded-full" style={{ background: tone.color }} />
          {tone.label}
        </span>
        <span className="text-muted-foreground text-xs">{open ? "Hide steps" : "Show steps"}</span>
      </button>
      {/* always mounted so the run is followed and onFinished fires; hidden when collapsed */}
      <div className={open ? "px-4 pb-3" : "hidden"}>
        <PipelineProgress key={run.id} run={run} onFinished={onFinished} onChange={setLive} compact={false} />
      </div>
    </section>
  );
}
