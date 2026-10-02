"""Indian API /stock: cross-checks and events (SPEC §3.2, §3.8). Pure functions.

- **keyMetrics** (BVPS, P/B, ROA, market cap) against the values we derive from the mapped
  statements and the price: a difference above ``checks.key_metric_tolerance`` is listed.
  keyMetrics are never stored or scored.
- **stockCorporateActionData** (bonus, splits) against our corporate actions: a vendor action
  we lack, or ours inside the vendor's window that it lacks, or a different ratio, is listed.
  Nothing is written to corporate_actions from here.
- **boardMeetings** → board-meeting events (``exchange='indianapi'``), so the results watcher's
  board calendar knows the next results date even without NSE.
- **analystView / recosBar** → the consensus, informational only (never scored).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from app.data.events import EVENT_COLUMNS
from app.data.indianapi_parse import Mapped, fiscal_year_of, number, rel_diff
from app.db.enums import CorporateActionType, EventKind
from app.fundamentals.indianapi_map import IndianApiMap

CRORE_INR = 1e7
EXCHANGE = "indianapi"
_BONUS = re.compile(r"ratio\s+of\s+(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _iso(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


# ───────────────────────── keyMetrics ─────────────────────────


@dataclass(frozen=True)
class MetricCheck:
    name: str  # book_value_per_share | price_to_book | roa_pct | market_cap_cr
    vendor: float | None
    ours: float | None
    rel: float | None
    ok: bool | None  # None: one side missing, not compared
    text: str


def key_metric(stock: Any, amap: IndianApiMap, name: str) -> float | None:
    spec = amap.key_metrics.get(name)
    km = stock.get("keyMetrics") if isinstance(stock, dict) else None
    if spec is None or not isinstance(km, dict):
        return None
    for it in km.get(spec.group) or []:
        if isinstance(it, dict) and it.get("key") == spec.key:
            return number(it.get("value"))
    return None


def latest_year_end(mapped: Mapped) -> date | None:
    ends = [v.period_end for v in mapped.values
            if v.item_code == "total_equity" and v.period_type == "instant"
            and v.period_end.month == 3]  # fmt: skip
    return max(ends) if ends else None


def key_metric_checks(stock: Any, mapped: Mapped, amap: IndianApiMap,
                      price: float | None) -> list[MetricCheck]:  # fmt: skip
    """The vendor's most-recent-fiscal-year keyMetrics against ours for the latest fiscal year
    of the mapped statements. ROA uses the profit including minority interest (the vendor's
    figure matches that definition) over the average total assets."""
    tol = amap.checks.key_metric_tolerance
    end = latest_year_end(mapped)
    ours: dict[str, float | None] = dict.fromkeys(("book_value_per_share", "price_to_book",
                                                   "roa_pct", "market_cap_cr"))  # fmt: skip
    fy = f"FY{fiscal_year_of(end)}" if end else "latest year"
    if end is not None:
        eq = mapped.get("total_equity", end, "instant")
        shares = mapped.get("shares_outstanding", end, "instant")
        assets = mapped.get("total_assets", end, "instant")
        prev = mapped.get("total_assets", date(end.year - 1, end.month, end.day), "instant")
        profit = mapped.get("profit_after_tax", end, "year")
        if profit is None:
            profit = mapped.get("pat", end, "year")
        if eq is not None and shares:
            ours["book_value_per_share"] = eq / shares
        bvps = ours["book_value_per_share"]
        if price is not None and bvps:
            ours["price_to_book"] = price / bvps
        if profit is not None and assets and prev:
            ours["roa_pct"] = profit / ((assets + prev) / 2) * 100
        if price is not None and shares:
            ours["market_cap_cr"] = price * shares / CRORE_INR
    out = []
    for name, mine in ours.items():
        theirs = key_metric(stock, amap, name)
        if theirs is None or mine is None:
            missing = "the vendor gives none" if theirs is None else "we cannot derive it"
            out.append(MetricCheck(name, theirs, mine, None, None,
                                   f"{name}: not compared ({missing})"))  # fmt: skip
            continue
        rel = rel_diff(theirs, mine)
        ok = rel <= tol
        out.append(MetricCheck(name, theirs, mine, rel, ok, (
            f"Indian API keyMetrics {name} {theirs:,.2f} vs ours {mine:,.2f} ({fy}): "
            f"{rel:.1%} apart" + ("" if ok else f", above {tol:.0%}"))))  # fmt: skip
    return out


# ───────────────────────── corporate actions ─────────────────────────


@dataclass(frozen=True)
class VendorAction:
    ex_date: date
    action_type: CorporateActionType
    ratio_old: float | None  # ``ratio_new`` shares for every ``ratio_old`` held
    ratio_new: float | None
    remarks: str


def vendor_corporate_actions(stock: Any) -> tuple[list[VendorAction], date | None]:
    """Bonus issues and splits, and the earliest date the vendor's corporate-action lists
    cover (any category; older actions are not expected in them)."""
    ca = stock.get("stockCorporateActionData") if isinstance(stock, dict) else None
    if not isinstance(ca, dict):
        return [], None
    dates = [d for rows in ca.values() if isinstance(rows, list) for r in rows
             if isinstance(r, dict) and (d := _iso(r.get("sortDate"))) is not None]  # fmt: skip
    out = []
    for r in ca.get("bonus") or []:
        ex = _iso(r.get("xbDate")) or _iso(r.get("sortDate"))
        if ex is None:
            continue
        m = _BONUS.search(str(r.get("remarks") or ""))
        new, held = (float(m.group(1)), float(m.group(2))) if m else (None, None)
        # "1:1" = one bonus share for each one held → 2 shares for 1
        out.append(VendorAction(ex, CorporateActionType.BONUS, held,
                                held + new if held and new else None,
                                str(r.get("remarks") or "")))  # fmt: skip
    for r in ca.get("splits") or []:
        ex = _iso(r.get("xsDate")) or _iso(r.get("sortDate"))
        if ex is None:
            continue
        old_fv, new_fv = number(r.get("oldFaceValue")), number(r.get("newFaceValue"))
        ratio = old_fv / new_fv if old_fv and new_fv else None
        out.append(VendorAction(ex, CorporateActionType.SPLIT, 1.0 if ratio else None, ratio,
                                str(r.get("remarks") or "")))  # fmt: skip
    return sorted(out, key=lambda a: a.ex_date), (min(dates) if dates else None)


@dataclass(frozen=True)
class OurAction:
    ex_date: date
    action_type: CorporateActionType
    ratio_old: float | None
    ratio_new: float | None


def _factor(old: float | None, new: float | None) -> float | None:
    return new / old if old and new else None


def corporate_action_checks(vendor: Iterable[VendorAction], since: date | None,
                            ours: Iterable[OurAction], window_days: int) -> list[str]:  # fmt: skip
    """Differences between the vendor's bonus / split list and ours (empty: they agree)."""
    mine = [a for a in ours if a.action_type in (CorporateActionType.BONUS,
                                                 CorporateActionType.SPLIT)]  # fmt: skip
    win = timedelta(days=window_days)
    out: list[str] = []
    matched: set[int] = set()
    for v in vendor:
        hit = next((i for i, a in enumerate(mine) if a.action_type is v.action_type
                    and abs(a.ex_date - v.ex_date) <= win), None)  # fmt: skip
        label = f"{v.action_type.value} ex {v.ex_date}"
        if hit is None:
            out.append(f"Indian API lists a {label} ({v.remarks}) that is not in our corporate "
                       "actions: prices may not be adjusted for it")  # fmt: skip
            continue
        matched.add(hit)
        f_v, f_o = _factor(v.ratio_old, v.ratio_new), _factor(mine[hit].ratio_old,
                                                              mine[hit].ratio_new)  # fmt: skip
        if f_v is not None and f_o is not None and abs(f_v / f_o - 1) > 1e-6:
            out.append(f"{label}: Indian API ratio {f_v:g}x, ours {f_o:g}x")
    for i, a in enumerate(mine):
        if i not in matched and since is not None and a.ex_date >= since:
            out.append(f"our {a.action_type.value} ex {a.ex_date} is not in the Indian API list "
                       f"(which covers {since} on)")  # fmt: skip
    return out


# ───────────────────────── board meetings ─────────────────────────


def board_meetings(stock: Any, *, symbol: str, isin: str | None) -> pd.DataFrame:
    """``boardMeetings`` (``boardMeetDate``, ``purpose``) as board-meeting event rows for
    ``store_events(exchange='indianapi')``."""
    ca = stock.get("stockCorporateActionData") if isinstance(stock, dict) else None
    rows = []
    for r in (ca or {}).get("boardMeetings") or []:
        day = _iso(r.get("boardMeetDate")) if isinstance(r, dict) else None
        if day is None:
            continue
        purpose = str(r.get("purpose") or "Board meeting").strip()
        row: dict[str, Any] = dict.fromkeys(EVENT_COLUMNS)
        row.update(kind=EventKind.BOARD_MEETING.value,
                   source_id=f"{symbol}:{day.isoformat()}:{purpose[:120]}"[:200],
                   symbol=symbol, isin=isin, company=r.get("companyName") or None,
                   title=f"Board meeting: {purpose}", detail=r.get("remarks") or None,
                   event_date=day, data={"purpose": purpose, "source": "indianapi"})  # fmt: skip
        rows.append(row)
    unique = list({r["source_id"]: r for r in rows}.values())
    df = pd.DataFrame(unique, columns=EVENT_COLUMNS).astype(object)
    return df.where(df.notna(), None)


# ───────────────────────── analyst consensus ─────────────────────────


def analyst_consensus(stock: Any) -> dict[str, Any] | None:
    """``recosBar``: the number of recommendations, the mean rating (1 Strong Buy … 5 Strong
    Sell) and the count per rating. Informational only: never scored."""
    bar = stock.get("recosBar") if isinstance(stock, dict) else None
    if not isinstance(bar, dict) or not bar.get("isDataPresent"):
        return None
    ratings = {
        str(r.get("ratingName")): int(n)
        for r in bar.get("stockAnalyst") or []
        if isinstance(r, dict) and (n := number(r.get("numberOfAnalysts"))) is not None
    }
    total = number(bar.get("noOfRecommendations"))
    mean = number(bar.get("meanValue"))
    if total is None and not ratings:
        return None
    return {"recommendations": int(total) if total is not None else sum(ratings.values()),
            "mean_rating": mean, "scale": "1 Strong Buy … 5 Strong Sell",
            "ratings": ratings}  # fmt: skip


# ───────────────────────── peers ─────────────────────────


def vendor_peers(stock: Any) -> list[dict[str, Any]]:
    """``companyProfile.peerCompanyList``: name, P/B, P/E and trailing-12-month ROE (fraction;
    the vendor reports percent). Rows without a name are skipped; missing ratios stay None."""
    profile = stock.get("companyProfile") if isinstance(stock, dict) else None
    rows = profile.get("peerCompanyList") if isinstance(profile, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        name = r.get("companyName") if isinstance(r, dict) else None
        if not isinstance(name, str) or not name.strip():
            continue
        roe = number(r.get("returnOnAverageEquityTrailing12Month"))
        out.append({"name": name.strip(), "pb": number(r.get("priceToBookValueRatio")),
                    "pe": number(r.get("priceToEarningsValueRatio")),
                    "roe": roe / 100 if roe is not None else None})  # fmt: skip
    return out


def company_key(name: str) -> str:
    """Name for matching across sources: lower case, no punctuation or Ltd / Limited suffix."""
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    while words and words[-1] in {"ltd", "limited"}:
        words.pop()
    return " ".join(words)
