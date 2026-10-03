"""manual portfolios and transactions

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-07 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "portfolios",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("opening_cash", sa.Float(), server_default="0", nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolios")),
        sa.UniqueConstraint("name", name=op.f("uq_portfolios_name")),
    )
    op.create_table(
        "portfolio_transactions",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column(
            "txn_type",
            sa.Enum(
                "buy",
                "sell",
                "dividend",
                "bonus",
                "split",
                name="txntype",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Double(), nullable=True),
        sa.Column("price", sa.Double(), nullable=True),
        sa.Column("fees", sa.Float(), server_default="0", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["portfolios.id"],
            name=op.f("fk_portfolio_transactions_portfolio_id_portfolios"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_portfolio_transactions_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolio_transactions")),
    )
    op.create_index(
        "ix_portfolio_txn_portfolio", "portfolio_transactions", ["portfolio_id", "txn_date"]
    )


def downgrade() -> None:
    op.drop_table("portfolio_transactions")
    op.drop_table("portfolios")
