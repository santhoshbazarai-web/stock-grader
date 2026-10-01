"use client";

// Live progress of an on-demand pipeline run (SPEC §3.7), streamed from
// GET /api/pipeline/{id}/events (Server-Sent Events). Each step shows success, warning or
// failure with its message; warnings from optional steps end up in the report's data gaps.
import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  Loader2,
  MinusCircle,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { PipelineRun, PipelineStep } from "@/lib/types";

/** Follows a run: the latest state, updated on every server event; closes when it ends. */
export function usePipelineRun(
  initial: PipelineRun | null,
): PipelineRun | null {
  const [run, setRun] = useState<PipelineRun | null>(initial);
  const id = initial?.id ?? null;
  useEffect(() => {
    setRun(initial);
    if (
      id == null ||
      initial?.status === "done" ||
      initial?.status === "failed"
    )
      return;
    const es = new EventSource(`/api/pipeline/${id}/events`);
    es.addEventListener("progress", (e) => {
      const next = JSON.parse((e as MessageEvent<string>).data) as PipelineRun;
      setRun((cur) => (cur && cur.version > next.version ? cur : next));
    });
    es.addEventListener("end", () => es.close());
    return () => es.close();
    // a new run id restarts the stream; `initial` changes with it
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);
  return run;
}

function StepIcon({ status }: { status: PipelineStep["status"] }) {
  const cls = "size-4 shrink-0";
  switch (status) {
    case "ok":
      return (
        <CheckCircle2
          className={cls}
          style={{ color: "var(--viz-good)" }}
          aria-label="done"
        />
      );
    case "warning":
      return (
        <AlertTriangle
          className={cls}
          style={{ color: "var(--viz-s4)" }}
          aria-label="warning"
        />
      );
    case "failed":
      return (
        <XCircle
          className={cls}
          style={{ color: "var(--viz-critical)" }}
          aria-label="failed"
        />
      );
    case "running":
      return <Loader2 className={`${cls} animate-spin`} aria-label="running" />;
    case "skipped":
      return (
        <MinusCircle
          className={`${cls} text-muted-foreground`}
          aria-label="skipped"
        />
      );
    default:
      return (
        <Circle
          className={`${cls} text-muted-foreground opacity-50`}
          aria-label="pending"
        />
      );
  }
}

export function PipelineProgress({
  run: initial,
  onFinished,
  compact = false,
}: {
  run: PipelineRun;
  onFinished?: (run: PipelineRun) => void;
  compact?: boolean;
}) {
  const run = usePipelineRun(initial) ?? initial;
  const notified = useRef(false);
  useEffect(() => {
    if (
      !notified.current &&
      (run.status === "done" || run.status === "failed")
    ) {
      notified.current = true;
      onFinished?.(run);
    }
  }, [run, onFinished]);

  const finished = run.steps.filter(
    (s) => !["pending", "running"].includes(s.status),
  ).length;
  const current = run.steps.find((s) => s.status === "running");
  const title =
    run.status === "queued"
      ? "Waiting for the worker…"
      : run.status === "running"
        ? `${current?.label ?? "Working"}…`
        : run.status === "done"
          ? "Report updated"
          : `Failed: ${run.error ?? "see the steps"}`;
  const [open, setOpen] = useState(!compact);
  return (
    <section
      aria-label={`Pipeline for ${run.symbol}`}
      className="flex flex-col gap-2 text-sm"
    >
      <div className="flex flex-wrap items-center gap-3">
        <p aria-live="polite" className="font-medium">
          {title}
        </p>
        <span className="text-muted-foreground text-xs">
          step {Math.min(finished + (current ? 1 : 0), run.steps.length)} of{" "}
          {run.steps.length}
        </span>
        {compact && (
          <button
            type="button"
            className="text-xs underline"
            onClick={() => setOpen(!open)}
            aria-expanded={open}
          >
            {open ? "Hide steps" : "Show steps"}
          </button>
        )}
      </div>
      <div
        role="progressbar"
        aria-label="Pipeline progress"
        aria-valuemin={0}
        aria-valuemax={run.steps.length}
        aria-valuenow={finished}
        className="bg-muted h-1.5 w-full overflow-hidden rounded-full"
      >
        <div
          className="h-full transition-all"
          style={{
            width: `${(100 * finished) / run.steps.length}%`,
            background:
              run.status === "failed" ? "var(--viz-critical)" : "var(--viz-s1)",
          }}
        />
      </div>
      {open && (
        <ol className="flex flex-col gap-1" aria-label="Pipeline steps">
          {run.steps.map((s) => (
            <li
              key={s.name}
              className="flex items-start gap-2"
              data-status={s.status}
              aria-label={`${s.label}: ${s.status}`}
            >
              <StepIcon status={s.status} />
              <div className="flex min-w-0 flex-col sm:flex-row sm:items-baseline sm:gap-2">
                <span
                  className={
                    s.status === "pending" ? "text-muted-foreground" : ""
                  }
                >
                  {s.label}
                  {s.optional && s.status === "pending" && (
                    <span className="text-muted-foreground text-xs">
                      {" "}
                      (optional)
                    </span>
                  )}
                </span>
                {s.message && (
                  <span className="text-muted-foreground text-xs">
                    {s.message}
                  </span>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
