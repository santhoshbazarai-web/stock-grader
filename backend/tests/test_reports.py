"""Report pipeline (SPEC §8): load → build → persist, on synthetic companies (report_support).

The valuation, scoring and technical maths have their own worked-example tests; here the
checks are that the pipeline wires them together consistently: blend arithmetic, the
provisional-grade MoS, peers, overrides, sector switching (rule 10), knock-outs, data gaps and
persistence.
"""

import json
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import JobName, load_config
from app.db.enums import StatementType, Timeframe
from app.db.models import (
    DataGap,
    FinAnnual,
    Instrument,
    Report,
    Score,
    Symbol,
    TechnicalSnapshot,
    UserOverride,
    ValuationSnapshot,
)
from app.db.upsert import upsert
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, run_job
from app.reports.build import quarterly_yoy
from app.reports.dto import PeerStats
from app.reports.service import build_for, latest_report, persist, refresh_report
from app.reports.valuation_run import effective_dates, ttm_by_quarter
from tests.conftest import REPO_CONFIG_DIR
from tests.jobs_support import Env
from tests.report_support import annual_rows, seed_company, seed_index

CFG = load_config(REPO_CONFIG_DIR)


# ───────────────────────── pure helpers ─────────────────────────


def _quarters(values: list[float | None], start: str = "2022-06-30") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(values), freq="QE")
    return pd.DataFrame({"eps_diluted": values}, index=idx)


def test_ttm_needs_four_consecutive_quarters() -> None:
    # 1+2+3+4 = 10, 2+3+4+5 = 14; a gap (missing quarter row) breaks the chain
    t = ttm_by_quarter(_quarters([1, 2, 3, 4, 5]), "eps_diluted")
    assert list(t) == [10.0, 14.0]
    gapped = _quarters([1, 2, 3, 4, 5]).drop(pd.Timestamp("2022-12-31"))
    assert list(ttm_by_quarter(gapped, "eps_diluted")) == []  # never 4 in a row
    assert list(ttm_by_quarter(_quarters([1, None, 3, 4, 5]), "eps_diluted")) == []


def test_quarterly_yoy_growth() -> None:
    # Q (2023-06) 12 vs (2022-06) 10 → +20%; a non-positive base gives no growth
    yoy = quarterly_yoy(_quarters([10, -1, 5, 6, 12, 3]), "eps_diluted")
    assert yoy[pd.Period("2023Q2")] == pytest.approx(0.20)
    assert pd.Period("2023Q3") not in yoy  # base -1


def test_effective_dates_fall_back_to_the_filing_lag() -> None:
    f = pd.DataFrame(
        {"announcement_date": [date(2023, 5, 10), None]},
        index=pd.DatetimeIndex(["2023-03-31", "2024-03-31"]),
    )
    eff, assumed = effective_dates(f, 60)
    assert assumed and list(eff) == [pd.Timestamp("2023-05-10"), pd.Timestamp("2024-05-30")]


# ───────────────────────── end to end ─────────────────────────


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_index(db)
    seed_company(db)
    return db


def test_report_is_internally_consistent(seeded: Session) -> None:
    built = build_for(seeded, "SYNTH", CFG)
    r = built.report
    json.dumps(r.model_dump(mode="json"))
    assert r.sources == {
        "prices": "fyers",
        "fundamentals": "screener",
        "shareholding": "nse",
        "statement_type": "consolidated",
    }
    # Price was scaled to 25 x the latest annual EPS, which equals TTM EPS here.
    assert r.peer_stats.pe == pytest.approx(25.0)
    # Fair value = renormalised weighted mean of the available methods.
    used = [m for m in r.valuation.methods if m.value is not None]
    assert {m.name for m in used} == {"dcf_base", "band_pe"}  # no peers → no relative
    fv = sum(m.value * m.weight for m in used) / sum(m.weight for m in used)  # type: ignore[operator]
    assert r.levels.fair_value == pytest.approx(fv)
    rel = next(m for m in r.valuation.methods if m.name == "relative")
    assert rel.reasons == [
        "PE: 0 peers with a positive multiple (< 3): 0 from the vendor peer list, 0 with a "
        "stored report"
    ]
    # The MoS is the provisional grade's (SPEC §7.3).
    assert r.mos_grade == r.provisional_grade
    assert r.levels.mos_pct == getattr(CFG.valuation.mos_by_grade, r.mos_grade or "")
    # Baseline <= FV <= top band, scenario ordering, reverse DCF present (FCFF model).
    assert r.levels.baseline is not None and r.levels.top_band is not None
    assert r.levels.baseline <= r.levels.fair_value <= r.levels.top_band  # type: ignore[operator]
    dcf = {d.scenario: d.value_per_share for d in r.valuation.dcf}
    assert dcf["bear"] < dcf["base"] < dcf["bull"]  # type: ignore[operator]
    inputs = r.valuation.dcf_inputs
    assert inputs is not None and inputs["g1"] == pytest.approx(0.12)  # min(5y CAGR, cap)
    assert inputs["wacc"] == pytest.approx(r.valuation.wacc)
    assert inputs["ebit_margin"] == pytest.approx(0.20)  # fixture margin
    assert r.valuation.reverse_dcf is not None and r.valuation.reverse_dcf.implied_growth
    # Sales grow exactly 12% a year in the fixture.
    assert r.fundamentals["sales_cagr_5y"] == pytest.approx(0.12)
    # Every output explains itself.
    assert r.reasons and r.decision.reasons and r.knockouts.reasons
    assert all(p.reasons for p in r.pillars)
    assert r.action is not None and r.grade is not None


def test_missing_inputs_are_data_gaps_not_defaults(seeded: Session) -> None:
    r = build_for(seeded, "SYNTH", CFG).report
    # No delivery data, no surveillance lists, no RS percentile, no auditor record in the fixture
    assert "knockout:liquidity: knock-out check not evaluated" in r.data_gaps
    assert "knockout:asm_gsm: knock-out check not evaluated" in r.data_gaps
    assert "rs_percentile: missing: technical sub-metric not scored" in r.data_gaps
    technical = next(p for p in r.pillars if p.pillar == "technical")
    assert technical.missing == ["rs_percentile", "delivery_ratio"]
    assert set(r.knockouts.unknown) == {"auditor", "asm_gsm", "liquidity"}


def test_persist_writes_every_snapshot(seeded: Session) -> None:
    built = build_for(seeded, "SYNTH", CFG)
    persist(seeded, built)
    seeded.flush()
    r = built.report
    iid = seeded.scalar(select(Instrument.id).where(Instrument.symbol == "SYNTH"))
    vs = seeded.scalars(
        select(ValuationSnapshot).where(ValuationSnapshot.instrument_id == iid)
    ).one()
    assert vs.fair_value == pytest.approx(r.levels.fair_value) and vs.zone == r.zone
    assert vs.sensitivity is not None and len(vs.sensitivity["values"]) == 9  # ±2% in 0.5% steps
    ts = seeded.scalars(
        select(TechnicalSnapshot).where(
            TechnicalSnapshot.instrument_id == iid, TechnicalSnapshot.timeframe == Timeframe.WEEKLY
        )
    ).one()
    assert ts.buy_zone_low == r.buy_zone.low and ts.invalidation == r.invalidation  # type: ignore[union-attr]
    sc = seeded.scalars(select(Score).where(Score.instrument_id == iid)).one()
    assert (sc.grade, sc.provisional_grade, sc.action) == (r.grade, r.provisional_grade, r.action)
    assert sc.total == pytest.approx(r.scores.total)
    assert latest_report(seeded, "synth") == r
    gaps = seeded.scalars(select(DataGap.field).where(DataGap.instrument_id == iid)).all()
    assert "knockout:liquidity" in gaps and "rs_percentile" in gaps


def test_standalone_fallback_is_flagged(db: Session) -> None:
    iid = seed_company(db)
    db.execute(FinAnnual.__table__.update().values(statement_type=StatementType.STANDALONE))
    r = build_for(db, "SYNTH", CFG).report
    assert r.sources["statement_type"] == "standalone"
    assert "standalone statements (no consolidated figures on file)" in r.red_flags
    assert iid


def test_pledge_knockout_caps_grade(db: Session) -> None:
    seed_index(db)
    seed_company(db, pledge=(12.0, 15.0))
    r = build_for(db, "SYNTH", CFG).report
    assert r.knockouts.triggered == ["pledge"] and r.knockouts.cap == "C"
    assert r.grade in ("C", "D")
    assert any(f.startswith("promoter pledge 15.0%") for f in r.red_flags)
    ep = {c.code: c.met for c in r.earned_premium_detail.conditions}
    assert ep["promoter_steady"] is False  # pledge rose 12 → 15


def test_bank_sector_never_uses_fcff_dcf(db: Session) -> None:
    seed_index(db)
    seed_company(db, sector="banks")
    r = build_for(db, "SYNTH", CFG).report
    assert r.valuation.model == "bank"
    assert r.valuation.dcf == [] and r.valuation.reverse_dcf is None
    assert {m.name for m in r.valuation.methods} == {"justified_pb", "band_pb", "relative_pb"}
    assert next(p for p in r.pillars if p.pillar == "quality").subs[0].name == "roa_pct"


def test_market_cap_and_cost_of_equity_are_explained(db: Session) -> None:
    seed_index(db)
    seed_company(db)
    r = build_for(db, "SYNTH", CFG).report
    v = r.valuation
    assert v.market_cap_cr == pytest.approx(r.cmp * 10.0)  # SHARES_CR = 10 in the seed
    assert any(x.startswith("market cap ₹") and "Cr shares (year ended" in x for x in v.reasons)
    ke_line = next(x for x in v.reasons if x.startswith("Ke "))
    assert f"Ke {v.cost_of_equity:.2%} = Rf 6.50%" in ke_line and "as of not dated" in ke_line
    # the configured risk-free rate has no date: listed once as a gap
    assert (
        sum(g.startswith("risk_free_rate: risk-free rate 6.50% has no date") for g in r.data_gaps)
        == 1
    )


def test_bank_uses_cost_of_equity_not_wacc(db: Session) -> None:
    seed_index(db)
    seed_company(db, sector="banks")
    r = build_for(db, "SYNTH", CFG).report
    v = r.valuation
    assert v.wacc is None and v.cost_of_equity is not None and v.dcf == []
    assert "bank model: valued on the cost of equity; no WACC or FCFF" in v.reasons
    # no reverse DCF for a bank: not a missing valuation input
    assert not [g for g in r.data_gaps if g.startswith("reverse_dcf_gap")]
    vp = next(p for p in r.pillars if p.pillar == "valuation")
    assert [s.name for s in vp.subs] == ["discount_to_fv"]
    jpb = next(m for m in v.methods if m.name == "justified_pb")
    assert jpb.value is not None


def test_no_shareholding_is_a_data_gap(db: Session) -> None:
    from sqlalchemy import delete

    from app.db.models import Shareholding

    seed_index(db)
    seed_company(db)
    db.execute(delete(Shareholding))
    r = build_for(db, "SYNTH", CFG).report
    assert r.shareholding is None
    assert any(g.startswith("shareholding: no shareholding pattern on file") for g in r.data_gaps)


def test_a_bank_needs_no_cash_flow(db: Session) -> None:
    """Bank results XBRL has no cash-flow statement: the bank's valuation, pillars and
    knock-outs must not depend on it, nor list it as a gap."""
    from sqlalchemy import update

    from app.db.models import FinAnnual

    seed_index(db)
    seed_company(db, sector="banks")
    db.execute(
        update(FinAnnual).values(cfo=None, purchase_of_fixed_assets=None, sale_of_fixed_assets=None)
    )
    r = build_for(db, "SYNTH", CFG).report
    assert {m.name for m in r.valuation.methods} == {"justified_pb", "band_pb", "relative_pb"}
    assert "negative_cfo" not in r.knockouts.unknown
    assert not [g for g in r.data_gaps if "cfo" in g.lower() or "cash flow" in g.lower()]


def test_overrides_change_the_valuation(seeded: Session) -> None:
    iid = seeded.scalar(select(Instrument.id).where(Instrument.symbol == "SYNTH"))
    base = build_for(seeded, "SYNTH", CFG).report
    upsert(
        seeded,
        UserOverride,
        [
            {"instrument_id": iid, "key": "wacc", "value": {"value": 0.10}},
            {"instrument_id": iid, "key": "g1", "value": {"value": 0.20}},
        ],
    )
    r = build_for(seeded, "SYNTH", CFG).report
    assert r.overrides == {"wacc": 0.10, "g1": 0.20}
    assert r.valuation.wacc == 0.10 and "WACC 10.00% (user override)" in r.valuation.reasons
    dcf = lambda rep: next(d for d in rep.valuation.dcf if d.scenario == "base").value_per_share  # noqa: E731
    assert dcf(r) > dcf(base)  # higher growth, lower WACC
    dcf_line = next(m for m in r.valuation.methods if m.name == "dcf_base")
    assert "user overrides: g1, wacc" in dcf_line.reasons


def test_sector_override_switches_model(seeded: Session) -> None:
    iid = seeded.scalar(select(Instrument.id).where(Instrument.symbol == "SYNTH"))
    upsert(
        seeded, UserOverride, [{"instrument_id": iid, "key": "sector", "value": {"value": "banks"}}]
    )
    assert build_for(seeded, "SYNTH", CFG).report.valuation.model == "bank"


def test_relative_valuation_uses_sector_peers(seeded: Session) -> None:
    # Three peers at PE 20 with the same ROCE and growth → adjusted multiple 20 x EPS.
    r0 = build_for(seeded, "SYNTH", CFG).report
    p = r0.peer_stats
    peers = [
        PeerStats(symbol=f"P{i}", sector="it_services", pe=20.0, pb=3.0, ev_ebitda=12.0,
                  roce=p.roce, roe=p.roe, eps_growth=p.eps_growth)
        for i in range(3)
    ]  # fmt: skip
    r = build_for(seeded, "SYNTH", CFG, peers=peers).report
    rel = next(m for m in r.valuation.methods if m.name == "relative")
    eps = r0.cmp / 25.0
    assert rel.value == pytest.approx(20.0 * eps)


def test_no_financials_means_no_grade_and_no_action(db: Session) -> None:
    seed_company(db)
    db.execute(FinAnnual.__table__.delete())
    r = build_for(db, "SYNTH", CFG).report
    assert r.grade is None and r.action is None
    assert "fin_annual: no annual financials on file" in r.data_gaps
    assert r.decision.reasons == ["no action: grade unavailable"]


def test_refresh_report_round_trip(seeded: Session) -> None:
    r = refresh_report(seeded, "SYNTH", CFG)
    stored = seeded.scalars(select(Report)).one()
    assert stored.payload["symbol"] == "SYNTH" and stored.as_of == r.as_of


# ───────────────────────── job ─────────────────────────


def test_valuation_scores_job_uses_this_runs_peers(env: Env) -> None:
    with env.session() as s:
        seed_index(s)
        for i, pe in enumerate((20.0, 22.0, 24.0, 26.0)):
            seed_company(s, f"IT{i}", pe=pe, seed=20 + i)
        s.commit()
    record = run_job(
        REGISTRY[JobName.VALUATION_SCORES],
        env.ctx,
        JobOptions(symbols=("IT0", "IT1", "IT2", "IT3", "NOPRICES")),
    )
    assert record.outcome is not None
    d = record.outcome.details
    assert d["reports"] == 4 and "NOPRICES" in d["failed"]
    with env.session() as s:
        reports = {p["symbol"]: p for p in s.scalars(select(Report.payload))}
    rel = next(m for m in reports["IT0"]["valuation"]["methods"] if m["name"] == "relative")
    assert rel["value"] is not None  # three peers from the same run
    assert "3 peers" in rel["reasons"][-1]


def test_annual_fixture_shape() -> None:
    rows = annual_rows(1, 0.12)
    assert len(rows) == 10 and rows[-1]["fiscal_year"] == 2024


def test_demo_seed_and_purge(db: Session) -> None:
    from app.devtools.demo import DEMO_STOCKS, purge, seed

    symbols = seed(db, CFG)
    assert symbols == [s for s, *_ in DEMO_STOCKS]
    it = latest_report(db, "DEMOIT")
    assert it is not None and it.name == "Demoit Ltd (synthetic demo)"
    assert it.sources["prices"] == "demo"
    rel = next(m for m in it.valuation.methods if m.name == "relative")
    assert rel.value is not None  # three IT peers from the same seed
    assert len(it.levels.model_dump()) and it.levels.discount_edge is not None
    # a synthetic symbol master: BSE codes, a former name / symbol, one BSE-only company
    from app.data.search import search

    def top(q: str) -> tuple[str | None, str]:
        hit = search(db, q, cfg=CFG.providers.symbols.search,
                     universe_index=CFG.jobs.universe_index, limit=1)[0]  # fmt: skip
        return hit.symbol, hit.match

    assert top("990001") == ("DEMOIT", "bse_code")
    assert top("demo infotech systems") == ("DEMOIT", "former_name")
    assert top("DEMOINFO") == ("DEMOIT", "former_symbol")
    assert top("demo rural traders") == (None, "name")
    assert purge(db, CFG) == len(DEMO_STOCKS)
    assert db.scalar(select(Instrument.id).where(Instrument.symbol == "NIFTY500")) is None
    assert db.scalar(select(Symbol.id)) is None


def test_a_plus_grade_persists(seeded: Session) -> None:
    # Regression: scores.grade was VARCHAR(4) and "A_plus" did not fit.
    built = build_for(seeded, "SYNTH", CFG)
    report = built.report.model_copy(update={"grade": "A_plus", "provisional_grade": "A_plus"})
    built.report = report
    persist(seeded, built)
    seeded.flush()
    assert seeded.scalars(select(Score.grade)).one() == "A_plus"


@pytest.mark.parametrize(
    ("brk", "expect"),
    [
        (date(2021, 6, 1), "window after the merger 01 Jun 2021 only (pre-break years excluded)"),
        (date(2023, 9, 1), "regression"),
    ],
)
def test_bank_pb_band_never_uses_the_pre_merger_window(db: Session, brk: date, expect: str) -> None:
    from app.core.config import StructuralEvent, StructuralEventsConfig

    seed_index(db)
    seed_company(db, sector="banks")
    ev = StructuralEvent(date=brk, kind="merger", description="synthetic merger")
    events = StructuralEventsConfig(events={"SYNTH": [ev]})
    cfg = CFG.model_copy(update={"structural_events": events})
    r = build_for(db, "SYNTH", cfg).report
    pb = next(m for m in r.valuation.methods if m.name == "band_pb")
    assert expect in pb.reasons[-1] or any(expect in x for x in pb.reasons)
    if expect == "regression":  # ~10 months after the break (< 2y): no plain band
        assert any("pre-break window not used" in x for x in pb.reasons)
        assert not any(x.startswith("pb 5y") or x.startswith("pb 10y") for x in pb.reasons)


def test_bank_relative_pb_says_why_without_three_peers(db: Session) -> None:
    seed_index(db)
    seed_company(db, sector="banks")
    r = build_for(db, "SYNTH", CFG).report
    rel = next(m for m in r.valuation.methods if m.name == "relative_pb")
    assert rel.value is None
    assert rel.reasons[0].startswith("P/B: 0 peers with a positive multiple (< 3)")
    assert rel.reasons[-1].startswith("configured peers without a report yet: HDFCBANK, ICICIBANK")
    assert "vendor peer list: no Indian API /stock answer on file" in r.data_gaps


def test_pledge_filled_from_disclosures_or_no_promoter() -> None:
    from app.reports.data import fill_pledge, no_promoter_note

    idx = pd.DatetimeIndex(["2025-12-31", "2026-03-31", "2026-06-30"], name="period_end")
    shp = pd.DataFrame({"promoter_pct": [26.0, 26.0, 26.0],
                        "promoter_pledge_pct": [1.5, None, None]}, index=idx)  # fmt: skip
    out = fill_pledge(shp, [(date(2026, 4, 10), 0.0), (date(2025, 1, 5), 9.0)], 92)
    assert out["promoter_pledge_pct"].tolist()[:2] == [1.5, 0.0]
    assert out["pledge_source"].tolist()[:2] == ["shareholding pattern",
                                                 "NSE pledge disclosure 10 Apr 2026"]  # fmt: skip
    # Jun 2026: the April disclosure is within 92 days too
    assert out["promoter_pledge_pct"].iloc[2] == 0.0
    # nothing within the window and a promoter: stays unknown
    late = fill_pledge(shp, [(date(2025, 1, 5), 9.0)], 92)
    assert pd.isna(late["promoter_pledge_pct"].iloc[2])
    # no promoter holding: nothing to pledge, and the note says since when
    none = shp.assign(promoter_pct=[26.0, 0.0, 0.0])
    filled = fill_pledge(none, [], 92)
    assert filled["promoter_pledge_pct"].tolist()[1:] == [0.0, 0.0]
    assert filled["pledge_source"].iloc[2] == "no identified promoter: nothing pledged"
    assert (no_promoter_note(none) or "").startswith("No identified promoter since Dec 2025")
    assert no_promoter_note(shp) is None
