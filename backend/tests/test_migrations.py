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
        "fin_line_items",  # SPEC v0.2 §3.4 long-format fundamentals
        "annual_reports",  # SPEC v0.2 §3.6 step 3: annual-report PDF ledger
        "pdf_line_candidates",  # ... and its review queue
        "symbols",  # SPEC v0.2 §3.4-3.5: ISIN symbol master
        "symbol_aliases",
        "pipeline_runs",  # SPEC v0.2 §3.4, §3.7: on-demand pipeline progress
        "events",  # SPEC v0.2 §3.4, §3.8: exchange event feeds
        "reconciliation_issues",  # SPEC v0.2 §3.9: cross-source differences
        "bhavcopy_days",  # SPEC v0.2 §3.2: NSE bhavcopy OHLCV history (price fallback)
        "report_theses",  # SPEC §8a: LLM thesis per exact fact sheet (P26)
        "bhavcopy_prices",
        "api_usage",  # SPEC §3.9a: metered API calls per month (Indian API budget)
        "vendor_responses",  # ... every raw vendor answer, stored before it is read
        "vendor_names",  # ... the vendor name that last gave a verified answer
        "ui_preferences",  # UI settings, e.g. the stock page "My metrics"
        "price_anomalies",  # SPEC §3.2: moves that look like a missing / doubled split
    } == MODEL_TABLES
