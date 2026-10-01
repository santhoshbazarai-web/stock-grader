"""api_usage: calls per metered API per month (Indian API budget)

Revision ID: 3a9e5c1d7f20
Revises: 7c41d2e9a0b5
Create Date: 2026-10-02 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3a9e5c1d7f20"
down_revision: str | None = "7c41d2e9a0b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_call_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "month"),
    )


def downgrade() -> None:
    op.drop_table("api_usage")
