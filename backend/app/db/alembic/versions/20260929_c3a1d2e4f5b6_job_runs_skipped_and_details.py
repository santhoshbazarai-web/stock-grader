"""job_runs: 'skipped' status and details column

Revision ID: c3a1d2e4f5b6
Revises: b50ffa78a35a
Create Date: 2026-09-29 02:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3a1d2e4f5b6"
down_revision: str | None = "b50ffa78a35a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CK = "ck_job_runs_jobstatus"


def upgrade() -> None:
    op.add_column(
        "job_runs",
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_constraint(op.f(_CK), "job_runs", type_="check")
    op.create_check_constraint(
        op.f(_CK), "job_runs", "status IN ('running', 'success', 'failed', 'skipped')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM job_runs WHERE status = 'skipped'")
    op.drop_constraint(op.f(_CK), "job_runs", type_="check")
    op.create_check_constraint(op.f(_CK), "job_runs", "status IN ('running', 'success', 'failed')")
    op.drop_column("job_runs", "details")
