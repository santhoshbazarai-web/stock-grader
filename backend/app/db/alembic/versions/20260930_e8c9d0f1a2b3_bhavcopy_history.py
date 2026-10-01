"""bhavcopy_days, bhavcopy_prices: NSE bhavcopy OHLCV history (SPEC v0.2 §3.2, price fallback)

Revision ID: e8c9d0f1a2b3
Revises: d7b8c9e0f1a2
Create Date: 2026-09-30 10:45:11.637285

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8c9d0f1a2b3"
down_revision: str | None = "d7b8c9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bhavcopy_days",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("raw_path", sa.String(length=512), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("trade_date", name=op.f("pk_bhavcopy_days")),
    )
    op.create_table(
        "bhavcopy_prices",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("series", sa.String(length=4), nullable=False),
        sa.Column("open", sa.Double(), nullable=True),
        sa.Column("high", sa.Double(), nullable=True),
        sa.Column("low", sa.Double(), nullable=True),
        sa.Column("close", sa.Double(), nullable=True),
        sa.Column("prev_close", sa.Double(), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("traded_value_cr", sa.Double(), nullable=True),
        sa.Column("deliverable_qty", sa.BigInteger(), nullable=True),
        sa.Column("delivery_pct", sa.Double(), nullable=True),
        sa.PrimaryKeyConstraint("symbol", "trade_date", name=op.f("pk_bhavcopy_prices")),
    )
    op.create_index("ix_bhavcopy_prices_date", "bhavcopy_prices", ["trade_date"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_bhavcopy_prices_date", table_name="bhavcopy_prices")
    op.drop_table("bhavcopy_prices")
    op.drop_table("bhavcopy_days")
