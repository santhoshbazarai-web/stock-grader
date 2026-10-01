"""Unit normalisation of results XBRL to rupees (SPEC §3.6 step 2).

XBRL amounts should be absolute rupees; these tests re-key the fixtures the way a filer who
entered figures in their stated rounding level (lakhs / millions / crores) would, and check the
parser recovers the same rupee values."""

import re
from pathlib import Path

import pytest

from app.core.config import load_config
from app.data.xbrl import parse_results, rounding_factor
from tests.conftest import REPO_CONFIG_DIR

FIX = Path(__file__).parent / "fixtures" / "xbrl"
CFG = load_config(REPO_CONFIG_DIR).providers.nse.results
Q2 = (FIX / "acme_q2fy25_standalone.xml").read_bytes()  # Jul-Sep 2024, EPS filed
Q4 = (FIX / "acme_q4fy24_consolidated.xml").read_bytes()  # states "Lakhs", amounts in rupees
_AMOUNT = re.compile(rb'(unitRef="INR" decimals=")-5(">)(-?\d+)(<)')


def rekey(doc: bytes, factor: float, level: str, *, drop_eps: bool = False,
          decimals: str = "2") -> bytes:  # fmt: skip
    """Amounts divided by ``factor`` with ``decimals`` precision, and the rounding level
    stated, as a filer keying figures in that unit would produce."""

    def sub(m: re.Match[bytes]) -> bytes:
        v = int(m.group(3)) / factor
        return m.group(1) + decimals.encode() + m.group(2) + f"{v:.2f}".encode() + m.group(4)

    out = _AMOUNT.sub(sub, doc)
    out = re.sub(
        rb"<in-bse-fin:LevelOfRoundingUsedInFinancialStatements[^>]*>[^<]*<[^>]*>", b"", out
    )
    stated = (f'<in-bse-fin:LevelOfRoundingUsedInFinancialStatements contextRef="OneD">{level}'
              "</in-bse-fin:LevelOfRoundingUsedInFinancialStatements>\n  ").encode()  # fmt: skip
    out = out.replace(b"<in-bse-fin:NameOfTheCompany", stated + b"<in-bse-fin:NameOfTheCompany", 1)
    if drop_eps:
        out = re.sub(rb"<in-bse-fin:Diluted[^>]*>[^<]*</in-bse-fin:Diluted\w+>", b"", out)
    return out


def quarter_values(doc: bytes) -> tuple[float, float, float, float, list[str]]:
    f = parse_results(doc, CFG)
    q = f.quarter
    assert q is not None
    return q["revenue"], q["pat"], f.quarter_items["revenue"].value, f.amount_scale, f.warnings


@pytest.mark.parametrize(
    ("factor", "level"),
    [(1e5, "Lakhs"), (1e6, "Millions"), (1e7, "Rs. in Crores"), (1e5, "Rupees in lakhs")],
)
def test_amounts_keyed_in_the_rounding_unit_are_scaled_to_rupees(factor: float, level: str) -> None:
    revenue, pat, revenue_inr, scale, warnings = quarter_values(rekey(Q2, factor, level))
    assert revenue == pytest.approx(1300) and pat == pytest.approx(224)  # ₹ crore, as filed
    assert revenue_inr == pytest.approx(1300e7)  # line items in ₹
    assert scale == factor
    assert any("keyed in" in w and "PAT / EPS" in w for w in warnings)


def test_without_eps_the_decimals_attribute_decides() -> None:
    revenue, _, _, scale, warnings = quarter_values(rekey(Q2, 1e5, "Lakhs", drop_eps=True))
    assert revenue == pytest.approx(1300) and scale == 1e5
    assert any("decimals >= 0" in w for w in warnings)


def test_rupee_amounts_with_a_stated_rounding_level_are_not_rescaled() -> None:
    # The Q4 fixture states "Lakhs" and files absolute rupees with decimals="-5" (XBRL rule)
    f = parse_results(Q4, CFG)
    assert f.rounding == "Lakhs" and f.amount_scale == 1.0
    assert f.quarter is not None and f.quarter["revenue"] == pytest.approx(1250)
    # misleading decimals="0" on rupee amounts: PAT / EPS still says rupees, so no scaling
    doc = re.sub(rb'unitRef="INR" decimals="-5"', b'unitRef="INR" decimals="0"', Q4)
    g = parse_results(doc, CFG)
    assert g.amount_scale == 1.0 and g.quarter is not None
    assert g.quarter["revenue"] == pytest.approx(1250)


def test_amounts_in_another_currency_are_not_read() -> None:
    doc = Q2.replace(b'<xbrli:unit id="INR"><xbrli:measure>iso4217:INR',
                     b'<xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
                     b'<xbrli:unit id="INR"><xbrli:measure>iso4217:INR')  # fmt: skip
    doc = doc.replace(b'<in-bse-fin:OtherIncome contextRef="OneD" unitRef="INR"',
                      b'<in-bse-fin:OtherIncome contextRef="OneD" unitRef="USD"')  # fmt: skip
    f = parse_results(doc, CFG)
    assert f.quarter is not None and f.quarter["other_income"] is None
    assert f.quarter["revenue"] == pytest.approx(1300)
    assert any("['USD']" in w for w in f.warnings)


@pytest.mark.parametrize(
    ("level", "factor"),
    [("Lakhs", 1e5), ("Rupees in Lakhs", 1e5), ("INR Crores", 1e7), ("Millions", 1e6),
     ("Thousands", 1e3), ("Rupees", 1.0), ("", None), (None, None), ("Units", None)],
)  # fmt: skip
def test_rounding_factor(level: str | None, factor: float | None) -> None:
    assert rounding_factor(level, CFG.rounding_levels) == factor
