"use client";

// Editable DCF assumptions + sector model (SPEC §9: saved as overrides, recomputed live).
// Inputs are percentages; empty = use the computed value. Saving POSTs the changed fields
// (a cleared field is sent as null, which removes that override) and hands back the new report.
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { pct } from "@/lib/format";
import type { OverridesResponse, StockReport } from "@/lib/types";

export const FIELDS = [
  { key: "g1", label: "Stage-1 growth (g1)" },
  { key: "ebit_margin", label: "EBIT margin" },
  { key: "wacc", label: "WACC" },
  { key: "g_terminal", label: "Terminal growth" },
  { key: "tax_rate", label: "Tax rate" },
  { key: "capex_pct", label: "Capex / sales" },
  { key: "normalised_roe", label: "Normalised ROE (banks)" },
] as const;

type Key = (typeof FIELDS)[number]["key"];

/** "12.5" (percent text) → 0.125; "" → null; invalid → undefined. */
export function parsePercent(text: string): number | null | undefined {
  const t = text.trim().replace(/%$/, "");
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) ? n / 100 : undefined;
}

function toText(v: number | null | undefined): string {
  return v == null ? "" : String(Math.round(v * 10000) / 100);
}

export function AssumptionsForm({
  report,
  sectors,
  onSaved,
}: {
  report: StockReport;
  sectors: string[];
  onSaved: (r: StockReport | null) => void;
}) {
  const saved = report.overrides;
  const computed: Record<string, number | null> = {
    ...(report.valuation.dcf_inputs ?? {}),
    normalised_roe: report.valuation.justified_pb_inputs?.normalised_roe ?? null,
  };
  const [values, setValues] = useState<Record<Key, string>>(() => initial());
  const [sector, setSector] = useState<string>(saved.sector ?? "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  function initial(): Record<Key, string> {
    return Object.fromEntries(FIELDS.map((f) => [f.key, toText(saved[f.key] as number | null | undefined)])) as Record<Key, string>;
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setValues(initial()), [report]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: Record<string, number | string | null> = {};
    for (const f of FIELDS) {
      const parsed = parsePercent(values[f.key]);
      if (parsed === undefined) {
        setMsg({ ok: false, text: `${f.label}: not a number` });
        return;
      }
      const before = (saved[f.key] as number | null | undefined) ?? null;
      if (parsed !== before) body[f.key] = parsed;
    }
    const sectorValue = sector === "" ? null : sector;
    if (sectorValue !== (saved.sector ?? null)) body.sector = sectorValue;
    if (Object.keys(body).length === 0) {
      setMsg({ ok: true, text: "Nothing changed" });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      const res = await api<OverridesResponse>(`/stocks/${report.symbol}/overrides`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      setMsg({ ok: true, text: `Recomputed. ${res.reasons.join("; ")}` });
      onSaved(res.report);
    } catch (err) {
      setMsg({ ok: false, text: err instanceof ApiError ? err.detail : "save failed" });
    } finally {
      setBusy(false);
    }
  }

  async function resetAll() {
    setBusy(true);
    setMsg(null);
    try {
      await api(`/stocks/${report.symbol}/overrides`, { method: "DELETE" });
      const fresh = await api<StockReport>(`/stocks/${report.symbol}/report?rebuild=true`);
      setMsg({ ok: true, text: "Overrides cleared; recomputed with computed assumptions" });
      onSaved(fresh);
    } catch (err) {
      setMsg({ ok: false, text: err instanceof ApiError ? err.detail : "reset failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3" aria-label="Valuation assumptions">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {FIELDS.map((f) => (
          <label key={f.key} className="flex flex-col gap-1 text-xs">
            <span className="font-medium">
              {f.label} <span className="text-muted-foreground font-normal">(%)</span>
            </span>
            <input
              name={f.key}
              inputMode="decimal"
              value={values[f.key]}
              placeholder={computed[f.key] != null ? `auto ${pct(computed[f.key], 2)}` : "auto"}
              onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
              className="border-input bg-background h-8 rounded-md border px-2 text-sm tabular-nums"
            />
          </label>
        ))}
        <label className="flex flex-col gap-1 text-xs sm:col-span-2">
          <span className="font-medium">Sector model</span>
          <select
            name="sector"
            value={sector}
            onChange={(e) => setSector(e.target.value)}
            className="border-input bg-background h-8 rounded-md border px-2 text-sm"
          >
            <option value="">auto ({report.valuation.sector})</option>
            {sectors.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          {busy ? "Recomputing…" : "Save & recompute"}
        </Button>
        <Button type="button" size="sm" variant="outline" disabled={busy || Object.keys(saved).length === 0} onClick={resetAll}>
          Clear overrides
        </Button>
        {msg && (
          <span role="status" className={`text-xs ${msg.ok ? "text-muted-foreground" : "text-destructive"}`}>
            {msg.text}
          </span>
        )}
      </div>
    </form>
  );
}
