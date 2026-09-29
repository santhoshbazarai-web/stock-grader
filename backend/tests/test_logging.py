import logging

import pytest

from app.core.logging import RedactQueryFilter, configure_logging, redact_query


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/api/brokers/kite/callback?request_token=abc123&action=login&status=success&state=s.1",
            "/api/brokers/kite/callback?request_token=REDACTED&action=login&status=success"
            "&state=REDACTED",
        ),
        (
            "/api/brokers/fyers/callback?s=ok&code=200&auth_code=eyJ.x-y&state=z",
            "/api/brokers/fyers/callback?s=ok&code=REDACTED&auth_code=REDACTED&state=REDACTED",
        ),
        ("/api/screener?sort=symbol&zone=fair", "/api/screener?sort=symbol&zone=fair"),
        ("/api/stocks/TCS?tokenize=1", "/api/stocks/TCS?tokenize=1"),  # whole names only
    ],
)
def test_redact_query(path: str, expected: str) -> None:
    assert redact_query(path) == expected


def test_uvicorn_access_log_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging("INFO")
    configure_logging("INFO")  # idempotent: one filter
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, RedactQueryFilter) for f in access.filters) == 1
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "172.30.0.10:1234",
            "GET",
            "/api/brokers/kite/callback?request_token=SECRET123&state=abc",
            "1.1",
            303,
        )
    assert "SECRET123" not in caplog.text
    assert "request_token=REDACTED&state=REDACTED" in caplog.text
