from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Eurofurence Creator System"
    environment: str = "development"
    registration_provider: Literal["unavailable", "manual_test"] = "unavailable"
    registration_manual_file: Path | None = Field(default=None, validate_default=True)
    active_event_id: int | None = Field(default=None, gt=0)
    badge_sequence_start: int = Field(default=1, gt=0)
    profile_image_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    invitation_attempts_per_minute: int = Field(default=10, gt=0)
    notification_provider_factory: str | None = None

    session_secret: SecretStr | None = None
    session_max_age: int = Field(default=14 * 24 * 60 * 60, gt=0)

    database_url: SecretStr | None = None

    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_region: str = "us-east-1"

    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_server_metadata_url: str | None = None
    oidc_issuer_url: str | None = None
    oidc_redirect_uri: str | None = None

    @field_validator("registration_provider")
    @classmethod
    def restrict_manual_registration(cls, value: str, info: ValidationInfo) -> str:
        if value == "manual_test" and info.data.get("environment") not in (
            "development",
            "test",
        ):
            raise ValueError("manual_test Registration requires development or test")
        return value

    @field_validator("registration_manual_file")
    @classmethod
    def require_manual_file(
        cls, value: Path | None, info: ValidationInfo
    ) -> Path | None:
        if info.data.get("registration_provider") == "manual_test" and value is None:
            raise ValueError(
                "manual_test Registration requires REGISTRATION_MANUAL_FILE"
            )
        return value

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = Settings()
