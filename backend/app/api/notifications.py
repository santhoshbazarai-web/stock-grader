"""In-app notifications from price alerts (P14), plus a Telegram test message."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, update

from app.alerts.notify import notify
from app.alerts.telegram import build_notifier
from app.api.deps import ConfigDep, SessionDep, SettingsDep
from app.db.models import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: int
    created_at: datetime
    symbol: str | None
    kind: str
    title: str
    body: str
    price: float | None
    read: bool
    telegram: str  # sent | failed | disabled
    telegram_error: str | None


class NotificationsView(BaseModel):
    items: list[NotificationOut]
    unread: int
    telegram_configured: bool


def _out(n: Notification) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        created_at=n.created_at,
        symbol=n.symbol,
        kind=n.kind,
        title=n.title,
        body=n.body,
        price=n.price,
        read=n.read_at is not None,
        telegram=n.telegram,
        telegram_error=n.telegram_error,
    )


@router.get("")
def list_notifications(
    session: SessionDep,
    settings: SettingsDep,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> NotificationsView:
    """Newest first, with the unread count (for the bell) and whether Telegram is set up."""
    q = select(Notification).order_by(Notification.created_at.desc(), Notification.id.desc())
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    unread = session.scalar(
        select(func.count()).select_from(Notification).where(Notification.read_at.is_(None))
    )
    return NotificationsView(
        items=[_out(n) for n in session.scalars(q.limit(limit))],
        unread=unread or 0,
        telegram_configured=bool(settings.telegram_bot_token and settings.telegram_chat_id),
    )


@router.post(
    "/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "No such notification"}},
)
def mark_read(notification_id: int, session: SessionDep) -> None:
    n = session.get(Notification, notification_id)
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no notification {notification_id}")
    if n.read_at is None:
        n.read_at = datetime.now(UTC)
        session.commit()


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_read(session: SessionDep) -> None:
    session.execute(
        update(Notification).where(Notification.read_at.is_(None)).values(read_at=func.now())
    )
    session.commit()


@router.post("/test", status_code=status.HTTP_201_CREATED)
def send_test(session: SessionDep, settings: SettingsDep, config: ConfigDep) -> NotificationOut:
    """Create a test notification and send it to Telegram (if configured), to check delivery."""
    n = notify(
        session,
        kind="test",
        title="Stock Grader test notification",
        body="Alerts will arrive here (and on Telegram, if configured).",
        notifier=build_notifier(settings, config.jobs.alerts.telegram_timeout_s),
    )
    session.commit()
    session.refresh(n)
    return _out(n)
