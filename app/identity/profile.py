from typing import Protocol

from app.registration.client import RegistrationLookup


class IdentityProfileUnavailable(Exception):
    """The trusted Identity profile source is unavailable."""


class IdentityProfileClient(Protocol):
    async def email(self, identity: RegistrationLookup) -> str | None:
        """Return email only from an approved, authenticated Identity source."""
        ...


class UnavailableIdentityProfileClient:
    async def email(self, identity: RegistrationLookup) -> str | None:
        return None


def get_identity_profile_client() -> IdentityProfileClient:
    return UnavailableIdentityProfileClient()
