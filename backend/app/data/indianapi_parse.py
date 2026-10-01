"""Indian API responses → canonical line items (pure; SPEC §3.2, §3.6). Rules and field names
come only from ``config/indianapi_map.yaml``; this module never guesses one.

- **Identity.** The vendor looks stocks up by name, so every /stock answer is checked: its
  ISIN (``companyProfile.isInId``) must equal ours, or its NSE code (``exchangeCodeNse``) our
  symbol. Anything else is another company and is never stored.
- **Units.** Declared per section in the map and verified on the data: in each annual /stock
  entry, net income / (diluted EPS x diluted shares in crore) is ≈ 1 when amounts are in
  crore (≈ 10 in millions). The median over the entries decides; /historical_stats must agree
  with /stock where both cover a year. A failed check refuses the section (a data gap).
- **Merge.** /stock is primary for every (period, item) it gives. /historical_stats extends
  the years and fills items /stock lacks; where both give an item for the same year and differ
  by more than ``consistency_tolerance`` (and the map does not mark it ``compare: false``) the
  /stock figure is kept and the difference returned for the reconciliation panel. Quarters
  come from quarter_results first (/stock's interim P&L is sometimes wrong in the fourth
  quarter), /stock interim entries filling what it lacks.
- **Missing is missing.** Non-numeric values are dropped; a 0 for a ``zero_means_missing``
  item is dropped; nothing is ever filled with 0 (AGENTS.md rule 1).

Values come out in rupees (amounts), rupees per share, or a share count, like fin_line_items.
"""

import calendar
import re
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app.fundamentals.indianapi_map import UNIT_CRORE, IndianApiMap, Model, VendorItem

CRORE_INR = 1e7
SHARES_PER_CRORE = 1e7
PeriodKind = Literal["year", "quarter", "instant"]
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_LABEL = re.compile(r"^\s*([A-Za-z]{3})\s+(\d{4})\s*$")


class VendorDataError(ValueError):
    """A response that can't be read as the endpoint's shape, or fails a unit check."""


# ───────────────────────── small helpers ─────────────────────────


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", "--", "NA", "N/A", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def label_end(label: str) -> date | None:
    """``"Mar 2026"`` → 2026-03-31 (the fiscal year or quarter ending then); TTM and anything
    else → None."""
    m = _LABEL.match(label)
    if m is None or m.group(1).lower() not in _MONTHS:
        return None
    return month_end(int(m.group(2)), _MONTHS[m.group(1).lower()])


def fiscal_year_of(end: date) -> int:
    """Indian fiscal years are named by the year they end in (FY2026 ends 31 March 2026)."""
    return end.year


def rel_diff(a: float, b: float) -> float:
    scale = max(abs(a), abs(b))
    return 0.0 if scale == 0 else abs(a - b) / scale


# ───────────────────────── identity ─────────────────────────


@dataclass(frozen=True)
class Identity:
    ok: bool
    reason: str
    name: str | None = None
    isin: str | None = None
    nse: str | None = None
    bse: str | None = None
    industry: str | None = None


def verify_identity(stock: Any, *, isin: str | None, symbol: str) -> Identity:
    """Is this /stock answer about our stock? ISIN match, else NSE-code match."""
    if not isinstance(stock, dict):
        return Identity(False, "not a /stock answer")
    raw_prof = stock.get("companyProfile")
    prof: dict[str, Any] = raw_prof if isinstance(raw_prof, dict) else {}
    v_isin = str(prof.get("isInId") or "").strip().upper() or None
    v_nse = str(prof.get("exchangeCodeNse") or "").strip().upper() or None
    v_bse = str(prof.get("exchangeCodeBse") or "").strip() or None
    name = str(stock.get("companyName") or "").strip() or None
    industry = str(stock.get("industry") or prof.get("mgIndustry") or "").strip() or None
    ours_isin, ours_sym = (isin or "").strip().upper(), symbol.strip().upper()
    if ours_isin and v_isin:
        ok = v_isin == ours_isin
        why = (f"ISIN {v_isin} matches" if ok
               else f"vendor returned a different company: {name} (ISIN {v_isin}, NSE {v_nse}),"
                    f" not {ours_sym} (ISIN {ours_isin})")  # fmt: skip
    elif v_nse:
        ok = v_nse == ours_sym
        other = f"vendor returned a different company: {name} (NSE {v_nse}), not {ours_sym}"
        why = f"NSE code {v_nse} matches (no ISIN to compare)" if ok else other
    else:
        ok, why = False, f"vendor answer for {ours_sym} has neither ISIN nor NSE code to check"
    return Identity(ok, why, name, v_isin, v_nse, v_bse, industry)


# ───────────────────────── /stock financials ─────────────────────────


@dataclass(frozen=True)
class StockPeriod:
    kind: Literal["annual", "interim"]
    end: date
    inc: dict[str, float]
    bal: dict[str, float]
    cas: dict[str, float]
    inc_months: float | None
    cas_months: float | None


def _section(items: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for it in items or []:
        if isinstance(it, dict) and isinstance(it.get("key"), str):
            v = number(it.get("value"))
            if v is not None:
                out[it["key"]] = v
    return out


def stock_periods(stock: Any) -> list[StockPeriod]:
    fin = stock.get("financials") if isinstance(stock, dict) else None
    if not isinstance(fin, list):
        raise VendorDataError("/stock has no financials list")
    out = []
    for f in fin:
        if not isinstance(f, dict) or not isinstance(f.get("stockFinancialMap"), dict):
            continue
        kind = str(f.get("Type") or "").lower()
        end = _iso(f.get("EndDate"))
        if kind not in ("annual", "interim") or end is None:
            continue
        m = f["stockFinancialMap"]
        inc, bal, cas = _section(m.get("INC")), _section(m.get("BAL")), _section(m.get("CAS"))
        months = inc.pop("periodLength", None), cas.pop("periodLength", None)
        out.append(StockPeriod("annual" if kind == "annual" else "interim", end, inc, bal, cas,
                               *months))  # fmt: skip
        bal.pop("periodLength", None)
    return out


def _iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class UnitCheck:
    crore_per_unit: float | None  # None: the check failed (refuse the section)
    ratio: float | None
    samples: int
    message: str


def check_stock_unit(periods: list[StockPeriod], amap: IndianApiMap) -> UnitCheck:
    """Median of net income / (diluted EPS x diluted shares) over annual entries; shares are
    in ``units.shares``. ≈ 1 → amounts in crore, ≈ 10 → millions (1 crore = 10 million)."""
    shares_crore = UNIT_CRORE[amap.units.shares]
    ratios = []
    for p in periods:
        if p.kind != "annual":
            continue
        ni, eps = p.inc.get("NetIncome"), p.inc.get("DilutedEPSExcludingExtraOrdItems")
        sh = p.inc.get("DilutedWeightedAverageShares")
        if ni and eps and sh:
            ratios.append(ni / (eps * sh * shares_crore))
    if not ratios:
        return UnitCheck(None, None, 0, "no annual entry has net income, EPS and shares")
    r = statistics.median(ratios)
    tol = amap.units.unit_check_tolerance
    found = next((u for u, cpu in UNIT_CRORE.items() if abs(r * cpu - 1) <= tol), None)
    declared = amap.units.stock_financials
    if found is None:
        return UnitCheck(None, r, len(ratios), f"amounts fit no known unit (ratio {r:.3g})")
    if found != declared:
        return UnitCheck(None, r, len(ratios), f"amounts look like {found}, the map declares "
                         f"{declared} (ratio {r:.3g}): not read")  # fmt: skip
    return UnitCheck(UNIT_CRORE[found], r, len(ratios), f"amounts in {found} (ratio {r:.3f}, "
                     f"{len(ratios)} years)")  # fmt: skip


def is_bank_layout(stock: Any, hists: Mapping[str, Any], amap: IndianApiMap) -> bool:
    keys: set[str] = set()
    if stock is not None:
        for p in stock_periods(stock):
            keys |= set(p.inc) | set(p.bal)
    labels = {lab for h in hists.values() if isinstance(h, dict) for lab in h}
    return bool(keys & set(amap.bank_markers.stock)) or bool(labels & set(amap.bank_markers.hist))


# ───────────────────────── /historical_stats ─────────────────────────


def hist_table(payload: Any) -> dict[str, dict[date, float]]:
    """``{label: {period end: value}}``; TTM and non-numeric cells dropped."""
    if not isinstance(payload, dict) or not payload:
        raise VendorDataError("historical_stats answer is not a non-empty object")
    out: dict[str, dict[date, float]] = {}
    for label, row in payload.items():
        if not isinstance(row, dict):
            continue
        cells = {}
        for col, v in row.items():
            end, val = label_end(str(col)), number(v)
            if end is not None and val is not None:
                cells[end] = val
        out[str(label)] = cells
    return out


# ───────────────────────── mapping ─────────────────────────


@dataclass(frozen=True)
class VendorValue:
    period_end: date
    period_type: PeriodKind
    statement: str
    item_code: str
    value: float  # ₹ for amounts, ₹ per share, or a share count
    unit: str
    origin: str  # e.g. "stock:INC:NetIncome", "hist:yoy_results:Net Profit"


@dataclass(frozen=True)
class Difference:
    """/stock and /historical_stats disagree for one item and period: the /stock figure is kept
    for years, quarter_results' for quarters (/stock's interim P&L is the less reliable)."""

    item_code: str
    period_end: date
    period_type: PeriodKind
    stock: float
    hist: float
    rel: float
    kept: Literal["/stock", "quarter_results"] = "/stock"

    def text(self) -> str:
        cr = CRORE_INR
        return (f"Indian API {self.item_code} {self.period_type} {self.period_end}: /stock "
                f"{self.stock / cr:,.0f} vs /historical_stats {self.hist / cr:,.0f} crore "
                f"({self.rel:.1%} apart; {self.kept} kept)")  # fmt: skip


@dataclass
class Mapped:
    model: Model
    values: list[VendorValue] = field(default_factory=list)
    differences: list[Difference] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # unit checks, refused sections
    gaps: list[str] = field(default_factory=list)

    def years(self, statement: str) -> list[int]:
        kinds = ("instant",) if statement == "bs" else ("year",)
        return sorted({fiscal_year_of(v.period_end) for v in self.values
                       if v.statement == statement and v.period_type in kinds
                       and v.period_end.month == 3})  # fmt: skip

    def get(self, code: str, end: date, kind: PeriodKind) -> float | None:
        key = (code, end, kind)
        hits = (v.value for v in self.values if (v.item_code, v.period_end, v.period_type) == key)
        return next(hits, None)


def _scale(item: VendorItem, raw: float, crore_per_unit: float, shares_crore: float) -> float:
    if item.unit == "amount":
        return raw * crore_per_unit * CRORE_INR
    if item.unit == "shares":
        return raw * shares_crore * SHARES_PER_CRORE
    return raw


def _from_stock(item: VendorItem, sec: Mapping[str, float]) -> tuple[float, str] | None:
    src = item.stock
    assert src is not None
    if src.keys:
        k = next((k for k in src.keys if k in sec), None)
        if k is None:
            return None
        v, tag = sec[k], f"stock:{src.section}:{k}"
    else:
        parts = [k for k in src.sum_of if k in sec]
        if not parts:
            return None
        v, tag = sum(sec[k] for k in parts), f"stock:{src.section}:" + "+".join(parts)
    v *= src.sign
    return (abs(v) if src.magnitude else v), tag


def _from_hist(item: VendorItem, table: Mapping[str, Mapping[date, float]], end: date,
               stats: str) -> tuple[float, str] | None:  # fmt: skip
    src = item.hist
    assert src is not None
    if src.labels:
        lab = next((x for x in src.labels if end in table.get(x, {})), None)
        return None if lab is None else (table[lab][end], f"hist:{stats}:{lab}")
    if not all(end in table.get(x, {}) for x in src.sum_of):
        return None  # a partial sum would understate the total
    return sum(table[x][end] for x in src.sum_of), f"hist:{stats}:" + "+".join(src.sum_of)


def map_statements(stock: Any | None, hists: Mapping[str, Any], amap: IndianApiMap,
                   model: Model | None = None) -> Mapped:  # fmt: skip
    """``hists``: stats name → /historical_stats answer (yoy_results, balancesheet, cashflow,
    quarter_results). ``model``: bank or general; detected from the layout when None."""
    model = model or ("bank" if is_bank_layout(stock, hists, amap) else "general")
    items = amap.models[model]
    out = Mapped(model)
    zero_missing = set(amap.zero_means_missing)
    shares_crore = UNIT_CRORE[amap.units.shares]
    chosen: dict[tuple[str, date, PeriodKind], VendorValue] = {}

    def put(code: str, item: VendorItem, end: date, kind: PeriodKind, raw: float, tag: str,
            cpu: float) -> None:  # fmt: skip
        if raw == 0 and code in zero_missing:
            return
        chosen[(code, end, kind)] = VendorValue(end, kind, item.statement, code,
                                                _scale(item, raw, cpu, shares_crore),
                                                item.unit, tag)  # fmt: skip

    # 1. /stock (primary for what it covers)
    periods: list[StockPeriod] = []
    stock_cpu: float | None = None
    if stock is not None:
        try:
            periods = stock_periods(stock)
        except VendorDataError as exc:
            out.gaps.append(f"Indian API /stock: {exc}")
        uc = check_stock_unit(periods, amap) if periods else None
        if uc is not None:
            out.notes.append(f"/stock {uc.message}")
            stock_cpu = uc.crore_per_unit
            if stock_cpu is None:
                out.gaps.append(f"Indian API /stock not read: {uc.message}")
    if stock_cpu is not None:
        # annual entries first: an interim balance sheet only fills a date they don't cover
        for p in sorted(periods, key=lambda p: p.kind != "annual"):
            for code, item in items.items():
                if item.stock is None:
                    continue
                kind: PeriodKind | None
                if item.statement == "bs":
                    kind = "instant"
                elif p.kind == "annual":
                    kind = "year" if (item.statement != "cf" or p.cas_months == 12) else None
                else:  # interim: quarterly P&L only (interim cash flows are year-to-date)
                    kind = "quarter" if item.statement == "pl" and p.inc_months == 3 else None
                if kind is None or (p.kind == "interim" and (code, p.end, kind) in chosen):
                    continue
                sec = {"INC": p.inc, "BAL": p.bal, "CAS": p.cas}[item.stock.section]
                got = _from_stock(item, sec)
                if got is not None:
                    put(code, item, p.end, kind, got[0], got[1], stock_cpu)

    # 2. /historical_stats: verify the unit against /stock, then extend / fill
    hist_cpu = UNIT_CRORE[amap.units.historical_stats]
    tables: dict[str, dict[str, dict[date, float]]] = {}
    for stats, payload in hists.items():
        try:
            tables[stats] = hist_table(payload)
        except VendorDataError as exc:
            out.gaps.append(f"Indian API historical_stats {stats}: {exc}")
    ratio = _hist_vs_stock(chosen, tables, items)
    if ratio is not None and abs(ratio - 1) > amap.units.unit_check_tolerance:
        out.gaps.append(f"Indian API historical_stats not read: its amounts are {ratio:.3g}x "
                        "the /stock ones for the same years (unit mismatch)")  # fmt: skip
        tables = {}
    quarterly = tables.pop("quarter_results", None)
    for code, item in items.items():
        if item.hist is None or item.hist.stats not in tables:
            continue
        table = tables[item.hist.stats]
        kind = "instant" if item.statement == "bs" else "year"
        ends = {e for lab in (item.hist.labels or item.hist.sum_of) for e in table.get(lab, {})}
        for end in sorted(ends):
            got = _from_hist(item, table, end, item.hist.stats)
            if got is None or (got[0] == 0 and code in zero_missing):
                continue
            have = chosen.get((code, end, kind))
            if have is None:
                put(code, item, end, kind, got[0], got[1], hist_cpu)
            elif item.compare and item.unit == "amount":
                h = _scale(item, got[0], hist_cpu, shares_crore)
                r = rel_diff(have.value, h)
                if r > amap.units.consistency_tolerance:
                    out.differences.append(Difference(code, end, kind, have.value, h, r))

    # 3. quarters: quarter_results first, /stock interim fills
    if quarterly is not None:
        for code, item in items.items():
            if item.statement != "pl" or item.hist is None:
                continue
            for end in sorted({e for lab in (item.hist.labels or item.hist.sum_of)
                               for e in quarterly.get(lab, {})}):  # fmt: skip
                got = _from_hist(item, quarterly, end, "quarter_results")
                if got is None or (got[0] == 0 and code in zero_missing):
                    continue
                have = chosen.get((code, end, "quarter"))
                put(code, item, end, "quarter", got[0], got[1], hist_cpu)
                if have is not None and item.compare and item.unit == "amount":
                    q = chosen[(code, end, "quarter")].value
                    r = rel_diff(have.value, q)
                    if r > amap.units.consistency_tolerance:
                        out.differences.append(Difference(code, end, "quarter", have.value, q, r,
                                                          "quarter_results"))  # fmt: skip

    out.values = sorted(chosen.values(), key=lambda v: (v.period_end, v.item_code, v.period_type))
    return out


def _hist_vs_stock(chosen: Mapping[tuple[str, date, PeriodKind], VendorValue],
                   tables: Mapping[str, Mapping[str, Mapping[date, float]]],
                   items: Mapping[str, VendorItem]) -> float | None:  # fmt: skip
    """Median of /stock ÷ /historical_stats (both in crore) for revenue-like annual items both
    give, to verify historical_stats' unit."""
    ratios = []
    for code in ("revenue", "pbt", "total_assets"):
        item = items.get(code)
        if item is None or item.hist is None or item.hist.stats not in tables:
            continue
        kind: PeriodKind = "instant" if item.statement == "bs" else "year"
        for (c, end, k), v in chosen.items():
            if c != code or k != kind:
                continue
            got = _from_hist(item, tables[item.hist.stats], end, item.hist.stats)
            if got is not None and got[0]:
                ratios.append((v.value / CRORE_INR) / got[0])
    return statistics.median(ratios) if ratios else None


# ───────────────────────── checks on the mapped values ─────────────────────────


@dataclass(frozen=True)
class QuarterSum:
    fiscal_year: int
    item_code: str
    quarters: float
    annual: float
    ok: bool


def quarter_sums(m: Mapped, codes: tuple[str, ...], tol: float) -> list[QuarterSum]:
    """For each fiscal year with all four quarters and the annual figure: do they add up?"""
    out = []
    for code in codes:
        years = {v.period_end: v.value for v in m.values
                 if v.item_code == code and v.period_type == "year"}  # fmt: skip
        quarters = {v.period_end: v.value for v in m.values
                    if v.item_code == code and v.period_type == "quarter"}  # fmt: skip
        for fy_end, annual in sorted(years.items()):
            qs = [quarters.get(_months_back(fy_end, k)) for k in (0, 3, 6, 9)]
            if any(q is None for q in qs):
                continue
            total = float(sum(q for q in qs if q is not None))
            out.append(QuarterSum(fiscal_year_of(fy_end), code, total, annual,
                                  rel_diff(total, annual) <= tol))  # fmt: skip
    return out


def _months_back(end: date, months: int) -> date:
    y, m = divmod(end.year * 12 + end.month - 1 - months, 12)
    return month_end(y, m + 1)
