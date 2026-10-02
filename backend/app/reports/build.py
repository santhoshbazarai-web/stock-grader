"""Builds the StockReport (SPEC §8) from :class:`StockData`. Pure function: no DB, no network.

Order (SPEC §7.3 circularity):
    fundamentals → technicals → non-valuation pillars + knock-outs → provisional grade
    → valuation with the provisional grade's MoS → valuation pillar → final grade
    → earned premium → buy zone → decision.
"""

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal

import pandas as pd

from app.core.config import AppConfig, Dataset, SectorModel
from app.data.gaps import GapRecord
from app.fundamentals.banking import PROXIES, bank_per_share, bank_summary
from app.fundamentals.depth import DataDepth, data_depth
from app.fundamentals.forensic import altman_z2, beneish, piotroski
from app.fundamentals.metrics import Metric, annual_metrics, by_year, summary_metrics
from app.fundamentals.structural import adjust_growth, crossing, yoy_growth
from app.reports.data import StockData
from app.reports.dto import (
    AnalystConsensusDto,
    BankMetricDto,
    BuyZoneDto,
    ConditionDto,
    DataDepthDto,
    DcfScenarioDto,
    DecisionDto,
    EarnedPremiumDto,
    KnockoutsDto,
    Levels,
    MethodDto,
    PillarDto,
    ReverseDcfDto,
    Scores,
    ShareholdingDto,
    StockReport,
    SubScoreDto,
    TechnicalDto,
    ValuationDto,
)
from app.reports.overrides import DCF_KEYS
from app.reports.valuation_run import DCF_MODELS, ValuationRun, run_valuation, ttm_by_quarter
from app.scoring.common import Grade, Pillar
from app.scoring.decision import Decision, DecisionInputs, decide
from app.scoring.earned_premium import EarnedPremium, EarnedPremiumInputs, earned_premium
from app.scoring.grade import Grading, resolve_grade
from app.scoring.knockouts import KnockoutInputs, KnockoutResult, knockouts
from app.scoring.pillars import PillarInputs, PillarScore, non_valuation_pillars, valuation_pillar
from app.technical.buy_zone import BuyZone, buy_zone
from app.technical.engine import TechnicalAnalysis, analyze
from app.valuation.blend import Valuation, lower_confidence

# Which dataset a missing sub-metric / knock-out input comes from (for data_gaps rows).
_GAP_DATASET: dict[str, Dataset] = {
    "eps_yoy_last4q": Dataset.FIN_QUARTERLY,
    "eps_acceleration": Dataset.FIN_QUARTERLY,
    "pledge_pct": Dataset.SHAREHOLDING,
    "promoter_change_qoq_pp": Dataset.SHAREHOLDING,
    "institutional_change_qoq_pp": Dataset.SHAREHOLDING,
    "stage": Dataset.DAILY_OHLCV,
    "trend": Dataset.DAILY_OHLCV,
    "rs_percentile": Dataset.INDEX_OHLCV,
    "delivery_ratio": Dataset.DELIVERY,
    "pledge": Dataset.SHAREHOLDING,
    "beta": Dataset.INDEX_OHLCV,
    "asm_gsm": Dataset.SURVEILLANCE,
    "liquidity": Dataset.DELIVERY,
}

FUNDAMENTAL_KEYS = (
    "roce_latest",
    "roe_latest",
    "roic_latest",
    "opm_latest",
    "roce_5y_avg",
    "cfo_to_ebitda_5y",
    "cfo_to_pat_5y",
    "fcf_conversion_5y",
    "sales_cagr_3y",
    "sales_cagr_5y",
    "sales_cagr_10y",
    "eps_cagr_3y",
    "eps_cagr_5y",
    "eps_cagr_10y",
    "debt_to_equity",
    "net_debt_to_ebitda",
    "interest_coverage",
    "ccc_days",
    "other_income_share",
    "revenue_ttm",
    "pat_ttm",
    "opm_ttm",
)


@dataclass
class Built:
    report: StockReport
    run: ValuationRun
    valuation: Valuation | None
    technical: TechnicalAnalysis
    grading: Grading
    buy_zone: BuyZone
    gaps: list[GapRecord] = field(default_factory=list)


def _v(x: object) -> float | None:
    if x is None:
        return None
    f = float(x)  # type: ignore[arg-type]
    return None if pd.isna(f) else f


def _at(series: pd.Series, year: int) -> float | None:
    return _v(series.get(year)) if year in series.index else None


def quarterly_yoy(quarterly: pd.DataFrame, col: str) -> dict[pd.Period, float]:
    """YoY growth per quarter vs the same quarter a year earlier (prior value must be > 0)."""
    if quarterly.empty or col not in quarterly.columns:
        return {}
    q = quarterly.sort_index()
    vals = dict(
        zip(
            pd.DatetimeIndex(q.index).to_period("Q"),
            pd.to_numeric(q[col], errors="coerce"),
            strict=True,
        )
    )
    out = {}
    for p, v in vals.items():
        prior = vals.get(p - 4)
        if (
            v is not None
            and prior is not None
            and not pd.isna(v)
            and not pd.isna(prior)
            and prior > 0
        ):
            out[p] = float(v / prior - 1)
    return out


def _last_two(d: dict[pd.Period, float]) -> tuple[float | None, float | None]:
    """(latest, previous) when they are consecutive quarters."""
    if not d:
        return None, None
    keys = sorted(d)
    latest = keys[-1]
    prev = latest - 1
    return d[latest], d.get(prev)


def _ttm_yoy(quarterly: pd.DataFrame, col: str) -> float | None:
    t = ttm_by_quarter(quarterly, col)
    if t.empty:
        return None
    p = t.index[-1]
    prior = t.get(p - 4)
    if prior is None or pd.isna(prior) or prior <= 0:
        return None
    return float(t.iloc[-1] / prior - 1)


def _shp_changes(shp: pd.DataFrame) -> dict[str, float | None]:
    out: dict[str, float | None] = dict.fromkeys(
        ("pledge", "pledge_prev", "promoter_change", "institutional_change")
    )
    if shp.empty:
        return out
    s = shp.sort_index()
    last = s.iloc[-1]
    out["pledge"] = _v(last.get("promoter_pledge_pct"))
    if len(s) < 2:
        return out
    prev = s.iloc[-2]
    out["pledge_prev"] = _v(prev.get("promoter_pledge_pct"))
    pa, pb = _v(last.get("promoter_pct")), _v(prev.get("promoter_pct"))
    out["promoter_change"] = pa - pb if pa is not None and pb is not None else None
    # DII already includes mutual funds in Indian shareholding patterns: FII + DII = MF+FII+DII.
    ia = [_v(last.get(c)) for c in ("fii_pct", "dii_pct")]
    ib = [_v(prev.get(c)) for c in ("fii_pct", "dii_pct")]
    if all(v is not None for v in ia + ib):
        out["institutional_change"] = sum(v for v in ia if v is not None) - sum(
            v for v in ib if v is not None
        )
    return out


def _shareholding_dto(shp: pd.DataFrame) -> ShareholdingDto | None:
    if shp.empty:
        return None
    s = shp.sort_index()
    last = s.iloc[-1]
    ch = _shp_changes(s)
    filed = last.get("filing_date")
    source = last.get("source")
    return ShareholdingDto(
        source=source if isinstance(source, str) else None,
        period_end=pd.Timestamp(s.index[-1]).date(),
        filing_date=None if filed is None or pd.isna(filed) else pd.Timestamp(filed).date(),
        promoter_pct=_v(last.get("promoter_pct")),
        promoter_pledge_pct=_v(last.get("promoter_pledge_pct")),
        fii_pct=_v(last.get("fii_pct")),
        dii_pct=_v(last.get("dii_pct")),
        mf_pct=_v(last.get("mf_pct")),
        public_pct=_v(last.get("public_pct")),
        promoter_change_pp=ch["promoter_change"],
        pledge_prev_pct=ch["pledge_prev"],
        quarters=len(s),
    )


def _avg_traded_value(tv: pd.Series | None, days: int) -> float | None:
    if tv is None:
        return None
    s = tv.dropna()
    return float(s.iloc[-days:].mean()) if len(s) >= days else None


def _pillar_dto(p: PillarScore, weight: float) -> PillarDto:
    return PillarDto(
        pillar=p.pillar.value,
        score=p.score,
        weight=weight,
        subs=[
            SubScoreDto(name=s.name, value=s.value, score=s.score, reason=s.reason) for s in p.subs
        ],
        missing=p.missing,
        reasons=p.reasons,
        confidence=p.confidence,
    )


_BANK_UNITS: dict[str, Literal["pct", "inr", "x"]] = {"bvps": "inr", "pb": "x"}


def _bank_metric_dtos(bank: dict[str, Metric]) -> list[BankMetricDto]:
    """Reported (GNPA, NNPA, CAR, CASA) and proxy bank metrics, each labelled (SPEC §4)."""
    return [
        BankMetricDto(name=k, value=m.value, unit=_BANK_UNITS.get(k, "pct"), proxy=k in PROXIES,
                      definition=PROXIES.get(k), reason=m.reason)
        for k, m in bank.items()
    ]  # fmt: skip


def build_report(data: StockData, config: AppConfig, *, lite: bool = False) -> Built:
    """``lite`` skips the DCF sensitivity grid (backtests do not need it)."""
    vc, sc, tc = config.valuation, config.scoring, config.technical
    close = data.daily["close"]
    cmp = float(close.iloc[-1])
    as_of: date = pd.Timestamp(close.index[-1]).date()
    sector_key = data.sector if data.sector in config.sectors.root else "default"
    sector = config.sectors.for_sector(sector_key)
    is_bank = sector.model is SectorModel.BANK
    is_financial = sector.model in (SectorModel.BANK, SectorModel.INSURANCE)
    gaps: list[GapRecord] = []
    data_gaps: list[str] = list(data.notes)
    red_flags: list[str] = []

    def gap(dataset: Dataset, fld: str | None, reason: str) -> None:
        gaps.append(GapRecord(dataset, data.symbol, reason, [], fld))
        data_gaps.append(f"{fld or dataset.value}: {reason}")

    if data.sector is not None and data.sector not in config.sectors.root:
        data_gaps.append(f"sector {data.sector!r} not in sectors.yaml: using default")
    elif data.sector is None:
        if data.industry_label:
            data_gaps.append(f"sector unmapped: {data.industry_source} industry "
                             f"'{data.industry_label}' has no entry in industries.yaml; "
                             "using the default model")  # fmt: skip
        else:
            data_gaps.append("sector unmapped: industry not classified yet "
                             "(industry_classification job); using the default model")  # fmt: skip
    if data.statement_type == "standalone":
        red_flags.append("standalone statements (no consolidated figures on file)")

    # ── fundamentals ──
    annual, quarterly = data.annual, data.quarterly
    if "is_derived" in annual.columns:  # SPEC §3.6 step 5: flagged, never silently mixed in
        derived = sorted(
            int(fy) for fy, flag in zip(annual["fiscal_year"], annual["is_derived"], strict=True)
            if flag is True or flag == 1
        )  # fmt: skip
        if derived:
            data_gaps.append(
                "FY" + ", FY".join(str(fy) for fy in derived) + ": P&L summed from the four "
                "quarterly results (no annual filing parsed); no EPS, balance sheet or cash "
                "flow for those years"
            )
    metrics: dict[str, Metric] = {}
    am = pd.DataFrame()
    if annual.empty:
        gap(Dataset.FIN_ANNUAL, None, "no annual financials on file")
    else:
        metrics = summary_metrics(
            annual,
            quarterly if not quarterly.empty else None,
            sc.fundamentals,
            tax_rate_fallback=vc.tax_rate_default,
        )
        am = annual_metrics(
            annual, tax_rate_fallback=vc.tax_rate_default, days=sc.fundamentals.days_in_year
        )
    # metrics that mean nothing for this sector model are dropped, not reported as missing
    fc = sc.fundamentals
    metrics = {k: m for k, m in metrics.items() if fc.applies(sector.model.value, k)}
    # SPEC §4: growth windows across a merger / demerger are measured per share
    metrics, structural_notes = adjust_growth(
        metrics, annual, data.structural_events, cagr_years=sc.fundamentals.cagr_years,
        shares_year_end=data.shares_year_end,
    )  # fmt: skip
    df = by_year(annual) if not annual.empty else pd.DataFrame()
    y = int(df.index.max()) if len(df) else None

    def mv(key: str) -> float | None:
        m = metrics.get(key)
        return m.value if m is not None else None

    pio = piotroski(annual) if not annual.empty else None
    ben = beneish(annual, sc.forensic) if not annual.empty else None
    alt = altman_z2(annual, sc.forensic, is_financial=is_financial) if not annual.empty else None
    if ben is not None and ben.flag:
        red_flags.append(f"Beneish M-score {ben.value:.2f}: {ben.flag}")
    if alt is not None and alt.flag == "distress":
        red_flags.append(f"Altman Z'' {alt.value:.2f}: distress zone")
    bank = bank_summary(annual) if is_bank and not annual.empty else {}
    if bank:
        bank.update(bank_per_share(annual, price=cmp, shares_year_end=data.shares_year_end))
        if data.sources.get("fundamentals") == "indianapi":
            # NIM, GNPA, NNPA and CAR are not in the vendor's data: they stay gaps, never
            # estimated from its lines
            bank["nim_pct"] = Metric(None, "not reported by the Indian API (data gap)")

    def am_at(col: str, year: int | None) -> float | None:
        if year is None or col not in am.columns:
            return None
        return _at(am[col], year)

    n = sc.trend_years
    rev = (
        pd.to_numeric(df["revenue"], errors="coerce")
        if "revenue" in df.columns
        else pd.Series(dtype=float)
    )
    rev_now, rev_prev = (_at(rev, y), _at(rev, y - 1)) if y is not None else (None, None)
    sales_growth_yoy = (
        rev_now / rev_prev - 1 if rev_now is not None and rev_prev and rev_prev > 0 else None
    )
    if y is not None and crossing(data.structural_events, y - 1, y) is not None:
        sales_growth_yoy, why = yoy_growth(annual, "revenue", y, data.structural_events,
                                           shares_year_end=data.shares_year_end)  # fmt: skip
        if why:
            structural_notes.append(why)
    eps_q_latest, eps_q_prev = _last_two(quarterly_yoy(quarterly, "eps_diluted"))
    shp = _shp_changes(data.shareholding)
    if data.shareholding.empty:
        why = "no shareholding pattern on file (promoter, FII/DII, pledge): run shareholding"
        gap(Dataset.SHAREHOLDING, None, why)

    # ── technicals ──
    t = analyze(
        data.daily,
        tc,
        benchmark_daily_close=data.benchmark_close,
        delivery_pct=data.delivery_pct,
        last_results_date=pd.Timestamp(data.last_results_date) if data.last_results_date else None,
    )
    stage = t.stage.stage
    trend = t.structure.trend if t.structure.swings else None

    # ── pillars + knock-outs ──
    pin = PillarInputs(
        roce_5y_avg=mv("roce_5y_avg"),
        roce_latest=mv("roce_latest"),
        roce_prior=am_at("roce", y - n if y else None),
        cfo_to_ebitda_5y=mv("cfo_to_ebitda_5y"),
        fcf_conversion_5y=mv("fcf_conversion_5y"),
        piotroski=pio.value if pio else None,
        sales_cagr_5y=mv("sales_cagr_5y"),
        eps_cagr_5y=mv("eps_cagr_5y"),
        eps_yoy_ttm=_ttm_yoy(quarterly, "eps_diluted"),
        eps_yoy_q_latest=eps_q_latest,
        eps_yoy_q_prev=eps_q_prev,
        debt_to_equity=mv("debt_to_equity"),
        interest_coverage=mv("interest_coverage"),
        net_debt_to_ebitda=mv("net_debt_to_ebitda"),
        ccc_days=am_at("ccc_days", y),
        ccc_days_prior=am_at("ccc_days", y - n if y else None),
        altman_z2=alt.value if alt else None,
        pledge_pct=shp["pledge"],
        promoter_change_qoq_pp=shp["promoter_change"],
        institutional_change_qoq_pp=shp["institutional_change"],
        other_income_share=mv("other_income_share"),
        rpt_flagged=data.overrides.rpt_flagged,
        stage=stage,
        rs_percentile=data.rs_percentile,
        trend=trend,
        delivery_ratio=t.delivery_ratio,
        is_bank=is_bank,
        roa_pct=bank["roa_pct"].value if "roa_pct" in bank else None,
        nim_pct=bank["nim_pct"].value if "nim_pct" in bank else None,
        gnpa_pct=bank["gnpa_pct"].value if "gnpa_pct" in bank else None,
        car_pct=bank["car_pct"].value if "car_pct" in bank else None,
        credit_cost_pct=bank["credit_cost_pct"].value if "credit_cost_pct" in bank else None,
        equity_to_assets_pct=(
            bank["equity_to_assets_pct"].value if "equity_to_assets_pct" in bank else None
        ),
    )
    pillars = non_valuation_pillars(pin, sc)

    run = run_valuation(data, metrics, sector_key, sector, vc, sensitivity=not lite)
    for name in run.assumed_nil:
        gap(Dataset.FIN_ANNUAL, name, "not reported: taken as nil in the valuation")
    for name, why in run.gaps:
        gap(_GAP_DATASET.get(name, Dataset.FIN_ANNUAL), name, why)
    if run.announcement_dates_assumed:
        gap(Dataset.FIN_QUARTERLY, "announcement_date", "unknown: filing lag assumed for bands")

    cfo = (
        pd.to_numeric(df["cfo"], errors="coerce") if "cfo" in df.columns else pd.Series(dtype=float)
    )
    ko = knockouts(
        KnockoutInputs(
            as_of=as_of,
            pledge_pct=shp["pledge"],
            cfo_history=[_v(v) for v in cfo.tolist()] if len(cfo) else None,
            cfo_applies=not is_financial,
            beneish_applies=not is_financial,
            auditor_resignations=data.overrides.auditor_resignations
            if data.overrides.auditor_resignations is not None
            else data.auditor_resignations,
            on_asm_gsm=data.on_asm_gsm,
            mcap_cr=run.market_cap_cr,
            avg_traded_value_cr_20d=_avg_traded_value(
                data.traded_value_cr, sc.knockouts.traded_value_days
            ),
            beneish_m=ben.value if ben else None,
        ),
        sc.knockouts,
    )
    red_flags += [r.removeprefix("knock-out: ") for r in ko.reasons if r.startswith("knock-out:")]

    valuations: dict[str, Valuation] = {}

    def valuation_for(g: Grade) -> PillarScore:
        val = run.blend(g.value, vc)
        valuations["v"] = val
        return valuation_pillar(
            cmp=cmp,
            fair_value=val.fair_value,
            implied_growth=run.reverse.implied_growth if run.reverse else None,
            hist_growth=mv("sales_cagr_5y"),
            cfg=sc,
            reverse_dcf_applies=run.sector.model in DCF_MODELS,
        )

    grading = resolve_grade(pillars, ko, valuation_for, sc)
    val = valuations.get("v")
    if val is not None and data.reconciliation_issues:  # SPEC v0.2 §3.9
        lowered = lower_confidence(val.confidence, vc.confidence.reconciliation_steps_down)
        n = len(data.reconciliation_issues)
        val = replace(val, confidence=lowered, reasons=[
            *val.reasons,
            f"{n} open reconciliation issue(s) across sources: confidence "
            + (f"{val.confidence.value} → {lowered.value}" if lowered is not val.confidence
               else f"stays {lowered.value}"),
        ])  # fmt: skip
    depth = data_depth(annual, sc.data_depth)  # SPEC §7.3
    if not depth.full:
        structural_notes.insert(0, f"Data depth {depth.level}: {depth.reason}")
        if val is not None:
            lowered = lower_confidence(val.confidence, sc.data_depth.valuation_steps_down)
            val = replace(val, confidence=lowered, reasons=[
                *val.reasons,
                f"data depth {depth.level} ({depth.pl_years} years of P&L): confidence "
                + (f"{val.confidence.value} → {lowered.value}" if lowered is not val.confidence
                   else f"stays {lowered.value}"),
            ])  # fmt: skip
    red_flags += [f"event: {f}" for f in data.event_red_flags]
    final = grading.final.grade

    # ── earned premium ──
    ep = earned_premium(
        EarnedPremiumInputs(
            implied_growth=run.reverse.implied_growth if run.reverse else None,
            hist_growth_5y=mv("sales_cagr_5y"),
            eps_yoy_q_latest=eps_q_latest,
            eps_yoy_q_prev=eps_q_prev,
            roce_latest=mv("roce_latest"),
            roce_prior=pin.roce_prior,
            opm_latest=am_at("opm", y),
            opm_prev_year=am_at("opm", y - 1 if y else None),
            sales_growth_yoy=sales_growth_yoy,
            institutional_change_qoq_pp=shp["institutional_change"],
            promoter_change_qoq_pp=shp["promoter_change"],
            pledge_pct=shp["pledge"],
            pledge_pct_prev_quarter=shp["pledge_prev"],
            rs_percentile=data.rs_percentile,
            stage=stage,
            from_52w_high=t.momentum.from_52w_high,
        ),
        sc,
    )

    # ── buy zone + decision ──
    if val is None or val.fair_value is None:
        bz = BuyZone("unavailable", reasons=["valuation levels unavailable"])
    else:
        bz = buy_zone(
            cmp=cmp,
            stage=stage,
            baseline=val.baseline,
            fair_value=val.fair_value,
            top_band=val.top_band,
            mos=val.mos_pct,
            grade=(final or grading.mos_grade or Grade.D).value,
            supports=t.supports,
            atr=t.atr_now,
            cfg=tc,
        )
    decision: Decision = decide(
        DecisionInputs(
            grade=final,
            zone=val.zone if val else None,
            cmp=cmp,
            earned_premium=ep.score,
            stage=stage,
            trend=trend,
            buy_zone=(bz.low, bz.high)
            if bz.status == "zone" and bz.low is not None and bz.high is not None
            else None,
        ),
        sc,
    )

    # ── data gaps from missing inputs ──
    for p in grading.pillars.values():
        for name in p.missing:
            gap(
                _GAP_DATASET.get(name, Dataset.FIN_ANNUAL),
                name,
                f"missing: {p.pillar.value} sub-metric not scored",
            )
    for code in ko.unknown:
        gap(
            _GAP_DATASET.get(code, Dataset.FIN_ANNUAL),
            f"knockout:{code}",
            "knock-out check not evaluated",
        )

    report = _assemble(
        data=data,
        config=config,
        cmp=cmp,
        as_of=as_of,
        run=run,
        val=val,
        t=t,
        grading=grading,
        ko=ko,
        ep=ep,
        bz=bz,
        decision=decision,
        metrics=metrics,
        extras={
            k: v
            for k, v in {
                "piotroski": pio.value if pio else None,
                "beneish_m": ben.value if ben else None,
                "altman_z2": alt.value if alt else None,
                **{k: m.value for k, m in bank.items()},
            }.items()
            if fc.applies(sector.model.value, k)
        },
        red_flags=red_flags,
        data_gaps=data_gaps,
        notes=structural_notes,
        bank=bank,
        depth=depth,
    )
    return Built(report, run, val, t, grading, bz, gaps)


def _assemble(
    *,
    data: StockData,
    config: AppConfig,
    cmp: float,
    as_of: date,
    run: ValuationRun,
    val: Valuation | None,
    t: TechnicalAnalysis,
    grading: Grading,
    ko: KnockoutResult,
    ep: EarnedPremium,
    bz: BuyZone,
    decision: Decision,
    metrics: dict[str, Metric],
    extras: dict[str, float | None],
    red_flags: list[str],
    data_gaps: list[str],
    notes: list[str],
    bank: dict[str, Metric],
    depth: DataDepth,
) -> StockReport:
    weights = config.scoring.weights.model_dump()
    pillars = grading.pillars
    total = grading.final.score
    final = grading.final.grade
    lines = val.methods if val else []
    methods = [
        MethodDto(
            name=ln.name,
            value=ln.value,
            weight=ln.weight,
            effective_weight=ln.effective_weight,
            reasons=run.method_reasons.get(ln.name, []),
        )
        for ln in lines
    ]
    rev = run.reverse
    reasons = [
        *notes,
        *(val.reasons[-2:] if val else ["no valuation: no provisional grade"]),
        *grading.reasons,
        *decision.reasons,
    ]
    rs_now = (
        float(t.rs_benchmark.iloc[-1])
        if t.rs_benchmark is not None and len(t.rs_benchmark)
        else None
    )
    return StockReport(
        shareholding=_shareholding_dto(data.shareholding),
        analyst_consensus=(
            AnalystConsensusDto(source="Indian API", **data.analyst_consensus)
            if data.analyst_consensus
            else None
        ),
        symbol=data.symbol,
        name=data.name,
        cmp=cmp,
        as_of=as_of,
        sources={**data.sources, "statement_type": data.statement_type},
        levels=Levels(
            baseline=val.baseline if val else None,
            fair_value=val.fair_value if val else None,
            top_band=val.top_band if val else None,
            mos_pct=val.mos_pct if val else None,
            confidence=val.confidence.value if val else None,
            discount_edge=val.fair_value * (1 - val.mos_pct) if val and val.fair_value else None,
            fair_upper=(
                val.fair_value * config.valuation.zones.fair_upper_mult
                if val and val.fair_value
                else None
            ),
        ),
        zone=val.zone.value if val and val.zone else None,
        buy_zone=BuyZoneDto(
            status=bz.status, low=bz.low, high=bz.high, basis=bz.basis, reasons=bz.reasons
        ),
        invalidation=bz.invalidation,
        rr_to_fv=bz.rr_to_fv,
        rr_to_top=bz.rr_to_top,
        valuation=ValuationDto(
            sector=run.sector_key,
            model=run.sector.model.value,
            methods=methods,
            reverse_dcf=ReverseDcfDto(
                implied_growth=rev.implied_growth,
                hist_growth=rev.hist_growth,
                gap=rev.gap,
                reasons=rev.reasons,
            )
            if rev
            else None,
            dcf=[
                DcfScenarioDto(
                    scenario=k,  # type: ignore[arg-type]
                    value_per_share=r.value_per_share,
                    terminal_share=r.terminal_share,
                    reasons=r.reasons,
                )
                for k, r in run.scenarios.items()
            ],
            wacc=run.wacc,
            cost_of_equity=run.ke,
            beta=run.beta,
            market_cap_cr=run.market_cap_cr,
            extra_methods=run.extra,
            dcf_inputs=(
                {k: float(getattr(run.base, k)) for k in DCF_KEYS} if run.base is not None else None
            ),
            justified_pb_inputs=run.justified_pb_inputs,
            justified_pb_grid=run.justified_pb_grid,
            reasons=run.reasons + (val.reasons if val else []),
        ),
        scores=Scores(
            quality=pillars[Pillar.QUALITY].score if Pillar.QUALITY in pillars else None,
            growth=pillars[Pillar.GROWTH].score if Pillar.GROWTH in pillars else None,
            valuation=pillars[Pillar.VALUATION].score if Pillar.VALUATION in pillars else None,
            health=pillars[Pillar.HEALTH].score if Pillar.HEALTH in pillars else None,
            governance=pillars[Pillar.GOVERNANCE].score if Pillar.GOVERNANCE in pillars else None,
            technical=pillars[Pillar.TECHNICAL].score if Pillar.TECHNICAL in pillars else None,
            total=total,
        ),
        grade=final.value if final else None,
        grade_label=final.label if final else None,
        earned_premium=ep.score,
        action=decision.action.value if decision.action else None,
        reasons=reasons,
        red_flags=red_flags,
        data_gaps=sorted(set(data_gaps)),
        thesis=None,
        reconciliation_issues=list(data.reconciliation_issues),
        provisional_grade=grading.provisional.grade.value if grading.provisional.grade else None,
        mos_grade=grading.mos_grade.value if grading.mos_grade else None,
        pillars=[_pillar_dto(p, float(weights[p.pillar.value])) for p in pillars.values()],
        bank_metrics=_bank_metric_dtos(bank),
        data_depth=DataDepthDto(level=depth.level, pl_years=depth.pl_years, reason=depth.reason),
        grade_confidence="full" if depth.full else "reduced",
        knockouts=KnockoutsDto(
            cap=ko.cap.value if ko.cap else None,
            triggered=ko.triggered,
            unknown=ko.unknown,
            reasons=ko.reasons,
        ),
        earned_premium_detail=EarnedPremiumDto(
            score=ep.score,
            max_possible=ep.max_possible,
            conditions=[
                ConditionDto(code=c.code, met=c.met, reason=c.reason) for c in ep.conditions
            ],
        ),
        decision=DecisionDto(
            action=decision.action.value if decision.action else None,
            rule=decision.rule.value if decision.rule else None,
            overridden_by_stage4=decision.overridden,
            checklist=decision.checklist,
            reasons=decision.reasons,
        ),
        technical=TechnicalDto(
            as_of=t.as_of.date(),
            stage=t.stage.stage,
            trend=t.structure.trend,
            rs_percentile=data.rs_percentile,
            mansfield_rs=rs_now,
            atr=t.atr_now,
            rsi=t.momentum.rsi,
            from_52w_high=t.momentum.from_52w_high,
            delivery_ratio=t.delivery_ratio,
            vcp=t.vcp.detected,
            reasons=t.reasons,
        ),
        fundamentals={
            **{k: metrics[k].value for k in FUNDAMENTAL_KEYS if k in metrics},
            **extras,
        },
        overrides=data.overrides.as_dict(),
        peer_stats=run.peer,
    )
