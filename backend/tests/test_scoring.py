"""Scoring (SPEC §7): maps, knock-outs, pillars, grade + provisional-grade resolution, earned
premium, decision matrix. Expected numbers are hand-computed in the comments."""

from dataclasses import replace
from datetime import date

import pytest

from app.core.config import DecisionRule, load_config
from app.scoring.common import Grade, Pillar, map_score, worse
from app.scoring.decision import STAGE4_REASON, Action, DecisionInputs, decide
from app.scoring.earned_premium import EarnedPremiumInputs, earned_premium
from app.scoring.grade import grade, grade_for, resolve_grade, total_score
from app.scoring.knockouts import KnockoutInputs, KnockoutResult, knockouts
from app.scoring.pillars import (
    PillarInputs,
    PillarScore,
    governance,
    growth,
    health,
    non_valuation_pillars,
    quality,
    technical,
    valuation_pillar,
)
from app.valuation.blend import Zone, blend
from tests.conftest import REPO_CONFIG_DIR

CFG = load_config(REPO_CONFIG_DIR)
S = CFG.scoring
KO = S.knockouts
NO_KO = KnockoutResult(None)

# ───────────────────────── score maps ─────────────────────────


@pytest.mark.parametrize(
    ("name", "x", "expected"),
    [
        # roce_5y_avg [[0.08,0],[0.12,40],[0.18,75],[0.25,100]]: 0.15 → 40 + 35 x 3/6 = 57.5
        ("roce_5y_avg", 0.15, 57.5),
        ("roce_5y_avg", 0.12, 40.0),  # on a point
        ("roce_5y_avg", 0.05, 0.0),  # clamped low
        ("roce_5y_avg", 0.40, 100.0),  # clamped high
        # debt_to_equity is decreasing [[1.5,0],[1.0,30],[0.5,70],[0.1,100]]:
        # 0.75 → 30 + 40 x 0.25/0.5 = 50
        ("debt_to_equity", 0.75, 50.0),
        ("debt_to_equity", 3.0, 0.0),
        ("debt_to_equity", 0.0, 100.0),
        ("interest_coverage", float("inf"), 100.0),  # debt-free → best score
    ],
)
def test_map_score(name: str, x: float, expected: float) -> None:
    assert map_score(getattr(S.maps, name), x) == pytest.approx(expected)


def test_map_score_missing_is_none() -> None:
    assert map_score(S.maps.roce_5y_avg, None) is None
    assert map_score(S.maps.roce_5y_avg, float("nan")) is None


def test_grade_ordering() -> None:
    assert worse(Grade.A, Grade.C) is Grade.C and worse(Grade.D, Grade.C) is Grade.D
    assert Grade.A_PLUS.label == "A+" and Grade.B.label == "B"


# ───────────────────────── knock-outs ─────────────────────────

AS_OF = date(2024, 6, 30)
CLEAN = KnockoutInputs(
    as_of=AS_OF,
    pledge_pct=2.0,
    cfo_history=[100, 120, -5, 130, 150],
    auditor_resignations=[],
    on_asm_gsm=False,
    mcap_cr=20_000,
    avg_traded_value_cr_20d=40,
    beneish_m=-2.6,
)


def test_clean_stock_has_no_knockouts() -> None:
    r = knockouts(CLEAN, KO)
    assert r.cap is None and r.triggered == [] and r.unknown == []
    assert r.reasons == ["no knock-outs triggered"]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"pledge_pct": 10.5}, "pledge"),  # > 10
        ({"cfo_history": [-1, 5, -2, 7, -3]}, "negative_cfo"),  # 3 of 5
        ({"auditor_resignations": [date(2023, 1, 15)]}, "auditor"),  # within 2 years
        ({"on_asm_gsm": True}, "asm_gsm"),
        ({"mcap_cr": 499.0}, "mcap"),  # < 500 Cr
        ({"avg_traded_value_cr_20d": 4.99}, "liquidity"),  # < 5 Cr
        ({"beneish_m": -1.5}, "beneish"),  # > -1.78
    ],
)
def test_each_knockout_caps_at_c(change: dict[str, object], code: str) -> None:
    r = knockouts(replace(CLEAN, **change), KO)  # type: ignore[arg-type]
    assert r.triggered == [code] and r.cap is Grade.C
    assert any(s.startswith("knock-out:") for s in r.reasons)


@pytest.mark.parametrize(
    ("change", "triggered"),
    [
        ({"pledge_pct": 10.0}, False),  # not > 10
        ({"mcap_cr": 500.0}, False),
        ({"beneish_m": -1.78}, False),
        ({"auditor_resignations": [date(2022, 6, 29)]}, False),  # just over 2 years ago
        ({"auditor_resignations": [date(2022, 6, 30)]}, True),  # exactly 2 years: inside
    ],
)
def test_knockout_boundaries(change: dict[str, object], triggered: bool) -> None:
    r = knockouts(replace(CLEAN, **change), KO)  # type: ignore[arg-type]
    assert bool(r.triggered) is triggered


@pytest.mark.parametrize(
    ("history", "outcome"),
    [
        ([-1, -1, -1, 5, 5], "hit"),
        ([-1, -1, None, None, 5], "unknown"),  # 2 negative + 2 unknown could still reach 3
        ([-1, 5, 5, 5, None], "pass"),  # 1 negative + 1 unknown can't reach 3
        ([-1, -1, -1], "hit"),  # only 3 years, all negative: already 3
        ([-1, 2, 3], "unknown"),  # 2 years missing from the window
        ([-9, -9, 1, 1, 1, 1, 1], "pass"),  # only the last 5 years count
        (None, "unknown"),
    ],
)
def test_negative_cfo_with_partial_history(
    history: list[float | None] | None, outcome: str
) -> None:
    r = knockouts(replace(CLEAN, cfo_history=history), KO)
    assert ("negative_cfo" in r.triggered) is (outcome == "hit")
    assert ("negative_cfo" in r.unknown) is (outcome == "unknown")


def test_negative_cfo_does_not_apply_to_lenders() -> None:
    r = knockouts(replace(CLEAN, cfo_history=None, cfo_applies=False), KO)
    assert "negative_cfo" not in r.unknown and "negative_cfo" not in r.triggered
    assert "knock-out check 'negative_cfo' not applicable to a lender / insurer" in r.reasons
    hit = knockouts(replace(CLEAN, cfo_history=[-1, -1, -1, -1, -1], cfo_applies=False), KO)
    assert hit.triggered == []


def test_missing_data_is_unknown_not_pass_or_fail() -> None:
    r = knockouts(KnockoutInputs(as_of=AS_OF), KO)
    assert r.cap is None and r.triggered == []
    assert set(r.unknown) == {
        "pledge", "negative_cfo", "auditor", "asm_gsm", "mcap", "liquidity", "beneish"
    }  # fmt: skip
    assert r.reasons[-1] == "no knock-outs triggered (7 unknown)"


def test_asm_check_can_be_disabled_and_leap_day_as_of() -> None:
    off = KO.model_copy(update={"on_asm_gsm": False})
    assert knockouts(replace(CLEAN, on_asm_gsm=True), off).cap is None
    r = knockouts(
        replace(CLEAN, as_of=date(2024, 2, 29), auditor_resignations=[date(2022, 3, 1)]), KO
    )
    assert r.triggered == ["auditor"]  # cutoff 2022-02-28


# ───────────────────────── pillars ─────────────────────────

GOOD = PillarInputs(
    roce_5y_avg=0.15,
    roce_latest=0.20,
    roce_prior=0.18,
    cfo_to_ebitda_5y=0.75,
    fcf_conversion_5y=0.65,
    piotroski=6,
    sales_cagr_5y=0.15,
    eps_cagr_5y=0.18,
    eps_yoy_ttm=0.20,
    eps_yoy_q_latest=0.25,
    eps_yoy_q_prev=0.15,
    debt_to_equity=0.5,
    interest_coverage=6,
    net_debt_to_ebitda=1.0,
    ccc_days=40,
    ccc_days_prior=55,
    altman_z2=2.6,
    pledge_pct=0,
    promoter_change_qoq_pp=0,
    institutional_change_qoq_pp=1,
    other_income_share=0.10,
    rpt_flagged=False,
    stage=2,
    rs_percentile=65,
    trend="up",
    delivery_ratio=1.15,
)


def test_quality_pillar() -> None:
    # roce_5y_avg 0.15 → 57.5; roce_trend 0.20-0.18 = 0.02 → 50 + 50 x 0.02/0.05 = 70
    # cfo_to_ebitda 0.75 → 80; fcf_conversion 0.65 → 50 + 35 x 0.15/0.30 = 67.5
    # piotroski 6 → 40 + 40 x 1/2 = 60.   mean = (57.5 + 70 + 80 + 67.5 + 60) / 5 = 67.0
    q = quality(GOOD, S)
    assert q.score == pytest.approx(67.0)
    assert [s.score for s in q.subs] == pytest.approx([57.5, 70, 80, 67.5, 60])
    assert q.missing == [] and q.reasons[-1] == "quality 67/100 from 5 of 5 sub-metrics"


def test_growth_pillar() -> None:
    # sales_cagr 0.15 → 75; eps_cagr 0.18 → 75; eps_yoy_ttm 0.20 → 80;
    # acceleration 0.25-0.15 = 0.10 → 50 + 50 x 0.10/0.20 = 75.   mean = 305/4 = 76.25
    assert growth(GOOD, S).score == pytest.approx(76.25)


def test_health_pillar() -> None:
    # D/E 0.5 → 70; ICR 6 → 80; ND/EBITDA 1.0 → 80; CCC trend 40-55 = -15 → 50 + 50 x 15/30
    # = 75; Z'' 2.6 → 70.   mean = 375/5 = 75
    assert health(GOOD, S).score == pytest.approx(75.0)


def test_governance_pillar_and_rpt_flag() -> None:
    # pledge 0 → 100; promoter 0 pp → 60; institutions +1 pp → 75; other income 0.10 → between
    # (0.20, 50) and (0.05, 100): 50 + 50 x 0.10/0.15 = 83.33; RPT clean → 100
    # mean = (100 + 60 + 75 + 83.333 + 100) / 5 = 83.667
    assert governance(GOOD, S).score == pytest.approx(83.6667, abs=1e-3)
    flagged = governance(replace(GOOD, rpt_flagged=True), S)
    assert flagged.subs[-1].score == 0 and flagged.score == pytest.approx(63.6667, abs=1e-3)


def test_technical_pillar() -> None:
    # Stage 2 → 100; RS 65 → 40 + 45 x 15/30 = 62.5; trend up → 100; delivery 1.15 → 75
    # mean = 337.5 / 4 = 84.375
    t = technical(GOOD, S)
    assert t.score == pytest.approx(84.375)
    assert t.subs[0].reason == "stage Stage 2 → 100"


def test_bank_pillars_use_bank_maps() -> None:
    # ROA 1.3% → 50 + 35 x 0.3/0.6 = 67.5; NIM 3.5% → 67.5 → quality 67.5
    # GNPA 2.25% → 50 + 35 x 0.75/1.5 = 67.5; CAR 15.5% → 50 + 35 x 1.5/3 = 67.5 → health 67.5
    bank = PillarInputs(is_bank=True, roa_pct=1.3, nim_pct=3.5, gnpa_pct=2.25, car_pct=15.5)
    assert quality(bank, S).score == pytest.approx(67.5)
    assert health(bank, S).score == pytest.approx(67.5)
    assert [s.name for s in quality(bank, S).subs] == ["roa_pct", "nim_pct"]


def test_pillar_needs_coverage_and_lists_missing() -> None:
    # 3 of 5 available (60% >= 50%): mean of the three
    q = quality(PillarInputs(roce_5y_avg=0.15, cfo_to_ebitda_5y=0.75, piotroski=6), S)
    assert q.score == pytest.approx((57.5 + 80 + 60) / 3)
    assert q.missing == ["roce_trend", "fcf_conversion_5y"]
    # 2 of 5 (40% < 50%): no score, never a default
    q2 = quality(PillarInputs(roce_5y_avg=0.15, piotroski=6), S)
    assert q2.score is None and "no score" in q2.reasons[-1]
    assert technical(PillarInputs(), S).score is None


def test_valuation_pillar() -> None:
    # (FV - CMP)/FV = (100 - 80)/100 = 0.20 → 80 + 20 x 0.05/0.15 = 86.667
    # reverse-DCF gap 0.10 - 0.15 = -0.05 → 80.   mean = 83.333
    v = valuation_pillar(cmp=80, fair_value=100, implied_growth=0.10, hist_growth=0.15, cfg=S)
    assert v.pillar is Pillar.VALUATION and v.score == pytest.approx(83.3333, abs=1e-3)
    no_fv = valuation_pillar(cmp=80, fair_value=None, implied_growth=0.10, hist_growth=0.15, cfg=S)
    assert no_fv.score == pytest.approx(80) and no_fv.missing == ["discount_to_fv"]


# ───────────────────────── total + grade ─────────────────────────


def fixed(**scores: float | None) -> dict[Pillar, PillarScore]:
    return {
        Pillar(k): PillarScore(Pillar(k), v, [], reasons=[f"{k} fixed"]) for k, v in scores.items()
    }


SIX = fixed(quality=80, growth=70, valuation=60, health=90, governance=50, technical=40)


def test_total_score_weighted() -> None:
    # (25x80 + 20x70 + 20x60 + 15x90 + 10x50 + 10x40) / 100 = 6850/100 = 68.5
    t = total_score(SIX, S)
    assert t.value == pytest.approx(68.5) and t.coverage == 1.0
    assert t.reasons == ["total 68.5/100"]


def test_total_excluding_valuation_renormalises() -> None:
    # (2000 + 1400 + 1350 + 500 + 400) / 80 = 70.625
    t = total_score(SIX, S, exclude=frozenset({Pillar.VALUATION}))
    assert t.value == pytest.approx(70.625)
    assert t.effective_weights[Pillar.QUALITY] == pytest.approx(25 / 80)


def test_total_with_missing_pillar_and_min_coverage() -> None:
    # governance missing: (6850 - 500) / 90 = 70.556, coverage 90%
    t = total_score({**SIX, Pillar.GOVERNANCE: PillarScore(Pillar.GOVERNANCE, None, [])}, S)
    assert t.value == pytest.approx(6350 / 90) and t.coverage == pytest.approx(0.9)
    assert "no score for governance" in t.reasons[0]
    # only health + governance + technical = 35% < 70%: no total
    t2 = total_score(fixed(health=90, governance=50, technical=40), S)
    assert t2.value is None and "no score" in t2.reasons[-1]


@pytest.mark.parametrize(
    ("score", "expected"),
    [(100, Grade.A_PLUS), (85, Grade.A_PLUS), (84.99, Grade.A), (75, Grade.A), (74.9, Grade.B),
     (60, Grade.B), (59.9, Grade.C), (45, Grade.C), (44.99, Grade.D), (0, Grade.D)],
)  # fmt: skip
def test_grade_cutoffs(score: float, expected: Grade) -> None:
    assert grade_for(score, S.grade_cutoffs) is expected


def test_knockout_cap_applies_only_downwards() -> None:
    all90 = fixed(quality=90, growth=90, valuation=90, health=90, governance=90, technical=90)
    capped = grade(all90, KnockoutResult(Grade.C, ["pledge"]), S)
    assert capped.grade is Grade.C and capped.uncapped is Grade.A_PLUS
    assert "capped at C by knock-outs: pledge" in capped.reasons[-2]
    all10 = fixed(quality=10, growth=10, valuation=10, health=10, governance=10, technical=10)
    assert grade(all10, KnockoutResult(Grade.C, ["pledge"]), S).grade is Grade.D


# ───────────────────────── provisional grade (MoS circularity) ─────────────────────────


def test_provisional_grade_excludes_valuation_and_picks_mos() -> None:
    # Provisional over five pillars at 90 → 90 → A+. Valuation pillar 10 →
    # final (80 x 90 + 20 x 10) / 100 = 74 → B. The MoS stays A+'s.
    five = fixed(quality=90, growth=90, health=90, governance=90, technical=90)
    calls: list[Grade] = []

    def valuation_for(g: Grade) -> PillarScore:
        calls.append(g)
        return PillarScore(Pillar.VALUATION, 10.0, [])

    r = resolve_grade(five, NO_KO, valuation_for, S)
    assert calls == [Grade.A_PLUS] and r.mos_grade is Grade.A_PLUS
    assert r.provisional.grade is Grade.A_PLUS and r.provisional.score == pytest.approx(90)
    assert r.final.grade is Grade.B and r.final.score == pytest.approx(74)
    assert any("differs from provisional A+" in s for s in r.reasons)
    assert r.pillars[Pillar.VALUATION].score == 10.0


def test_provisional_grade_is_capped_before_picking_mos() -> None:
    five = fixed(quality=90, growth=90, health=90, governance=90, technical=90)
    calls: list[Grade] = []

    def valuation_for(g: Grade) -> PillarScore:
        calls.append(g)
        return PillarScore(Pillar.VALUATION, 90.0, [])

    r = resolve_grade(five, KnockoutResult(Grade.C, ["mcap"]), valuation_for, S)
    assert calls == [Grade.C] and r.final.grade is Grade.C and r.final.uncapped is Grade.A_PLUS


def test_no_provisional_grade_skips_valuation() -> None:
    def valuation_for(g: Grade) -> PillarScore:  # pragma: no cover - must not be called
        raise AssertionError("called")

    r = resolve_grade(fixed(technical=90), NO_KO, valuation_for, S)
    assert r.provisional.grade is None and r.final.grade is None and r.mos_grade is None
    assert "no MoS" in r.reasons[-1]


def test_resolve_grade_with_real_blend() -> None:
    # Five pillars from GOOD: quality 67, growth 76.25, health 75, governance 83.667,
    # technical 84.375. Ex-valuation total = (25x67 + 20x76.25 + 15x75 + 10x83.667
    # + 10x84.375) / 80 = (1675 + 1525 + 1125 + 836.67 + 843.75) / 80 = 75.068 → A.
    # blend with grade A → MoS = mos_by_grade.A; FV = 0.45x1000 + 0.30x900 + 0.25x1100
    # = 995; MoS(A) 0.15 → discount edge 845.75; baseline = bear DCF 700 → CMP 800 is Discount.
    sector = CFG.sectors.root["it_services"]
    seen: dict[str, object] = {}

    def valuation_for(g: Grade) -> PillarScore:
        v = blend(
            cmp=800,
            sector=sector,
            method_values={"dcf_base": 1000, "band_pe": 900, "relative": 1100},
            provisional_grade=g.value,
            config=CFG.valuation,
            bear_dcf=700,
        )
        seen["valuation"] = v
        return valuation_pillar(
            cmp=800, fair_value=v.fair_value, implied_growth=0.12, hist_growth=0.15, cfg=S
        )

    r = resolve_grade(non_valuation_pillars(GOOD, S), NO_KO, valuation_for, S)
    assert r.provisional.score == pytest.approx(75.068, abs=1e-3) and r.mos_grade is Grade.A
    v = seen["valuation"]
    assert v.mos_pct == CFG.valuation.mos_by_grade.A  # type: ignore[attr-defined]
    assert v.fair_value == pytest.approx(995) and v.zone is Zone.DISCOUNT  # type: ignore[attr-defined]
    assert r.final.grade is not None and r.final.reasons


# ───────────────────────── earned premium ─────────────────────────

EP_ALL = EarnedPremiumInputs(
    implied_growth=0.10,
    hist_growth_5y=0.15,
    eps_yoy_q_latest=0.30,
    eps_yoy_q_prev=0.20,
    roce_latest=0.22,
    roce_prior=0.18,
    opm_latest=0.21,
    opm_prev_year=0.19,
    sales_growth_yoy=0.20,
    institutional_change_qoq_pp=0.5,
    promoter_change_qoq_pp=0.0,
    pledge_pct=0.0,
    pledge_pct_prev_quarter=0.0,
    rs_percentile=85,
    stage=2,
    from_52w_high=-0.05,
)


def test_earned_premium_all_met() -> None:
    ep = earned_premium(EP_ALL, S)
    assert ep.score == 8 and ep.unknown == [] and ep.reasons[-1] == "earned premium 8/8"
    assert all(c.met for c in ep.conditions)


@pytest.mark.parametrize(
    ("change", "code", "met"),
    [
        ({"implied_growth": 0.15}, "implied_growth", True),  # equal: <= holds
        ({"implied_growth": 0.16}, "implied_growth", False),
        ({"eps_yoy_q_latest": 0.20}, "eps_acceleration", False),  # equal: not accelerating
        ({"roce_latest": 0.18}, "roce_up", False),
        ({"sales_growth_yoy": 0.15}, "operating_leverage", False),  # needs > 15%
        ({"opm_latest": 0.19}, "operating_leverage", False),
        ({"institutional_change_qoq_pp": 0.0}, "institutions_up", False),
        ({"promoter_change_qoq_pp": -0.1}, "promoter_steady", False),
        ({"pledge_pct": 1.0}, "promoter_steady", False),  # new pledge
        ({"rs_percentile": 80}, "rs_leader", True),  # >= 80
        ({"rs_percentile": 79.9}, "rs_leader", False),
        ({"from_52w_high": -0.10}, "stage2_near_high", True),  # exactly 10%
        ({"from_52w_high": -0.11}, "stage2_near_high", False),
        ({"stage": 1}, "stage2_near_high", False),
    ],
)
def test_earned_premium_conditions(change: dict[str, object], code: str, met: bool) -> None:
    ep = earned_premium(replace(EP_ALL, **change), S)  # type: ignore[arg-type]
    cond = next(c for c in ep.conditions if c.code == code)
    assert cond.met is met
    assert ep.score == 8 - (0 if met else 1)


def test_earned_premium_unknowns_are_listed_not_scored() -> None:
    ep = earned_premium(EarnedPremiumInputs(rs_percentile=90, stage=3), S)
    assert ep.score == 1  # RS only; stage 3 is a known "no"
    assert set(ep.unknown) == {
        "implied_growth", "eps_acceleration", "roce_up", "operating_leverage",
        "institutions_up", "promoter_steady",
    }  # fmt: skip
    assert ep.max_possible == 7 and ep.reasons[-1] == "earned premium 1/8 (6 unknown: up to 7)"
    assert "stage2_near_high" in earned_premium(EarnedPremiumInputs(stage=2), S).unknown


def test_bank_earned_premium_uses_bank_criteria() -> None:
    bank = replace(EP_ALL, bank=True, roe_pct=16.0, roe_pct_prior=14.0, roa_pct=1.8,
                   loan_growth_pct=15.0, deposit_growth_pct=13.0, nim_pct=None)  # fmt: skip
    ep = earned_premium(bank, S)
    codes = [c.code for c in ep.conditions]
    assert codes == [
        "eps_acceleration", "roe_up", "roa_strong", "loan_growth", "deposit_growth",
        "nim_strong", "institutions_up", "promoter_steady", "rs_leader", "stage2_near_high",
    ]  # fmt: skip
    assert not {"implied_growth", "roce_up", "operating_leverage"} & set(codes)
    assert ep.out_of == 10 and ep.score == 9 and ep.unknown == ["nim_strong"]
    assert ep.reasons[-1] == "earned premium 9/10 (1 unknown: up to 10)"
    roa = next(c for c in ep.conditions if c.code == "roa_strong")
    assert roa.reason == "ROA 1.8% (>= 1.5%)"
    slow = earned_premium(replace(bank, loan_growth_pct=8.0, roe_pct=13.7), S)
    assert {c.code: c.met for c in slow.conditions}["loan_growth"] is False
    assert {c.code: c.met for c in slow.conditions}["roe_up"] is False


def test_momentum_threshold_scales_with_the_number_of_conditions() -> None:
    # 6 of 8 → ceil(6 x 10 / 8) = 8 of 10 for a bank
    base = replace(BASE, grade=A, zone=Zone.PREMIUM, earned_premium_out_of=10)
    assert decide(replace(base, earned_premium=8), S).action is Action.MOMENTUM_ENTRY
    d = decide(replace(base, earned_premium=7), S)
    assert d.action is Action.WAIT and "premium not earned: EP 7 < 8" in d.reasons


# ───────────────────────── decision matrix ─────────────────────────

A, AP, B, C, D = Grade.A, Grade.A_PLUS, Grade.B, Grade.C, Grade.D
DD, DI, FA, PR, EX = (
    Zone.DEEP_DISCOUNT, Zone.DISCOUNT, Zone.FAIR, Zone.PREMIUM, Zone.EXTREME_PREMIUM
)  # fmt: skip
SB, BUY, ACC, BOP, ME = (
    Action.STRONG_BUY, Action.BUY, Action.ACCUMULATE, Action.BUY_ON_PULLBACK,
    Action.MOMENTUM_ENTRY,
)  # fmt: skip
WAIT, HOLD, BP, AV = Action.WAIT, Action.HOLD, Action.BOOK_PROFITS, Action.AVOID

# Favourable technicals: Stage 2, up-trend, EP 7, CMP 100 inside the buy zone 95-105.
BASE = DecisionInputs(grade=A, zone=FA, cmp=100, earned_premium=7, stage=2, trend="up",
                      buy_zone=(95, 105))  # fmt: skip

# Every cell of SPEC §7.5 with BASE inputs: (grade, zone, rule, action, action under Stage 4)
MATRIX = [
    (AP, DD, "strong_buy", SB, WAIT),
    (AP, DI, "buy_or_accumulate", BUY, WAIT),
    (AP, FA, "buy_on_pullback", BOP, WAIT),
    (AP, PR, "momentum_or_wait", ME, WAIT),
    (AP, EX, "hold_if_owned", HOLD, HOLD),
    (A, DD, "strong_buy", SB, WAIT),
    (A, DI, "buy_or_accumulate", BUY, WAIT),
    (A, FA, "buy_on_pullback", BOP, WAIT),
    (A, PR, "momentum_or_wait", ME, WAIT),
    (A, EX, "hold_if_owned", HOLD, HOLD),
    (B, DD, "buy_with_confirmation", BUY, WAIT),
    (B, DI, "accumulate_slowly", ACC, WAIT),
    (B, FA, "wait", WAIT, WAIT),
    (B, PR, "avoid", AV, AV),
    (B, EX, "book_profits", BP, BP),
    (C, DD, "value_trap_check", WAIT, WAIT),
    (C, DI, "watch", WAIT, WAIT),
    (C, FA, "avoid", AV, AV),
    (C, PR, "avoid", AV, AV),
    (C, EX, "avoid", AV, AV),
    (D, DD, "avoid", AV, AV),
    (D, DI, "avoid", AV, AV),
    (D, FA, "avoid", AV, AV),
    (D, PR, "avoid", AV, AV),
    (D, EX, "avoid", AV, AV),
]


def test_matrix_table_covers_every_cell() -> None:
    assert {(g, z) for g, z, *_ in MATRIX} == {(g, z) for g in Grade for z in Zone}


@pytest.mark.parametrize(("grade", "zone", "rule", "action", "_stage4"), MATRIX)
def test_decision_matrix_cell(
    grade: Grade, zone: Zone, rule: str, action: Action, _stage4: Action
) -> None:
    d = decide(replace(BASE, grade=grade, zone=zone), S)
    assert d.rule is DecisionRule(rule) and d.action is action and not d.overridden
    assert d.reasons and d.reasons[-1] == f"action: {action.label}"


@pytest.mark.parametrize(("grade", "zone", "rule", "action", "stage4"), MATRIX)
def test_stage4_overrides_every_buy(
    grade: Grade, zone: Zone, rule: str, action: Action, stage4: Action
) -> None:
    # Stage 4 (up-trend kept so B x Deep Discount is still "confirmed" by trend → Buy → Wait)
    d = decide(replace(BASE, grade=grade, zone=zone, stage=4), S)
    assert d.action is stage4
    assert d.overridden is (stage4 is not action)
    if d.overridden:
        assert any(r.endswith(STAGE4_REASON) for r in d.reasons)


@pytest.mark.parametrize(
    ("change", "action", "reason_part"),
    [
        # A x Discount: Buy inside the buy zone, else Accumulate
        ({"zone": DI, "cmp": 110.0}, ACC, "outside the buy zone 95.00-105.00"),
        ({"zone": DI, "buy_zone": None}, ACC, "no technical buy zone yet"),
        ({"zone": DI, "cmp": 95.0}, BUY, "inside"),  # on the edge
        # A x Premium: Momentum Entry needs EP >= 6
        ({"zone": PR, "earned_premium": 6}, ME, "EP 6 >= 6"),
        ({"zone": PR, "earned_premium": 5}, WAIT, "EP 5 < 6"),
        ({"zone": PR, "earned_premium": None}, WAIT, "EP unknown < 6"),
        # A x Fair: pullback target
        ({"zone": FA}, BOP, "pullback to the buy zone 95.00-105.00"),
        ({"zone": FA, "buy_zone": None}, BOP, "no zone yet"),
        # B x Deep Discount: Buy only with confirmation (stage 2 or up-trend)
        ({"grade": B, "zone": DD, "stage": 1, "trend": "range"}, WAIT, "awaiting technical"),
        ({"grade": B, "zone": DD, "stage": 1, "trend": "up"}, BUY, "up trend"),
        ({"grade": B, "zone": DD, "stage": 2, "trend": "down"}, BUY, "Stage 2"),
        ({"grade": B, "zone": DD, "stage": None, "trend": None}, WAIT, "awaiting technical"),
    ],
)
def test_conditional_cells(change: dict[str, object], action: Action, reason_part: str) -> None:
    d = decide(replace(BASE, **change), S)  # type: ignore[arg-type]
    assert d.action is action
    assert any(reason_part in r for r in d.reasons), d.reasons


def test_checklists() -> None:
    for g in (AP, A):
        d = decide(replace(BASE, grade=g, zone=DD), S)
        assert d.checklist == S.decision.checklists.why_cheap
    trap = decide(replace(BASE, grade=C, zone=DD), S)
    assert trap.action is WAIT and trap.checklist == S.decision.checklists.value_trap
    # the Stage 4 override keeps the "why is it cheap?" checklist
    d4 = decide(replace(BASE, zone=DD, stage=4), S)
    assert d4.action is WAIT and d4.overridden and d4.checklist == S.decision.checklists.why_cheap
    assert decide(replace(BASE, zone=FA), S).checklist == []


def test_missing_grade_or_zone() -> None:
    assert decide(replace(BASE, grade=None), S).action is None
    none_zone = decide(replace(BASE, grade=B, zone=None), S)
    assert none_zone.action is None and none_zone.reasons == [
        "no action: valuation zone unavailable"
    ]
    d = decide(replace(BASE, grade=D, zone=None), S)  # the whole D row is Avoid
    assert d.action is AV and "every zone" in d.reasons[0]
