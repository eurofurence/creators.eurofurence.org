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

    return classify_result(result, lookup)


def classify_result(
    result: RegistrationResult, lookup: RegistrationLookup
) -> Eligibility:
    if (
        not isinstance(result, RegistrationResult)
        or result.lookup != lookup
        or not isinstance(result.reg_id, str)
        or not result.reg_id.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in result.reg_id)
    ):
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
    """Validate a fresh initial verification; later workflows use bound evidence."""
    eligibility = await check_eligibility(client, lookup)
    if eligibility is not Eligibility.ELIGIBLE:
        raise EligibilityRequired(eligibility)
