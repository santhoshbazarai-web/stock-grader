"""alerts.state + notifications (intraday alerts, P14)

Revision ID: b8f7d9e0a1c2
Revises: a7e6c8d9f0b1
Create Date: 2026-09-29 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8f7d9e0a1c2"
down_revision: str | None = "a7e6c8d9f0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "alerts", sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("alert_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("price", sa.Double(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram", sa.String(length=16), nullable=False),
        sa.Column("telegram_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alerts.id"],
            name=op.f("fk_notifications_alert_id_alerts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index("ix_notifications_created", "notifications", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_column("alerts", "state")
