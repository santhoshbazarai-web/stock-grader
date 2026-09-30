"""The versioned XBRL tag map (fundamentals/xbrl_map.yaml) and pre-Ind-AS filings."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import load_config
from app.data.xbrl import parse_results
from app.fundamentals.xbrl_map import MAP_PATH, XbrlMapError, get_xbrl_map, load_xbrl_map
from tests.conftest import REPO_CONFIG_DIR

FIX = Path(__file__).parent / "fixtures" / "xbrl"
CFG = load_config(REPO_CONFIG_DIR).providers.nse.results


def approx(v: float) -> object:
    return pytest.approx(v, rel=1e-9)


def test_repo_map_is_valid_versioned_and_covers_every_canonical_fin_field() -> None:
    m = get_xbrl_map()
    assert m.version >= 2
    assert m.unmapped("fin_quarterly") == []
    assert m.unmapped("fin_annual") == ["sga"]  # not in the results format
    groups = {g for s in m.items.values() for g in (*s.tags, *s.sum_of)}
    assert {"ind_as", "pre_ind_as", "bank"} <= groups
    # every canonical P&L item with an Ind AS tag also has a pre-Ind-AS or bank alternative,
    # or the same element name serves both layouts
    for code in ("revenue", "depreciation", "pbt", "pat", "eps_diluted", "total_assets",
                 "total_debt", "receivables", "net_block"):  # fmt: skip
        spec = m.items[code]
        assert {"pre_ind_as", "bank"} & {*spec.tags, *spec.sum_of}, code


def _broken(tmp_path: Path, change: Callable[[dict[str, Any]], None]) -> Path:
    doc = yaml.safe_load(MAP_PATH.read_text())
    change(doc)
    path = tmp_path / "xbrl_map.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(version=0), "greater than or equal to 1"),
        (lambda d: d["items"]["revenue"].update(statement="notes"), "statement"),
        (lambda d: d["items"]["revenue"].update(unit="per_share"), "unit per_share but"),
        (lambda d: d["items"]["revenue"].update(tags={}), "needs tags or sum_of"),
        (lambda d: d["items"]["revenue"]["tags"].update(ind_as=[]), "non-empty identifiers"),
        (lambda d: d["items"]["revenue"]["tags"].update(ind_as=["Bad Name"]), "identifiers"),
        (lambda d: d["items"].update(ebitda=d["items"]["pbt"]), "computed by the parser"),
        (lambda d: d["items"].update(promoter_pct=d["items"]["crar_pct"] | {"target": "canonical"}),
         "not a fin-table field"),
        (lambda d: d["items"]["total_income"].update(target="nowhere"), "target"),
        (lambda d: d["items"]["gross_npa"].update(target="line") or d["items"].update(
            pat=d["items"]["pat"] | {"target": "line"}), "target must be canonical"),
        (lambda d: d["info"].pop("rounding"), "info is missing"),
        (lambda d: d["items"]["revenue"].update(surprise=1), "Extra inputs"),
    ],
)  # fmt: skip
def test_invalid_map_fails_fast(
    tmp_path: Path, change: Callable[[dict[str, Any]], None], message: str
) -> None:
    with pytest.raises(XbrlMapError, match=message):
        load_xbrl_map(_broken(tmp_path, change))


def test_pre_ind_as_filing_maps_through_the_pre_ind_as_tags() -> None:
    f = parse_results((FIX / "acme_q4fy15_standalone_igaap.xml").read_bytes(), CFG)
    q, a = f.quarter, f.annual
    assert q is not None and a is not None and f.map_version == get_xbrl_map().version
    # Jan-Mar 2015 quarter (₹ crore)
    assert q["revenue"] == approx(800) and q["cogs"] == approx(400)
    assert q["pbt"] == approx(114) and q["tax"] == approx(30) and q["pat"] == approx(84)
    assert q["depreciation"] == approx(30) and q["interest"] == approx(8)
    assert q["ebitda"] == approx(114 + 8 + 30 - 12)
    assert q["eps_diluted"] == approx(4.2) and q["shares_diluted_cr"] == approx(84 / 4.2)
    # FY2014-15, balance sheet at 31 Mar 2015
    assert a["fiscal_year"] == 2015 and a["revenue"] == approx(3000) and a["pat"] == approx(310)
    assert a["total_assets"] == approx(2300) and a["total_equity"] == approx(1500)
    assert a["retained_earnings"] == approx(1480)
    assert a["total_debt"] == approx(200 + 50)
    assert a["receivables"] == approx(300) and a["cash_and_equivalents"] == approx(90)
    assert a["current_assets"] == approx(800) and a["current_liabilities"] == approx(500)
    assert a["payables"] == approx(250) and a["net_block"] == approx(900 + 40)
    assert a["non_operating_investments"] == approx(60)
    assert a["cfo"] is None  # results XBRL had no cash flow before FY2019-20
    # which tag produced each value is kept, for the audit trail in fin_line_items
    assert f.quarter_items["revenue"].tag == "pre_ind_as:NetSalesIncomeFromOperations"
    assert f.year_items["total_debt"].tag == "sum:pre_ind_as:LongTermBorrowings+ShortTermBorrowings"
    assert f.year_items["revenue"].value == approx(3000 * 1e7)  # line items are in ₹
    assert f.year_items["equity_share_capital"].tag == "pre_ind_as:ShareCapital"


def test_ind_as_line_items_include_non_canonical_lines() -> None:
    f = parse_results((FIX / "acme_q4fy24_consolidated.xml").read_bytes(), CFG)
    items = f.quarter_items
    assert items["employee_benefit_expense"].value == approx(200e7)
    assert (
        items["total_expenses"].value == approx(985e7)
        and items["total_income"].tag == "ind_as:Income"
    )
    assert items["eps_basic"].value == approx(10.52)
    assert items["cogs"].tag == (
        "sum:ind_as:CostOfMaterialsConsumed+PurchasesOfStockInTrade+"
        "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade"
    )
    assert f.year_items["purchase_of_fixed_assets"].value == approx(300e7)  # magnitude
    assert f.year_items["capital_work_in_progress"].value == approx(150e7)
