from enum import StrEnum

from app.registration.client import (
    RegistrationClient,
    RegistrationLookup,
    RegistrationResult,
    RegistrationStatus,
    RegistrationUnavailable,
)


class Eligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNAVAILABLE = "UNAVAILABLE"


async def check_eligibility(
    client: RegistrationClient, lookup: RegistrationLookup
) -> Eligibility:
    """Check fresh evidence without changing approvals, badges or other state."""
    try:
        result = await client.lookup(lookup)
    except RegistrationUnavailable, TimeoutError:
        return Eligibility.UNAVAILABLE

    if not isinstance(result, RegistrationResult) or result.lookup != lookup:
        return Eligibility.UNAVAILABLE
    if result.status in (RegistrationStatus.PAID, RegistrationStatus.CHECKED_IN):
        return Eligibility.ELIGIBLE
    if result.status is RegistrationStatus.INELIGIBLE:
        return Eligibility.INELIGIBLE
    return Eligibility.UNAVAILABLE


class EligibilityRequired(Exception):
    def __init__(self, eligibility: Eligibility):
        super().__init__(eligibility.value)
        self.eligibility = eligibility


async def require_eligible_registration(
    client: RegistrationClient, lookup: RegistrationLookup
) -> None:
    """Call before granting submission/approval/confirmation, never trust a preflight."""
    eligibility = await check_eligibility(client, lookup)
    if eligibility is not Eligibility.ELIGIBLE:
        raise EligibilityRequired(eligibility)
