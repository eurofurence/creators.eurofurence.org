from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol


@dataclass(frozen=True)
class RegistrationLookup:
    issuer: str
    subject: str
    event_id: int
    event_year: int


class RegistrationStatus(Enum):
    """Internal categories, not EF wire values; the real adapter must map them."""

    PAID = auto()
    CHECKED_IN = auto()
    INELIGIBLE = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class RegistrationResult:
    lookup: RegistrationLookup
    status: RegistrationStatus


class RegistrationUnavailable(Exception):
    """The adapter could not verify this person's event registration."""


class RegistrationClient(Protocol):
    async def lookup(self, lookup: RegistrationLookup) -> RegistrationResult:
        """Verify person/event binding; normalize known statuses or raise unavailable.

        Unknown, missing or ambiguous provider data must not become INELIGIBLE.
        Only the confirmed EF contract may define how these categories are mapped.
        """
        ...


class UnavailableRegistrationClient:
    async def lookup(self, lookup: RegistrationLookup) -> RegistrationResult:
        raise RegistrationUnavailable


def get_registration_client() -> RegistrationClient:
    # No live transport until the EF Registration contract and access are supplied.
    return UnavailableRegistrationClient()
