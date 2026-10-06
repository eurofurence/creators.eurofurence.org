"""Notification transport boundary; provider credentials never enter domain records."""

from dataclasses import dataclass
from importlib import import_module
from typing import Protocol

from app.config import settings


@dataclass(frozen=True)
class Notification:
    delivery_id: str
    recipient_id: int
    event_id: int
    application_id: int
    kind: str
    subject: str
    body: str
    category: str = "Operational"


class DeliveryFailure(Exception):
    def __init__(self, *, retry_after: int | None = None):
        super().__init__("Notification provider did not accept delivery")
        self.retry_after = retry_after


class ProviderUnavailable(DeliveryFailure):
    pass


class NotificationProvider(Protocol):
    async def send(self, notification: Notification) -> None:
        """Return only after provider acceptance; raise DeliveryFailure otherwise.

        Resolve the local recipient using the approved EF identifier contract.
        Use registered Operational type keys and provider channel preferences.
        Bound calls to less than the five-minute worker lease. Where supported,
        use delivery_id for provider deduplication. Never log payloads/secrets.
        """
        ...


class UnavailableNotificationProvider:
    async def send(self, notification: Notification) -> None:
        raise ProviderUnavailable


def get_notification_provider() -> NotificationProvider:
    path = settings.notification_provider_factory
    if not path:
        return UnavailableNotificationProvider()
    try:
        module, name = path.split(":", 1)
        provider = getattr(import_module(module), name)()
        if not callable(getattr(provider, "send", None)):
            raise TypeError
        return provider
    except Exception:  # noqa: BLE001 - sanitize provider construction failures
        # Configuration/constructor errors may contain credentials; do not echo them.
        raise RuntimeError(
            "Notification provider configuration is unavailable"
        ) from None
