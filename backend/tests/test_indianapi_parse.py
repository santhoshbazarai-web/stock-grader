"""Indian API mapping (app/data/indianapi_parse.py, config/indianapi_map.yaml) on the recorded
responses in tests/fixtures/indianapi: identity, units, the /stock vs /historical_stats merge
and its 2% consistency check, quarters adding up to years, and missing values staying missing.
Expected figures are read from the fixtures (₹ crore)."""

import contextlib
import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.data.indianapi_parse import (
    CRORE_INR,
    check_stock_unit,
    label_end,
    map_statements,
    quarter_sums,
    stock_periods,
    verify_identity,
)
from app.fundamentals.indianapi_map import get_indianapi_map
from tests.conftest import REPO_CONFIG_DIR

FIX = Path(__file__).parent / "fixtures" / "indianapi"
AMAP = get_indianapi_map(REPO_CONFIG_DIR)
CR = CRORE_INR
HDFC_ISIN, TCS_ISIN = "INE040A01034", "INE467B01029"
FY = {y: date(y, 3, 31) for y in range(2015, 2027)}


def load(name: str) -> Any:
    return json.loads((FIX / f"{name}.json").read_text())


def hists(co: str) -> dict[str, Any]:
    names = {"yoy_results": "pl", "balancesheet": "bs", "cashflow": "cf", "quarter_results": "qr"}
    return {
        stats: load(f"{co}_{s}") for stats, s in names.items() if (FIX / f"{co}_{s}.json").exists()
    }


@pytest.fixture(scope="module")
def hdfc() -> Any:
    return map_statements(load("hdfcbank_stock"), hists("hdfcbank"), AMAP)


@pytest.fixture(scope="module")
def tcs() -> Any:
    return map_statements(load("tcs_stock"), hists("tcs"), AMAP)


def crore(m: Any, code: str, end: date, kind: str = "year") -> float | None:
    v = m.get(code, end, kind)
    return None if v is None else v / CR


# ───────────────────────── identity ─────────────────────────


def test_identity_by_isin_then_nse_code() -> None:
    stock = load("hdfcbank_stock")
    ok = verify_identity(stock, isin=HDFC_ISIN, symbol="HDFCBANK")
    assert ok.ok and ok.isin == HDFC_ISIN and ok.nse == "HDFCBANK" and ok.bse == "500180"
    wrong = verify_identity(load("tcs_stock"), isin=HDFC_ISIN, symbol="HDFCBANK")
    assert not wrong.ok
    assert wrong.reason.startswith("vendor returned a different company: Tata Consultancy")
    # no ISIN on our side: the NSE code decides
    assert verify_identity(stock, isin=None, symbol="hdfcbank").ok
    assert not verify_identity(stock, isin=None, symbol="ICICIBANK").ok
    assert not verify_identity({"companyName": "X"}, isin=None, symbol="X").ok
    assert not verify_identity([], isin=HDFC_ISIN, symbol="HDFCBANK").ok


# ───────────────────────── units ─────────────────────────


@pytest.mark.parametrize("co", ["hdfcbank", "tcs", "itc", "tatasteel"])
def test_stock_amounts_are_verified_to_be_crore(co: str) -> None:
    uc = check_stock_unit(stock_periods(load(f"{co}_stock")), AMAP)
    assert uc.crore_per_unit == 1.0 and uc.ratio == pytest.approx(1.0, abs=0.01)
    assert uc.samples >= 7


def _scaled(stock: Any, factor: float) -> Any:
    """A copy with every INC/BAL/CAS amount x factor (EPS and share counts untouched)."""
    out = copy.deepcopy(stock)
    keep = {"DilutedWeightedAverageShares", "TotalCommonSharesOutstanding", "periodLength",
            "DilutedEPSExcludingExtraOrdItems", "DilutedNormalizedEPS", "periodType",
            "DPS-CommonStockPrimaryIssue", "TangibleBookValueperShareCommonEq"}  # fmt: skip
    for f in out["financials"]:
        for items in f["stockFinancialMap"].values():
            for it in items or []:
                if it["key"] not in keep and it["value"] not in (None, ""):
                    with contextlib.suppress(ValueError):
                        it["value"] = str(float(it["value"]) * factor)
    return out


def test_amounts_in_millions_are_detected_and_refused() -> None:
    millions = _scaled(load("tcs_stock"), 10)
    uc = check_stock_unit(stock_periods(millions), AMAP)
    assert uc.crore_per_unit is None and uc.ratio == pytest.approx(10.0, rel=0.01)
    assert "look like million, the map declares crore" in uc.message
    m = map_statements(millions, {}, AMAP)
    assert m.values == [] and any("not read" in g for g in m.gaps)


def test_historical_stats_in_another_unit_is_refused() -> None:
    h = hists("tcs")
    h["yoy_results"] = {k: {c: v / 100 if isinstance(v, int | float) else v for c, v in row.items()}
                        for k, row in h["yoy_results"].items()}  # fmt: skip
    m = map_statements(load("tcs_stock"), h, AMAP)
    assert any("unit mismatch" in g for g in m.gaps)
    assert m.years("pl") == list(range(2020, 2027))  # /stock only: the history is not read


def test_labels() -> None:
    assert label_end("Mar 2026") == date(2026, 3, 31) and label_end("Jun 2025") == date(2025, 6, 30)
    assert label_end("Dec 2024") == date(2024, 12, 31)
    assert label_end("TTM") is None and label_end("2026") is None


# ───────────────────────── coverage and values ─────────────────────────


def test_hdfcbank_bank_model_twelve_years(hdfc: Any) -> None:
    assert hdfc.model == "bank"
    assert hdfc.years("pl") == hdfc.years("bs") == hdfc.years("cf") == list(range(2015, 2027))
    # /stock (primary), FY2026
    assert crore(hdfc, "deposits", FY[2026], "instant") == pytest.approx(3099638.29)
    assert crore(hdfc, "advances", FY[2026], "instant") == pytest.approx(3048329.65)
    assert crore(hdfc, "interest_earned", FY[2026]) == pytest.approx(348615.15)
    assert crore(hdfc, "net_interest_income", FY[2026]) == pytest.approx(163123.92)
    assert crore(hdfc, "loan_loss_provisions", FY[2026]) == pytest.approx(14425.67)
    assert crore(hdfc, "operating_expenses", FY[2026]) == pytest.approx(193404.46)  # magnitude
    assert crore(hdfc, "total_equity", FY[2026], "instant") == pytest.approx(586059.47)
    assert crore(hdfc, "pat", FY[2026]) == pytest.approx(76025.97)  # owners' share
    assert crore(hdfc, "profit_after_tax", FY[2026]) == pytest.approx(79219.46)
    assert crore(hdfc, "minority_interest_pl", FY[2026]) == pytest.approx(3193.49)  # sign flipped
    assert hdfc.get("eps_diluted", FY[2026], "year") == pytest.approx(49.28)
    assert hdfc.get("dps", FY[2026], "year") == pytest.approx(13.0)
    assert hdfc.get("shares_outstanding", FY[2026], "instant") == pytest.approx(1539.34e7)
    # /historical_stats (extends the history), FY2015
    assert crore(hdfc, "revenue", FY[2015]) == pytest.approx(50666)
    assert crore(hdfc, "profit_after_tax", FY[2015]) == pytest.approx(10703)
    assert crore(hdfc, "deposits", FY[2015], "instant") == pytest.approx(450284)
    assert crore(hdfc, "total_equity", FY[2015], "instant") == pytest.approx(501 + 62653)
    assert crore(hdfc, "cfo", FY[2015]) == pytest.approx(-21281)
    # not in /historical_stats and before /stock's years: missing, never 0 (rule 1)
    assert hdfc.get("pat", FY[2015], "year") is None
    assert hdfc.get("advances", FY[2015], "instant") is None
    # the vendor's 0 depreciation in bank quarters means "not given"
    assert hdfc.get("depreciation", date(2025, 6, 30), "quarter") is None
    assert all(v.value != 0 or v.item_code not in AMAP.zero_means_missing for v in hdfc.values)


def test_tcs_general_model_twelve_years(tcs: Any) -> None:
    assert tcs.model == "general"
    assert tcs.years("pl") == tcs.years("bs") == tcs.years("cf") == list(range(2015, 2027))
    assert crore(tcs, "revenue", FY[2026]) == pytest.approx(267021)
    assert crore(tcs, "interest", FY[2026]) == pytest.approx(1227)  # history only
    assert crore(tcs, "other_income", FY[2026]) == pytest.approx(-124)
    assert crore(tcs, "net_block", FY[2026], "instant") == pytest.approx(24724)
    assert crore(tcs, "fixed_assets", FY[2026], "instant") == pytest.approx(31343)
    assert crore(tcs, "cash_and_equivalents", FY[2026], "instant") == pytest.approx(3188 + 3217)
    assert crore(tcs, "purchase_of_fixed_assets", FY[2026]) == pytest.approx(4700)  # magnitude
    assert crore(tcs, "dividends_paid", FY[2026]) == pytest.approx(39437)
    assert crore(tcs, "minority_interest_pl", FY[2026]) == pytest.approx(244)


@pytest.mark.parametrize("co", ["itc", "tatasteel"])
def test_general_stock_only(co: str) -> None:
    m = map_statements(load(f"{co}_stock"), {}, AMAP)
    assert m.model == "general" and m.gaps == []
    assert m.years("pl") == m.years("bs") == m.years("cf") == list(range(2019, 2027))
    assert m.get("interest", FY[2026], "year") is None  # /stock has no gross interest: a gap


def test_tatasteel_cash_without_equivalents_is_the_cash_given() -> None:
    m = map_statements(load("tatasteel_stock"), {}, AMAP)
    assert crore(m, "cash_and_equivalents", FY[2026], "instant") == pytest.approx(8975.50)


# ───────────────────────── consistency ─────────────────────────


@pytest.mark.parametrize("co", ["hdfcbank", "tcs"])
def test_stock_and_history_agree_within_2_percent(co: str) -> None:
    """For overlapping years, the core figures from /stock and /historical_stats agree; the
    items that don't (cash-flow splits, HDFCBANK FY2024 CFO) are reported, /stock kept."""
    m = map_statements(load(f"{co}_stock"), hists(co), AMAP)
    core = {"revenue", "pbt", "profit_after_tax", "total_equity", "total_assets", "deposits",
            "interest", "other_income", "eps_diluted", "equity_share_capital"}  # fmt: skip
    year_diffs = [d for d in m.differences if d.period_type != "quarter"]
    assert not [d for d in year_diffs if d.item_code in core]
    assert {d.item_code for d in year_diffs} <= {"cfo", "cfi", "cff", "net_cash_flow",
                                                "total_debt"}  # fmt: skip
    for d in year_diffs:
        assert d.rel > AMAP.units.consistency_tolerance and d.kept == "/stock"
        assert m.get(d.item_code, d.period_end, d.period_type) == d.stock


def test_hdfcbank_fourth_quarter_pbt_from_quarter_results(hdfc: Any) -> None:
    """/stock's interim March PBT is wrong (4,750 vs 27,672 crore): quarter_results is kept."""
    q4 = date(2026, 3, 31)
    assert crore(hdfc, "pbt", q4, "quarter") == pytest.approx(27672)
    d = next(d for d in hdfc.differences if d.item_code == "pbt" and d.period_end == q4)
    assert d.kept == "quarter_results" and "quarter_results kept" in d.text()


def test_hdfcbank_quarters_add_up_to_the_year(hdfc: Any) -> None:
    found = quarter_sums(hdfc, ("profit_after_tax",), AMAP.units.quarter_sum_tolerance)
    sums = {(s.fiscal_year, s.item_code): s for s in found}
    fy26 = sums[(2026, "profit_after_tax")]
    assert fy26.quarters / CR == pytest.approx(17090 + 20364 + 20691 + 21074)
    assert fy26.annual / CR == pytest.approx(79219.46) and fy26.ok
    assert all(s.ok for s in sums.values()) and len(sums) == 3  # FY2024..FY2026
    quarters = sorted({v.period_end for v in hdfc.values if v.period_type == "quarter"})
    assert len(quarters) >= 13 and quarters[-1] == date(2026, 6, 30)


def test_map_validation(tmp_path: Path) -> None:
    import yaml

    from app.fundamentals.indianapi_map import IndianApiMapError, load_indianapi_map

    raw = yaml.safe_load((REPO_CONFIG_DIR / "indianapi_map.yaml").read_text())
    bad = copy.deepcopy(raw)
    bad["models"]["general"]["revenue"]["unit"] = "per_share"  # xbrl_map says amount
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(bad))
    with pytest.raises(IndianApiMapError, match="xbrl_map says amount"):
        load_indianapi_map(p)
    bad2 = copy.deepcopy(raw)
    bad2["models"]["general"]["cfo"]["hist"]["stats"] = "balancesheet"
    p.write_text(yaml.safe_dump(bad2))
    with pytest.raises(IndianApiMapError, match="a cf item reads stats=balancesheet"):
        load_indianapi_map(p)
