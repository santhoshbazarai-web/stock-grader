"""Exchange financial-results XBRL → canonical ``fin_quarterly`` / ``fin_annual`` records.

Listed companies file every quarterly and annual result with NSE and BSE as an XBRL instance
in the SEBI/BSE Ind AS results taxonomy (element prefix ``in-bse-fin``); both exchanges get the
same document. This module is pure: bytes in, dataclasses out (rule 2).

An instance holds *facts* (``<in-bse-fin:RevenueFromOperations contextRef="OneD"
unitRef="INR" decimals="-5">123400000</...>``), *contexts* (entity + period, optionally a
dimension such as a segment) and *units*. A results filing reports several periods at once:

- the quarter (``DateOfStartOfReportingPeriod`` .. ``DateOfEndOfReportingPeriod``);
- year-to-date (``DateOfStartOfFinancialYear`` .. period end); in a Q4 filing this is the
  fiscal year, which becomes the ``fin_annual`` row;
- an instant at period end (the statement of assets and liabilities, half-yearly);
- comparatives (the same quarter last year, the previous year), which are ignored: only the
  numbers first reported for a period are its point-in-time truth (rule 4);
- dimensional contexts (segments), also ignored.

Monetary facts are absolute rupees and become ₹ crore; per-share facts stay ₹. Element names
come from ``fundamentals/xbrl_map.yaml`` only (versioned; Ind AS, bank and pre-Ind-AS tags).

Untrusted input: parsed with ``defusedxml`` (no entity expansion, no external entities or DTDs).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from app.core.config import NseResultsConfig
from app.data.canonical import Table, fields_for, fiscal_year
from app.db.enums import StatementType
from app.fundamentals.xbrl_map import ItemSpec, XbrlMap, get_xbrl_map

IST = ZoneInfo("Asia/Kolkata")
XBRLI = "http://www.xbrl.org/2003/instance"
_SKIP_NAMESPACES = frozenset(
    {
        XBRLI,
        "http://www.xbrl.org/2003/linkbase",
        "http://www.w3.org/1999/xlink",
        "http://xbrl.org/2006/xbrldi",
    }
)
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
CRORE = 1e7  # the wide fin tables hold ₹ crore and share counts in crore


class XbrlFormatError(ValueError):
    """Not an XBRL instance, unsafe XML, or no usable results in it."""


# ───────────────────────── instance model ─────────────────────────


@dataclass(frozen=True)
class Context:
    id: str
    start: date | None
    end: date | None  # duration end, or the instant
    instant: bool
    dimensional: bool  # has a segment/scenario (e.g. a business segment): ignored

    @property
    def days(self) -> int | None:
        if self.instant or self.start is None or self.end is None:
            return None
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class Fact:
    name: str  # local name, namespace dropped
    context: str
    unit: str | None  # the unit's measure local name: INR, pure, shares, INRPerShare...
    text: str


@dataclass
class Instance:
    contexts: dict[str, Context]
    facts: list[Fact]

    def plain(self, context_id: str) -> dict[str, Fact]:
        """First fact per element in a (non-dimensional) context."""
        out: dict[str, Fact] = {}
        for f in self.facts:
            if f.context == context_id and f.name not in out:
                out[f.name] = f
        return out

    def text(self, names: tuple[str, ...]) -> str | None:
        """A descriptive fact (company name, period dates...) from any non-dimensional context."""
        for name in names:
            for f in self.facts:
                ctx = self.contexts.get(f.context)
                if f.name == name and f.text.strip() and ctx is not None and not ctx.dimensional:
                    return f.text.strip()
        return None


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def _date(text: str | None) -> date | None:
    if not text:
        return None
    raw = text.strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_instance(content: bytes) -> Instance:
    try:
        root = SafeET.fromstring(content)
    except DefusedXmlException as exc:
        raise XbrlFormatError(f"unsafe XML rejected ({type(exc).__name__})") from exc
    except SafeET.ParseError as exc:
        raise XbrlFormatError(f"not XML: {exc}") from exc
    if _local(root.tag) != (XBRLI, "xbrl"):
        raise XbrlFormatError(f"not an XBRL instance (root element {root.tag!r})")

    contexts: dict[str, Context] = {}
    for el in root.iter(f"{{{XBRLI}}}context"):
        cid = el.get("id")
        if not cid:
            continue
        period = el.find(f"{{{XBRLI}}}period")
        if period is None:
            continue
        instant = period.findtext(f"{{{XBRLI}}}instant")
        dimensional = (
            el.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}segment") is not None
            or el.find(f"{{{XBRLI}}}scenario") is not None
        )
        if instant is not None:
            contexts[cid] = Context(cid, None, _date(instant), True, dimensional)
        else:
            start = _date(period.findtext(f"{{{XBRLI}}}startDate"))
            end = _date(period.findtext(f"{{{XBRLI}}}endDate"))
            contexts[cid] = Context(cid, start, end, False, dimensional)

    units: dict[str, str] = {}
    for el in root.iter(f"{{{XBRLI}}}unit"):
        uid = el.get("id")
        measures = [m.text or "" for m in el.iter(f"{{{XBRLI}}}measure")]
        if uid and measures:
            # divide units (INR per share) → join numerator/denominator measure names
            units[uid] = "Per".join(_local_measure(m) for m in measures)

    facts: list[Fact] = []
    for el in root:
        ns, name = _local(el.tag)
        ctx = el.get("contextRef")
        if ns in _SKIP_NAMESPACES or not ctx or el.get(_XSI_NIL) == "true":
            continue
        unit_ref = el.get("unitRef")
        facts.append(Fact(name, ctx, units.get(unit_ref) if unit_ref else None, el.text or ""))
    if not facts:
        raise XbrlFormatError("XBRL instance has no facts")
    return Instance(contexts, facts)


def _local_measure(measure: str) -> str:
    """``iso4217:INR`` → ``INR``; ``xbrli:shares`` → ``shares``."""
    return measure.strip().rpartition(":")[2]


# ───────────────────────── results extraction ─────────────────────────


@dataclass(frozen=True)
class ItemValue:
    """One mapped line item of one period, in the map's unit (amounts in ₹)."""

    value: float
    tag: str  # "group:Element" of the tag that matched, or "sum:group:A+B" for a summed item


@dataclass
class ResultsFiling:
    company: str | None
    symbol: str | None
    scrip_code: str | None
    isin: str | None
    statement_type: StatementType | None
    audited: bool | None
    period_start: date | None
    period_end: date
    board_meeting: date | None
    is_bank: bool
    quarter: dict[str, Any] | None  # canonical fin_quarterly record (+ "extra")
    annual: dict[str, Any] | None  # canonical fin_annual record (+ "extra", "fiscal_year")
    warnings: list[str] = field(default_factory=list)
    contexts: dict[str, str] = field(default_factory=dict)  # quarter / year / balance_sheet → id
    map_version: int = 0  # xbrl_map.yaml version that produced the values
    # The same periods as mapped line items (amounts in ₹), for fin_line_items.
    quarter_items: dict[str, ItemValue] = field(default_factory=dict)
    year_items: dict[str, ItemValue] = field(default_factory=dict)

    def periods(self) -> list[str]:
        out = []
        if self.quarter is not None:
            out.append(f"quarter {self.period_end.isoformat()}")
        if self.annual is not None:
            out.append(f"year {self.period_end.isoformat()}")
        return out


def _number(f: Fact, unit: str) -> float | None:
    try:
        v = float(f.text.strip().replace(",", ""))
    except ValueError:
        return None
    if unit == "pct" and (f.unit or "").lower() == "pure":
        v *= 100  # XBRL percentages are decimal fractions
    return v


def _item(facts: Mapping[str, Fact], spec: ItemSpec) -> ItemValue | None:
    for group, name in spec.tag_candidates():
        f = facts.get(name)
        if f is not None and (v := _number(f, spec.unit)) is not None:
            return ItemValue(abs(v) if spec.magnitude else v, f"{group}:{name}")
    for group, names in spec.sum_groups():
        parts = [(n, _number(facts[n], spec.unit)) for n in names if n in facts]
        reported = [(n, v) for n, v in parts if v is not None]
        if reported:
            total = float(sum(v for _, v in reported))
            tag = f"sum:{group}:" + "+".join(n for n, _ in reported)
            return ItemValue(abs(total) if spec.magnitude else total, tag)
    return None


def extract_items(
    duration: Mapping[str, Fact], instant: Mapping[str, Fact], xmap: XbrlMap
) -> dict[str, ItemValue]:
    """Every mapped item of one period: balance-sheet items from the instant context at the
    period end, P&L and cash flow from the period's duration context, ratios from either."""
    out: dict[str, ItemValue] = {}
    for code, spec in xmap.items.items():
        if spec.statement == "bs":
            v = _item(instant, spec)
        elif spec.statement == "ratio":
            v = _item(duration, spec) or _item(instant, spec)
        else:
            v = _item(duration, spec)
        if v is not None:
            out[code] = v
    return out


def _add(*xs: float | None) -> float | None:
    return None if any(x is None for x in xs) else float(sum(x for x in xs if x is not None))


def wide_record(items: Mapping[str, ItemValue], table: Table, xmap: XbrlMap) -> dict[str, Any]:
    """Line items → the canonical fin_quarterly / fin_annual record (₹ crore; shares in crore).
    Derivations are documented in ``CANONICAL_FIELDS[...].derived["nse"]``."""
    rec: dict[str, Any] = {}
    for name in fields_for(table):
        spec = xmap.items.get(name)
        v = items.get(name)
        if spec is None or spec.target != "canonical" or v is None:
            rec[name] = None
        else:
            rec[name] = v.value / CRORE if spec.unit in ("amount", "shares") else v.value
    pbt, interest = rec.get("pbt"), rec.get("interest")
    rec["ebit"] = _add(pbt, interest)
    ebitda_parts = _add(pbt, interest, rec.get("depreciation"))
    oi = rec.get("other_income")
    rec["ebitda"] = None if ebitda_parts is None or oi is None else ebitda_parts - oi
    eps, pat = rec.get("eps_diluted"), rec.get("pat")
    if eps and pat is not None:
        rec["shares_diluted_cr"] = pat / eps
    if table == "fin_annual":
        shares, equity = rec.get("shares_diluted_cr"), rec.get("total_equity")
        if shares and equity is not None and shares > 0:
            rec["book_value_per_share"] = equity / shares
    extra = {
        code: v.value / CRORE if xmap.items[code].unit == "amount" else v.value
        for code, v in items.items()
        if xmap.items[code].target == "extra"
    }
    rec["extra"] = extra or None
    return rec


def _has_pl(rec: Mapping[str, Any]) -> bool:
    return any(rec.get(k) is not None for k in ("revenue", "pbt", "pat"))


def statement_type_from_text(text: str | None) -> StatementType | None:
    t = (text or "").strip().lower()
    if t.startswith("consolidated"):
        return StatementType.CONSOLIDATED
    if t.startswith("standalone") or t.startswith("non-consolidated"):
        return StatementType.STANDALONE
    return None


def audited_from_text(text: str | None) -> bool | None:
    t = (text or "").strip().lower().replace("-", "").replace(" ", "")
    if t.startswith("unaudited"):
        return False
    if t.startswith("audited"):
        return True
    return None


def parse_results(
    content: bytes,
    cfg: NseResultsConfig,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
    xmap: XbrlMap | None = None,
) -> ResultsFiling:
    """A results filing → its quarter row and (for a Q4 / annual filing) its fiscal-year row.
    ``period_start`` / ``period_end`` (e.g. from the exchange's filing list) are used when the
    document does not state its reporting period."""
    xmap = xmap or get_xbrl_map()
    info = {k: tuple(v) for k, v in xmap.info.items()}
    inst = parse_instance(content)
    warnings: list[str] = []
    end = _date(inst.text(info["period_end"])) or period_end
    start = _date(inst.text(info["period_start"])) or period_start
    plain = [c for c in inst.contexts.values() if not c.dimensional and c.end is not None]
    if end is None:  # infer: the latest end date among plain duration contexts
        ends = [c.end for c in plain if not c.instant and c.end is not None]
        if not ends:
            raise XbrlFormatError("no reporting period in the filing")
        end = max(ends)
        warnings.append("reporting period not stated; inferred from the contexts")
    durations = [c for c in plain if not c.instant and c.end == end and c.days is not None]
    instants = [c for c in plain if c.instant and c.end == end]

    def best(ctxs: list[Context], ok: Any) -> Context | None:
        """Among contexts of the right length, the one carrying the most facts."""
        cands = [c for c in ctxs if ok(c)]
        return max(cands, key=lambda c: len(inst.plain(c.id)), default=None)

    # Comparatives end on other dates, so any quarter-length context ending at ``end`` is the
    # quarter, and any year-length one the fiscal year (only in Q4 / annual filings).
    q_ctx = best(durations, lambda c: cfg.quarter_days.contains(c.days))
    y_ctx = best(durations, lambda c: cfg.year_days.contains(c.days))
    bs_ctx = best(instants, lambda c: True)

    bs_facts = inst.plain(bs_ctx.id) if bs_ctx else {}
    q_items = extract_items(inst.plain(q_ctx.id), bs_facts, xmap) if q_ctx else {}
    y_items = extract_items(inst.plain(y_ctx.id), bs_facts, xmap) if y_ctx else {}
    if q_items and y_items:  # period-end balances filed only in the quarter's context
        for code, v in q_items.items():
            if xmap.items[code].carry_to_year:
                y_items.setdefault(code, v)
    quarter = annual = None
    if q_ctx is not None:
        quarter = wide_record(q_items, "fin_quarterly", xmap)
    if y_ctx is not None:
        annual = wide_record(y_items, "fin_annual", xmap)
        annual["fiscal_year"] = fiscal_year(datetime.combine(end, time()))
    if quarter is not None and not _has_pl(quarter):
        warnings.append("quarter context has no revenue, PBT or PAT; ignored")
        quarter, q_items = None, {}
    if annual is not None and not _has_pl(annual):
        warnings.append("fiscal-year context has no revenue, PBT or PAT; ignored")
        annual, y_items = None, {}
    if quarter is None and annual is None:
        raise XbrlFormatError(
            f"no quarter or fiscal-year results for the period ending {end.isoformat()}"
        )

    is_bank = any(f.name == xmap.bank_marker for f in inst.facts)
    nature = inst.text(info["nature"])
    statement_type = statement_type_from_text(nature)
    if statement_type is None:
        warnings.append(f"standalone/consolidated not stated ({nature!r})")
    return ResultsFiling(
        company=inst.text(info["company"]),
        symbol=inst.text(info["symbol"]),
        scrip_code=inst.text(info["scrip_code"]),
        isin=inst.text(info["isin"]),
        statement_type=statement_type,
        audited=audited_from_text(inst.text(info["audited"])),
        period_start=start or (q_ctx.start if q_ctx else None),
        period_end=end,
        board_meeting=_date(inst.text(info["board_meeting"])),
        is_bank=is_bank,
        quarter=quarter,
        annual=annual,
        warnings=warnings,
        contexts={
            role: ctx.id
            for role, ctx, rec in (
                ("quarter", q_ctx, quarter),
                ("year", y_ctx, annual),
                ("balance_sheet", bs_ctx, annual),
            )
            if ctx is not None and rec is not None
        },
        map_version=xmap.version,
        quarter_items=q_items,
        year_items=y_items,
    )


# ───────────────────────── inspection ─────────────────────────


def mapped_elements() -> set[str]:
    """Every element name the parser reads (``xbrl_map.yaml``)."""
    return get_xbrl_map().all_elements()


def describe(content: bytes, cfg: NseResultsConfig) -> str:
    """Human-readable dump of a filing for checking the element mapping against real
    documents: what was read, from which contexts, and which numeric elements in those
    contexts the mapping does not use (candidates to add to ``fundamentals/xbrl_map.yaml``)."""
    inst = parse_instance(content)
    filing = parse_results(content, cfg)
    used = {cid: role for role, cid in filing.contexts.items()}
    lines = [
        f"xbrl_map.yaml version {filing.map_version}",
        f"company {filing.company}  symbol {filing.symbol}  scrip {filing.scrip_code}  "
        f"isin {filing.isin}",
        f"basis {filing.statement_type}  audited {filing.audited}  period "
        f"{filing.period_start}..{filing.period_end}  board meeting {filing.board_meeting}  "
        f"bank {filing.is_bank}",
        "",
        "contexts:",
    ]
    for c in sorted(inst.contexts.values(), key=lambda c: (c.dimensional, c.id)):
        span = f"instant {c.end}" if c.instant else f"{c.start}..{c.end} ({c.days}d)"
        note = (
            f"used: {used[c.id]}" if c.id in used
            else "dimensional, ignored" if c.dimensional else "not used"
        )  # fmt: skip
        lines.append(f"  {c.id:<12} {span:<32} {len(inst.plain(c.id)):>4} facts  {note}")
    for label, rec in (("quarter", filing.quarter), ("year", filing.annual)):
        if rec is None:
            continue
        lines += ["", f"canonical {label} (₹ Cr; per-share in ₹):"]
        for k, v in rec.items():
            shown = "—" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))
            lines.append(f"  {k:<28} {shown}")
    mapped = mapped_elements()
    unmapped = sorted(
        {
            (f.name, f.context)
            for f in inst.facts
            if f.context in used and f.name not in mapped and f.unit is not None
        }
    )
    for label, items in (("quarter", filing.quarter_items), ("year", filing.year_items)):
        if items:
            lines += ["", f"line items, {label} (₹; tag that matched):"]
            lines += [f"  {code:<28} {v.value:>22,.2f}  {v.tag}" for code, v in items.items()]
    lines += ["", f"numeric elements in the used contexts not mapped ({len(unmapped)}):"]
    lines += [f"  {name}  [{cid}]" for name, cid in unmapped]
    if filing.warnings:
        lines += ["", "warnings:", *[f"  {w}" for w in filing.warnings]]
    return "\n".join(lines)


# ───────────────────────── point in time ─────────────────────────


def announcement_date(
    disseminated_at: datetime | None, board_meeting: date | None, available_after: time
) -> date | None:
    """The first day a close-based signal could have used the filing (rule 4).

    - Disseminated by the exchange before ``available_after`` (the close, IST) → that day;
      at or after it → the next day.
    - Only the board-meeting date known (an uploaded document) → the day after it, since
      results are usually released after the meeting, often after the close.
    - Neither → ``None`` (a data gap; backtests skip the row).
    """
    if disseminated_at is not None:
        local = disseminated_at.astimezone(IST) if disseminated_at.tzinfo else disseminated_at
        day = local.date()
        return day if local.time() < available_after else day + timedelta(days=1)
    if board_meeting is not None:
        return board_meeting + timedelta(days=1)
    return None
