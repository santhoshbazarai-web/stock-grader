from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect

from app.db.models import Base
from tests.conftest import BACKEND_DIR, alembic_config

MODEL_TABLES = set(Base.metadata.tables)


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_single_head() -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "alembic"))
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1


def test_upgrade_downgrade_roundtrip(engine: Engine) -> None:
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    assert _tables(engine) == MODEL_TABLES

    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "base")
    assert _tables(engine) == set()

    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    assert _tables(engine) == MODEL_TABLES


def test_migration_matches_models(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_spec_tables_present() -> None:
    # SPEC §3.4
    assert {
        "instruments",
        "prices_daily",
        "corporate_actions",
        "delivery_daily",
        "fin_annual",
        "fin_quarterly",
        "shareholding",
        "index_membership",
        "surveillance_flags",
        "valuation_snapshots",
        "technical_snapshots",
        "scores",
        "reports",
        "watchlist",
        "alerts",
        "broker_tokens",
        "job_runs",
        "data_gaps",
        "user_overrides",
        "backtests",  # SPEC §8 /api/backtests
        "screener_presets",  # SPEC §9 screener presets
        "notifications",  # in-app alert notifications
        "result_filings",  # exchange results filings (XBRL) ledger
    } == MODEL_TABLES
