"""``make doctor`` (SPEC v0.2 §3.10): checks a home deployment end to end and says what to fix.

    python -m app.doctor [--json]

Checks, in order (each ✓ ok, ! warning or ✗ failure; exit code 1 on any failure):

- **env**: the settings load (FERNET_KEY valid, APP_PASSWORD set), the password is long enough,
  the database does not use the development password, and the enabled brokers / Telegram have
  their credentials. Secret values are never printed.
- **config**: ``config/*.yaml`` validates.
- **database**: Postgres answers and the schema is at the latest migration.
- **redis**: Redis answers.
- **brokers**: each enabled broker's token (connected / expired / missing).
- **nse**: NSE's site and archives answer (only reachability; a block is a warning).
- **disk**: free space where raw files and backups are written.
- **backups**: the last successful nightly pg_dump (``BACKUP_DIR``, when mounted).
- **worker**: a scheduled job ran recently (the worker is alive), and the Telegram bot polls.

Every check is independent: one failing (e.g. no database) does not hide the others.
"""

import argparse
import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import requests
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from redis import Redis
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.alerts.bot import bot_status
from app.core.config import AppConfig, ConfigError, load_config
from app.core.security import TokenCipher
from app.core.settings import MIN_PROD_PASSWORD, Settings, config_dir_from_env
from app.data.broker_tokens import BrokerTokenStore, broker_configured
from app.data.providers.nse import BROWSER_HEADERS
from app.db.enums import Broker
from app.db.models import JobRun

Status = Literal["ok", "warn", "fail"]
SYMBOL = {"ok": "✓", "warn": "!", "fail": "✗"}
_GB = 1024**3


@dataclass(frozen=True)
class Check:
    area: str
    status: Status
    detail: str


class Doctor:
    def __init__(self, *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 http: requests.Session | None = None) -> None:  # fmt: skip
        self.checks: list[Check] = []
        self._now = now
        self._http = http or requests.Session()

    def add(self, area: str, status: Status, detail: str) -> None:
        self.checks.append(Check(area, status, detail))

    # ───────────── checks ─────────────

    def run(self) -> list[Check]:
        try:
            settings = Settings()
        except ValidationError as exc:
            fields = ", ".join(sorted({str(e["loc"][0]).upper() for e in exc.errors() if e["loc"]}))
            self.add("env", "fail", f"settings invalid or missing: {fields or exc.title} "
                     "(see .env.example)")  # fmt: skip
            settings = None
        try:
            config = load_config(settings.config_dir if settings else config_dir_from_env())
            self.add("config", "ok", "config/*.yaml valid")
        except ConfigError as exc:
            self.add("config", "fail", str(exc).splitlines()[0][:300])
            config = None
        if settings is not None:
            self._guard("env", lambda: self._env(settings, config))
            engine_ok = self._guard("database", lambda: self._database(settings))
            self._guard("redis", lambda: self._redis(settings, config))
            if engine_ok and config is not None:
                self._guard("brokers", lambda: self._brokers(settings, config))
                self._guard("worker", lambda: self._worker(settings, config))
        if config is not None:
            self._guard("nse", lambda: self._nse(config))
            self._guard("disk", lambda: self._disk(settings, config))
            self._guard("backups", lambda: self._backups(config))
        # env first, then config, then the rest in run order
        rank = {"env": 0, "config": 1}
        self.checks.sort(key=lambda c: rank.get(c.area, 2))
        return self.checks

    def _guard[T](self, area: str, check: Callable[[], T]) -> T | None:
        """Run one check; an unexpected error is reported as that check's failure, so the
        doctor always finishes and shows every other check."""
        try:
            return check()
        except Exception as exc:
            self.add(area, "fail", f"check crashed: {type(exc).__name__}: {str(exc)[:200]}")
            return None

    def _env(self, settings: Settings, config: AppConfig | None) -> None:

        problems = []
        if len(settings.app_password.get_secret_value()) < MIN_PROD_PASSWORD:
            problems.append(f"APP_PASSWORD shorter than {MIN_PROD_PASSWORD} characters")
        if urlsplit(settings.database_url).password in (None, "", "stockgrader"):
            problems.append("DATABASE_URL uses the development password")
        if config is not None:
            for b in Broker:
                if config.providers.brokers[b].enabled and not broker_configured(b, settings):
                    problems.append(f"{b.value} enabled but its API credentials are not set")
        if not (settings.telegram_bot_token and settings.telegram_chat_id):
            problems.append("Telegram not configured (optional)")
        self.add("env", "warn" if problems else "ok",
                 "; ".join(problems) if problems else
                 f"settings load ({settings.app_env}), secrets set")  # fmt: skip

    def _database(self, settings: Settings) -> bool:
        try:
            engine = create_engine(settings.database_url, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                current = MigrationContext.configure(conn).get_current_revision()
        except Exception as exc:
            self.add("database", "fail", f"cannot connect: {type(exc).__name__}")
            return False
        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).parent / "db" / "alembic"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
        if current != head:
            self.add("database", "fail", f"schema at {current or 'empty'}, latest is {head}: "
                     "run the migrations (make home-up does)")  # fmt: skip
            return False
        self.add("database", "ok", f"connected, schema at the latest migration ({head})")
        return True

    def _redis(self, settings: Settings, config: AppConfig | None) -> None:
        try:
            r = Redis.from_url(settings.redis_url, socket_timeout=5)
            r.ping()
        except Exception as exc:
            self.add("redis", "fail", f"cannot connect: {type(exc).__name__}")
            return
        self.add("redis", "ok", "answers")
        if config is None or not (settings.telegram_bot_token and settings.telegram_chat_id):
            return

        st = bot_status(r)
        last = st.get("last_poll_at")
        if not config.jobs.telegram_bot.enabled:
            return
        if not last:
            self.add("telegram bot", "warn", "never polled: is the worker running?")
            return
        age_min = (self._now() - datetime.fromisoformat(last)).total_seconds() / 60
        limit = config.jobs.doctor.bot_max_idle_minutes
        self.add(
            "telegram bot",
            "ok" if age_min <= limit else "warn",
            f"last poll {age_min:.0f} min ago"
            + (f"; last error: {st['last_error']}" if st.get("last_error") else ""),
        )

    def _brokers(self, settings: Settings, config: AppConfig) -> None:

        factory = sessionmaker(bind=create_engine(settings.database_url))
        store = BrokerTokenStore(factory, TokenCipher(settings.fernet_key.get_secret_value()))
        for b in Broker:
            if not config.providers.brokers[b].enabled:
                continue
            if not broker_configured(b, settings):
                self.add(f"broker {b.value}", "warn", "credentials not set")
                continue
            st = store.status(b)
            detail = st.reason + (f" (expires {st.expires_at:%d %b %H:%M} UTC)"
                                  if st.connected and st.expires_at else "")  # fmt: skip
            self.add(
                f"broker {b.value}",
                "ok" if st.connected else "warn",
                detail + ("" if st.connected else ": reconnect in Settings → Brokers"),
            )

    def _worker(self, settings: Settings, config: AppConfig) -> None:

        with Session(create_engine(settings.database_url)) as s:
            last = s.scalar(select(func.max(JobRun.started_at)))
        if last is None:
            self.add("worker", "warn", "no scheduled job has run yet: is the worker running?")
            return
        hours = (self._now() - last).total_seconds() / 3600
        limit = config.jobs.doctor.worker_max_idle_hours
        self.add("worker", "ok" if hours <= limit else "warn",
                 f"last job run {hours:.1f} h ago" + ("" if hours <= limit
                                                     else ": is the worker running?"))  # fmt: skip

    def _nse(self, config: AppConfig) -> None:

        nse = config.providers.nse
        for name, url in (("site", f"{nse.base_url}/"), ("archives", f"{nse.archives_url}/")):
            try:
                resp = self._http.get(url, headers=BROWSER_HEADERS,
                                      timeout=config.jobs.doctor.nse_timeout_s)  # fmt: skip
                code = resp.status_code
            except requests.RequestException as exc:
                self.add(f"nse {name}", "warn", f"unreachable ({type(exc).__name__}): prices "
                         "and filings fall back to other sources")  # fmt: skip
                continue
            # the archives root may answer 403/404 to a bare GET: any HTTP answer means reachable
            ok = code < 400 or name == "archives"
            self.add(
                f"nse {name}",
                "ok" if ok else "warn",
                f"HTTP {code}" + ("" if ok else ": blocked? (browser session refused)"),
            )

    def _disk(self, settings: Settings | None, config: AppConfig) -> None:
        cfg = config.jobs.doctor
        paths = {"raw data": settings.raw_data_dir if settings else None,
                 "backups": Path(os.environ["BACKUP_DIR"]) if os.environ.get("BACKUP_DIR")
                 else None}  # fmt: skip
        for label, path in paths.items():
            if path is None:
                continue
            probe = path
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            free = shutil.disk_usage(probe).free / _GB
            status: Status = "fail" if free < cfg.disk_fail_gb else \
                "warn" if free < cfg.disk_warn_gb else "ok"  # fmt: skip
            self.add(f"disk ({label})", status, f"{free:,.1f} GB free at {path}")

    def _backups(self, config: AppConfig) -> None:
        root = os.environ.get("BACKUP_DIR")
        if not root:
            self.add("backups", "warn", "BACKUP_DIR not visible here: run `make doctor` "
                     "(it mounts the backup path) to check them")  # fmt: skip
            return
        marker = Path(root) / ".last-success"
        if not os.access(root, os.R_OK | os.X_OK):
            self.add("backups", "warn", f"cannot read {root} (permissions)")
            return
        if not marker.is_file():
            self.add("backups", "warn", f"no successful backup recorded in {root} yet")
            return
        last = datetime.fromtimestamp(int(marker.read_text().strip() or 0), UTC)
        hours = (self._now() - last).total_seconds() / 3600
        dumps = sorted((Path(root) / "daily").glob("stockgrader-*.dump"))
        limit = config.jobs.doctor.backup_max_age_hours
        self.add(
            "backups",
            "ok" if hours <= limit else "warn",
            f"last backup {hours:.1f} h ago, {len(dumps)} daily dump(s) in {root}",
        )


def render(checks: list[Check]) -> str:
    width = max((len(c.area) for c in checks), default=0)
    lines = [f"{SYMBOL[c.status]} {c.area:<{width}}  {c.detail}" for c in checks]
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    lines.append("")
    lines.append(f"{fails} failure(s), {warns} warning(s)" if fails or warns else "all good")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a Stock Grader deployment")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    checks = Doctor().run()
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        print(render(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
