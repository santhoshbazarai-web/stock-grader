"""Helpers for API tests: an app wired to the test DB session, logged in."""

from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.main import create_app

PASSWORD = "test-password"  # conftest sets APP_PASSWORD


def login(client: TestClient) -> str:
    res = client.post("/api/auth/login", json={"password": PASSWORD})
    assert res.status_code == 200, res.text
    return str(res.json()["token"])


def app_client(db: Session, *, authed: bool = True, **kwargs: object) -> Iterator[TestClient]:
    app = create_app()

    def session_override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_session] = session_override
    with TestClient(app, **kwargs) as c:  # type: ignore[arg-type]
        if authed:
            login(c)
        yield c
