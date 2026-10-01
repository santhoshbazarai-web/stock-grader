import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.deps import get_redis
from app.core.auth import get_session_signer
from app.core.config import get_config
from app.core.security import get_cipher
from app.core.settings import get_settings

pytest_plugins = ["tests.jobs_support"]  # the `env` fixture for job tests

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_CONFIG_DIR = BACKEND_DIR.parent / "config"

# A throwaway PostgreSQL 16 database; tests drop and recreate its schema.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://stockgrader:stockgrader@localhost:5432/stockgrader_test",
)


TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_real_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: object) -> None:
    host = address[0] if isinstance(address, tuple) else None
    if host is not None and host not in _LOCAL_HOSTS:
        raise RuntimeError(f"tests must not use the network (attempted {address!r})")
    _real_connect(self, address)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only localhost (test Postgres/Redis) is reachable; external hosts raise.
    Proxy variables are cleared so a local HTTP proxy can't tunnel requests out."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RAW_DATA_DIR", str(tmp_path / "raw"))  # never the repo's data/raw
    monkeypatch.setenv("APP_PASSWORD", "test-password")
    monkeypatch.setenv("CONFIG_DIR", str(REPO_CONFIG_DIR))
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    caches = (get_settings, get_config, get_cipher, get_session_signer, get_redis)
    for c in caches:
        c.cache_clear()
    yield
    for c in caches:
        c.cache_clear()


# ───────────────────────── database ─────────────────────────


def alembic_config(connection: Connection) -> Config:
    cfg = Config()  # no ini file → env.py leaves logging alone
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "alembic"))
    cfg.attributes["connection"] = connection
    return cfg


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DATABASE_URL)
    try:
        with eng.connect():
            pass
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable at TEST_DATABASE_URL ({exc.orig})")
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def migrated_engine(engine: Engine) -> Engine:
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    return engine


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Session]:
    """Session inside a transaction that is rolled back after the test."""
    with migrated_engine.connect() as conn:
        trans = conn.begin()
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            trans.rollback()


# ───────────────────────── redis ─────────────────────────


@pytest.fixture(scope="session")
def redis_client() -> Iterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL)
    try:
        client.ping()
    except RedisConnectionError as exc:
        pytest.skip(f"Redis not reachable at TEST_REDIS_URL ({exc})")
    yield client
    client.close()
