from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BMS_", env_file=".env", extra="ignore")

    database_url: SecretStr
    secret_key: SecretStr = Field(min_length=32)
    interactive_hours: int = Field(default=8, ge=1)
    sync_hours: int = Field(default=72, ge=1)
    offline_grant_hours: int = Field(default=72, ge=1)
    password_min_length: int = Field(default=15, ge=15, le=128)
    failed_password_limit: int = Field(default=5, ge=1)
    lockout_minutes: int = Field(default=15, ge=1)
    scheduler_interval_minutes: int = Field(default=15, ge=1)
    scheduling_horizon_days: int = Field(default=7, ge=1, le=31)
    clock_warning_seconds: int = Field(default=300, ge=0)
    evidence_max_bytes: int = Field(default=500_000, ge=1)
    evidence_max_dimension: int = Field(default=1600, ge=1)
    evidence_ticket_seconds: int = Field(default=300, ge=30, le=3600)
    evidence_directory: Path = Path(".local") / "evidence"
    sensor_service_url: str | None = None
    sensor_service_token: SecretStr | None = None
    sensor_poll_seconds: int = Field(default=15, ge=1, le=300)

    @field_validator("sensor_service_url")
    @classmethod
    def trusted_sensor_url(cls, value):
        if value is None:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            )
        ):
            raise ValueError(
                "Sensor service requires HTTPS or loopback HTTP, without URL credentials"
            )
        return value.rstrip("/")

    @field_validator("secret_key")
    @classmethod
    def reject_example_secret(cls, value):
        if value.get_secret_value().startswith("REPLACE"):
            raise ValueError("A generated secret key is required")
        return value


@lru_cache
def settings() -> Settings:
    return Settings()
