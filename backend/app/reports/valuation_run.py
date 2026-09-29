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
from itertools import pairwise

import numpy as np
import pandas as pd

from app.core.config import SectorConfig, SectorModel, ValuationConfig
from app.fundamentals.metrics import Metric, by_year
from app.reports.data import StockData
from app.reports.dto import PeerStats
from app.valuation.bands import band, band_prices
from app.valuation.blend import Valuation, blend
from app.valuation.dcf import (
    DcfInputs,
    DcfResult,
    SensitivityGrid,
    base_inputs,
    blume_beta,
    cost_of_equity,
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
    justified_pb,
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
    announcement_dates_assumed: bool = False
    reasons: list[str] = field(default_factory=list)

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
    assumed_nil: list[str] = []

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
        s = pd.Series(pd.to_numeric(values, errors="coerce").to_numpy(), index=a_eff, dtype=float)
        return s.dropna()

    def acol(name: str) -> pd.Series:
        return annual[name] if name in annual.columns else pd.Series(np.nan, index=annual.index)

    shares = pd.to_numeric(acol("shares_diluted_cr"), errors="coerce")
    ttm_eps = ttm_by_quarter(q, "eps_diluted")
    if len(ttm_eps):
        period_eff = dict(zip(pd.DatetimeIndex(q.index).to_period("Q"), q_eff, strict=True))
        eps_series = pd.Series(
            ttm_eps.to_numpy(), index=pd.DatetimeIndex([period_eff[p] for p in ttm_eps.index])
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
    shares_now = _num(latest.get("shares_diluted_cr"))
    mcap = cmp * shares_now if shares_now else None
    beta = (
        blume_beta(close, data.benchmark_close, vc, as_of=as_of)
        if data.benchmark_close is not None
        else None
    )
    if beta is None:
        reasons.append("beta unavailable (benchmark history missing or too short)")
    ke = cost_of_equity(beta, mcap, vc) if beta is not None and mcap else None
    pbt, tax = _num(latest.get("pbt")), _num(latest.get("tax"))
    tax_rate = tax / pbt if pbt and pbt > 0 and tax is not None and 0 <= tax / pbt <= 1 else None
    if tax_rate is None:
        tax_rate = vc.tax_rate_default
    wacc_rate: float | None = data.overrides.wacc
    if wacc_rate is not None:
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
                grid = sensitivity_grid(base, vc)
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
        g_lr = sector.long_run_growth or 0.0
        if ke is None:
            use("justified_pb", ModelValue(None, ["cost of equity unavailable"]))
        else:
            use("justified_pb", justified_pb(roe=roe, ke=ke, g=g_lr, bvps=bvps))
            pat, div = _num(latest.get("pat")), _num(latest.get("dividends_paid"))
            if pat and pat > 0 and div is not None:
                ri = residual_income(
                    bvps=bvps, roe=roe, ke=ke, g=g_lr, payout=div / pat, years=vc.dcf.stage1_years
                )
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
        announcement_dates_assumed=announcement_assumed,
        reasons=reasons,
    )
