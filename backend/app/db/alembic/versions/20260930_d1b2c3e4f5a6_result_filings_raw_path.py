"""result_filings.raw_path: where the document was cached before parsing

Revision ID: d1b2c3e4f5a6
Revises: c9a8e0f1b2d3
Create Date: 2026-09-30 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1b2c3e4f5a6"
down_revision: str | None = "c9a8e0f1b2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("result_filings", sa.Column("raw_path", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("result_filings", "raw_path")
