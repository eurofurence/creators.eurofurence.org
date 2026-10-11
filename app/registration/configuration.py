"""Explicit operator configuration for the Registration v1 contract."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class RegistrationV1Event(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True
    )

    event_id: int = Field(gt=0)
    event_year: int = Field(ge=2000, le=9999)
    base_url: str
    issuer: str
    # Selecting this mode attests that EF confirmed the issuer's subject mapping.
    identity_mapping: Literal[
        "unconfirmed", "confirmed_oidc_subject", "confirmed_user_token"
    ] = "unconfirmed"
    auth_method: Literal["api_key", "bearer", "user_bearer"]
    credential: SecretStr | None = Field(default=None, repr=False)
    # Set only after deployment, mapping and authentication acceptance tests.
    production_verified: bool = False

    @model_validator(mode="after")
    def match_authentication(self):
        if self.auth_method == "user_bearer":
            if (
                self.credential is not None
                or self.identity_mapping == "confirmed_oidc_subject"
            ):
                raise ValueError(
                    "User-token mode requires transient tokens and confirmed user-token ownership"
                )
        elif self.credential is None or self.identity_mapping == "confirmed_user_token":
            raise ValueError(
                "Service mode requires a credential and confirmed subject mapping"
            )
        return self

    @field_validator("base_url", "issuer")
    @classmethod
    def require_https_url(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            valid = (
                url.scheme == "https"
                and url.hostname
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                and not any(c.isspace() or ord(c) < 32 for c in value)
                and "\\" not in value
            )
            _ = url.port  # Reject malformed ports before making a request.
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "Registration URLs require HTTPS without credentials/query/fragment"
            )
        return value

    @field_validator("credential")
    @classmethod
    def require_credential(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        if (
            not secret
            or secret.upper().startswith("INSERT_")
            or any(not 33 <= ord(c) <= 126 for c in secret)
        ):
            raise ValueError(
                "Registration requires a nonempty, valid service credential"
            )
        return value
