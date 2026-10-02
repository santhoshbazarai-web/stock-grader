"""Seed HDFCBANK for the UI screenshots (not collected by pytest: no ``test_`` prefix).

    TEST_DATABASE_URL=postgresql+psycopg://…/screens SCREENS_DUMP=/tmp/screens.sql \
        pytest tests/seed_screenshots.py -p no:cacheprovider

Builds the Prompt B acceptance data (Indian API fixtures; synthetic prices ending at the
vendor's quoted NSE price, NOT real quotes), stores the HDFCBANK report and dumps the database
to SCREENS_DUMP for ``psql`` into the DB the demo server uses."""

import os
import subprocess

from sqlalchemy import make_url

from app.core.config import get_config
from app.reports.service import refresh_report
from tests.conftest import TEST_DATABASE_URL
from tests.jobs_support import Env
from tests.test_acceptance_indianapi import reports  # noqa: F401  (fixture)


def test_seed(env: Env, reports: dict) -> None:  # type: ignore[type-arg]  # noqa: F811
    with env.session() as s:
        refresh_report(s, "HDFCBANK", get_config())
        s.commit()
    url = make_url(TEST_DATABASE_URL)
    subprocess.run(
        ["pg_dump", "-h", url.host or "localhost", "-p", str(url.port or 5432), "-U",
         url.username or "", "-d", url.database or "", "-f", os.environ["SCREENS_DUMP"],
         "--no-owner"],
        check=True, env={**os.environ, "PGPASSWORD": url.password or ""},
    )  # fmt: skip
