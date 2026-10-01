"""Screener export import on saved sample workbooks (tests/fixtures/screener, regenerate with
make_samples.py). Expected FY2024 values are hand-computed from the sheet:

  cogs   = Raw Material Cost - Change in Inventory      = 400 - 10            = 390
  ebitda = PBT + Interest + Depreciation - Other Income = 310 + 10 + 30 - 20  = 330
  ebit   = PBT + Interest                               = 310 + 10            = 320
  equity = Equity Share Capital + Reserves              = 20 + 980            = 1000
  eps    = Net profit / Adjusted shares (Cr)            = 232 / 20            = 11.6
  bvps   = equity / shares                              = 1000 / 20           = 50
"""

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Dataset
from app.data.gaps import InMemoryGapRecorder
from app.data.providers.base import (
    FundamentalsProvider,
    ProviderUnavailable,
    ShareholdingProvider,
)
from app.data.providers.screener_import import (
    ScreenerFormatError,
    ScreenerProvider,
    import_screener,
    parse_data_sheet,
)
from app.db.enums import StatementType
from app.db.models import FinAnnual, FinQuarterly, Instrument, Shareholding
from app.db.upsert import upsert

FIX = Path(__file__).parent / "fixtures" / "screener"
PLAIN = FIX / "sample_export.xlsx"
WITH_SHP = FIX / "sample_export_with_shareholding.xlsx"
NOW = datetime(2024, 5, 1, 10, 0, tzinfo=UTC)


# ───────────── parsing ─────────────


def test_annual_fy2024_hand_computed() -> None:
    data = parse_data_sheet(PLAIN)
    assert data.company_name == "SAMPLE INDUSTRIES LTD"
    assert len(data.annual) == 10
    fy = data.annual.loc["2024-03-31"]
    expected = {
        "revenue": 1000, "cogs": 390, "ebitda": 330, "other_income": 20, "depreciation": 30,
        "ebit": 320, "interest": 10, "pbt": 310, "tax": 78, "pat": 232, "total_assets": 1500,
        "total_equity": 1000, "total_debt": 150, "cash_and_equivalents": 90,
        "non_operating_investments": 200, "receivables": 180, "inventory": 120, "net_block": 600,
        "cfo": 280, "dividends_paid": 50, "shares_diluted_cr": 20, "eps_diluted": 11.6,
        "book_value_per_share": 50, "fiscal_year": 2024,
    }  # fmt: skip
    for name, value in expected.items():
        assert fy[name] == pytest.approx(value), name


def test_fields_screener_cannot_supply_stay_missing() -> None:
    fy = parse_data_sheet(PLAIN).annual.loc["2024-03-31"]
    for name in ("current_liabilities", "payables", "purchase_of_fixed_assets",
                 "sale_of_fixed_assets", "minority_interest_bs"):  # fmt: skip
        assert pd.isna(fy[name]), name


def test_blank_cell_is_missing_not_zero() -> None:
    annual = parse_data_sheet(PLAIN).annual
    assert pd.isna(annual.loc["2015-03-31", "receivables"])
    assert annual.loc["2015-03-31", "revenue"] == pytest.approx(round(1000 / 1.1**9, 2))


def test_quarterly() -> None:
    q = parse_data_sheet(PLAIN).quarterly
    assert len(q) == 10
    mar = q.loc["2024-03-31"]
    assert (mar["revenue"], mar["ebitda"], mar["pat"], mar["ebit"]) == (270, 90, 64, 87)
    assert "fiscal_year" not in q.columns


def test_shareholding_block_optional() -> None:
    plain = parse_data_sheet(PLAIN)
    assert plain.shareholding.empty
    assert any("SHAREHOLDING" in w for w in plain.warnings)

    shp = parse_data_sheet(WITH_SHP).shareholding
    assert len(shp) == 4
    latest = shp.loc["2024-03-31"]
    assert (latest["promoter_pct"], latest["fii_pct"], latest["dii_pct"]) == (54.9, 18.6, 12.7)
    assert latest["num_shareholders"] == 244900
    assert pd.isna(latest["promoter_pledge_pct"])


def test_accepts_bytes() -> None:
    assert len(parse_data_sheet(PLAIN.read_bytes()).annual) == 10


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (FIX / "not_screener.xlsx", "no 'Data Sheet'"),
        (b"this is not an xlsx file", "not an Excel workbook"),
    ],
)
def test_rejects_non_screener_files(content: object, match: str) -> None:
    with pytest.raises(ScreenerFormatError, match=match):
        parse_data_sheet(content)  # type: ignore[arg-type]


# ───────────── import into the database ─────────────


@pytest.fixture
def sample_instrument(db: Session) -> int:
    upsert(db, Instrument, [{"symbol": "SAMPLEIND", "name": "Sample Industries", "source": "nse"}])
    return db.scalars(select(Instrument.id).where(Instrument.symbol == "SAMPLEIND")).one()


def _import(db: Session, path: Path = WITH_SHP, basis: StatementType = StatementType.CONSOLIDATED):
    gaps = InMemoryGapRecorder()
    summary = import_screener(
        db, symbol="SAMPLEIND", content=path, statement_type=basis, gaps=gaps, now=NOW
    )
    db.commit()  # the caller owns the transaction (outer test transaction still rolls back)
    return summary, gaps


def test_import_writes_all_three_tables(db: Session, sample_instrument: int) -> None:
    summary, _ = _import(db)
    assert (summary.annual_rows, summary.quarterly_rows, summary.shareholding_rows) == (10, 10, 4)

    row = db.scalars(
        select(FinAnnual).where(FinAnnual.period_end == datetime(2024, 3, 31).date())
    ).one()
    assert row.instrument_id == sample_instrument
    assert row.source == "screener" and row.fetched_at == NOW
    assert row.statement_type is StatementType.CONSOLIDATED
    assert (row.revenue, row.ebitda, row.eps_diluted, row.fiscal_year) == (1000, 330, 11.6, 2024)
    assert row.current_liabilities is None and row.announcement_date is None
    assert db.scalars(select(FinQuarterly)).first() is not None
    assert len(db.scalars(select(Shareholding)).all()) == 4


def test_reimport_is_idempotent(db: Session, sample_instrument: int) -> None:
    _import(db)
    _import(db)
    assert len(db.scalars(select(FinAnnual)).all()) == 10
    assert len(db.scalars(select(FinQuarterly)).all()) == 10


def test_consolidated_and_standalone_coexist(db: Session, sample_instrument: int) -> None:
    _import(db, basis=StatementType.CONSOLIDATED)
    _import(db, basis=StatementType.STANDALONE)
    assert len(db.scalars(select(FinAnnual)).all()) == 20


def test_import_records_gaps(db: Session, sample_instrument: int) -> None:
    summary, gaps = _import(db, path=PLAIN)
    fields = {(g.dataset, g.field) for g in summary.gaps}
    assert (Dataset.FIN_ANNUAL, "current_liabilities") in fields
    assert (Dataset.FIN_ANNUAL, "purchase_of_fixed_assets") in fields
    assert (Dataset.FIN_ANNUAL, "announcement_date") in fields
    assert (Dataset.FIN_QUARTERLY, "announcement_date") in fields
    assert (Dataset.SHAREHOLDING, None) in fields  # no shareholding block at all
    assert (Dataset.FIN_ANNUAL, "revenue") not in fields
    assert (Dataset.FIN_ANNUAL, "ebitda") not in fields  # derived → available
    assert len(gaps.open) >= 1


def test_import_unknown_symbol(db: Session) -> None:
    with pytest.raises(ValueError, match="unknown instrument"):
        import_screener(
            db, symbol="NOPE", content=PLAIN, statement_type=StatementType.CONSOLIDATED,
            gaps=InMemoryGapRecorder(),
        )  # fmt: skip


# ───────────── serving uploads through the router protocol ─────────────


def test_provider_serves_consolidated_first(db: Session, sample_instrument: int) -> None:
    _import(db, basis=StatementType.STANDALONE)
    _import(db, basis=StatementType.CONSOLIDATED)
    p = ScreenerProvider(lambda: db)
    assert isinstance(p, FundamentalsProvider) and isinstance(p, ShareholdingProvider)

    df = p.annual("SAMPLEIND")
    assert df.attrs["statement_type"] == "consolidated" and df.attrs["warnings"] == []
    assert df.attrs["as_of"] == NOW
    assert df.loc["2024-03-31", "revenue"] == 1000
    assert len(p.quarterly("SAMPLEIND")) == 10
    assert len(p.shareholding("SAMPLEIND")) == 4


def test_provider_flags_standalone_fallback(db: Session, sample_instrument: int) -> None:
    _import(db, basis=StatementType.STANDALONE)
    df = ScreenerProvider(lambda: db).annual("SAMPLEIND")
    assert df.attrs["statement_type"] == "standalone"
    assert "standalone" in df.attrs["warnings"][0]


def test_provider_without_upload_is_unavailable(db: Session, sample_instrument: int) -> None:
    p = ScreenerProvider(lambda: db)
    with pytest.raises(ProviderUnavailable):
        p.annual("SAMPLEIND")
    with pytest.raises(ProviderUnavailable):
        p.shareholding("SAMPLEIND")
