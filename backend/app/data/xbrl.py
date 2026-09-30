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
come from ``app.data.canonical`` only (``labels["nse"]`` and the ``XBRL_*`` tables).

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
from app.data.canonical import (
    CANONICAL_FIELDS,
    XBRL_BANK_EXTRA,
    XBRL_BANK_MARKER,
    XBRL_BANK_PCT,
    XBRL_BANK_STOCKS,
    XBRL_INFO,
    XBRL_MAGNITUDES,
    XBRL_SUMS,
    Table,
    fields_for,
    fiscal_year,
)
from app.db.enums import StatementType

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
_SCALE = {"cr": 1e-7, "cr_shares": 1e-7}  # absolute rupees / shares → crore


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
    if unit in _SCALE and (f.unit or "").upper() in ("INR", "SHARES"):
        v *= _SCALE[unit]
    elif unit == "pct" and (f.unit or "").lower() == "pure":
        v *= 100  # XBRL percentages are decimal fractions
    return v


def _pick(facts: Mapping[str, Fact], names: tuple[str, ...], unit: str) -> float | None:
    for name in names:
        f = facts.get(name)
        if f is not None and (v := _number(f, unit)) is not None:
            return v
    return None


def _sum(facts: Mapping[str, Fact], names: tuple[str, ...], unit: str) -> float | None:
    vals = [_number(facts[n], unit) for n in names if n in facts]
    reported = [v for v in vals if v is not None]
    return float(sum(reported)) if reported else None


def _add(*xs: float | None) -> float | None:
    return None if any(x is None for x in xs) else float(sum(x for x in xs if x is not None))


def canonical_record(facts: Mapping[str, Fact], table: Table) -> dict[str, Any]:
    """One period's facts → canonical record. Derivations are documented in
    ``CANONICAL_FIELDS[...].derived["nse"]``."""
    rec: dict[str, Any] = {}
    for name in fields_for(table):
        spec = CANONICAL_FIELDS[name]
        v = _pick(facts, tuple(lbl.text for lbl in spec.labels.get("nse", ())), spec.unit)
        if v is None and name in XBRL_SUMS:
            v = _sum(facts, XBRL_SUMS[name], spec.unit)
        if v is not None and name in XBRL_MAGNITUDES:
            v = abs(v)
        rec[name] = v
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
    extra: dict[str, float] = {}
    if XBRL_BANK_MARKER in facts:
        for key, names in XBRL_BANK_EXTRA.items():
            v = _pick(facts, names, "pct" if key in XBRL_BANK_PCT else "cr")
            if v is not None:
                extra[key] = v
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
) -> ResultsFiling:
    """A results filing → its quarter row and (for a Q4 / annual filing) its fiscal-year row.
    ``period_start`` / ``period_end`` (e.g. from the exchange's filing list) are used when the
    document does not state its reporting period."""
    inst = parse_instance(content)
    warnings: list[str] = []
    end = _date(inst.text(XBRL_INFO["period_end"])) or period_end
    start = _date(inst.text(XBRL_INFO["period_start"])) or period_start
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

    quarter = annual = None
    if q_ctx is not None:
        quarter = canonical_record(inst.plain(q_ctx.id), "fin_quarterly")
    if y_ctx is not None:
        facts = {**(inst.plain(bs_ctx.id) if bs_ctx else {}), **inst.plain(y_ctx.id)}
        annual = canonical_record(facts, "fin_annual")
        annual["fiscal_year"] = fiscal_year(datetime.combine(end, time()))
    if quarter is not None and not _has_pl(quarter):
        warnings.append("quarter context has no revenue, PBT or PAT; ignored")
        quarter = None
    if annual is not None and not _has_pl(annual):
        warnings.append("fiscal-year context has no revenue, PBT or PAT; ignored")
        annual = None
    if quarter is not None and annual is not None and quarter.get("extra"):
        extra = dict(annual.get("extra") or {})
        for key in XBRL_BANK_STOCKS & set(quarter["extra"]):
            extra.setdefault(key, quarter["extra"][key])
        annual["extra"] = extra or None
    if quarter is None and annual is None:
        raise XbrlFormatError(
            f"no quarter or fiscal-year results for the period ending {end.isoformat()}"
        )

    is_bank = any(f.name == XBRL_BANK_MARKER for f in inst.facts)
    nature = inst.text(XBRL_INFO["nature"])
    statement_type = statement_type_from_text(nature)
    if statement_type is None:
        warnings.append(f"standalone/consolidated not stated ({nature!r})")
    return ResultsFiling(
        company=inst.text(XBRL_INFO["company"]),
        symbol=inst.text(XBRL_INFO["symbol"]),
        scrip_code=inst.text(XBRL_INFO["scrip_code"]),
        isin=inst.text(XBRL_INFO["isin"]),
        statement_type=statement_type,
        audited=audited_from_text(inst.text(XBRL_INFO["audited"])),
        period_start=start or (q_ctx.start if q_ctx else None),
        period_end=end,
        board_meeting=_date(inst.text(XBRL_INFO["board_meeting"])),
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
    )


# ───────────────────────── inspection ─────────────────────────


def mapped_elements() -> set[str]:
    """Every element name the parser reads (canonical labels and the XBRL_* tables)."""
    names = {lbl.text for spec in CANONICAL_FIELDS.values() for lbl in spec.labels.get("nse", ())}
    for table in (XBRL_SUMS, XBRL_INFO, XBRL_BANK_EXTRA):
        names.update(n for group in table.values() for n in group)
    return names


def describe(content: bytes, cfg: NseResultsConfig) -> str:
    """Human-readable dump of a filing for checking the element mapping against real
    documents: what was read, from which contexts, and which numeric elements in those
    contexts the mapping does not use (candidates to add to ``app.data.canonical``)."""
    inst = parse_instance(content)
    filing = parse_results(content, cfg)
    used = {cid: role for role, cid in filing.contexts.items()}
    lines = [
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
