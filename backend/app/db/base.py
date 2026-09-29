from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import Date, DateTime, Double, Enum, MetaData, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names so Alembic autogenerate produces stable migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        float: Double(),
        date: Date(),
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB(),
        list[str]: JSONB(),
        list[Any]: JSONB(),
    }

    # Natural key used by ``app.db.upsert.upsert`` for ON CONFLICT. Every table defines one.
    __upsert_key__: ClassVar[tuple[str, ...]]


def str_enum(enum_cls: type[StrEnum]) -> Enum:
    """VARCHAR + CHECK constraint storing the enum *values* (not member names)."""
    return Enum(
        enum_cls,
        name=enum_cls.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class SourcedMixin:
    """Every ingested data table records which provider served the row and when."""

    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ComputedMixin:
    """Derived snapshots record when they were computed."""

    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
