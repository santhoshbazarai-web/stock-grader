"""Helpers for provider tests: serve every configured warm-up page (web_session.py)."""

from typing import Any

import responses

from app.core.config import BrowserSessionConfig


def mock_warmup(browser: BrowserSessionConfig, mock: Any = responses) -> None:
    """Register the site's warm-up pages (homepage -> page) on ``mock``."""
    for url in browser.warmup_urls:
        mock.add(responses.GET, url, body="<html/>")
