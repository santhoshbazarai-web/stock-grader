"""fin_line_items: long-format fundamentals with versions (SPEC v0.2 §3.4, §3.6)

Revision ID: e2c3d4f5a6b7
Revises: d1b2c3e4f5a6
Create Date: 2026-09-30 01:22:21.755807

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2c3d4f5a6b7"
down_revision: str | None = "d1b2c3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fin_line_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "period_type",
            sa.Enum(
                "quarter",
                "year",
                "instant",
                name="periodtype",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "statement",
            sa.Enum(
                "pl",
                "bs",
                "cf",
                "ratio",
                name="linestatement",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "basis",
            sa.Enum(
                "consolidated",
                "standalone",
                name="statementtype",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("value_inr", sa.Double(), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("filing_id", sa.BigInteger(), nullable=True),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usable_from", sa.Date(), nullable=True),
        sa.Column("derived", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("tag", sa.String(length=512), nullable=True),
        sa.Column("map_version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["filing_id"],
            ["result_filings.id"],
            name=op.f("fk_fin_line_items_filing_id_result_filings"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_fin_line_items_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_line_items")),
        sa.UniqueConstraint(
            "instrument_id",
            "period_end",
            "period_type",
            "statement",
            "basis",
            "item_code",
            "version",
            name="uq_fin_line_items_key",
        ),
    )
    op.create_index("ix_fin_line_items_filing", "fin_line_items", ["filing_id"], unique=False)
    op.create_index(
        "ix_fin_line_items_instrument_basis",
        "fin_line_items",
        ["instrument_id", "basis", "period_end"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_fin_line_items_instrument_basis", table_name="fin_line_items")
    op.drop_index("ix_fin_line_items_filing", table_name="fin_line_items")
    op.drop_table("fin_line_items")
