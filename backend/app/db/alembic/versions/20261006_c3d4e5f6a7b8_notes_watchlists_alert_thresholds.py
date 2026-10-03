"""stock notes, multiple watchlists, alert thresholds and new alert types

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-06 09:00:00.000000

"""

# ruff: noqa: E501
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("enters_buy_zone", "crosses_fv", "crosses_top_band", "crosses_invalidation")
NEW = (*OLD, "price_above", "price_below", "results_date")


def _alert_check(values: Sequence[str]) -> None:
    op.drop_constraint(op.f("ck_alerts_alerttype"), "alerts", type_="check")
    quoted = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint("alerttype", "alerts", f"alert_type IN ({quoted})")


def upgrade() -> None:
    def ts() -> list[sa.Column]:  # type: ignore[type-arg]
        return [
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        ]  # fmt: skip

    op.create_table(
        "watchlists",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        *ts(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlists")),
        sa.UniqueConstraint("name", name=op.f("uq_watchlists_name")),
    )
    op.execute("INSERT INTO watchlists (name) VALUES ('Default')")
    op.add_column("watchlist", sa.Column("watchlist_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE watchlist SET watchlist_id = (SELECT id FROM watchlists WHERE name = 'Default')"
    )
    op.alter_column("watchlist", "watchlist_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_watchlist_watchlist_id_watchlists"),
        "watchlist", "watchlists", ["watchlist_id"], ["id"], ondelete="CASCADE",
    )  # fmt: skip
    op.drop_constraint("uq_watchlist_instrument_id", "watchlist", type_="unique")
    op.create_unique_constraint(
        op.f("uq_watchlist_watchlist_id_instrument_id"),
        "watchlist",
        ["watchlist_id", "instrument_id"],
    )

    op.create_table(
        "stock_notes",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        *ts(),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_stock_notes_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_notes")),
    )
    op.create_index("ix_stock_notes_instrument", "stock_notes", ["instrument_id", "created_at"])

    op.add_column("alerts", sa.Column("threshold", sa.Double(), nullable=True))
    _alert_check(NEW)


def downgrade() -> None:
    op.execute(
        "DELETE FROM alerts WHERE alert_type NOT IN ('enters_buy_zone','crosses_fv','crosses_top_band','crosses_invalidation')"
    )
    _alert_check(OLD)
    op.drop_column("alerts", "threshold")
    op.drop_table("stock_notes")
    op.drop_constraint("uq_watchlist_watchlist_id_instrument_id", "watchlist", type_="unique")
    op.create_unique_constraint("uq_watchlist_instrument_id", "watchlist", ["instrument_id"])
    op.drop_constraint("fk_watchlist_watchlist_id_watchlists", "watchlist", type_="foreignkey")
    op.drop_column("watchlist", "watchlist_id")
    op.drop_table("watchlists")
