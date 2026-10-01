"""Schema conventions, checked on metadata (no database needed)."""

import pytest
from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint

from app.db.base import Base

# Ingested data tables: every row records the provider that served it and when.
DATA_TABLES = {
    "instruments",
    "prices_daily",
    "corporate_actions",
    "delivery_daily",
    "fin_annual",
    "fin_quarterly",
    "shareholding",
    "index_membership",
    "surveillance_flags",
}
SNAPSHOT_TABLES = {"valuation_snapshots", "technical_snapshots", "scores", "reports"}

MODELS = {m.class_.__tablename__: m.class_ for m in Base.registry.mappers}


@pytest.mark.parametrize("table", sorted(DATA_TABLES))
def test_data_tables_have_source_and_fetched_at(table: str) -> None:
    cols = Base.metadata.tables[table].c
    assert not cols["source"].nullable
    assert not cols["fetched_at"].nullable
    assert cols["fetched_at"].type.timezone  # type: ignore[attr-defined]


@pytest.mark.parametrize("table", sorted(SNAPSHOT_TABLES))
def test_snapshot_tables_have_computed_at(table: str) -> None:
    assert "computed_at" in Base.metadata.tables[table].c


@pytest.mark.parametrize("table", sorted(MODELS))
def test_upsert_key_matches_unique_constraint(table: str) -> None:
    model = MODELS[table]
    key = set(model.__upsert_key__)
    constraints = [
        {c.name for c in con.columns}
        for con in model.__table__.constraints
        if isinstance(con, UniqueConstraint | PrimaryKeyConstraint)
    ]
    assert key in constraints


def test_financial_fields_nullable() -> None:
    # Rule 1: missing financial data is NULL, never a default.
    for table in ("fin_annual", "fin_quarterly", "shareholding", "delivery_daily"):
        for col in Base.metadata.tables[table].c:
            if col.type.python_type is float:
                assert col.nullable, f"{table}.{col.name}"
                assert col.server_default is None, f"{table}.{col.name}"
