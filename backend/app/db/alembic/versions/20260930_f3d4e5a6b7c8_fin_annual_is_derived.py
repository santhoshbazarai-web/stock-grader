"""fin_annual.is_derived: fiscal-year P&L summed from its quarters (SPEC v0.2 §3.6 step 5)

Revision ID: f3d4e5a6b7c8
Revises: e2c3d4f5a6b7
Create Date: 2026-09-30 15:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3d4e5a6b7c8"
down_revision: str | None = "e2c3d4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fin_annual",
        sa.Column("is_derived", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("fin_annual", "is_derived")
