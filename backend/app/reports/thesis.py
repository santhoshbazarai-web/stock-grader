"""LLM thesis, the pure part (SPEC §8a): numbers in → text out, no new facts.

The report is turned into a **fact sheet** (labelled lines, numbers already in display units),
the fact sheet into a prompt, and the model's draft is **checked** before it is kept:

- every number in the draft must be one the fact sheet states (``number_rel_tolerance``, or
  the rounding of the number as written: "₹2,452" cites "₹2,452.36"). Signs are ignored and
  thousands separators (Western or Indian) are dropped. Dates must be ones the sheet states;
- no ``forbidden_phrases`` (targets, predictions, advice beyond the report's own action);
- it may not name a grade, multi-word action ("strong buy" when the report says Book Profits)
  or multi-word zone ("deep discount") that the facts don't;
- ``min_words`` ≤ length ≤ ``max_words``.

A draft that fails is retried with the problems listed (:func:`retry_prompt`). The checks catch
invented or altered numbers; they cannot catch numbers written as words or a wrong sign, so the
UI labels the text as machine-written from the figures on the page.

Nothing here does I/O: the model client and storage are in ``reports/thesis_service.py``.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import ThesisConfig, ThesisUnit
from app.reports.dto import StockReport
from app.scoring.decision import Action
from app.valuation.blend import Zone

# Bump when the fact sheet or the prompt changes: stored theses then no longer match.
PROMPT_VERSION = 1

_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])\d[\d,]*(?:\.\d+)?")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_GRADE = re.compile(r"\bgrade\s+(A\+|[A-D])(?![A-Za-z0-9+])", re.IGNORECASE)


@dataclass(frozen=True)
class Fact:
    label: str
    text: str


@dataclass(frozen=True)
class FactSheet:
    symbol: str
    facts: tuple[Fact, ...]
    grade_label: str | None
    action: str | None
    zone: str | None

    def block(self) -> str:
        return "\n".join(f"- {f.label}: {f.text}" for f in self.facts)

    def digest(self) -> str:
        """Identifies the exact facts (and prompt version) a thesis was written from."""
        doc = json.dumps([PROMPT_VERSION, self.symbol, [(f.label, f.text) for f in self.facts]])
        return hashlib.sha256(doc.encode()).hexdigest()


@dataclass(frozen=True)
class Check:
    ok: bool
    problems: list[str] = field(default_factory=list)


# ───────────────────────── fact sheet ─────────────────────────


def _n(v: float, dp: int) -> str:
    return f"{v:,.{dp}f}"


def fmt(value: float, unit: ThesisUnit) -> str:
    if unit == "inr":
        return f"₹{_n(value, 2)}"
    if unit == "pct":
        return f"{_n(value * 100, 1)}%"
    if unit == "x":
        return f"{_n(value, 2)}x"
    if unit == "days":
        return f"{_n(value, 0)} days"
    if unit == "cr":
        return f"₹{_n(value, 0)} crore"
    return _n(value, 0)


def _words(code: str | None) -> str | None:
    return code.replace("_", " ").title() if code else None


def fact_sheet(r: StockReport, cfg: ThesisConfig) -> FactSheet:
    facts: list[Fact] = []

    def add(label: str, value: float | None, unit: ThesisUnit) -> None:
        if value is not None:
            facts.append(Fact(label, fmt(value, unit)))

    def say(label: str, text: str | None) -> None:
        if text:
            facts.append(Fact(label, text))

    say("Company", f"{r.name} ({r.symbol})" if r.name else r.symbol)
    say("Sector (valuation model)", f"{r.valuation.sector} ({r.valuation.model})")
    say("Report date", r.as_of.isoformat())
    add("Current price (CMP)", r.cmp, "inr")
    lv = r.levels
    add("Baseline (floor)", lv.baseline, "inr")
    add("Fair value", lv.fair_value, "inr")
    add("Top band (premium ceiling)", lv.top_band, "inr")
    say("Valuation confidence", lv.confidence)
    add("Margin of safety required", lv.mos_pct, "pct")
    say("Zone", _words(r.zone))
    say("Grade", r.grade_label)
    say("Action", _words(r.action))
    bz = r.buy_zone
    if bz is not None and bz.status == "zone" and bz.low is not None and bz.high is not None:
        say("Buy zone", f"{fmt(bz.low, 'inr')} to {fmt(bz.high, 'inr')}")
    elif bz is not None:
        say("Buy zone", f"none ({bz.reasons[0]})" if bz.reasons else "none")
    add("Invalidation level", r.invalidation, "inr")
    for pillar in ("quality", "growth", "valuation", "health", "governance", "technical", "total"):
        score = getattr(r.scores, pillar)
        if score is not None:
            facts.append(Fact(f"{pillar.title()} score", f"{_n(score, 0)} out of 100"))
    ep = r.earned_premium_detail
    say("Earned premium", f"{ep.score} of {ep.max_possible} conditions met")
    for m in r.valuation.methods:
        add(f"Valuation method {m.name}", m.value, "inr")
    rd = r.valuation.reverse_dcf
    if rd is not None:
        add("Growth implied by the price (reverse DCF)", rd.implied_growth, "pct")
        add("Historical growth", rd.hist_growth, "pct")
    t = r.technical
    if t.stage is not None:
        say("Technical stage", f"Stage {t.stage}")
    say("Trend", t.trend)
    add("Relative-strength percentile", t.rs_percentile, "count")
    add("Distance from 52-week high", t.from_52w_high, "pct")
    for key, spec in cfg.fundamentals.items():
        add(spec.label, r.fundamentals.get(key), spec.unit)
    say("Red flags", "; ".join(r.red_flags) if r.red_flags else "none")
    if r.knockouts.triggered:
        say("Knock-outs triggered", "; ".join(r.knockouts.triggered))
    if r.reconciliation_issues:
        say("Open source differences", "; ".join(r.reconciliation_issues))
    for reason in [*r.decision.reasons, *r.reasons][: cfg.max_reasons]:
        say("Reason", reason)
    return FactSheet(r.symbol, tuple(facts), r.grade_label, r.action, r.zone)


# ───────────────────────── prompt ─────────────────────────


def build_prompt(sheet: FactSheet, cfg: ThesisConfig) -> str:
    return (
        "You write a short investment thesis for one Indian listed stock, for the owner of a "
        "personal stock-grading tool. Use ONLY the facts below.\n"
        "Rules:\n"
        f"- One paragraph of plain English, {cfg.min_words} to {cfg.max_words} words.\n"
        "- Every number you write must appear in the facts, written the same way. Do not "
        "calculate, round differently, convert units or add any other number or date.\n"
        "- Do not add facts about the company, its business, management, news or peers.\n"
        "- Do not predict prices or give targets. State the report's action as given; do not "
        "recommend a different one.\n"
        "- Explain why the report reaches its grade, zone and action, and name the main risk "
        "the facts show.\n"
        "- Output only the paragraph: no title, no list, no disclaimer.\n\n"
        f"FACTS\n{sheet.block()}\n"
    )


def retry_prompt(sheet: FactSheet, cfg: ThesisConfig, draft: str, problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        build_prompt(sheet, cfg)
        + f"\nYour previous draft was rejected:\n{listed}\n\nPrevious draft:\n{draft}\n\n"
        "Write the paragraph again, fixing every problem.\n"
    )


# ───────────────────────── check ─────────────────────────


def _numbers(text: str) -> list[tuple[float, int, str]]:
    """(value, decimals, as written) for each number, dates removed."""
    out = []
    for m in _NUMBER.finditer(_DATE.sub(" ", text)):
        raw = m.group().rstrip(",")
        clean = raw.replace(",", "")
        decimals = len(clean.split(".")[1]) if "." in clean else 0
        out.append((float(clean), decimals, raw))
    return out


def _cited(value: float, decimals: int, allowed: list[float], rel_tol: float) -> bool:
    half_unit = 0.5 * 10.0**-decimals
    return any(abs(value - f) <= max(rel_tol * abs(f), half_unit) + 1e-9 for f in allowed)


def check(text: str, sheet: FactSheet, cfg: ThesisConfig) -> Check:
    problems: list[str] = []
    block = sheet.block()
    words = len(text.split())
    if words < cfg.min_words or words > cfg.max_words:
        problems.append(f"{words} words; write {cfg.min_words} to {cfg.max_words}")

    allowed = [v for v, _, _ in _numbers(block)]
    tol = cfg.number_rel_tolerance
    unknown = [raw for v, d, raw in _numbers(text) if not _cited(v, d, allowed, tol)]
    if unknown:
        problems.append("numbers not in the facts: " + ", ".join(dict.fromkeys(unknown)))
    dates = set(_DATE.findall(block))
    bad_dates = [d for d in _DATE.findall(text) if d not in dates]
    if bad_dates:
        problems.append("dates not in the facts: " + ", ".join(dict.fromkeys(bad_dates)))

    low = text.lower()
    said = [p for p in cfg.forbidden_phrases if p.lower() in low]
    if said:
        problems.append("forbidden phrases: " + ", ".join(said))

    # grades, actions and zones: only those the facts name (the reasons may cite a provisional
    # grade or the zone boundary), so the text cannot contradict the report
    named = block.lower()
    grades = {g.upper() for g in _GRADE.findall(text)} - {g.upper() for g in _GRADE.findall(block)}
    if grades:
        problems.append(f"grade is {sheet.grade_label or 'not stated'}; the text names grade "
                        + ", ".join(sorted(grades)))  # fmt: skip
    others = [a.label for a in Action if "_" in a.value and _spoken(a.value) in low
              and _spoken(a.value) not in named]  # fmt: skip
    if others:
        problems.append(f"action is {_words(sheet.action)}; the text names " + ", ".join(others))
    zones = [_words(z.value) or "" for z in Zone if "_" in z.value and _spoken(z.value) in low
             and _spoken(z.value) not in named]  # fmt: skip
    if zones:
        problems.append(f"zone is {_words(sheet.zone)}; the text names " + ", ".join(zones))
    return Check(not problems, problems)


def _spoken(code: str) -> str:
    return code.replace("_", " ")


def clean_draft(raw: str) -> str:
    """Model output → one paragraph: strip whitespace, surrounding quotes and line breaks."""
    text = " ".join(raw.split())
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def sheet_summary(sheet: FactSheet) -> dict[str, Any]:
    """The facts as stored with a thesis (what the text was checked against)."""
    return {"symbol": sheet.symbol, "facts": [[f.label, f.text] for f in sheet.facts]}
