"""result_filings: ledger of exchange results filings (XBRL)

Revision ID: c9a8e0f1b2d3
Revises: b8f7d9e0a1c2
Create Date: 2026-09-30 09:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9a8e0f1b2d3"
down_revision: str | None = "b8f7d9e0a1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "result_filings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("document", sa.String(length=512), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column(
            "statement_type",
            sa.Enum(
                "consolidated",
                "standalone",
                name="statementtype",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("audited", sa.Boolean(), nullable=True),
        sa.Column("is_bank", sa.Boolean(), nullable=True),
        sa.Column("disseminated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("announcement_date", sa.Date(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "parsed",
                "failed",
                name="filingstatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("periods", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_result_filings_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_result_filings")),
        sa.UniqueConstraint(
            "instrument_id", "document", name=op.f("uq_result_filings_instrument_id_document")
        ),
    )
    op.create_index("ix_result_filings_status", "result_filings", ["status"])


def downgrade() -> None:
    op.drop_index("ix_result_filings_status", table_name="result_filings")
    op.drop_table("result_filings")
