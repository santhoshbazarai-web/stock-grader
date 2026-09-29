"""Idempotent bulk upserts (PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE``).

Jobs re-run safely (SPEC §10): writing the same rows twice leaves the table unchanged apart
from refreshed ``fetched_at`` / ``updated_at`` / ``computed_at`` timestamps.
"""

from collections.abc import Iterable, Mapping, Sequence
from itertools import batched
from typing import Any, cast

from sqlalchemy import Table, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.base import Base

# Columns refreshed to now() on every upsert unless the caller supplies a value.
REFRESHED_ON_UPSERT = ("fetched_at", "updated_at", "computed_at")
# Columns never overwritten on conflict.
PRESERVED_ON_UPSERT = ("created_at",)
# PostgreSQL caps a statement at 65535 bind parameters.
_MAX_BIND_PARAMS = 65_535


def upsert(
    session: Session,
    model: type[Base],
    rows: Iterable[Mapping[str, Any]],
    *,
    key: Sequence[str] | None = None,
    update: Sequence[str] | None = None,
) -> int:
    """Insert ``rows`` into ``model``'s table, updating existing rows on natural-key conflict.

    ``key`` defaults to ``model.__upsert_key__`` and must match a unique constraint or PK.
    ``update`` defaults to every supplied column outside the key. All rows must share the same
    columns (so a missing value is an explicit ``None``, never silently skipped).
    Returns the number of rows sent. Does not commit.
    """
    rows = list(rows)
    if not rows:
        return 0

    table = cast(Table, model.__table__)
    columns = list(rows[0])
    column_set = set(columns)
    for i, row in enumerate(rows):
        if set(row) != column_set:
            raise ValueError(f"row {i} has columns {sorted(row)}, expected {sorted(column_set)}")
    unknown = column_set - set(table.c.keys())
    if unknown:
        raise ValueError(f"unknown columns for {table.name}: {sorted(unknown)}")

    key = tuple(key if key is not None else model.__upsert_key__)
    missing_key = set(key) - column_set
    if missing_key:
        raise ValueError(f"rows must include key columns {sorted(missing_key)}")

    if update is None:
        update = [c for c in columns if c not in key and c not in PRESERVED_ON_UPSERT]
    refreshed = [c for c in REFRESHED_ON_UPSERT if c in table.c and c not in column_set]

    batch_size = max(1, _MAX_BIND_PARAMS // len(columns))
    for batch in batched(rows, batch_size):
        stmt = insert(table).values(list(batch))
        set_: dict[str, Any] = {c: stmt.excluded[c] for c in update}
        set_.update({c: func.now() for c in refreshed})
        if set_:
            stmt = stmt.on_conflict_do_update(index_elements=list(key), set_=set_)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(key))
        session.execute(stmt)
    return len(rows)
