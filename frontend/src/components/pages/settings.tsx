"use client";

// Settings (SPEC §9): connect brokers, edit config with a YAML validator, manage uploads.
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { Empty, ErrorText, Page } from "@/components/common";
import { NOTIFICATIONS_CHANGED } from "@/components/notification-bell";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import type {
  BrokerStatus,
  ConfigFileName,
  ConfigView,
  Notification,
  NotificationsView,
  UploadedDataset,
} from "@/lib/types";

import { BrokerList } from "./brokers";
import { Filings } from "./filings";

const CALLBACK_REASON: Record<string, string> = {
  invalid_state: "the login link expired or was not issued by this app — try Connect again",
  login_declined: "the login was cancelled at the broker",
  exchange_failed: "the broker rejected the one-time code — try again",
};

export function callbackMessage(params: URLSearchParams): { ok: boolean; text: string } | null {
  const broker = params.get("broker");
  const status = params.get("status");
  if (!broker || !status) return null;
  const name = broker === "kite" ? "Zerodha Kite" : "Fyers";
  if (status === "connected") return { ok: true, text: `${name} connected.` };
  const reason = CALLBACK_REASON[params.get("reason") ?? ""] ?? "unknown error";
  return { ok: false, text: `${name} not connected: ${reason}.` };
}

// ───────────────────────── brokers ─────────────────────────

function Brokers() {
  const params = useSearchParams();
  const [statuses, setStatuses] = useState<BrokerStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const banner = callbackMessage(new URLSearchParams(params.toString()));
  useEffect(() => {
    api<BrokerStatus[]>("/brokers/status")
      .then(setStatuses)
      .catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, []);
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle className="text-base">Brokers</CardTitle>
        <CardDescription>
          Read-only market data. Tokens are stored encrypted and expire daily (Kite at 06:00 IST); reconnect each
          morning.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {banner && (
          <p role="status" className={`rounded-md border px-3 py-2 text-sm ${banner.ok ? "" : "text-destructive"}`}>
            {banner.text}
          </p>
        )}
        {error && <ErrorText>{error}</ErrorText>}
        {statuses ? <BrokerList statuses={statuses} connect /> : !error && <p className="text-muted-foreground text-sm">Loading…</p>}
      </CardContent>
    </Card>
  );
}

// ───────────────────────── config editor ─────────────────────────

type Check = { state: "idle" | "checking" | "valid" | "invalid"; detail?: string };

export function ConfigEditor() {
  const [view, setView] = useState<ConfigView | null>(null);
  const [name, setName] = useState<ConfigFileName>("scoring");
  const [text, setText] = useState("");
  const [check, setCheck] = useState<Check>({ state: "idle" });
  const [saveMsg, setSaveMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stored = view?.files.find((f) => f.name === name)?.yaml ?? "";
  const dirty = view != null && text !== stored;

  const load = useCallback(async (keep?: ConfigFileName) => {
    try {
      const v = await api<ConfigView>("/config");
      setView(v);
      setText(v.files.find((f) => f.name === (keep ?? "scoring"))?.yaml ?? "");
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "failed to load config");
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const validate = useCallback(
    (yaml: string) => {
      setCheck({ state: "checking" });
      api(`/config?dry_run=true`, { method: "PUT", body: JSON.stringify({ name, yaml }) })
        .then(() => setCheck({ state: "valid" }))
        .catch((e) => setCheck({ state: "invalid", detail: e instanceof ApiError ? e.detail : "validation failed" }));
    },
    [name],
  );

  function edit(v: string) {
    setText(v);
    setSaveMsg(null);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => validate(v), 600);
  }

  function switchTo(n: ConfigFileName) {
    if (dirty && !window.confirm(`Discard unsaved changes to ${name}.yaml?`)) return;
    setName(n);
    setText(view?.files.find((f) => f.name === n)?.yaml ?? "");
    setCheck({ state: "idle" });
    setSaveMsg(null);
  }

  async function save() {
    try {
      const r = await api<{ note: string }>("/config", { method: "PUT", body: JSON.stringify({ name, yaml: text }) });
      setSaveMsg({ ok: true, text: `Saved ${name}.yaml — ${r.note}` });
      await load(name);
      setCheck({ state: "idle" });
    } catch (e) {
      setSaveMsg({ ok: false, text: e instanceof ApiError ? e.detail : "save failed" });
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Tab") return;
    e.preventDefault(); // YAML is indented with spaces, never tabs
    const el = e.currentTarget;
    const { selectionStart: a, selectionEnd: b } = el;
    const next = `${text.slice(0, a)}  ${text.slice(b)}`;
    edit(next);
    requestAnimationFrame(() => el.setSelectionRange(a + 2, a + 2));
  }

  const lines = text.split("\n").length;
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle className="text-base">Config</CardTitle>
        <CardDescription>
          Every threshold and weight lives here. Edits are validated as you type, exactly as at startup; only a
          fully valid file can be saved. The API applies it at once; restart the worker to pick it up.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {error && <ErrorText>{error}</ErrorText>}
        <div role="tablist" aria-label="Config files" className="flex flex-wrap gap-1">
          {(view?.files ?? []).map((f) => (
            <button
              key={f.name}
              role="tab"
              aria-selected={f.name === name}
              onClick={() => switchTo(f.name)}
              className={`rounded-md border px-2.5 py-1 font-mono text-xs ${f.name === name ? "bg-primary text-primary-foreground border-primary" : "hover:bg-accent"}`}
            >
              {f.name}.yaml
            </button>
          ))}
        </div>
        <div className="flex overflow-hidden rounded-md border font-mono text-xs leading-5">
          <pre aria-hidden className="bg-muted/60 text-muted-foreground select-none px-2 py-2 text-right">
            {Array.from({ length: lines }, (_, i) => i + 1).join("\n")}
          </pre>
          <textarea
            aria-label={`${name}.yaml`}
            spellCheck={false}
            value={text}
            onChange={(e) => edit(e.target.value)}
            onKeyDown={onKeyDown}
            rows={Math.max(lines + 1, 12)}
            className="bg-background flex-1 resize-none overflow-x-auto overflow-y-hidden px-3 py-2 whitespace-pre outline-none"
            wrap="off"
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" onClick={save} disabled={!dirty || check.state !== "valid"}>
            Save {name}.yaml
          </Button>
          <Button size="sm" variant="outline" disabled={!dirty} onClick={() => { setText(stored); setCheck({ state: "idle" }); setSaveMsg(null); }}>
            Revert
          </Button>
          <span role="status" className="text-xs">
            {check.state === "checking" && <span className="text-muted-foreground">Validating…</span>}
            {check.state === "valid" && <span style={{ color: "var(--viz-good)" }}>✓ Valid{dirty ? " — ready to save" : ""}</span>}
            {check.state === "idle" && <span className="text-muted-foreground">{dirty ? "Unsaved changes" : "Saved version"}</span>}
          </span>
          {saveMsg && <span className={`text-xs ${saveMsg.ok ? "text-muted-foreground" : "text-destructive"}`}>{saveMsg.text}</span>}
        </div>
        {check.state === "invalid" && (
          <pre role="alert" className="text-destructive bg-destructive/5 overflow-x-auto rounded-md border p-3 text-xs whitespace-pre-wrap">
            {check.detail}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}

// ───────────────────────── uploads ─────────────────────────

type Summary = {
  symbol: string;
  annual_rows: number;
  quarterly_rows: number;
  shareholding_rows: number;
  data_gaps: string[];
  warnings: string[];
};

function Uploads() {
  const [list, setList] = useState<UploadedDataset[] | null>(null);
  const [symbol, setSymbol] = useState("");
  const [basis, setBasis] = useState<"consolidated" | "standalone">("consolidated");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rebuild, setRebuild] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  async function queueRefresh(sym: string) {
    await api(`/stocks/${sym}/refresh`, { method: "POST" }).catch(() => undefined);
    setRebuild("Data refresh queued; the worker fetches prices and rebuilds the report within a minute.");
  }

  const load = useCallback(() => {
    api<UploadedDataset[]>("/uploads/screener").then(setList).catch(() => setList([]));
  }, []);
  useEffect(load, [load]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return setError("Choose a Screener .xlsx export");
    if (!/^[A-Za-z0-9&_.-]{1,32}$/.test(symbol.trim())) return setError("Enter the NSE symbol this export is for");
    const body = new FormData();
    body.set("file", file);
    body.set("symbol", symbol.trim());
    body.set("statement_type", basis);
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const summary = await api<Summary>("/uploads/screener", { method: "POST", body });
      setResult(summary);
      setRebuild("Rebuilding the report…");
      api(`/stocks/${summary.symbol}/report?rebuild=true`)
        .then(() => setRebuild("Report rebuilt with the new fundamentals."))
        .catch((err) =>
          setRebuild(
            err instanceof ApiError && err.status === 404
              ? "No prices stored yet: queue a data refresh to fetch them, then open the report."
              : `Report not rebuilt: ${err instanceof ApiError ? err.detail : "error"}`,
          ),
        );
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "upload failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle className="text-base">Screener uploads (optional)</CardTitle>
        <CardDescription>
          A Screener.in Excel export (“Export to Excel”) fills what the exchange filings lack: SG&amp;A, and years
          before XBRL filing began. Periods already stored from filings keep the filed figures; the export only fills
          their empty fields. The export does not say whether figures are consolidated or standalone, so choose it
          here; consolidated is preferred.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <form onSubmit={submit} className="flex flex-wrap items-end gap-3" aria-label="Upload Screener export">
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-muted-foreground">Export (.xlsx)</span>
            <input
              ref={fileInput}
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="text-sm"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-muted-foreground">Symbol</span>
            <input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="e.g. TCS" className="border-input bg-background h-8 w-32 rounded-md border px-2 text-sm uppercase" />
          </label>
          <fieldset className="flex items-center gap-3 text-xs">
            <legend className="text-muted-foreground mb-1">Statements</legend>
            {(["consolidated", "standalone"] as const).map((b) => (
              <label key={b} className="inline-flex items-center gap-1">
                <input type="radio" name="basis" value={b} checked={basis === b} onChange={() => setBasis(b)} />
                {b}
              </label>
            ))}
          </fieldset>
          <Button size="sm" type="submit" disabled={busy}>
            {busy ? "Importing…" : "Upload"}
          </Button>
        </form>
        {error && <ErrorText>{error}</ErrorText>}
        {result && (
          <div role="status" className="rounded-md border p-3 text-sm">
            <p>
              Imported <strong>{result.symbol}</strong>: {result.annual_rows} years, {result.quarterly_rows} quarters,{" "}
              {result.shareholding_rows} shareholding filings.{" "}
              <Link href={`/stocks/${result.symbol}`} className="underline">
                Open report
              </Link>
            </p>
            {rebuild && (
              <p className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                {rebuild}
                {rebuild.startsWith("No prices") && (
                  <Button size="sm" variant="outline" onClick={() => queueRefresh(result.symbol)}>
                    Queue data refresh
                  </Button>
                )}
              </p>
            )}
            {result.warnings.length > 0 && <p className="text-muted-foreground text-xs">Warnings: {result.warnings.join("; ")}</p>}
            {result.data_gaps.length > 0 && (
              <p className="text-muted-foreground text-xs">Not in the export (recorded as data gaps): {result.data_gaps.join(", ")}</p>
            )}
          </div>
        )}
        {list == null ? (
          <p className="text-muted-foreground text-sm">Loading…</p>
        ) : list.length === 0 ? (
          <Empty>No Screener uploads yet.</Empty>
        ) : (
          <table className="w-full text-sm tabular-nums" aria-label="Uploaded fundamentals">
            <thead className="text-muted-foreground text-xs">
              <tr>
                <th className="py-1 text-left font-normal">Stock</th>
                <th className="py-1 text-left font-normal">Statements</th>
                <th className="py-1 text-right font-normal">Years</th>
                <th className="py-1 text-right font-normal">Quarters</th>
                <th className="py-1 text-right font-normal">Uploaded</th>
              </tr>
            </thead>
            <tbody>
              {list.map((u) => (
                <tr key={`${u.symbol}-${u.statement_type}`} className="border-t">
                  <td className="py-1.5">
                    <Link href={`/stocks/${u.symbol}`} className="font-medium hover:underline">
                      {u.symbol}
                    </Link>{" "}
                    <span className="text-muted-foreground text-xs">{u.name}</span>
                  </td>
                  <td className="py-1.5 text-xs">{u.statement_type}</td>
                  <td className="py-1.5 text-right">
                    {u.annual_years} <span className="text-muted-foreground text-xs">(FY{u.first_fiscal_year}–{u.last_fiscal_year})</span>
                  </td>
                  <td className="py-1.5 text-right">{u.quarters}</td>
                  <td className="text-muted-foreground py-1.5 text-right text-xs">
                    {new Date(u.uploaded_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

function Notifications() {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    api<NotificationsView>("/notifications?limit=1")
      .then((v) => setConfigured(v.telegram_configured))
      .catch(() => setConfigured(null));
  }, []);
  async function test() {
    setMsg(null);
    try {
      const n = await api<Notification>("/notifications/test", { method: "POST" });
      window.dispatchEvent(new Event(NOTIFICATIONS_CHANGED));
      setMsg(
        n.telegram === "sent"
          ? { ok: true, text: "Test sent in-app and to Telegram." }
          : n.telegram === "failed"
            ? { ok: false, text: `In-app OK; Telegram failed: ${n.telegram_error ?? "error"}` }
            : { ok: true, text: "Test notification created (in-app only)." },
      );
    } catch (e) {
      setMsg({ ok: false, text: e instanceof ApiError ? e.detail : "failed" });
    }
  }
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle className="text-base">Notifications</CardTitle>
        <CardDescription>
          Price alerts, results changes and broker reminders appear under the bell and in the{" "}
          <Link href="/notifications" className="underline">
            notification centre
          </Link>
          ; Telegram (delivery plus a read-only /grade, /buyzone, /status bot) is optional.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <p>
          Telegram:{" "}
          {configured == null ? "—" : configured ? (
            <strong>configured</strong>
          ) : (
            <span className="text-muted-foreground">
              not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env and restart the worker and API.
            </span>
          )}
        </p>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={test}>
            Send test notification
          </Button>
          {msg && (
            <span role="status" className={`text-xs ${msg.ok ? "text-muted-foreground" : "text-destructive"}`}>
              {msg.text}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function Settings() {
  return (
    <Page>
      <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
      <Brokers />
      <Notifications />
      <ConfigEditor />
      <Filings />
      <Uploads />
    </Page>
  );
}
