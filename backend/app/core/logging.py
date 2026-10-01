"""Logging setup shared by the api and worker processes."""

import logging
import re

# Query parameters that carry one-time OAuth codes or tokens (broker callbacks). Their values
# never reach the logs (AGENTS.md rule 8).
SENSITIVE_QUERY_PARAMS = ("request_token", "auth_code", "code", "state", "access_token", "token")
_SENSITIVE = re.compile(r"([?&](?:" + "|".join(SENSITIVE_QUERY_PARAMS) + r")=)[^&#\s]*")


def redact_query(path: str) -> str:
    return _SENSITIVE.sub(r"\1REDACTED", path)


class RedactQueryFilter(logging.Filter):
    """Redacts sensitive query values in uvicorn's access log, whose record args are
    ``(client_addr, method, full_path, http_version, status_code)``."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_query(args[2]), *args[3:])
        return True


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactQueryFilter) for f in access.filters):
        access.addFilter(RedactQueryFilter())
