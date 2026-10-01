"""Notification centre (P23): in-app notifications from price alerts (P14), results changes
(P21), broker-token reminders (P22); filters, paging, read state, Telegram re-delivery, the
Telegram bot's status, and a test message. Every notification is created by
``app.alerts.notify.notify``, which also sends it to Telegram when configured."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.alerts.bot import bot_status
from app.alerts.notify import notify
from app.alerts.telegram import build_notifier
from app.api.deps import ConfigDep, RedisDep, SessionDep, SettingsDep
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
    kinds: dict[str, int] = Field(default_factory=dict, description="All notifications by kind")
    next_before_id: int | None = Field(None, description="Pass as before_id for the next page")


class TelegramStatus(BaseModel):
    configured: bool  # TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set (delivery works)
    bot_enabled: bool  # jobs.telegram_bot.enabled
    state: str | None  # polling | standby (another worker polls) | stopped | None = never ran
    last_poll_at: datetime | None
    last_command_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    ignored_messages: int  # messages from chats not on the allow-list (never answered)


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
    kind: Annotated[list[str] | None, Query()] = None,
    symbol: str | None = None,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> NotificationsView:
    """Newest first (by id), filtered by read state, kind and symbol; ``before_id`` pages
    back. Also the unread count (the bell), counts per kind and whether Telegram is set up."""
    q = select(Notification).order_by(Notification.id.desc())
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    if kind:
        q = q.where(Notification.kind.in_(kind))
    if symbol:
        q = q.where(Notification.symbol == symbol.upper())
    if before_id is not None:
        q = q.where(Notification.id < before_id)
    rows = list(session.scalars(q.limit(limit + 1)))
    unread = session.scalar(
        select(func.count()).select_from(Notification).where(Notification.read_at.is_(None))
    )
    kinds = dict(session.execute(
        select(Notification.kind, func.count()).group_by(Notification.kind)).all())  # fmt: skip
    return NotificationsView(
        items=[_out(n) for n in rows[:limit]],
        unread=unread or 0,
        telegram_configured=bool(settings.telegram_bot_token and settings.telegram_chat_id),
        kinds=kinds,
        next_before_id=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.get("/telegram")
def telegram_status(settings: SettingsDep, config: ConfigDep, redis: RedisDep) -> TelegramStatus:
    """Delivery set-up and the bot's heartbeat (written by the worker's bot thread)."""
    st = bot_status(redis)

    def ts(key: str) -> datetime | None:
        return datetime.fromisoformat(st[key]) if st.get(key) else None

    return TelegramStatus(
        configured=bool(settings.telegram_bot_token and settings.telegram_chat_id),
        bot_enabled=config.jobs.telegram_bot.enabled,
        state=st.get("state"), last_poll_at=ts("last_poll_at"),
        last_command_at=ts("last_command_at"), last_error=st.get("last_error"),
        last_error_at=ts("last_error_at"), ignored_messages=st["ignored_messages"],
    )  # fmt: skip


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


@router.post(
    "/{notification_id}/unread",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "No such notification"}},
)
def mark_unread(notification_id: int, session: SessionDep) -> None:
    n = session.get(Notification, notification_id)
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no notification {notification_id}")
    n.read_at = None
    session.commit()


@router.post(
    "/{notification_id}/resend",
    responses={404: {"description": "No such notification"},
               409: {"description": "Telegram not configured"}},
)  # fmt: skip
def resend(
    notification_id: int, session: SessionDep, settings: SettingsDep, config: ConfigDep
) -> NotificationOut:
    """Send a notification to Telegram again (e.g. after a failed delivery)."""
    n = session.get(Notification, notification_id)
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no notification {notification_id}")
    notifier = build_notifier(settings, config.jobs.alerts.telegram_timeout_s)
    if notifier is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Telegram is not configured")
    delivery = notifier.send(f"{n.title}\n{n.body}")
    n.telegram, n.telegram_error = delivery.status, delivery.error
    session.commit()
    return _out(n)


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
