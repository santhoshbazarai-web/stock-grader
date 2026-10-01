"""Pure pieces of SPEC v0.2 §3.8-3.9: the before/after report diff behind results
notifications, and the cross-source reconciliation with its cause heuristics (hand-computed
fixtures)."""

from datetime import date

import pytest

from app.fundamentals.reconcile import CRORE, reconcile
from app.reports.diff import change_message, quarter_label, report_changes, summary
from app.valuation.blend import Confidence, lower_confidence

FY24 = date(2024, 3, 31)
TOL, MIN_DIFF = 0.02, 5_000_000
FACTORS = [100, 1000, 100000, 10000000]
SOURCES = ["nse_xbrl", "annual_report_pdf", "market_lens", "yfinance"]


def cr(x: float) -> float:
    return x * CRORE


def run(values: dict, **kw: object) -> list:  # type: ignore[type-arg]
    return reconcile(values, sources=SOURCES, tolerance_rel=TOL, min_diff_inr=MIN_DIFF,
                     unit_factors=FACTORS, **kw).findings  # type: ignore[arg-type]  # fmt: skip


# ───────────────────────── report diff (§3.8) ─────────────────────────

BEFORE = {"as_of": "2024-06-13", "grade": "B", "zone": "fair", "action": "wait",
          "fair_value": 1240.0, "cmp": 1200.0}  # fmt: skip


def test_summary_from_payload() -> None:
    payload = {"as_of": "2024-06-14", "grade": "A_plus", "grade_label": "A+", "zone": "discount",
               "action": "buy", "levels": {"fair_value": 1390.0}, "cmp": 1180.0}  # fmt: skip
    assert summary(payload) == {"as_of": "2024-06-14", "grade": "A+", "zone": "discount",
                                "action": "buy", "fair_value": 1390.0, "cmp": 1180.0}  # fmt: skip


def test_spec_example_message() -> None:
    after = {**BEFORE, "grade": "A", "zone": "discount", "fair_value": 1390.0,
             "as_of": "2024-06-14", "action": "wait"}  # fmt: skip
    changes = report_changes(BEFORE, after, fv_change_rel=0.05)
    assert [c.field for c in changes] == ["grade", "fair_value", "zone"]
    title, body = change_message("XYZ", "Q2 FY25 results", changes, after)
    assert title == "XYZ Q2 FY25 results: Grade B→A, FV ₹1,240→₹1,390, zone Fair→Discount"
    assert body.startswith("New report as of 2024-06-14: grade A, zone Discount, action Wait")


def test_fv_threshold_and_no_change() -> None:
    # 1240 → 1300 is +4.8%: below 5%, nothing to notify
    assert report_changes(BEFORE, {**BEFORE, "fair_value": 1300.0}, fv_change_rel=0.05) == []
    # 1240 → 1310 is +5.6%
    [c] = report_changes(BEFORE, {**BEFORE, "fair_value": 1310.0}, fv_change_rel=0.05)
    assert c.text == "FV ₹1,240→₹1,310"
    # FV lost (inputs missing) is a change too
    [c] = report_changes(BEFORE, {**BEFORE, "fair_value": None}, fv_change_rel=0.05)
    assert c.text == "FV ₹1,240→n/a"


def test_action_change_title_shows_current_fv() -> None:
    after = {**BEFORE, "action": "buy_on_pullback"}
    changes = report_changes(BEFORE, after, fv_change_rel=0.05)
    title, _ = change_message("XYZ", None, changes, after)
    assert title == "XYZ results: FV ₹1,240, action Wait→Buy on Pullback"


@pytest.mark.parametrize(
    ("end", "fy_end", "label"),
    [(date(2024, 9, 30), 3, "Q2 FY25"), (date(2024, 6, 30), 3, "Q1 FY25"),
     (date(2024, 3, 31), 3, "Q4 FY24"), (date(2024, 12, 31), 3, "Q3 FY25"),
     (date(2024, 3, 31), 12, "Q1 FY24"), (None, 3, None)],
)  # fmt: skip
def test_quarter_label(end: date | None, fy_end: int, label: str | None) -> None:
    assert quarter_label(end, fy_end) == label


# ───────────────────────── reconciliation (§3.9) ─────────────────────────


def test_within_tolerance_is_not_an_issue() -> None:
    # 1,000 vs 1,019 cr: 1.9% apart
    assert run({(FY24, "year", "revenue"): {"nse_xbrl": cr(1000), "yfinance": cr(1019)}}) == []
    # 3% apart but only ₹3 lakh: rounding, not an issue
    assert run({(FY24, "year", "pat"): {"nse_xbrl": 1e7, "yfinance": 1.03e7}}) == []


def test_difference_and_reference_order() -> None:
    [f] = run({(FY24, "year", "pat"): {"yfinance": cr(95), "nse_xbrl": cr(100),
                                       "annual_report_pdf": cr(100.5)}})  # fmt: skip
    assert (f.source, f.reference_source) == ("yfinance", "nse_xbrl")
    assert f.diff_rel == pytest.approx(0.05) and f.cause is None
    assert f.reasons[0] == ("PAT (year to 31 Mar 2024, consolidated): yfinance ₹95.00 cr vs "
                            "NSE XBRL ₹100.00 cr, 5.0% apart")  # fmt: skip
    assert f.reasons[1] == "no cause found (unexplained difference)"
    assert f.values == {"yfinance": cr(95), "nse_xbrl": cr(100), "annual_report_pdf": cr(100.5)}
    # without the exchange figure the PDF is the reference
    [g] = run({(FY24, "year", "pat"): {"yfinance": cr(95), "annual_report_pdf": cr(100)}})
    assert g.reference_source == "annual_report_pdf"


def test_units_cause() -> None:
    # PDF keyed in lakh but read as rupees → 1/100,000 of the XBRL figure
    [f] = run({(FY24, "year", "total_assets"): {"nse_xbrl": cr(5000), "annual_report_pdf":
                                                 cr(5000) / 100000}})  # fmt: skip
    assert f.cause == "units" and "100,000x apart" in f.reasons[1]
    [g] = run({(FY24, "year", "revenue"): {"nse_xbrl": cr(12), "market_lens": cr(1200)}})
    assert g.cause == "units" and "100x" in g.reasons[1]


def test_basis_cause() -> None:
    key = (FY24, "year", "revenue")
    [f] = run({key: {"nse_xbrl": cr(1000), "yfinance": cr(820)}}, other_basis={key: cr(815)})
    assert f.cause == "basis"
    assert f.reasons[1] == ("yfinance matches the standalone figure (₹815.00 cr): "
                            "consolidated / standalone mix-up")  # fmt: skip


def test_restatement_cause() -> None:
    key = (FY24, "year", "total_equity")
    [f] = run({key: {"nse_xbrl": cr(2100), "market_lens": cr(2000)}},
              earlier_versions={key: [cr(2001)]})  # fmt: skip
    assert f.cause == "restatement" and "first filed (₹2,001.00 cr)" in f.reasons[1]


def test_single_source_and_compared_pairs() -> None:
    res = reconcile(
        {(FY24, "year", "cfo"): {"nse_xbrl": cr(10)},
         (FY24, "quarter", "revenue"): {"nse_xbrl": cr(250), "yfinance": cr(251)}},
        sources=SOURCES, tolerance_rel=TOL, min_diff_inr=MIN_DIFF, unit_factors=FACTORS,
    )  # fmt: skip
    assert res.findings == [] and res.compared == [((FY24, "quarter", "revenue"), "yfinance")]
    assert res.reasons == ["1 figure(s) compared across sources, 0 differ by more than 2%"]


def test_lower_confidence() -> None:
    assert lower_confidence(Confidence.HIGH, 1) is Confidence.MEDIUM
    assert lower_confidence(Confidence.MEDIUM, 1) is Confidence.LOW
    assert lower_confidence(Confidence.LOW, 1) is Confidence.LOW
    assert lower_confidence(Confidence.HIGH, 0) is Confidence.HIGH
    assert lower_confidence(Confidence.HIGH, 2) is Confidence.LOW


def test_per_share_items_skip_the_rupee_floor() -> None:
    # EPS 92.40 vs 46.20: below the ₹50-lakh floor in rupees, but EPS is per share
    values = {(FY24, "year", "eps_diluted"): {"nse_xbrl": 92.40, "yfinance": 46.20}}
    assert run(values) == []
    [f] = run(values, per_share={"eps_diluted"})
    assert f.reasons[0] == ("Diluted EPS (year to 31 Mar 2024, consolidated): yfinance ₹46.20 vs "
                            "NSE XBRL ₹92.40, 50.0% apart")  # fmt: skip
    assert run({(FY24, "year", "eps_diluted"): {"nse_xbrl": 49.30, "yfinance": 49.28}},
               per_share={"eps_diluted"}) == []  # fmt: skip
