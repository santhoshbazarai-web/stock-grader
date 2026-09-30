"""pipeline_runs: on-demand per-symbol pipeline with resumable steps (SPEC v0.2 §3.7)

Revision ID: c6a7b8d9e0f1
Revises: b5f6a7c8d9e0
Create Date: 2026-09-30 06:19:09.883834

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6a7b8d9e0f1"
down_revision: str | None = "b5f6a7c8d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("force", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "done",
                "failed",
                name="pipelinestatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("report_as_of", sa.Date(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_pipeline_runs_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_runs")),
    )
    op.create_index(
        "ix_pipeline_runs_status", "pipeline_runs", ["status", "created_at"], unique=False
    )
    op.create_index(
        "ix_pipeline_runs_symbol", "pipeline_runs", ["symbol", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_runs_symbol", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_status", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
