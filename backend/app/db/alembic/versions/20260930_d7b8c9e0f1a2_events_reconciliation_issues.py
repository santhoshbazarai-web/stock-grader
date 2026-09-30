"""events, reconciliation_issues, pipeline_runs.context (SPEC v0.2 §3.8, §3.9)

Revision ID: d7b8c9e0f1a2
Revises: c6a7b8d9e0f1
Create Date: 2026-09-30 10:09:39.906994

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7b8c9e0f1a2"
down_revision: str | None = "c6a7b8d9e0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_issues",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "period_type",
            sa.Enum(
                "quarter",
                "year",
                "instant",
                name="periodtype",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "basis",
            sa.Enum(
                "consolidated",
                "standalone",
                name="statementtype",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("reference_source", sa.String(length=32), nullable=False),
        sa.Column("reference_value_inr", sa.Double(), nullable=False),
        sa.Column("value_inr", sa.Double(), nullable=False),
        sa.Column("diff_rel", sa.Double(), nullable=False),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cause", sa.String(length=32), nullable=True),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "resolved",
                "ignored",
                name="issuestatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_reconciliation_issues_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_issues")),
        sa.UniqueConstraint(
            "instrument_id",
            "period_end",
            "period_type",
            "item_code",
            "source",
            name="uq_reconciliation_issues_key",
        ),
    )
    op.create_index(
        "ix_reconciliation_issues_status",
        "reconciliation_issues",
        ["instrument_id", "status"],
        unique=False,
    )
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "announcement",
                "board_meeting",
                "results",
                "pledge",
                "sast",
                "insider_trade",
                "bulk_deal",
                "block_deal",
                name="eventkind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("bse_code", sa.String(length=16), nullable=True),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("red_flag", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("disseminated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_path", sa.String(length=512), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_events_instrument_id_instruments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_runs.id"],
            name=op.f("fk_events_pipeline_run_id_pipeline_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint(
            "exchange", "kind", "source_id", name=op.f("uq_events_exchange_kind_source_id")
        ),
    )
    op.create_index("ix_events_instrument", "events", ["instrument_id", "event_date"], unique=False)
    op.create_index(
        "ix_events_kind_disseminated", "events", ["kind", "disseminated_at"], unique=False
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pipeline_runs", "context")
    op.drop_index("ix_events_kind_disseminated", table_name="events")
    op.drop_index("ix_events_instrument", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_reconciliation_issues_status", table_name="reconciliation_issues")
    op.drop_table("reconciliation_issues")
