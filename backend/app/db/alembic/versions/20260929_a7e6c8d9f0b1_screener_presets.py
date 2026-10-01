"""screener_presets: saved screener filters

Revision ID: a7e6c8d9f0b1
Revises: f6d5b7c8e9a0
Create Date: 2026-09-29 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7e6c8d9f0b1"
down_revision: str | None = "f6d5b7c8e9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screener_presets",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screener_presets")),
        sa.UniqueConstraint("name", name=op.f("uq_screener_presets_name")),
    )


def downgrade() -> None:
    op.drop_table("screener_presets")
