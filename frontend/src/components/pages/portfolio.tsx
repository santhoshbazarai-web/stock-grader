"use client";

// Manual portfolio (SPEC §9). Holdings come only from the transactions entered or imported
// here; nothing is fetched from a broker. Prices are the latest stored closes. "-" with a
// reason where a price or fair value is missing, never 0.
import { AlertTriangle, Trash2 } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Page } from "@/components/common";
import { EmptyState, Skeleton, Tabs, Term } from "@/components/ds";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { inr, num, ZONE_LABEL } from "@/lib/format";
import type { PortfolioInfo, PortfolioPosition, PortfolioTxn, PortfolioView, TxnType } from "@/lib/types";

const TABS = [
  { id: "dashboard", label: "Dashboard" },
  { id: "positions", label: "Positions" },
  { id: "transactions", label: "Transactions" },
];
const COLORS = ["var(--viz-s1)", "var(--viz-s2)", "var(--viz-s3)", "var(--viz-s4)", "var(--viz-s5, #888)", "var(--viz-s6, #aaa)"];
const input = "border-input bg-background h-8 rounded-md border px-2 text-sm";
const axisTick = { fontSize: 11, fill: "var(--viz-muted)" };
const signed = (v: number | null, f: (x: number) => string) => (v == null ? "-" : `${v > 0 ? "+" : ""}${f(v)}`);
const tone = (v: number | null) => (v == null ? "" : v >= 0 ? "text-[var(--sem-discount)]" : "text-[var(--sem-premium)]");
const TXN_LABEL: Record<TxnType, string> = { buy: "Buy", sell: "Sell", dividend: "Dividend", bonus: "Bonus", split: "Split" };

function Card({ label, value, sub, glossary, valueClass }: { label: string; value: string; sub?: string; glossary?: string; valueClass?: string }) {
  return (
    <div className="bg-card flex flex-col gap-0.5 rounded-xl border p-3 shadow-[var(--shadow-card)]">
      <span className="text-muted-foreground text-xs">{glossary ? <Term label={label} k={glossary} /> : label}</span>
      <span className={`tnum text-xl font-semibold ${valueClass ?? ""}`}>{value}</span>
      {sub && <span className="text-muted-foreground tnum text-xs">{sub}</span>}
    </div>
  );
}

function Dash({ v, title }: { v: PortfolioView; title?: string }) {
  const s = v.summary;
  return (
    <section aria-label="Summary" className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
      <Card label="Market value" value={s.market_value == null ? "-" : inr(s.market_value)} sub={s.unpriced.length ? `excludes ${s.unpriced.join(", ")} (no price)` : `as of ${v.as_of}`} />
      <Card label="Unrealised P&L" value={signed(s.unrealised_pnl, inr)} sub={s.unrealised_pct == null ? undefined : signed(s.unrealised_pct, (x) => `${num(x, 1)}%`)} valueClass={tone(s.unrealised_pnl)} />
      <Card label="Realised P&L" value={signed(s.realised_pnl, inr)} sub={`dividends ${inr(s.dividends)}`} valueClass={tone(s.realised_pnl)} />
      <Card label="Total cost" value={inr(s.total_cost)} sub={`${s.holdings} holding${s.holdings === 1 ? "" : "s"}`} />
      <Card label="Cash" value={inr(s.cash)} sub="opening cash ± transactions" />
      <div title={s.xirr_reason ?? title}>
        <Card label="XIRR" value={s.xirr == null ? "-" : `${num(s.xirr * 100, 1)}%`} sub={s.xirr_reason ?? "annualised, incl. dividends"} valueClass={tone(s.xirr)} glossary="xirr" />
      </div>
    </section>
  );
}

function Charts({ v }: { v: PortfolioView }) {
  const pnl = v.positions.filter((p) => p.pnl != null).map((p) => ({ symbol: p.symbol, pnl: p.pnl as number }));
  if (v.positions.length === 0) return <EmptyState title="No holdings yet">Add a buy on the Transactions tab or import a CSV.</EmptyState>;
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <figure className="flex flex-col gap-1" aria-label="Allocation by sector">
        <figcaption className="text-sm font-semibold">Allocation by sector</figcaption>
        {v.allocation.length === 0 ? (
          <p className="text-muted-foreground text-sm">No holding has a stored price.</p>
        ) : (
          <div className="h-64">
            <ResponsiveContainer>
              <PieChart>
                <Pie data={v.allocation} dataKey="value" nameKey="sector" innerRadius="50%" outerRadius="80%" paddingAngle={1}>
                  {v.allocation.map((a, i) => (
                    <Cell key={a.sector} fill={COLORS[i % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip formatter={(val, name) => [inr(Number(val)), String(name)]} contentStyle={{ background: "var(--viz-surface)", borderColor: "var(--viz-grid)", fontSize: 12 }} />
                <Legend wrapperStyle={{ fontSize: 12 }} formatter={(name) => `${name} ${num(v.allocation.find((a) => a.sector === name)?.weight_pct ?? 0, 0)}%`} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        )}
      </figure>
      <figure className="flex flex-col gap-1" aria-label="Unrealised P&L by holding">
        <figcaption className="text-sm font-semibold">Unrealised P&L by holding (₹)</figcaption>
        {pnl.length === 0 ? (
          <p className="text-muted-foreground text-sm">No holding has a stored price.</p>
        ) : (
          <div className="h-64">
            <ResponsiveContainer>
              <BarChart data={pnl}>
                <CartesianGrid stroke="var(--viz-grid)" vertical={false} />
                <XAxis dataKey="symbol" tick={axisTick} />
                <YAxis tick={axisTick} width={64} tickFormatter={(x: number) => num(x, 0)} />
                <Tooltip formatter={(val) => inr(Number(val))} contentStyle={{ background: "var(--viz-surface)", borderColor: "var(--viz-grid)", fontSize: 12 }} />
                <Bar dataKey="pnl" name="Unrealised P&L" radius={[2, 2, 0, 0]}>
                  {pnl.map((p) => (
                    <Cell key={p.symbol} fill={p.pnl >= 0 ? "var(--viz-good)" : "var(--viz-critical)"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </figure>
    </div>
  );
}

function Missing({ why }: { why: string | null }) {
  return (
    <span tabIndex={0} title={why ?? "not available"} className="text-muted-foreground cursor-help">
      -
    </span>
  );
}

function Positions({ rows }: { rows: PortfolioPosition[] }) {
  if (rows.length === 0) return <EmptyState title="No open positions">Closed positions keep their realised P&L in the summary.</EmptyState>;
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm tabular-nums" aria-label="Positions">
        <thead className="text-muted-foreground text-xs">
          <tr>
            {["Stock", "Qty", "Avg cost", "Price", "Value", "P&L", "P&L %", "Weight", "Grade", "Zone", "Fair-value gap", "Flags"].map((h) => (
              <th key={h} scope="col" className={`px-2 py-2 font-normal whitespace-nowrap ${["Stock", "Grade", "Zone", "Flags"].includes(h) ? "text-left" : "text-right"}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.symbol} className="border-t align-top">
              <td className="px-2 py-2">
                <Link href={`/stocks/${p.symbol}`} className="font-medium hover:underline">
                  {p.symbol}
                </Link>
                <div className="text-muted-foreground max-w-40 truncate text-xs">{p.sector ?? p.name}</div>
              </td>
              <td className="px-2 py-2 text-right">{num(p.quantity, 2)}</td>
              <td className="px-2 py-2 text-right">{p.avg_cost == null ? <Missing why="no shares held" /> : inr(p.avg_cost)}</td>
              <td className="px-2 py-2 text-right">{p.price == null ? <Missing why={p.missing} /> : <span title={p.price_date ? `close of ${p.price_date}` : undefined}>{inr(p.price)}</span>}</td>
              <td className="px-2 py-2 text-right">{p.value == null ? <Missing why={p.missing} /> : inr(p.value)}</td>
              <td className={`px-2 py-2 text-right ${tone(p.pnl)}`}>{p.pnl == null ? <Missing why={p.missing} /> : signed(p.pnl, inr)}</td>
              <td className={`px-2 py-2 text-right ${tone(p.pnl_pct)}`}>{p.pnl_pct == null ? <Missing why={p.missing ?? "needs a cost"} /> : signed(p.pnl_pct, (x) => `${num(x, 1)}%`)}</td>
              <td className="px-2 py-2 text-right">{p.weight_pct == null ? <Missing why={p.missing} /> : `${num(p.weight_pct, 1)}%`}</td>
              <td className="px-2 py-2">{p.grade ?? <Missing why="no report for this stock yet" />}</td>
              <td className="px-2 py-2 text-xs">{p.zone ? (ZONE_LABEL[p.zone] ?? p.zone) : <Missing why="no report for this stock yet" />}</td>
              <td className="px-2 py-2 text-right">{p.fv_gap_pct == null ? <Missing why={p.fair_value == null ? "no fair value in the latest report" : p.missing} /> : signed(p.fv_gap_pct, (x) => `${num(x, 1)}%`)}</td>
              <td className="px-2 py-2">
                {p.flags.length === 0 ? (
                  <span className="text-muted-foreground text-xs">-</span>
                ) : (
                  <ul className="flex flex-col gap-0.5">
                    {p.flags.map((f) => (
                      <li key={f} className="flex items-center gap-1 text-xs text-[var(--sem-premium)]">
                        <AlertTriangle className="size-3" aria-hidden /> {f}
                      </li>
                    ))}
                  </ul>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type Draft = { symbol: string; txn_type: TxnType; txn_date: string; quantity: string; price: string; fees: string; notes: string };
const blank = (): Draft => ({ symbol: "", txn_type: "buy", txn_date: new Date().toISOString().slice(0, 10), quantity: "", price: "", fees: "", notes: "" });
const LABELS: Record<TxnType, [string, string]> = {
  buy: ["Quantity", "Price per share"],
  sell: ["Quantity", "Price per share"],
  dividend: ["Shares (blank = held)", "Dividend per share"],
  bonus: ["New shares…", "…for every old shares"],
  split: ["New shares…", "…for every old shares"],
};

function Transactions({ pid, onChange }: { pid: number; onChange: () => void }) {
  const [rows, setRows] = useState<PortfolioTxn[] | null>(null);
  const [d, setD] = useState<Draft>(blank());
  const [editing, setEditing] = useState<number | null>(null);
  const [csv, setCsv] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    api<PortfolioTxn[]>(`/portfolios/${pid}/transactions`).then(setRows).catch((e) => setErr(e instanceof ApiError ? e.detail : "failed"));
  }, [pid]);
  useEffect(load, [load]);

  const num0 = (s: string) => (s.trim() === "" ? null : Number(s));
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setMsg(null);
    const body = { symbol: d.symbol.trim(), txn_type: d.txn_type, txn_date: d.txn_date, quantity: num0(d.quantity), price: num0(d.price), fees: num0(d.fees) ?? 0, notes: d.notes.trim() || null };
    try {
      await api(editing ? `/portfolios/${pid}/transactions/${editing}` : `/portfolios/${pid}/transactions`, { method: editing ? "PUT" : "POST", body: JSON.stringify(body) });
      setD(blank());
      setEditing(null);
      setMsg(editing ? "Transaction updated" : "Transaction added");
      load();
      onChange();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.detail : "could not save");
    }
  }
  async function del(id: number) {
    await api(`/portfolios/${pid}/transactions/${id}`, { method: "DELETE" });
    load();
    onChange();
  }
  async function importCsv() {
    setErr(null);
    setMsg(null);
    try {
      const r = await api<{ added: number; errors: string[] }>(`/portfolios/${pid}/transactions/import`, { method: "POST", body: JSON.stringify({ csv }) });
      setMsg(`Imported ${r.added} transaction(s)${r.errors.length ? `; skipped: ${r.errors.join(" | ")}` : ""}`);
      setCsv("");
      load();
      onChange();
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : "import failed");
    }
  }
  const [ql, pl] = LABELS[d.txn_type];

  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={save} className="flex flex-wrap items-end gap-2" aria-label={editing ? "Edit transaction" : "Add transaction"}>
        <input aria-label="Symbol" placeholder="Symbol" value={d.symbol} onChange={(e) => setD({ ...d, symbol: e.target.value })} className={`${input} w-28 uppercase`} />
        <select aria-label="Type" value={d.txn_type} onChange={(e) => setD({ ...d, txn_type: e.target.value as TxnType })} className={input}>
          {(Object.keys(TXN_LABEL) as TxnType[]).map((t) => (
            <option key={t} value={t}>
              {TXN_LABEL[t]}
            </option>
          ))}
        </select>
        <input aria-label="Date" type="date" value={d.txn_date} onChange={(e) => setD({ ...d, txn_date: e.target.value })} className={input} />
        <input aria-label={ql} placeholder={ql} inputMode="decimal" value={d.quantity} onChange={(e) => setD({ ...d, quantity: e.target.value })} className={`${input} w-36`} />
        <input aria-label={pl} placeholder={pl} inputMode="decimal" value={d.price} onChange={(e) => setD({ ...d, price: e.target.value })} className={`${input} w-36`} />
        <input aria-label="Fees" placeholder="Fees ₹" inputMode="decimal" value={d.fees} onChange={(e) => setD({ ...d, fees: e.target.value })} className={`${input} w-24`} />
        <input aria-label="Notes" placeholder="Notes" value={d.notes} onChange={(e) => setD({ ...d, notes: e.target.value })} className={`${input} w-48`} />
        <Button size="sm" type="submit">
          {editing ? "Save changes" : "Add transaction"}
        </Button>
        {editing && (
          <Button size="sm" type="button" variant="ghost" onClick={() => { setEditing(null); setD(blank()); }}>
            Cancel
          </Button>
        )}
        <span className="text-muted-foreground w-full text-xs">Splits and bonuses come from the stored corporate actions; enter a Bonus / Split here only if one is missing. Entered by hand: nothing is read from your broker.</span>
      </form>
      {err && <p role="alert" className="text-destructive text-sm">{err}</p>}
      {msg && <p role="status" className="text-sm">{msg}</p>}

      <details className="text-sm">
        <summary className="cursor-pointer">Import / export CSV</summary>
        <div className="mt-2 flex flex-col gap-2">
          <a href={`/api/portfolios/${pid}/transactions/export`} download className="text-primary w-fit underline">
            Export CSV
          </a>
          <input type="file" aria-label="CSV file" accept=".csv,text/csv,text/plain" onChange={async (e) => setCsv((await e.target.files?.[0]?.text()) ?? "")} />
          <textarea aria-label="CSV text" rows={4} value={csv} onChange={(e) => setCsv(e.target.value)} placeholder={"date,symbol,type,quantity,price,fees,notes\n2024-01-05,TCS,buy,10,3500,20,"} className="bg-background rounded-md border px-2 py-1.5 font-mono text-xs" />
          <div>
            <Button size="sm" variant="outline" onClick={importCsv} disabled={!csv.trim()}>
              Import
            </Button>
          </div>
        </div>
      </details>

      {rows == null ? (
        <Skeleton className="h-32 w-full" />
      ) : rows.length === 0 ? (
        <EmptyState title="No transactions">Add your first buy above.</EmptyState>
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm tabular-nums" aria-label="Transactions">
            <thead className="text-muted-foreground text-xs">
              <tr>
                {["Date", "Stock", "Type", "Qty", "Price", "Fees", "Notes", ""].map((h, i) => (
                  <th key={i} scope="col" className={`px-2 py-2 font-normal ${["Qty", "Price", "Fees"].includes(h) ? "text-right" : "text-left"}`}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr key={t.id} className="border-t">
                  <td className="px-2 py-1.5 whitespace-nowrap">{t.txn_date}</td>
                  <td className="px-2 py-1.5 font-medium">{t.symbol}</td>
                  <td className="px-2 py-1.5">{TXN_LABEL[t.txn_type]}</td>
                  <td className="px-2 py-1.5 text-right">{t.quantity == null ? "-" : num(t.quantity, 4)}</td>
                  <td className="px-2 py-1.5 text-right">{t.price == null ? "-" : inr(t.price)}</td>
                  <td className="px-2 py-1.5 text-right">{inr(t.fees)}</td>
                  <td className="text-muted-foreground max-w-48 truncate px-2 py-1.5 text-xs">{t.notes}</td>
                  <td className="px-2 py-1.5 text-right whitespace-nowrap">
                    <Button size="sm" variant="ghost" onClick={() => { setEditing(t.id); setD({ symbol: t.symbol, txn_type: t.txn_type, txn_date: t.txn_date, quantity: t.quantity == null ? "" : String(t.quantity), price: t.price == null ? "" : String(t.price), fees: t.fees ? String(t.fees) : "", notes: t.notes ?? "" }); }} aria-label={`Edit transaction ${t.id}`}>
                      Edit
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => del(t.id)} aria-label={`Delete transaction ${t.id}`}>
                      <Trash2 />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function PortfolioPage() {
  const [list, setList] = useState<PortfolioInfo[]>([]);
  const [pid, setPid] = useState<number | null>(null);
  const [view, setView] = useState<PortfolioView | null>(null);
  const [tab, setTab] = useState("dashboard");
  const [name, setName] = useState("");
  const [cash, setCash] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const loadList = useCallback(async () => {
    const l = await api<PortfolioInfo[]>("/portfolios");
    setList(l);
    setPid((cur) => (cur != null && l.some((x) => x.id === cur) ? cur : (l[0]?.id ?? null)));
    setLoaded(true);
  }, []);
  useEffect(() => {
    loadList().catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, [loadList]);

  const loadView = useCallback(() => {
    if (pid == null) return setView(null);
    api<PortfolioView>(`/portfolios/${pid}/view`).then(setView).catch((e) => setError(e instanceof ApiError ? e.detail : "failed"));
  }, [pid]);
  useEffect(loadView, [loadView]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const p = await api<PortfolioInfo>("/portfolios", { method: "POST", body: JSON.stringify({ name: name.trim(), opening_cash: cash.trim() === "" ? 0 : Number(cash) }) });
      setName("");
      setCash("");
      await loadList();
      setPid(p.id);
    } catch (e2) {
      setError(e2 instanceof ApiError ? e2.detail : "could not create");
    }
  }
  async function remove() {
    if (pid == null) return;
    await api(`/portfolios/${pid}`, { method: "DELETE" });
    setPid(null);
    await loadList();
  }

  return (
    <Page>
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold">Portfolio</h1>
          <p className="text-muted-foreground text-sm">Entered by hand: no positions or holdings are read from Fyers or Zerodha. Prices are our latest stored closes.</p>
        </div>
        <form onSubmit={create} className="flex items-center gap-1" aria-label="New portfolio">
          <input aria-label="Portfolio name" placeholder="New portfolio name" value={name} onChange={(e) => setName(e.target.value)} className={`${input} w-44`} />
          <input aria-label="Opening cash" placeholder="Opening cash ₹" inputMode="decimal" value={cash} onChange={(e) => setCash(e.target.value)} className={`${input} w-32`} />
          <Button size="sm" variant="outline" type="submit" disabled={!name.trim()}>
            Create
          </Button>
        </form>
      </header>
      {error && <p role="alert" className="text-destructive text-sm">{error}</p>}
      {!loaded ? (
        <Skeleton className="h-40 w-full" />
      ) : list.length === 0 ? (
        <EmptyState title="No portfolio yet">Create one above, then add transactions or import a CSV.</EmptyState>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Portfolios">
            {list.map((p) => (
              <button key={p.id} type="button" aria-pressed={p.id === pid} onClick={() => setPid(p.id)} className={`rounded-full border px-3 py-1 text-sm ${p.id === pid ? "bg-primary text-primary-foreground" : ""}`}>
                {p.name} <span className="opacity-70">({p.transactions})</span>
              </button>
            ))}
            <Button size="sm" variant="ghost" onClick={remove}>
              <Trash2 /> Delete portfolio
            </Button>
          </div>
          {view ? <Dash v={view} /> : <Skeleton className="h-24 w-full" />}
          {view?.warnings.map((w) => (
            <p key={w} className="text-muted-foreground text-xs">
              ⚠ {w}
            </p>
          ))}
          <Tabs tabs={TABS} value={tab} onChange={setTab} label="Portfolio sections" />
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} className="flex flex-col gap-4">
            {tab === "dashboard" && (view ? <Charts v={view} /> : <Skeleton className="h-64 w-full" />)}
            {tab === "positions" && (view ? <Positions rows={view.positions} /> : <Skeleton className="h-64 w-full" />)}
            {tab === "transactions" && pid != null && <Transactions pid={pid} onChange={() => { loadView(); loadList(); }} />}
          </div>
        </>
      )}
    </Page>
  );
}
