"""Create an in-app notification and, when configured, send it to Telegram too."""

from sqlalchemy.orm import Session

from app.alerts.telegram import Delivery, TelegramNotifier
from app.db.models import Notification


def notify(
    session: Session,
    *,
    kind: str,
    title: str,
    body: str,
    notifier: TelegramNotifier | None,
    symbol: str | None = None,
    price: float | None = None,
    alert_id: int | None = None,
) -> Notification:
    """Adds (does not commit) the notification. Telegram failure never loses the in-app one."""
    delivery = notifier.send(f"{title}\n{body}") if notifier is not None else Delivery("disabled")
    n = Notification(
        alert_id=alert_id,
        symbol=symbol,
        kind=kind,
        title=title,
        body=body,
        price=price,
        telegram=delivery.status,
        telegram_error=delivery.error,
    )
    session.add(n)
    return n
