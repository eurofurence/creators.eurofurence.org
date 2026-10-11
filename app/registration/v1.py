"""Read-only EF Registration v1 adapter; never infer deployment or identity mapping.

Contract: eurofurence/reg-attendee-service api/openapi-spec/openapi.yaml,
revision 86b87a43d81438373e6af495c30cd5bd4c3fcf15 (issue #11).
"""

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Annotated

import httpx2
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.registration.client import (
    RegistrationLookup,
    RegistrationResult,
    RegistrationStatus,
    RegistrationUnavailable,
)
from app.registration.configuration import RegistrationV1Event

REQUEST_TIMEOUT = 5
LOOKUP_TIMEOUT = 15
MAX_RESPONSE_BYTES = 1024 * 1024
_private_request = ContextVar("registration_private_request", default=False)


class _PrivateRequestFilter(logging.Filter):
    def filter(self, record):
        # HTTPX logs identity-bearing URLs; HTTP Core can log response headers.
        return not _private_request.get()


for _logger_name in (
    "httpx2",
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.http2",
    "httpcore2.proxy",
    "httpcore2.socks",
):
    logging.getLogger(_logger_name).addFilter(_PrivateRequestFilter())

_AttendeeId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class _Response(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)


class _IdentityResponse(_Response):
    ids: list[_AttendeeId] = Field(min_length=1, max_length=1)


class _AttendeeResponse(_Response):
    id: _AttendeeId
    nickname: str = Field(min_length=1, max_length=80)


class _StatusResponse(_Response):
    status: str


_STATUSES = {
    "paid": RegistrationStatus.PAID,
    "checked in": RegistrationStatus.CHECKED_IN,
    **dict.fromkeys(
        ("new", "approved", "partially paid", "cancelled", "deleted", "waiting"),
        RegistrationStatus.INELIGIBLE,
    ),
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


class RegistrationV1Client:
    def __init__(
        self,
        events: Sequence[RegistrationV1Event],
        *,
        environment: str,
        transport: httpx2.AsyncBaseTransport | None = None,
        user_token: SecretStr | None = None,
        token_lookup: RegistrationLookup | None = None,
    ):
        self.events = tuple(events)
        self.environment = environment
        self.transport = transport
        self.user_token = user_token
        self.token_lookup = token_lookup

    def _event(self, lookup: RegistrationLookup) -> RegistrationV1Event:
        matches = [event for event in self.events if event.event_id == lookup.event_id]
        if len(matches) != 1:
            raise RegistrationUnavailable
        event = matches[0]
        if (
            event.event_year != lookup.event_year
            or event.issuer != lookup.issuer
            or event.identity_mapping
            != (
                "confirmed_user_token"
                if event.auth_method == "user_bearer"
                else "confirmed_oidc_subject"
            )
            or (
                self.environment not in ("development", "test", "staging")
                and not event.production_verified
            )
            # The v1 identity endpoint explicitly requires an alphanumeric ID.
            or (
                event.auth_method != "user_bearer"
                and not re.fullmatch(r"[A-Za-z0-9]+", lookup.subject)
            )
        ):
            raise RegistrationUnavailable
        return event

    async def lookup(self, lookup: RegistrationLookup) -> RegistrationResult:
        event = self._event(lookup)
        if event.auth_method == "user_bearer":
            if self.token_lookup != lookup or self.user_token is None:
                raise RegistrationUnavailable
            secret = self.user_token.get_secret_value()
        else:
            secret = event.credential.get_secret_value()
        if not secret or any(not 33 <= ord(c) <= 126 for c in secret):
            raise RegistrationUnavailable
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if event.auth_method == "api_key":
            headers["X-Api-Key"] = secret
        else:
            headers["Authorization"] = f"Bearer {secret}"

        private = _private_request.set(True)
        try:
            async with asyncio.timeout(LOOKUP_TIMEOUT):
                async with httpx2.AsyncClient(
                    base_url=event.base_url.rstrip("/") + "/",
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    # Self lookup binds ownership through the freshly issued token,
                    # without equating OIDC sub with a Registration identity string.
                    path = (
                        "attendees"
                        if event.auth_method == "user_bearer"
                        else f"attendees/identity/{lookup.subject}"
                    )
                    identity = await self._get(client, path, _IdentityResponse)
                    attendee_id = identity.ids[0]
                    attendee = await self._get(
                        client, f"attendees/{attendee_id}", _AttendeeResponse
                    )
                    if attendee.id != attendee_id or not attendee.nickname.strip():
                        raise RegistrationUnavailable
                    status = await self._get(
                        client, f"attendees/{attendee_id}/status", _StatusResponse
                    )
                    # Detect an ownership transfer while reading the other endpoints.
                    current = await self._get(client, path, _IdentityResponse)
                    if current.ids != identity.ids:
                        raise RegistrationUnavailable
                    return RegistrationResult(
                        lookup,
                        _STATUSES.get(status.status, RegistrationStatus.UNKNOWN),
                        reg_id=str(attendee_id),
                        nickname=attendee.nickname,
                    )
        except (
            httpx2.HTTPError,
            httpx2.InvalidURL,
            TimeoutError,
            ValueError,
            RecursionError,
        ):
            # Exceptions may include credentials, URLs or complete attendee data.
            raise RegistrationUnavailable from None
        finally:
            _private_request.reset(private)

    @staticmethod
    async def _get(client, path, schema):
        async with client.stream("GET", path) as response:
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if (
                response.status_code != 200
                or content_type.strip().lower() != "application/json"
                # Bound memory before JSON parsing, including compressed responses.
                or response.headers.get("content-encoding", "identity").lower()
                != "identity"
            ):
                raise RegistrationUnavailable
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise RegistrationUnavailable
                body.extend(chunk)
            return schema.model_validate(
                json.loads(body, object_pairs_hook=_unique_object)
            )
