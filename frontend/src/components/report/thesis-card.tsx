"use client";

// AI thesis (SPEC §8a): five short parts written by Gemini (or a local Ollama) from the numbers
// on this page only. The backend rejects drafts that cite a number the report does not state,
// so the card shows text only when it passed; otherwise it says "Thesis unavailable" and why.
import { Loader2, RefreshCw, Sparkles } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import type { Thesis } from "@/lib/types";

function when(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// `version` changes whenever the report is rebuilt, so the card re-reads the thesis for the new numbers.
export function ThesisCard({
  symbol,
  version,
}: {
  symbol: string;
  version: string;
}) {
  const [thesis, setThesis] = useState<Thesis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api<Thesis>(`/stocks/${encodeURIComponent(symbol)}/thesis`)
      .then((t) => live && setThesis(t))
      .catch(
        (e) => live && setError(e instanceof ApiError ? e.detail : "failed"),
      );
    return () => {
      live = false;
    };
  }, [symbol, version]);

  const write = useCallback(
    async (force: boolean) => {
      setBusy(true);
      setError(null);
      try {
        setThesis(
          await api<Thesis>(
            `/stocks/${encodeURIComponent(symbol)}/thesis?force=${force}`,
            { method: "POST" },
          ),
        );
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : "failed");
      } finally {
        setBusy(false);
      }
    },
    [symbol],
  );

  if (!thesis)
    return (
      <p className="text-muted-foreground text-sm">
        {error ? `Thesis unavailable: ${error}` : "Loading thesis…"}
      </p>
    );

  if (thesis.status === "disabled")
    return (
      <p className="text-muted-foreground text-sm" data-testid="thesis-off">
        {thesis.reasons[0]}
        {thesis.reasons[0]?.includes("GEMINI_API_KEY") ? " (then restart the API). Only this page's numbers are sent." : ""}
      </p>
    );

  const action = (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        size="sm"
        variant="outline"
        disabled={busy}
        onClick={() => write(thesis.status === "ok")}
      >
        {busy ? (
          <Loader2 className="animate-spin" aria-hidden />
        ) : thesis.status === "ok" ? (
          <RefreshCw aria-hidden />
        ) : (
          <Sparkles aria-hidden />
        )}
        {thesis.status === "ok"
          ? "Regenerate"
          : thesis.status === "missing"
            ? "Write thesis"
            : "Try again"}
      </Button>
      {busy && (
        <span className="text-muted-foreground text-xs" role="status">
          Writing the thesis; this can take a few seconds…
        </span>
      )}
      {error && !busy && (
        <span className="text-xs" role="alert">
          {error}
        </span>
      )}
    </div>
  );

  return (
    <div className="flex flex-col gap-3 text-sm">
      {thesis.status === "ok" && thesis.text && (
        <>
          <p className="max-w-prose leading-relaxed whitespace-pre-line" data-testid="thesis-text">
            {thesis.text}
          </p>
          <p className="text-muted-foreground text-xs">
            Written by {thesis.model}
            {thesis.generated_at ? ` (${when(thesis.generated_at)})` : ""}.
            Every number in it was checked against the report; the wording was
            not.
          </p>
        </>
      )}
      {thesis.status === "missing" && (
        <p className="text-muted-foreground">
          No thesis for these numbers yet.
        </p>
      )}
      {thesis.status === "rejected" && (
        <div className="flex flex-col gap-1">
          <p>
            Thesis unavailable: no draft from {thesis.model} passed the facts
            check in {thesis.attempts} attempt(s).
          </p>
          <ul className="text-muted-foreground list-disc pl-5 text-xs">
            {thesis.problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}
      {thesis.status === "failed" && (
        <p>
          Thesis unavailable: the model could not be reached
          {thesis.problems[0] ? ` (${thesis.problems[0]})` : ""}.
        </p>
      )}
      {action}
    </div>
  );
}
