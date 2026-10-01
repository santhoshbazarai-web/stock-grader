"""symbol master: symbols (ISIN join of NSE / BSE / Fyers), symbol_aliases, pg_trgm search
indexes (SPEC v0.2 §3.5)

Revision ID: b5f6a7c8d9e0
Revises: a4e5f6b7c8d9
Create Date: 2026-09-30 03:37:44.289725

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5f6a7c8d9e0"
down_revision: str | None = "a4e5f6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# pg_trgm GIN indexes for search (declared on the models with models._trgm)
TRGM_INDEXES = (
    ("ix_instruments_symbol_trgm", "instruments", "lower(symbol)"),
    ("ix_instruments_name_trgm", "instruments", "lower(name)"),
    ("ix_symbols_name_trgm", "symbols", "lower(name)"),
    ("ix_symbol_aliases_alias_trgm", "symbol_aliases", "lower(alias)"),
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "symbols",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("nse_symbol", sa.String(length=32), nullable=True),
        sa.Column("nse_series", sa.String(length=8), nullable=True),
        sa.Column("bse_code", sa.String(length=10), nullable=True),
        sa.Column("bse_id", sa.String(length=32), nullable=True),
        sa.Column("fyers_symbol", sa.String(length=48), nullable=True),
        sa.Column("listing_date", sa.Date(), nullable=True),
        sa.Column("face_value", sa.Double(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "inactive",
                name="symbolstatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_seen", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_symbols_instrument_id_instruments"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_symbols")),
        sa.UniqueConstraint("isin", name=op.f("uq_symbols_isin")),
    )
    op.create_index("ix_symbols_bse_code", "symbols", ["bse_code"], unique=False)
    op.create_index("ix_symbols_nse_symbol", "symbols", ["nse_symbol"], unique=False)
    op.create_index("ix_symbols_fyers_symbol", "symbols", ["fyers_symbol"], unique=False)
    op.create_index("ix_symbols_instrument_id", "symbols", ["instrument_id"], unique=False)
    op.create_table(
        "symbol_aliases",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=200), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "former_symbol",
                "former_name",
                "bse_symbol",
                "bse_name",
                "user",
                name="aliaskind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["symbol_id"],
            ["symbols.id"],
            name=op.f("fk_symbol_aliases_symbol_id_symbols"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_symbol_aliases")),
        sa.UniqueConstraint(
            "symbol_id", "kind", "alias", name=op.f("uq_symbol_aliases_symbol_id_kind_alias")
        ),
    )
    for name, table, expr in TRGM_INDEXES:
        op.execute(
            f"CREATE INDEX {name} ON {table} USING gin ({expr} gin_trgm_ops) "
            "WITH (fastupdate = off)"
        )


def downgrade() -> None:
    for name, _, _ in TRGM_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.drop_table("symbol_aliases")
    op.drop_index("ix_symbols_nse_symbol", table_name="symbols")
    op.drop_index("ix_symbols_bse_code", table_name="symbols")
    op.drop_table("symbols")
