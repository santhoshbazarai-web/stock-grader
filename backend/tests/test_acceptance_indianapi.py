"""Prompt B acceptance (Task 12), NSE disabled: statements come only from the Indian API
fixtures (recorded answers, fake HTTP), prices are a synthetic random walk ending at the
vendor's quoted NSE price (not real quotes). Each stock gets a full report.

- HDFCBANK: 12 years of P&L / BS / CF, depth "full", bank valuation (justified P/B, P/B
  band; no WACC / FCFF DCF), levels, grade, action, zone; the merger note.
- TCS, ITC: general mapping, FCFF DCF and P/E bands. TATASTEEL: general mapping; its sector
  (metals) is valued as a cyclical (normalised EV/EBITDA + bands), never with a bank model.
"""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import insert, select, update

from app.core.config import get_config
from app.db.models import Instrument, PriceDaily
from app.devtools.synthetic import ensure_instrument, store_prices
from app.jobs.common import readjust
from app.reports.dto import StockReport
from app.reports.service import build_for
from tests.jobs_support import Env
from tests.test_indianapi_job import COMPANIES, FIX, FakeVendor, run, setup

EXTRA = {  # symbol → (fixture prefix, ISIN, master name, vendor name)
    "ITC": ("itc", "INE154A01025", "ITC Limited", "ITC"),
    "TATASTEEL": ("tatasteel", "INE081A01020", "Tata Steel Limited", "Tata Steel"),
}
LAST_SESSION = date(2026, 9, 30)  # after the FY2026 statements
SECTORS = {"HDFCBANK": "banks", "TCS": "it_services", "ITC": "fmcg", "TATASTEEL": "metals"}


DAYS = pd.bdate_range(end=LAST_SESSION, periods=780)  # three years
INDEX_RET = np.random.default_rng(99).normal(0.0004, 0.009, len(DAYS))


def _index(env: Env) -> None:
    """A synthetic NIFTY500 (the beta benchmark)."""
    level = 20000 * np.exp(np.cumsum(INDEX_RET))
    frame = pd.DataFrame({"open": level, "high": level * 1.005, "low": level * 0.995,
                          "close": level, "volume": 1.0}, index=DAYS)  # fmt: skip
    with env.session() as s:
        store_prices(s, ensure_instrument(s, "NIFTY500", None, is_index=True), frame)
        s.commit()


def _prices(env: Env, symbol: str, last: float, seed: int) -> None:
    """Synthetic daily bars (fyers) ending at ``last``, beta ~0.9 to the index."""
    rng = np.random.default_rng(seed)
    days = DAYS
    walk = np.exp(np.cumsum(0.9 * INDEX_RET + rng.normal(0, 0.008, len(days))))
    close = walk / walk[-1] * last
    with env.session() as s:
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
        assert iid is not None
        s.execute(insert(PriceDaily), [
            {"instrument_id": iid, "date": d.date(), "open": c * 0.998, "high": c * 1.01,
             "low": c * 0.99, "close": c, "volume": 1_000_000, "source": "fyers"}
            for d, c in zip(days, close, strict=True)
        ])  # fmt: skip
        s.execute(update(Instrument).where(Instrument.id == iid)
                  .values(sector=SECTORS[symbol]))  # fmt: skip
        readjust(s, iid, get_config().providers.adjustment)
        s.commit()


@pytest.fixture
def reports(env: Env) -> dict[str, StockReport]:
    COMPANIES.update(EXTRA)
    try:
        vendor = FakeVendor()
        setup(env, vendor)
        rec = run(env, *SECTORS)
    finally:
        for k in EXTRA:
            del COMPANIES[k]
    assert rec.status.value == "success", rec.outcome
    assert not env.nse.action_requests and not env.nse.filing_requests  # NSE never asked
    _index(env)
    out = {}
    for i, sym in enumerate(SECTORS):
        prefix = (COMPANIES | EXTRA)[sym][0]
        quote = json.loads((FIX / f"{prefix}_stock.json").read_text())["currentPrice"]["NSE"]
        _prices(env, sym, float(quote), seed=i)
        with env.session() as s:
            out[sym] = build_for(s, sym, get_config()).report
    return out


def _methods(r: StockReport) -> set[str]:
    return {m.name for m in r.valuation.methods}


def test_hdfcbank_bank_model_from_twelve_years(env: Env, reports: dict[str, StockReport]) -> None:
    r = reports["HDFCBANK"]
    assert r.sources["fundamentals"] == "indianapi"
    assert r.data_depth is not None and r.data_depth.level == "full"
    assert r.data_depth.pl_years >= 12 and r.grade_confidence == "full"
    methods = _methods(r)
    assert "justified_pb" in methods and not {m for m in methods if "dcf" in m}
    assert r.levels.fair_value is not None and r.levels.baseline is not None
    assert r.grade is not None and r.action is not None and r.zone is not None
    assert any(x.startswith("Structural break FY2024 (merger)") for x in r.reasons)
    names = {m.name: m for m in r.bank_metrics}
    assert names["bvps"].proxy and names["bvps"].value == pytest.approx(380.72, abs=0.05)
    assert names["gnpa_pct"].value is None and not names["gnpa_pct"].proxy
    health = next(p for p in r.pillars if p.pillar == "health")
    assert health.score is not None and health.confidence == "reduced"
    # GNPA, NNPA, CAR, CASA not reported: pulled toward 50 (was ~95 from the proxies alone)
    assert health.score < 70 and any("of the input weight missing" in x for x in health.reasons)
    # relative P/B from the vendor's peer list (5 banks), adjusted for ROE only
    rel = next(m for m in r.valuation.methods if m.name == "relative_pb")
    assert rel.value is not None and rel.reasons[0].startswith("peers (5 vendor): ICICI Bank 2.85x")


def test_tcs_general_model_fcff_and_bands(reports: dict[str, StockReport]) -> None:
    r = reports["TCS"]
    methods = _methods(r)
    assert {"dcf_base", "band_pe"} <= methods and "justified_pb" not in methods
    assert r.valuation.model == "fcff" and r.valuation.wacc is not None
    assert r.levels.fair_value is not None and r.levels.baseline is not None
    assert r.grade is not None and r.action is not None and r.zone is not None
    assert r.data_depth is not None and r.data_depth.level == "full"
    assert r.bank_metrics == []


@pytest.mark.parametrize(("symbol", "model"), [("ITC", "fcff"), ("TATASTEEL", "cyclical")])
def test_stock_only_companies_name_their_gaps(
    reports: dict[str, StockReport], symbol: str, model: str
) -> None:
    """Only /stock fixtures exist for ITC and TATASTEEL (8 years, no history, so no interest
    expense): general mapping, the missing inputs named (interest → no WACC, ROCE, coverage),
    never a bank model, never a crash, never a 0 standing in for interest."""
    r = reports[symbol]
    assert r.sources["fundamentals"] == "indianapi" and r.bank_metrics == []
    assert r.valuation.model == model and "justified_pb" not in _methods(r)
    assert r.data_depth is not None and r.data_depth.pl_years == 8
    gaps = [*r.valuation.reasons, *r.data_gaps]
    assert any("interest" in x for x in gaps), gaps
    assert r.levels.top_band is not None or r.levels.fair_value is None  # never a made-up FV


def test_periods_known_on_the_same_day_do_not_break_the_report(
    env: Env, reports: dict[str, StockReport]
) -> None:
    """A filing's comparatives (or Q4 with the full year, or a vendor year dated by the assumed
    lag) make several periods known on one day: the report still builds, using the latest."""
    from app.db.models import FinAnnual, FinQuarterly

    with env.session() as s:
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == "HDFCBANK"))
        # FY2026 filed with FY2025 as its comparative; Q4 and the year on the same day
        s.execute(update(FinAnnual).where(FinAnnual.instrument_id == iid,
                                          FinAnnual.fiscal_year.in_([2025, 2026]))
                  .values(announcement_date=date(2026, 4, 18)))  # fmt: skip
        s.execute(update(FinQuarterly).where(FinQuarterly.instrument_id == iid,
                                             FinQuarterly.period_end >= date(2025, 12, 31))
                  .values(announcement_date=date(2026, 4, 18)))  # fmt: skip
        s.commit()
        r = build_for(s, "HDFCBANK", get_config()).report
    assert r.grade is not None and r.levels.fair_value is not None
    assert any("band" in m.name and m.value is not None for m in r.valuation.methods)


def test_bank_report_lists_only_metrics_that_apply(reports: dict[str, StockReport]) -> None:
    r = reports["HDFCBANK"]
    # 10-year growth across the merger: per share, weighted shares where no year-end count
    assert r.fundamentals["sales_cagr_10y"] is not None
    assert r.fundamentals["eps_cagr_10y"] is not None
    for k in ("roce_latest", "roic_latest", "ccc_days", "opm_ttm", "piotroski", "altman_z2"):
        assert k not in r.fundamentals, k  # not applicable to a bank: never "missing"
    assert "beneish" not in r.knockouts.unknown
    assert not any("ebitda_cagr" in x for x in r.reasons)
    assert "roe_latest" in r.fundamentals  # the rest is unchanged
    # how the 10-year growth across the merger was measured is in the notes
    notes = r.fundamentals_notes
    assert notes["sales_cagr_10y"].startswith("per share (")
    assert notes["eps_cagr_10y"].startswith("per share (")
    # every metric left None says why
    assert all(k in notes for k, v in r.fundamentals.items() if v is None)


def test_bank_fundamentals_charts_and_earned_premium(
    env: Env, reports: dict[str, StockReport]
) -> None:
    from app.reports.history import BANK_SERIES, load_history

    with env.session() as s:
        h = load_history(s, "HDFCBANK", get_config())
    assert h is not None and h.model == "bank" and h.series == list(BANK_SERIES)
    assert h.years_available == len(h.years) == 10  # the window; 12 years on file
    last = h.years[-1]
    assert last.revenue is None and last.ebitda is None and last.fcf is None  # not shown
    assert last.nii and last.advances and last.deposits and last.pat
    assert last.roe == pytest.approx(0.137, abs=0.01) and last.bvps == pytest.approx(381, rel=0.01)
    assert last.cd_ratio is not None and 0.5 < last.cd_ratio < 1.5
    assert not {"revenue", "ebitda", "roce", "ccc_days"} & set(h.missing)
    r = reports["HDFCBANK"]
    codes = {c.code for c in r.earned_premium_detail.conditions}
    assert r.earned_premium_detail.out_of == 10
    assert {"roe_up", "roa_strong", "loan_growth", "deposit_growth", "nim_strong"} <= codes
    assert not {"roce_up", "operating_leverage", "implied_growth"} & codes
    # equity-valued: no "taken as nil" EV inputs among the gaps
    assert not [g for g in r.data_gaps if g.startswith("non_operating_investments")]


def test_bank_governance_from_vendor_shareholding(
    env: Env, reports: dict[str, StockReport]
) -> None:
    from app.reports.history import load_history

    r = reports["HDFCBANK"]
    shp = r.shareholding
    assert shp is not None and shp.source == "indianapi"
    assert (shp.fii_pct, shp.dii_pct, shp.public_pct) == (41.82, 30.62, 27.56)
    assert shp.promoter_pct == 0.0 and shp.promoter_pledge_pct == 0.0
    assert shp.note is not None and shp.note.startswith("No identified promoter since Jun 2023")
    gov = next(p for p in r.pillars if p.pillar == "governance")
    assert gov.score is not None
    assert {s.name: s.value for s in gov.subs}["pledge_pct"] == 0.0
    assert "pledge" not in r.knockouts.unknown and "pledge" not in r.knockouts.triggered
    assert not any(g.startswith("shareholding:") for g in r.data_gaps)
    with env.session() as s:
        h = load_history(s, "HDFCBANK", get_config())
    assert h is not None and h.shareholding[-1].fii_pct == 41.82
    assert h.shareholding[-1].promoter_pledge_pct == 0.0 and h.shareholding_note
