"""result_filings.parse_failed_version: unparseable filings wait for a parser change

Revision ID: 7c41d2e9a0b5
Revises: 1be238f8bf12
Create Date: 2026-10-01 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c41d2e9a0b5"
down_revision: str | None = "1be238f8bf12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "result_filings", sa.Column("parse_failed_version", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("result_filings", "parse_failed_version")
