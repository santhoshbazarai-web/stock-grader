"""Decision matrix (SPEC §7.5). Pure function.

The grade x zone table lives in ``scoring.yaml`` (``decision.matrix``); each cell names a rule:

    strong_buy             Strong Buy, always with the "Why is it cheap?" checklist
    buy_or_accumulate      Buy if CMP is inside the technical buy zone, else Accumulate
    buy_on_pullback        Buy on Pullback, targeting the technical buy zone
    momentum_or_wait       Momentum Entry if earned premium >= momentum_entry_min, else Wait
    hold_if_owned          Hold if owned; don't initiate
    buy_with_confirmation  Buy if the stage or trend is in ``decision.confirmation``, else Wait
    accumulate_slowly      Accumulate (slowly, in small tranches)
    value_trap_check       Wait, with the value-trap checklist
    watch                  Wait (watch list)
    wait / avoid / book_profits

Stage 4 override: any buying action (Strong Buy, Buy, Accumulate, Buy on Pullback, Momentum
Entry) becomes Wait with the reason "downtrend — wait for Stage 1 base". Checklists stay.

No grade → no action. No zone → an action only when the grade's whole row is one rule (D).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.config import DecisionRule, ScoringConfig
from app.scoring.common import Grade
from app.valuation.blend import Zone

STAGE4_REASON = "downtrend — wait for Stage 1 base"


class Action(StrEnum):
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    ACCUMULATE = "accumulate"
    BUY_ON_PULLBACK = "buy_on_pullback"
    MOMENTUM_ENTRY = "momentum_entry"
    WAIT = "wait"
    HOLD = "hold"
    BOOK_PROFITS = "book_profits"
    AVOID = "avoid"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


BUY_ACTIONS = frozenset(
    {
        Action.STRONG_BUY,
        Action.BUY,
        Action.ACCUMULATE,
        Action.BUY_ON_PULLBACK,
        Action.MOMENTUM_ENTRY,
    }
)


@dataclass(frozen=True)
class DecisionInputs:
    grade: Grade | None
    zone: Zone | None
    cmp: float
    earned_premium: int | None = None
    earned_premium_out_of: int = 8  # conditions scored (banks: 10)
    stage: int | None = None
    trend: str | None = None
    buy_zone: tuple[float, float] | None = None  # (low, high) when there is a technical zone


@dataclass(frozen=True)
class Decision:
    action: Action | None
    rule: DecisionRule | None
    overridden: bool = False  # Stage 4 turned a buy into Wait
    checklist: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _zone_text(bz: tuple[float, float] | None) -> str:
    return f"buy zone {bz[0]:,.2f}-{bz[1]:,.2f}" if bz else "no technical buy zone yet"


def apply_rule(
    rule: DecisionRule, x: DecisionInputs, cfg: ScoringConfig
) -> tuple[Action, list[str], list[str]]:
    """(action, checklist, reasons) for one matrix cell, before the Stage 4 override."""
    d = cfg.decision
    match rule:
        case DecisionRule.STRONG_BUY:
            return (
                Action.STRONG_BUY,
                list(d.checklists.why_cheap),
                ["deep discount on a top-grade stock: check why it is cheap first"],
            )
        case DecisionRule.BUY_OR_ACCUMULATE:
            bz = x.buy_zone
            if bz is not None and bz[0] <= x.cmp <= bz[1]:
                return Action.BUY, [], [f"CMP {x.cmp:,.2f} inside the {_zone_text(bz)}"]
            where = f"CMP {x.cmp:,.2f} outside the {_zone_text(bz)}" if bz else _zone_text(bz)
            return Action.ACCUMULATE, [], [f"{where}: accumulate in tranches"]
        case DecisionRule.BUY_ON_PULLBACK:
            target = f"the {_zone_text(x.buy_zone)}" if x.buy_zone else "support (no zone yet)"
            return Action.BUY_ON_PULLBACK, [], [f"fairly valued: buy on a pullback to {target}"]
        case DecisionRule.MOMENTUM_OR_WAIT:
            need = math.ceil(cfg.earned_premium.momentum_entry_min * x.earned_premium_out_of / 8)
            ep = x.earned_premium
            if ep is not None and ep >= need:
                return Action.MOMENTUM_ENTRY, [], [f"premium is earned: EP {ep} >= {need}"]
            shown = "unknown" if ep is None else str(ep)
            return Action.WAIT, [], [f"premium not earned: EP {shown} < {need}"]
        case DecisionRule.HOLD_IF_OWNED:
            return Action.HOLD, [], ["extreme premium: hold if owned; don't initiate"]
        case DecisionRule.BUY_WITH_CONFIRMATION:
            conf = d.confirmation
            ok_stage = x.stage is not None and x.stage in conf.stages
            ok_trend = x.trend is not None and x.trend in conf.trends
            if ok_stage or ok_trend:
                why = f"Stage {x.stage}" if ok_stage else f"{x.trend} trend"
                return Action.BUY, [], [f"deep discount with technical confirmation ({why})"]
            return (
                Action.WAIT,
                [],
                [
                    f"deep discount, awaiting technical confirmation (now {_now(x)}): needs "
                    f"{_needs(conf.stages, conf.trends)}"
                ],
            )
        case DecisionRule.ACCUMULATE_SLOWLY:
            return Action.ACCUMULATE, [], ["discount: accumulate slowly, in small tranches"]
        case DecisionRule.VALUE_TRAP_CHECK:
            return (
                Action.WAIT,
                list(d.checklists.value_trap),
                ["cheap but low grade: run the value-trap check before any position"],
            )
        case DecisionRule.WATCH:
            return Action.WAIT, [], ["discount on a low grade: watch list only"]
        case DecisionRule.WAIT:
            return Action.WAIT, [], ["no edge at this grade and zone: wait"]
        case DecisionRule.AVOID:
            return Action.AVOID, [], ["grade and zone do not justify a position: avoid"]
        case DecisionRule.BOOK_PROFITS:
            return Action.BOOK_PROFITS, [], ["extreme premium on a mid grade: book profits"]


_TREND = {"up": "an uptrend", "down": "a downtrend", "range": "a trading range"}


def _or(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} or {parts[-1]}"


def _needs(stages: Sequence[int], trends: Sequence[str]) -> str:
    """``[2], ["up"]`` → "Stage 2 or an uptrend"."""
    parts = []
    if stages:
        parts.append("Stage " + _or([str(s) for s in stages]))
    parts += [_TREND.get(t, f"a {t} trend") for t in trends]
    return _or(parts) if parts else "nothing configured"


def _now(x: "DecisionInputs") -> str:
    stage = f"Stage {x.stage}" if x.stage is not None else "stage unknown"
    trend = _TREND.get(x.trend, x.trend) if x.trend is not None else "trend unknown"
    return f"{stage}, {trend}"


def decide(x: DecisionInputs, cfg: ScoringConfig) -> Decision:
    if x.grade is None:
        return Decision(None, None, reasons=["no action: grade unavailable"])
    row = cfg.decision.matrix[x.grade.value]
    if x.zone is None:
        rules = set(row.values())
        if len(rules) != 1:
            return Decision(None, None, reasons=["no action: valuation zone unavailable"])
        rule = rules.pop()
        where = f"grade {x.grade.label} (every zone)"
    else:
        rule = row[x.zone.value]
        where = f"grade {x.grade.label} x {x.zone.value.replace('_', ' ')}"
    action, checklist, reasons = apply_rule(rule, x, cfg)
    reasons = [f"{where} → {rule.value.replace('_', ' ')}", *reasons]
    overridden = False
    if x.stage == 4 and action in BUY_ACTIONS:
        reasons.append(f"{action.label} overridden to Wait: {STAGE4_REASON}")
        action, overridden = Action.WAIT, True
    reasons.append(f"action: {action.label}")
    return Decision(action, rule, overridden, checklist, reasons)
