"use client";

// "Add alert" form: enters buy zone, crosses fair value / top band / invalidation, price above
// or below, results date. Alerts notify in-app and on Telegram; they never place orders.
import { Bell } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { ALERT_TYPES } from "@/lib/alerts";
import type { Alert, AlertType } from "@/lib/types";

const field = "border-input bg-background h-8 rounded-md border px-2 text-sm";

export function AddAlertForm({ symbol, onCreated }: { symbol?: string; onCreated?: (a: Alert) => void }) {
  const [sym, setSym] = useState(symbol ?? "");
  const [type, setType] = useState<AlertType>("enters_buy_zone");
  const [value, setValue] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const needs = ALERT_TYPES.find((t) => t.value === type)?.needs;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    const n = value.trim() === "" ? null : Number(value);
    if (needs === "price" && (n == null || !(n > 0))) return setMsg("Enter a price above 0");
    if (needs === "days" && n != null && !(n > 0)) return setMsg("Days must be above 0");
    try {
      const a = await api<Alert>("/alerts", { method: "POST", body: JSON.stringify({ symbol: sym.trim(), alert_type: type, threshold: needs ? n : null }) });
      setMsg(`Alert set: ${a.condition}`);
      setValue("");
      onCreated?.(a);
    } catch (err) {
      setMsg(err instanceof ApiError ? err.detail : "could not create the alert");
    }
  }
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2" aria-label="Create alert">
      {!symbol && <input aria-label="Alert symbol" placeholder="Symbol" value={sym} onChange={(e) => setSym(e.target.value)} className={`${field} w-28 uppercase`} />}
      <select aria-label="Alert type" value={type} onChange={(e) => setType(e.target.value as AlertType)} className={field}>
        {ALERT_TYPES.map((t) => (
          <option key={t.value} value={t.value}>
            {t.label}
          </option>
        ))}
      </select>
      {needs && <input aria-label={needs === "price" ? "Alert price" : "Days before"} placeholder={needs === "price" ? "₹ price" : "days (optional)"} inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} className={`${field} w-28`} />}
      <Button size="sm" type="submit">
        Create alert
      </Button>
      <span className="text-muted-foreground w-full text-xs">In-app and Telegram notifications only. No orders are ever placed.</span>
      {msg && (
        <span role="status" className="w-full text-xs">
          {msg}
        </span>
      )}
    </form>
  );
}

/** Stock-page button that opens the form in a small popover. */
export function AddAlertButton({ symbol }: { symbol: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <Button size="sm" variant="outline" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Bell /> Add alert
      </Button>
      {open && (
        <div className="bg-popover absolute right-0 z-30 mt-1 w-[22rem] max-w-[90vw] rounded-lg border p-3 shadow-lg">
          <AddAlertForm symbol={symbol} />
        </div>
      )}
    </div>
  );
}
