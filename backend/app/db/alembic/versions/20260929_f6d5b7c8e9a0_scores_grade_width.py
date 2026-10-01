"""scores: widen grade columns to fit "A_plus"

Revision ID: f6d5b7c8e9a0
Revises: e5c4a6b7d8f9
Create Date: 2026-09-29 08:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6d5b7c8e9a0"
down_revision: str | None = "e5c4a6b7d8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("provisional_grade", "grade")


def upgrade() -> None:
    for name in _COLUMNS:
        op.alter_column(
            "scores", name, type_=sa.String(8), existing_type=sa.String(4), existing_nullable=True
        )


def downgrade() -> None:
    for name in _COLUMNS:
        op.alter_column(
            "scores", name, type_=sa.String(4), existing_type=sa.String(8), existing_nullable=True
        )
