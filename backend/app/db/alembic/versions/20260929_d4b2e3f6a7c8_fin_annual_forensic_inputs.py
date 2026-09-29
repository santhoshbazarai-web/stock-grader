"""fin_annual: current_assets, retained_earnings, sga (forensic score inputs)

Revision ID: d4b2e3f6a7c8
Revises: c3a1d2e4f5b6
Create Date: 2026-09-29 03:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b2e3f6a7c8"
down_revision: str | None = "c3a1d2e4f5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("sga", "current_assets", "retained_earnings")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("fin_annual", sa.Column(name, sa.Double(), nullable=True))


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("fin_annual", name)
