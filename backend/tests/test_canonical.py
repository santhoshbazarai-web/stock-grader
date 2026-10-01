"""The canonical field map stays consistent with the database schema."""

from datetime import UTC, datetime

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from app.data.canonical import (
    CANONICAL_FIELDS,
    Label,
    fields_for,
    frame_to_rows,
    labels_for,
    pick,
    unavailable_from,
)
from app.db.base import Base
from app.db.models import FinAnnual, Instrument
from app.db.upsert import upsert

TABLE_MODELS = {"fin_annual", "fin_quarterly", "shareholding"}


@pytest.mark.parametrize("table", sorted(TABLE_MODELS))
def test_every_canonical_field_is_a_column(table: str) -> None:
    columns = set(Base.metadata.tables[table].c.keys())
    assert set(fields_for(table)) <= columns  # type: ignore[arg-type]


@pytest.mark.parametrize("table", sorted(TABLE_MODELS))
def test_every_value_column_is_canonical(table: str) -> None:
    bookkeeping = {
        "id", "instrument_id", "period_end", "source", "fetched_at", "statement_type",
        "announcement_date", "filing_date", "fiscal_year", "extra", "is_derived",
    }  # fmt: skip
    columns = set(Base.metadata.tables[table].c.keys()) - bookkeeping
    assert columns == set(fields_for(table))  # type: ignore[arg-type]


def test_every_field_is_documented() -> None:
    for name, spec in CANONICAL_FIELDS.items():
        assert spec.description, name
        for source, formula in spec.derived.items():
            assert (formula and source not in spec.labels) or spec.labels[source], name


def test_pick_order_sign_and_scale() -> None:
    labels = (Label("A"), Label("B", sign=-1))
    assert pick({"A": None, "B": -5e7}, labels, 1e-7) == pytest.approx(5.0)
    assert pick({"A": 2.0, "B": 3.0}, labels) == 2.0
    assert pick({"A": float("nan")}, labels) is None
    assert pick({}, labels) is None


def test_unavailable_from() -> None:
    missing = unavailable_from("screener", "fin_annual")
    assert "current_liabilities" in missing and "revenue" not in missing
    assert "ebitda" not in missing  # derived
    assert set(labels_for("nse", "shareholding")) == {"promoter_pct", "public_pct"}


def test_frame_to_rows_upserts(db: Session) -> None:
    upsert(db, Instrument, [{"symbol": "X", "source": "nse"}])
    iid = db.query(Instrument.id).scalar()
    df = pd.DataFrame(
        {"revenue": [100.0], "pat": [float("nan")], "fiscal_year": [2024]},
        index=pd.DatetimeIndex([pd.Timestamp("2024-03-31")]),
    ).reindex(columns=[*fields_for("fin_annual"), "fiscal_year"])
    rows = frame_to_rows(
        df, "fin_annual", instrument_id=iid, source="yfinance",
        fetched_at=datetime(2024, 5, 1, tzinfo=UTC), extra_cols={"statement_type": "consolidated"},
    )  # fmt: skip
    assert rows[0]["pat"] is None and rows[0]["revenue"] == 100.0
    upsert(db, FinAnnual, rows)
    stored = db.query(FinAnnual).one()
    assert stored.revenue == 100.0 and stored.pat is None and stored.fiscal_year == 2024


def test_docs_table_is_current() -> None:
    from pathlib import Path

    from app.data.canonical import mapping_markdown

    doc = Path(__file__).resolve().parents[2] / "docs" / "CANONICAL_FIELDS.md"
    assert doc.read_text() == mapping_markdown(), (
        "docs/CANONICAL_FIELDS.md is stale: run `uv run python -m app.data.canonical "
        "> ../docs/CANONICAL_FIELDS.md` from backend/"
    )
