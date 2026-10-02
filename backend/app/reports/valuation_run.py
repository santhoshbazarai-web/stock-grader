"""Runs every SPEC §5 method the stock's sector model calls for. Pure functions.

Inputs come from :class:`app.reports.data.StockData` plus the fundamentals summary; the output
feeds ``valuation.blend`` once the provisional grade (and so the MoS) is known.

Choices made here (documented in SPEC §5 implementation notes):
- Band denominators are step series keyed by announcement date. When a filing has no
  announcement date (e.g. Screener uploads) it is treated as known
  ``bands.assumed_announcement_lag_days`` after period end, and a data gap is reported.
- PE uses TTM EPS from four consecutive quarters, falling back to annual EPS. EV/EBITDA and
  P/B use annual EBITDA, net debt and book value per share.
- A band uses the first lookback in ``bands.lookback_years`` with enough observations; the
  method value is the band median converted back to a price.
- The primary band for the baseline / top band is the sector's highest-weighted band method.
- DCF, reverse DCF and EPV run only for the FCFF and cyclical models (rule 10).
- Relative valuation needs ``relative.min_peers`` sector peers with a positive multiple (peer
  figures come from their latest reports); quality = ROCE (PE) or ROE (P/B), growth = 5y EPS
  CAGR.
"""

import statistics
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from typing import Any

import numpy as np
import pandas as pd

from app.core.config import SectorConfig, SectorModel, ValuationConfig
from app.fundamentals.metrics import Metric, average, by_year, column, ratio
from app.reports.data import StockData
from app.reports.dto import PeerStats
from app.valuation.bands import band, band_prices
from app.valuation.blend import Valuation, blend
from app.valuation.dcf import (
    DcfInputs,
    DcfResult,
    SensitivityGrid,
    base_inputs,
    beta_estimate,
    cost_of_equity_explained,
    default_g1,
    run_scenarios,
    sensitivity_grid,
    wacc,
)
from app.valuation.epv import epv, graham_number, normalised_ebit
from app.valuation.relative import relative_value
from app.valuation.reverse_dcf import ReverseDcf, reverse_dcf
from app.valuation.sector_models import (
    ModelValue,
    insurance_appraisal,
    justified_pb_grid,
    justified_pb_two_stage,
    nav_value,
    normalised_ev_ebitda,
    p_ev_value,
    residual_income,
    sotp_value,
)

DCF_MODELS = frozenset({SectorModel.FCFF, SectorModel.CYCLICAL})
BAND_METHODS = {"pe": "band_pe", "ev_ebitda": "band_ev_ebitda", "pb": "band_pb"}


@dataclass
class ValuationRun:
    sector_key: str
    sector: SectorConfig
    cmp: float
    method_values: dict[str, float | None]
    method_reasons: dict[str, list[str]]
    scenarios: dict[str, DcfResult]
    base: DcfInputs | None
    sensitivity: SensitivityGrid | None
    reverse: ReverseDcf | None
    epv: float | None
    extra: dict[str, float | None]
    primary_band_prices: dict[int, float] | None
    bvps: float | None
    single_model_value: float | None
    beta: float | None
    ke: float | None
    wacc: float | None
    market_cap_cr: float | None
    peer: PeerStats
    assumed_nil: list[str] = field(default_factory=list)
    gaps: list[tuple[str, str]] = field(default_factory=list)
    announcement_dates_assumed: bool = False
    reasons: list[str] = field(default_factory=list)
    # banks: two-stage justified P/B inputs and its normalised ROE x g x Ke sensitivity
    justified_pb_inputs: dict[str, float | None] | None = None
    justified_pb_grid: list[dict[str, float | None]] = field(default_factory=list)

    def blend(self, grade: str, config: ValuationConfig) -> Valuation:
        bear = self.scenarios.get("bear")
        bull = self.scenarios.get("bull")
        return blend(
            cmp=self.cmp,
            sector=self.sector,
            method_values=self.method_values,
            provisional_grade=grade,
            config=config,
            bear_dcf=bear.value_per_share if bear else None,
            bull_dcf=bull.value_per_share if bull else None,
            epv=self.epv,
            band_prices=self.primary_band_prices,
            book_value_ps=self.bvps,
            single_model_value=self.single_model_value,
        )


# ───────────────────────── helpers ─────────────────────────


def _num(v: object) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


def _latest_shares(annual: pd.DataFrame, quarterly: pd.DataFrame) -> tuple[float | None, str]:
    """The most recent diluted share count on file and its period. Both frames are indexed
    by period end; a balance-sheet-only year row has no count, so the latest row alone is not
    enough. The later period wins (a quarter after the last fiscal year)."""
    best: tuple[pd.Timestamp, float, str] | None = None
    for frame, label in ((annual, "year"), (quarterly, "quarter")):
        if frame.empty or "shares_diluted_cr" not in frame.columns:
            continue
        s = pd.to_numeric(frame["shares_diluted_cr"], errors="coerce").dropna()
        s = s[s > 0].sort_index()
        if s.empty:
            continue
        end = pd.Timestamp(s.index[-1])
        if best is None or end > best[0]:
            best = (end, float(s.iloc[-1]), f"{label} ended {end.date()}")
    return (best[1], best[2]) if best else (None, "")


def _risk_free_gap(vc: ValuationConfig, today: date) -> str | None:
    if vc.risk_free_as_of is None:
        return (f"risk-free rate {vc.risk_free_rate:.2%} has no date: set "
                "valuation.risk_free_as_of after checking the 10-year G-sec yield")  # fmt: skip
    age = (today - vc.risk_free_as_of).days
    if age > vc.risk_free_max_age_days:
        return (f"risk-free rate {vc.risk_free_rate:.2%} is {age} days old "
                f"(as of {vc.risk_free_as_of}); update valuation.yaml")  # fmt: skip
    return None


def effective_dates(frame: pd.DataFrame, lag_days: int) -> tuple[pd.DatetimeIndex, bool]:
    """Announcement date per row, or period end + ``lag_days`` when unknown (flagged)."""
    ends = pd.DatetimeIndex(frame.index)
    if "announcement_date" in frame.columns:
        ann = pd.to_datetime(frame["announcement_date"], errors="coerce")
    else:
        ann = pd.Series(pd.NaT, index=frame.index)
    assumed = bool(ann.isna().any())
    fallback = pd.Series(ends + pd.Timedelta(days=lag_days), index=frame.index)
    return pd.DatetimeIndex(ann.fillna(fallback)), assumed


def known_series(values: Any, periods: pd.DatetimeIndex, known: pd.DatetimeIndex) -> pd.Series:
    """A point-in-time step series: on each date, the value of the latest *period* known by
    then. Rows are keyed by the date they became known; several periods known the same day
    (a filing's comparatives, Q4 with the full year, a vendor year dated by the assumed lag)
    keep only the latest period, and a period older than one already known never replaces it.
    """
    frame = pd.DataFrame({"value": pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(),
                          "period": periods, "known": known}).dropna()  # fmt: skip
    if frame.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    frame = frame.sort_values(["known", "period"], kind="stable")
    newer = frame["period"] > frame["period"].cummax().shift(1)
    frame = frame[newer | (frame.index == frame.index[0])]
    frame = frame.drop_duplicates("known", keep="last")
    return pd.Series(frame["value"].to_numpy(), index=pd.DatetimeIndex(frame["known"]),
                     dtype=float)  # fmt: skip


def normalised_roe(annual: pd.DataFrame, years: int) -> tuple[float | None, str]:
    """Median ROE (PAT / average equity) over the last ``years`` fiscal years."""
    df = by_year(annual)
    if df.empty:
        return None, "normalised ROE: no annual statements"
    roe = ratio(column(df, "pat"), average(column(df, "total_equity"))).dropna().tail(years)
    if roe.empty:
        return None, "normalised ROE: needs PAT and two years of equity"
    return float(roe.median()), (f"normalised ROE {roe.median():.1%} = median of FY"
                                 f"{int(roe.index.min())}-FY{int(roe.index.max())} "
                                 f"({len(roe)} years)")  # fmt: skip


def ttm_by_quarter(quarterly: pd.DataFrame, col: str) -> pd.Series:
    """TTM sum of ``col`` keyed by quarter-end period, only over 4 consecutive quarters."""
    if quarterly.empty or col not in quarterly.columns:
        return pd.Series(dtype=float)
    q = quarterly.sort_index()
    vals = pd.to_numeric(q[col], errors="coerce")
    periods = pd.DatetimeIndex(q.index).to_period("Q")
    out: dict[pd.Period, float] = {}
    for i in range(3, len(q)):
        window = periods[i - 3 : i + 1]
        chunk = vals.iloc[i - 3 : i + 1]
        if all((b - a).n == 1 for a, b in pairwise(window)) and chunk.notna().all():
            out[periods[i]] = float(chunk.sum())
    return pd.Series(out, dtype=float)


def _latest_before(series: pd.Series, when: pd.Timestamp) -> float | None:
    s = series.dropna()
    s = s[s.index <= when]
    return float(s.iloc[-1]) if len(s) else None


def _median_positive(values: list[float | None]) -> float | None:
    pos = [v for v in values if v is not None and v > 0]
    return float(statistics.median(pos)) if pos else None


@dataclass
class _Balance:
    net_debt: float
    minority: float
    non_op: float
    shares: float


def _balance(df: pd.DataFrame) -> tuple[_Balance | None, list[str], list[str]]:
    """Latest-year balance items for equity bridges. Debt, cash and shares are required;
    minority interest and non-operating investments are taken as nil when not reported
    (returned as ``assumed_nil`` so the caller records a data gap), as in the DCF."""
    if df.empty:
        return None, [], ["no annual data"]
    row = df.iloc[-1]
    need = {
        k: _num(row.get(k)) for k in ("total_debt", "cash_and_equivalents", "shares_diluted_cr")
    }
    missing = [k for k, v in need.items() if v is None]
    if missing:
        return None, [], [f"missing {', '.join(missing)} (latest year)"]
    nil = [
        k for k in ("minority_interest_bs", "non_operating_investments") if _num(row.get(k)) is None
    ]
    return (
        _Balance(
            net_debt=(need["total_debt"] or 0.0) - (need["cash_and_equivalents"] or 0.0),
            minority=_num(row.get("minority_interest_bs")) or 0.0,
            non_op=_num(row.get("non_operating_investments")) or 0.0,
            shares=need["shares_diluted_cr"] or 0.0,
        ),
        nil,
        [],
    )


# ───────────────────────── main ─────────────────────────


def run_valuation(
    data: StockData,
    metrics: dict[str, Metric],
    sector_key: str,
    sector: SectorConfig,
    config: ValuationConfig,
    *,
    sensitivity: bool = True,
) -> ValuationRun:
    vc = config
    close = data.daily["close"]
    cmp = float(close.iloc[-1])
    as_of = pd.Timestamp(close.index[-1])
    df = by_year(data.annual) if not data.annual.empty else pd.DataFrame()
    reasons: list[str] = []
    mr: dict[str, list[str]] = {}
    mv: dict[str, float | None] = {}
    extra: dict[str, float | None] = {}
    jpb_grid: list[dict[str, float | None]] = []
    jpb_inputs: dict[str, float | None] | None = None
    assumed_nil: list[str] = []
    gaps: list[tuple[str, str]] = []  # (field, reason): inputs the report lists as data gaps

    def mval(key: str) -> float | None:
        m = metrics.get(key)
        return m.value if m is not None else None

    # ── per-share series (point-in-time step series) ──
    lag = vc.bands.assumed_announcement_lag_days
    annual = data.annual.sort_index()
    a_eff, a_assumed = effective_dates(annual, lag.annual)
    q = data.quarterly.sort_index()
    q_eff, q_assumed = effective_dates(q, lag.quarterly)

    def annual_series(values: pd.Series) -> pd.Series:
        return known_series(pd.to_numeric(values, errors="coerce").to_numpy(),
                            pd.DatetimeIndex(annual.index), a_eff)  # fmt: skip

    def acol(name: str) -> pd.Series:
        return annual[name] if name in annual.columns else pd.Series(np.nan, index=annual.index)

    shares = pd.to_numeric(acol("shares_diluted_cr"), errors="coerce")
    ttm_eps = ttm_by_quarter(q, "eps_diluted")
    if len(ttm_eps):
        period_eff = dict(zip(pd.DatetimeIndex(q.index).to_period("Q"), q_eff, strict=True))
        eps_series = known_series(
            ttm_eps.to_numpy(),
            pd.DatetimeIndex([p.end_time.normalize() for p in ttm_eps.index]),
            pd.DatetimeIndex([period_eff[p] for p in ttm_eps.index]),
        )
        eps_label = "TTM EPS"
    else:
        eps_series = annual_series(acol("eps_diluted"))
        eps_label = "annual EPS"
    bvps_raw = pd.to_numeric(acol("book_value_per_share"), errors="coerce")
    bvps_raw = bvps_raw.fillna(pd.to_numeric(acol("total_equity"), errors="coerce") / shares)
    bvps_series = annual_series(bvps_raw)
    ebitda_ps = annual_series(pd.to_numeric(acol("ebitda"), errors="coerce") / shares)
    net_debt_ps = annual_series(
        (
            pd.to_numeric(acol("total_debt"), errors="coerce")
            - pd.to_numeric(acol("cash_and_equivalents"), errors="coerce")
        )
        / shares
    )
    eps_now = _latest_before(eps_series, as_of)
    bvps = _latest_before(bvps_series, as_of)
    ebitda_ps_now = _latest_before(ebitda_ps, as_of)
    nd_ps_now = _latest_before(net_debt_ps, as_of)
    announcement_assumed = a_assumed or (q_assumed and len(ttm_eps) > 0)
    if announcement_assumed:
        reasons.append(
            "announcement dates missing: filings treated as known "
            f"{lag.quarterly}/{lag.annual} days after quarter/year end (data gap)"
        )

    # ── bands ──
    denominators: dict[str, tuple[pd.Series, pd.Series | None, float | None, float]] = {
        "pe": (eps_series, None, eps_now, 0.0),
        "ev_ebitda": (ebitda_ps, net_debt_ps, ebitda_ps_now, nd_ps_now or 0.0),
        "pb": (bvps_series, None, bvps, 0.0),
    }
    band_px: dict[str, dict[int, float] | None] = {}
    band_median: dict[str, float | None] = {}
    for m in vc.bands.multiples:
        den, off, cur, cur_off = denominators[m]
        key = BAND_METHODS[m]
        mr[key] = []
        chosen = None
        for lb in vc.bands.lookback_years:
            b = band(
                m,
                close,
                den,
                lookback_years=lb,
                min_observations=vc.bands.min_observations,
                offset=off,
                as_of=as_of,
            )
            mr[key] += b.reasons
            if b.ok:
                chosen = b
                break
        if m == "ev_ebitda" and nd_ps_now is None:
            chosen = None
            mr[key].append("net debt per share unavailable")
        px = band_prices(chosen, cur, cur_off) if chosen is not None else None
        if chosen is not None and px is None:
            mr[key].append(f"current {m} denominator not positive")
        band_px[key] = px
        band_median[m] = chosen.median if chosen is not None else None
        mv[key] = px[0] if px else None
    if eps_label == "annual EPS":
        mr.setdefault("band_pe", []).append("PE on annual EPS (no 4 consecutive quarters)")
    weights = {str(k): float(w) for k, w in (sector.weights or {}).items()}
    band_keys = [k for k in weights if k in band_px]
    primary = max(band_keys, key=lambda k: weights[k]) if band_keys else None
    primary_prices = band_px.get(primary) if primary else None
    if primary:
        reasons.append(f"primary band: {primary}")

    # ── cost of capital ──
    latest = df.iloc[-1] if len(df) else pd.Series(dtype=float)
    shares_now, shares_label = _latest_shares(data.annual, data.quarterly)
    mcap = cmp * shares_now if shares_now else None
    if mcap is not None:
        reasons.append(f"market cap ₹{mcap:,.0f} Cr = {shares_now:,.2f} Cr shares "
                       f"({shares_label}) x price ₹{cmp:,.2f}")  # fmt: skip
    else:
        why = "no diluted share count (PAT / diluted EPS) in any annual or quarterly row"
        reasons.append(f"market cap unavailable: {why}")
        gaps.append(("market_cap", why))
    est = (
        beta_estimate(close, data.benchmark_close, vc, as_of=as_of)
        if data.benchmark_close is not None
        else None
    )
    beta = est.value if est is not None else None
    if est is None:
        why = (f"benchmark {vc.beta.benchmark} history missing or shorter than "
               f"{vc.beta.lookback_years} years")  # fmt: skip
        reasons.append(f"beta unavailable: {why}")
        gaps.append(("beta", why))
    elif est.clamped is not None:
        bound = vc.beta.floor if est.clamped == "floor" else vc.beta.cap
        why = (f"beta {est.unclamped:.2f} clamped at the {est.clamped} {bound:.2f} "
               "(valuation.yaml beta); cost of equity uses the clamped value")  # fmt: skip
        reasons.append(why)
        gaps.append(("beta", why))
    rf_gap = _risk_free_gap(vc, as_of.date())
    if rf_gap is not None:
        gaps.append(("risk_free_rate", rf_gap))
    ke: float | None = None
    if beta is not None and mcap:
        ke, ke_text = cost_of_equity_explained(beta, mcap, vc)
        reasons.append(f"{ke_text} (Rf: {vc.risk_free_source}, as of "
                       f"{vc.risk_free_as_of or 'not dated'})")  # fmt: skip
    else:
        missing = [n for n, v in (("beta", beta), ("market cap", mcap)) if not v]
        reasons.append(f"cost of equity unavailable: no {' / '.join(missing)}")
        gaps.append(("cost_of_equity", f"needs {' and '.join(missing)}"))
    pbt, tax = _num(latest.get("pbt")), _num(latest.get("tax"))
    tax_rate = tax / pbt if pbt and pbt > 0 and tax is not None and 0 <= tax / pbt <= 1 else None
    if tax_rate is None:
        tax_rate = vc.tax_rate_default
    wacc_rate: float | None = data.overrides.wacc
    if sector.model not in DCF_MODELS:
        wacc_rate = None  # banks / insurers / NAV / SOTP: equity models only (rule 10)
        reasons.append(f"{sector.model.value} model: valued on the cost of equity; "
                       "no WACC or FCFF")  # fmt: skip
    elif wacc_rate is not None:
        reasons.append(f"WACC {wacc_rate:.2%} (user override)")
    elif ke is not None and mcap:
        debt = _num(latest.get("total_debt"))
        prev_debt = _num(df.iloc[-2].get("total_debt")) if len(df) > 1 else None
        if debt is None:
            reasons.append("WACC unavailable: total debt not reported")
        else:
            try:
                w = wacc(
                    ke=ke,
                    market_cap_cr=mcap,
                    debt_cr=debt,
                    interest_cr=_num(latest.get("interest")),
                    avg_debt_cr=(debt + prev_debt) / 2 if prev_debt is not None else None,
                    tax_rate=tax_rate,
                )
                wacc_rate = w.wacc
                reasons.append(f"WACC {w.wacc:.2%} (Ke {ke:.2%}, beta {beta:.2f})")
            except ValueError as exc:
                reasons.append(f"WACC unavailable: {exc}")
    else:
        reasons.append("WACC unavailable: no cost of equity")

    # ── DCF / reverse DCF / EPV ──
    scenarios: dict[str, DcfResult] = {}
    base = None
    grid = None
    rev = None
    epv_value = None
    hist_growth = mval("sales_cagr_5y")
    bal, bal_nil, bal_reasons = _balance(df)
    if sector.model in DCF_MODELS:
        dcf_over = data.overrides.dcf()
        g1 = dcf_over.get("g1", default_g1(hist_growth, sector, vc))
        if wacc_rate is None:
            mr["dcf_base"] = ["no WACC: DCF not run"]
        else:
            bi = base_inputs(
                annual, g1=g1, wacc_rate=wacc_rate, config=vc, overrides=dcf_over or None
            )
            mr["dcf_base"] = list(bi.reasons)
            assumed_nil += bi.assumed_nil
            if bi.inputs is not None:
                base = bi.inputs
                scenarios = run_scenarios(base, vc)
                for k, r in scenarios.items():
                    mr.setdefault(f"dcf_{k}", []).extend(r.reasons)
                grid = sensitivity_grid(base, vc) if sensitivity else None
                rev = reverse_dcf(base, cmp, hist_growth, vc)
        base_res = scenarios.get("base")
        mv["dcf_base"] = base_res.value_per_share if base_res else None

        if wacc_rate is not None and bal is not None:
            e = epv(
                normalised_ebit_cr=normalised_ebit(annual, vc.epv.normalise_years),
                tax_rate=tax_rate,
                wacc=wacc_rate,
                net_debt=bal.net_debt,
                minority_interest=bal.minority,
                non_op_investments=bal.non_op,
                shares_cr=bal.shares,
            )
            epv_value = e.value
            mr["epv"] = e.reasons
        else:
            mr["epv"] = ["EPV needs WACC and balance-sheet items", *bal_reasons]
        extra["epv"] = epv_value
    else:
        mr["dcf_base"] = [f"{sector.model.value} model: no FCFF DCF (rule 10)"]
    g = graham_number(eps_now, bvps, vc)
    extra["graham_number"] = g.value
    mr["graham_number"] = g.reasons

    # ── relative ──
    roce, roe, eps_growth = mval("roce_latest"), mval("roe_latest"), mval("eps_cagr_5y")

    def rel(
        key: str, label: str, attr: str, qattr: str, own_q: float | None, per_share: float | None
    ) -> None:
        peers = [p for p in data.peers if (getattr(p, attr) or 0) > 0]
        if len(peers) < vc.relative.min_peers:
            mv[key] = None
            mr[key] = [
                f"{label}: {len(peers)} sector peers with a positive multiple "
                f"(< {vc.relative.min_peers})"
            ]
            return
        r = relative_value(
            peer_multiples=[float(getattr(p, attr)) for p in peers],
            peer_quality=_median_positive([getattr(p, qattr) for p in peers]),
            peer_growth=_median_positive([p.eps_growth for p in peers]),
            quality=own_q,
            growth=eps_growth,
            per_share_metric=per_share,
            config=vc,
            label=label,
        )
        mv[key], mr[key] = r.value, r.reasons

    # ── sector models ──
    single: float | None = None
    ov = data.overrides

    def use(key: str, r: ModelValue) -> None:
        mv[key], mr[key] = r.value, r.reasons

    if sector.model in DCF_MODELS:
        rel("relative", "PE", "pe", "roce", roce, eps_now)
    if sector.model is SectorModel.CYCLICAL and sector.normalise_years:
        if bal is None:
            use("normalised_ev_ebitda", ModelValue(None, bal_reasons))
        else:
            use(
                "normalised_ev_ebitda",
                normalised_ev_ebitda(
                    annual,
                    normalise_years=sector.normalise_years,
                    band_median_ev_ebitda=band_median.get("ev_ebitda"),
                    net_debt=bal.net_debt,
                    minority_interest=bal.minority,
                    non_op_investments=bal.non_op,
                    shares_cr=bal.shares,
                ),
            )
    if sector.model is SectorModel.BANK:
        jp = vc.justified_pb
        g_t = min(vc.dcf.terminal_growth, jp.max_terminal_growth)
        roe_norm, norm_why = normalised_roe(annual, jp.normalised_roe_years)
        if ov.normalised_roe is not None:
            roe_norm, norm_why = ov.normalised_roe, "normalised ROE: user override"
        pat, div = _num(latest.get("pat")), _num(latest.get("dividends_paid"))
        retention = 1 - div / pat if pat and pat > 0 and div is not None else None
        if ke is None:
            use("justified_pb", ModelValue(None, ["cost of equity unavailable"]))
        else:
            jv = justified_pb_two_stage(roe=roe, roe_norm=roe_norm, ke=ke, g=g_t, bvps=bvps,
                                        retention=retention, years=jp.stage1_years)  # fmt: skip
            use("justified_pb", ModelValue(jv.value, [*jv.reasons, norm_why]))
            jpb_grid = justified_pb_grid(
                roe=roe, roe_norm=roe_norm, ke=ke, g=g_t, bvps=bvps, retention=retention,
                years=jp.stage1_years, roe_steps=jp.grid.roe_steps, g_steps=jp.grid.g_steps,
                ke_steps=jp.grid.ke_steps,
            )  # fmt: skip
            jpb_inputs = {"roe": roe, "normalised_roe": roe_norm, "ke": ke, "g": g_t,
                          "retention": retention,
                          "stage1_years": float(jp.stage1_years)}  # fmt: skip
            if retention is not None:
                ri = residual_income(bvps=bvps, roe=roe, ke=ke, g=g_t, payout=1 - retention,
                                     years=vc.dcf.stage1_years)  # fmt: skip
                extra["residual_income"] = ri.value
                mr["residual_income"] = ri.reasons
            else:
                mr["residual_income"] = ["payout ratio unavailable"]
        rel("relative_pb", "P/B", "pb", "roe", roe, bvps)
    if sector.model is SectorModel.INSURANCE:
        ev_ps = ov.embedded_value_per_share
        use("band_p_ev", p_ev_value(ev_ps, ov.p_ev_band_median, "own P/EV median"))
        use("relative_p_ev", p_ev_value(ev_ps, ov.peer_p_ev, "peer P/EV"))
        app = insurance_appraisal(ev_ps, ov.vnb_per_share, ov.vnb_multiple)
        extra["appraisal_value"] = app.value
        mr["appraisal_value"] = app.reasons
    if sector.model is SectorModel.NAV:
        nv = nav_value(ov.nav_per_share, sector.nav_discount or 0.0)
        single, mr["nav"] = nv.value, nv.reasons
    if sector.model is SectorModel.SOTP:
        if bal is None:
            single, mr["sotp"] = None, bal_reasons
        else:
            sv = sotp_value(
                listed_holdings_value_cr=ov.listed_holdings_value_cr,
                standalone_value_cr=ov.standalone_value_cr,
                holding_discount=sector.holding_discount or 0.0,
                net_debt=bal.net_debt,
                shares_cr=bal.shares,
            )
            single, mr["sotp"] = sv.value, sv.reasons
    if bal_nil and sector.model not in DCF_MODELS:
        assumed_nil += bal_nil

    # Only the sector's weighted methods are blended; the rest stay informational.
    blended = {k: mv.get(k) for k in weights}
    for k, v in mv.items():
        if k not in weights:
            extra.setdefault(k, v)

    peer = PeerStats(
        symbol=data.symbol,
        sector=sector_key,
        pe=cmp / eps_now if eps_now and eps_now > 0 else None,
        pb=cmp / bvps if bvps and bvps > 0 else None,
        ev_ebitda=(
            (cmp + nd_ps_now) / ebitda_ps_now
            if ebitda_ps_now and ebitda_ps_now > 0 and nd_ps_now is not None
            else None
        ),
        roce=roce,
        roe=roe,
        eps_growth=eps_growth,
    )
    return ValuationRun(
        sector_key=sector_key,
        sector=sector,
        cmp=cmp,
        method_values=blended,
        method_reasons=mr,
        scenarios=scenarios,
        base=base,
        sensitivity=grid,
        reverse=rev,
        epv=epv_value,
        extra=extra,
        primary_band_prices=primary_prices,
        bvps=bvps,
        single_model_value=single,
        beta=beta,
        ke=ke,
        wacc=wacc_rate,
        market_cap_cr=mcap,
        peer=peer,
        assumed_nil=sorted(set(assumed_nil)),
        gaps=gaps,
        announcement_dates_assumed=announcement_assumed,
        justified_pb_inputs=jpb_inputs,
        justified_pb_grid=jpb_grid,
        reasons=reasons,
    )
