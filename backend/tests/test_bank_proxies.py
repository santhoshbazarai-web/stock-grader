"""Bank proxy metrics (app/fundamentals/banking.py) and the bank health pillar with proxies
standing in for missing GNPA / CAR (app/scoring/pillars.py), on the HDFCBANK Indian API
fixtures (₹ crore; hand-checked below)."""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.core.config import get_config
from app.core.settings import config_dir_from_env
from app.data.indianapi_parse import SHARES_PER_CRORE, map_statements
from app.fundamentals.banking import PROXIES, bank_per_share, bank_summary
from app.fundamentals.indianapi_map import get_indianapi_map
from app.scoring.pillars import PillarInputs, health

FIX = Path(__file__).parent / "fixtures" / "indianapi"
CR = 1e7
EXTRA = ("advances", "deposits", "investments", "loan_loss_provisions", "operating_expenses",
         "net_interest_income")  # fmt: skip
HdfcData = tuple[pd.DataFrame, dict[int, float]]


@pytest.fixture(scope="module")
def hdfc() -> HdfcData:
    stock = json.loads((FIX / "hdfcbank_stock.json").read_text())
    hists = {
        s: json.loads((FIX / f"hdfcbank_{f}.json").read_text())
        for s, f in (("yoy_results", "pl"), ("balancesheet", "bs"), ("cashflow", "cf"))
    }
    m = map_statements(stock, hists, get_indianapi_map(config_dir_from_env()))
    rows, shares = [], {}
    for fy in (2024, 2025, 2026):
        end = date(fy, 3, 31)

        def cr(code: str, kind: str, end: date = end) -> float | None:
            v = m.get(code, end, kind)  # type: ignore[arg-type]
            return v / CR if v is not None else None

        pat, eps = cr("pat", "year"), m.get("eps_diluted", end, "year")
        rows.append({
            "period_end": pd.Timestamp(end), "fiscal_year": fy, "pat": pat,
            "other_income": cr("other_income", "year"),
            "total_equity": cr("total_equity", "instant"),
            "total_assets": cr("total_assets", "instant"),
            "dividends_paid": cr("dividends_paid", "year"),
            "shares_diluted_cr": pat / eps if pat and eps else None,
            "extra": {k: cr(k, "instant" if k in ("advances", "deposits", "investments")
                            else "year") for k in EXTRA},
        })  # fmt: skip
        shares[fy] = (m.get("shares_outstanding", end, "instant") or 0) / SHARES_PER_CRORE
    return pd.DataFrame(rows).set_index("period_end"), shares


def test_hdfcbank_fy26_proxies(hdfc: HdfcData) -> None:
    annual, _ = hdfc
    b = bank_summary(annual)
    # CD ratio 30,48,330 / 30,99,638 cr = 98.3%
    assert b["cd_ratio_pct"].value == pytest.approx(98.35, abs=0.05)
    # equity / assets 5,86,059 / 49,08,041 cr = 11.9%
    assert b["equity_to_assets_pct"].value == pytest.approx(11.94, abs=0.02)
    # deposits +14.3%, loans +12.1%
    assert b["deposit_growth_pct"].value == pytest.approx(14.34, abs=0.02)
    assert b["loan_growth_pct"].value == pytest.approx(12.07, abs=0.02)
    # credit cost 14,426 / avg(27,20,046, 30,48,330) = 0.50%
    assert b["credit_cost_pct"].value == pytest.approx(0.500, abs=0.003)
    # payout 20,706 / 76,026 = 27.2%
    assert b["payout_pct"].value == pytest.approx(27.24, abs=0.05)
    # GNPA, NNPA, CAR are not in the vendor's data: gaps with the missing inputs named
    assert b["gnpa_pct"].value is None and "gross_npa" in (b["gnpa_pct"].reason or "")
    assert b["car_pct"].value is None and "crar_pct" in (b["car_pct"].reason or "")
    assert "gnpa_pct" not in PROXIES and "cd_ratio_pct" in PROXIES


def test_per_share_proxies(hdfc: HdfcData) -> None:
    annual, shares = hdfc
    ps = bank_per_share(annual, price=721.20, shares_year_end=shares)
    # BVPS 5,86,059.47 cr / 1,539.34 cr shares = 380.72 (the vendor's keyMetrics: 380.72)
    assert ps["bvps"].value == pytest.approx(380.72, abs=0.01)
    assert ps["bvps"].reason == "proxy: equity / year-end shares"
    assert ps["pb"].value == pytest.approx(721.20 / 380.72, rel=1e-4)
    # PAT per year-end share: 70,792 / 1,530.44 → 76,026 / 1,539.34: +6.7%
    assert ps["eps_growth_ps_pct"].value == pytest.approx(6.73, abs=0.05)
    # FY24 needs FY23, which this frame does not hold: None, never guessed
    fy24 = bank_per_share(annual.iloc[:1], price=None, shares_year_end=shares, year=2024)
    assert fy24["eps_growth_ps_pct"].value is None
    assert bank_per_share(annual, price=None, shares_year_end=shares)["pb"].value is None


def test_health_scores_from_proxies_with_reduced_confidence(hdfc: HdfcData) -> None:
    annual, _ = hdfc
    b = bank_summary(annual)
    cfg = get_config().scoring
    x = PillarInputs(is_bank=True, credit_cost_pct=b["credit_cost_pct"].value,
                     equity_to_assets_pct=b["equity_to_assets_pct"].value)  # fmt: skip
    p = health(x, cfg)
    assert p.score is not None and p.confidence == "reduced"
    assert p.missing == ["gnpa_pct", "car_pct"]
    assert [s.name for s in p.subs] == ["credit_cost_pct", "equity_to_assets_pct"]
    assert "(proxy: gnpa_pct not reported)" in p.subs[0].reason
    assert p.reasons[-1] == ("health: gnpa_pct, car_pct not reported; scored from proxies "
                             "(reduced confidence)")  # fmt: skip
    # credit cost 0.50% → 100; equity/assets 11.94% → 85 + (0.94/3)·15 ≈ 89.7
    assert p.score == pytest.approx((100 + 89.7) / 2, abs=0.2)


def test_reported_metrics_are_never_replaced() -> None:
    cfg = get_config().scoring
    x = PillarInputs(is_bank=True, gnpa_pct=1.5, car_pct=17.0, credit_cost_pct=3.0,
                     equity_to_assets_pct=5.0)  # fmt: skip
    p = health(x, cfg)
    assert p.confidence == "full" and p.score == pytest.approx(85.0)
    assert [s.name for s in p.subs] == ["gnpa_pct", "car_pct"]
    # one reported, one proxied
    q = health(PillarInputs(is_bank=True, gnpa_pct=1.5, equity_to_assets_pct=11.0), cfg)
    assert q.confidence == "reduced" and q.missing == ["car_pct"] and q.score == pytest.approx(85)
    # nothing at all: no score, no proxy invented
    r = health(PillarInputs(is_bank=True), cfg)
    assert r.score is None and r.confidence == "full"
