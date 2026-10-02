"use client";

// "My metrics": six metrics the user picks from the catalogue, saved in the database
// (/api/preferences/my_metrics) so they follow the user across stocks and devices.
import { Pencil } from "lucide-react";
import { useEffect, useState } from "react";

import { MetricTable } from "@/components/ds";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { DEFAULT_MY_METRICS, METRICS, metricsFor } from "@/lib/metrics";
import type { StockReport } from "@/lib/types";

export function MyMetricsCard({ report }: { report: StockReport }) {
  const [ids, setIds] = useState<string[]>(DEFAULT_MY_METRICS);
  const [editing, setEditing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api<{ value: string[] | null }>("/preferences/my_metrics")
      .then((p) => live && Array.isArray(p.value) && p.value.length === 6 && setIds(p.value))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  const catalogue = metricsFor(report);
  const byId = new Map(catalogue.map((m) => [m.id, m]));
  // a saved metric that does not apply to this model (a bank has no P/E) is shown as missing
  const rows = ids.map((id) => {
    const m = byId.get(id);
    if (!m) return { label: METRICS.find((x) => x.id === id)?.label ?? id.replace(/_/g, " "), value: null, reason: "does not apply to this kind of company" };
    const v = m.get(report);
    return { label: m.label, value: v.text, reason: v.reason, tone: v.tone };
  });

  async function save(next: string[]) {
    setIds(next);
    try {
      await api("/preferences/my_metrics", { method: "PUT", body: JSON.stringify({ value: next }) });
      setMsg("Saved");
    } catch {
      setMsg("Could not save");
    }
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">My metrics</h3>
        <Button size="sm" variant="ghost" onClick={() => setEditing((e) => !e)} aria-expanded={editing}>
          <Pencil /> {editing ? "Done" : "Edit"}
        </Button>
      </div>
      {editing ? (
        <div className="flex flex-col gap-1.5" role="group" aria-label="Choose six metrics">
          {ids.map((id, i) => (
            <select
              key={i}
              aria-label={`Metric ${i + 1}`}
              value={id}
              className="bg-background rounded-md border px-2 py-1 text-sm"
              onChange={(e) => save(ids.map((x, j) => (j === i ? e.target.value : x)))}
            >
              {catalogue.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label} ({m.group})
                </option>
              ))}
              {!byId.has(id) && <option value={id}>{id}</option>}
            </select>
          ))}
          {msg && (
            <span role="status" className="text-muted-foreground text-xs">
              {msg}
            </span>
          )}
        </div>
      ) : (
        <MetricTable rows={rows} />
      )}
    </div>
  );
}
