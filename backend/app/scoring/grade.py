"""Total score and grade (SPEC §7.3). Pure functions.

    total  = sum(w_p x score_p) / sum(w_p) over the pillars with a score. The weights are from
             ``scoring.weights`` and are renormalised over the available pillars, which is
             reported. If those carry less than ``total_min_weight_coverage`` of the weight in
             play, there is no total and no grade.
    grade  = A+ >= cutoff A_plus, A >= A, B >= B, C >= C, else D; then capped by knock-outs.

Resolving the MoS circularity (grade → MoS → zone → Valuation pillar → grade):
    1. Provisional grade: the total *without* the Valuation pillar (the other five weights
       renormalised), with knock-out caps applied.
    2. That grade picks the MoS (``valuation.mos_by_grade``), which gives the valuation
       (FV / zone) and so the Valuation pillar. The caller supplies this step as a function.
    3. Final grade: the total over all six pillars, with knock-out caps applied. The MoS stays
       the provisional grade's (no second pass), which is reported when the two grades differ.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from app.core.config import GradeCutoffs, ScoringConfig
from app.scoring.common import Grade, Pillar, worse
from app.scoring.knockouts import KnockoutResult
from app.scoring.pillars import PillarScore


@dataclass(frozen=True)
class TotalScore:
    value: float | None
    coverage: float  # share of the weight in play carried by available pillars
    effective_weights: dict[Pillar, float]
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GradeResult:
    grade: Grade | None
    score: float | None
    uncapped: Grade | None  # the grade before knock-out caps
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Grading:
    provisional: GradeResult
    final: GradeResult
    mos_grade: Grade | None  # the grade whose MoS was used (= provisional)
    pillars: dict[Pillar, PillarScore]
    reasons: list[str] = field(default_factory=list)


def total_score(
    pillars: Mapping[Pillar, PillarScore],
    cfg: ScoringConfig,
    *,
    exclude: frozenset[Pillar] = frozenset(),
) -> TotalScore:
    weights = {Pillar(k): float(v) for k, v in cfg.weights.model_dump().items()}
    in_play = {p: w for p, w in weights.items() if p not in exclude}
    avail = {
        p: w for p, w in in_play.items() if p in pillars and pillars[p].score is not None and w > 0
    }
    total_w = sum(in_play.values())
    covered = sum(avail.values())
    coverage = covered / total_w if total_w else 0.0
    eff = {p: w / covered for p, w in avail.items()} if covered else {}
    label = "total" if not exclude else f"total excluding {', '.join(sorted(exclude))}"
    missing = [p.value for p in in_play if p not in avail and in_play[p] > 0]
    reasons: list[str] = []
    if missing:
        reasons.append(
            f"{label}: no score for {', '.join(missing)}; weights renormalised over the rest "
            f"({coverage:.0%} of the weight)"
        )
    if not avail or coverage < cfg.total_min_weight_coverage:
        reasons.append(
            f"{label}: available pillars carry {coverage:.0%} of the weight "
            f"(< {cfg.total_min_weight_coverage:.0%}): no score"
        )
        return TotalScore(None, coverage, eff, reasons)
    value = sum(eff[p] * (pillars[p].score or 0.0) for p in avail)
    reasons.append(f"{label} {value:.1f}/100")
    return TotalScore(value, coverage, eff, reasons)


def grade_for(score: float, cutoffs: GradeCutoffs) -> Grade:
    for g in (Grade.A_PLUS, Grade.A, Grade.B, Grade.C):
        if score >= getattr(cutoffs, g.value):
            return g
    return Grade.D


def grade(
    pillars: Mapping[Pillar, PillarScore],
    ko: KnockoutResult,
    cfg: ScoringConfig,
    *,
    exclude: frozenset[Pillar] = frozenset(),
) -> GradeResult:
    total = total_score(pillars, cfg, exclude=exclude)
    reasons = list(total.reasons)
    if total.value is None:
        reasons.append("no grade: insufficient data")
        return GradeResult(None, None, None, reasons)
    raw = grade_for(total.value, cfg.grade_cutoffs)
    final = raw
    if ko.cap is not None:
        final = worse(raw, ko.cap)
        if final is not raw:
            reasons.append(
                f"grade {raw.label} capped at {final.label} "
                f"by knock-outs: {', '.join(ko.triggered)}"
            )
    reasons.append(f"grade {final.label} (score {total.value:.1f})")
    return GradeResult(final, total.value, raw, reasons)


def resolve_grade(
    non_valuation: Mapping[Pillar, PillarScore],
    ko: KnockoutResult,
    valuation_for: Callable[[Grade], PillarScore],
    cfg: ScoringConfig,
) -> Grading:
    """``valuation_for(provisional_grade)`` runs the valuation with that grade's MoS and returns
    the Valuation pillar."""
    pillars: dict[Pillar, PillarScore] = {
        p: s for p, s in non_valuation.items() if p is not Pillar.VALUATION
    }
    prov = grade(pillars, ko, cfg, exclude=frozenset({Pillar.VALUATION}))
    reasons = [f"provisional (ex-valuation): {r}" for r in prov.reasons[-1:]]
    if prov.grade is None:
        reasons.append("no provisional grade, so no MoS: valuation pillar and final grade skipped")
        final = GradeResult(None, None, None, ["no grade: provisional grade unavailable"])
        return Grading(prov, final, None, pillars, reasons)

    pillars[Pillar.VALUATION] = valuation_for(prov.grade)
    reasons.append(f"MoS from provisional grade {prov.grade.label}")
    final = grade(pillars, ko, cfg)
    if final.grade is not None and final.grade is not prov.grade:
        reasons.append(
            f"final grade {final.grade.label} differs from provisional {prov.grade.label}; "
            f"MoS kept at the provisional grade's (no second pass)"
        )
    reasons.append(final.reasons[-1])
    return Grading(prov, final, prov.grade, pillars, reasons)
