"""instruments.basic_industry, industry_source, classified_at: sector model from industries.yaml

Revision ID: 1be238f8bf12
Revises: f72f7aee3b39
Create Date: 2026-10-01 10:20:45.553837

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1be238f8bf12"
down_revision: str | None = "f72f7aee3b39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.add_column("instruments", sa.Column("basic_industry", sa.String(length=200), nullable=True))
    op.add_column("instruments", sa.Column("industry_source", sa.String(length=16), nullable=True))
    op.add_column(
        "instruments", sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:

    op.drop_column("instruments", "classified_at")
    op.drop_column("instruments", "industry_source")
    op.drop_column("instruments", "basic_industry")
