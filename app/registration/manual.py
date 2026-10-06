"""Explicit local manual-test data, never an EF production wire contract."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from app.registration.client import (
    RegistrationLookup,
    RegistrationResult,
    RegistrationStatus,
    RegistrationUnavailable,
)


class ManualRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    issuer: str = Field(min_length=1, max_length=2000)
    subject: str = Field(min_length=1, max_length=2000)
    event_id: int = Field(gt=0)
    event_year: int = Field(ge=2000, le=9999)
    status: Literal["PAID", "CHECKED_IN", "INELIGIBLE", "UNKNOWN", "UNAVAILABLE"]
    reg_id: str = Field(min_length=1, max_length=200)
    nickname: str = Field(min_length=1, max_length=200)

    def binding(self) -> RegistrationLookup:
        return RegistrationLookup(
            self.issuer, self.subject, self.event_id, self.event_year
        )


_records = TypeAdapter(list[ManualRegistration])


def load_manual_records(path: Path):
    try:
        with path.open("rb") as source:
            data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise RegistrationUnavailable
        records = _records.validate_json(data)
    except OSError, ValidationError:
        raise RegistrationUnavailable from None
    by_identity = {record.binding(): record for record in records}
    if len(by_identity) != len(records):
        raise RegistrationUnavailable
    return by_identity


class ManualTestRegistrationClient:
    def __init__(self, path: Path | None, *, environment: str):
        if environment not in ("development", "test"):
            raise RuntimeError("manual_test Registration requires development or test")
        if path is None:
            raise RuntimeError(
                "manual_test Registration requires REGISTRATION_MANUAL_FILE"
            )
        self.path = path

    async def lookup(self, lookup: RegistrationLookup) -> RegistrationResult:
        # Reload on every check so an operator can exercise outages/status changes.
        return await run_in_threadpool(self._lookup, lookup)

    def _lookup(self, lookup: RegistrationLookup) -> RegistrationResult:
        by_identity = load_manual_records(self.path)
        record = by_identity.get(lookup)
        if record is None or record.status == "UNAVAILABLE":
            raise RegistrationUnavailable
        return RegistrationResult(
            lookup, RegistrationStatus[record.status], record.reg_id, record.nickname
        )
