from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic.alias_generators import to_camel

from bms.security import normalize_password, validate_new_password


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def trim_non_secret_fields(cls, value, info):
        if isinstance(value, str) and info.field_name not in {"password", "current_password"}:
            return value.strip()
        return value


class Role(StrEnum):
    OFFICER = "officer"
    SUPERVISOR = "supervisor"
    ADMINISTRATOR = "administrator"
    LEADERSHIP = "leadership"


class Command(Model):
    client_operation_id: UUID
    device_id: UUID | None = None
    device_occurred_at: datetime | None = None
    offline: bool = False

    @field_validator("device_occurred_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value


class NewPasswordMixin(Model):
    password: SecretStr = Field(max_length=128)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value):
        return SecretStr(validate_new_password(value.get_secret_value()))


class SignIn(Command):
    school_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    user_id: str = Field(min_length=1, max_length=100, pattern=r"^\S+$")
    password: SecretStr = Field(min_length=1, max_length=128)
    client_type: Literal["web", "android"] = "web"


class CreateUser(Command, NewPasswordMixin):
    id: UUID
    name: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=100, pattern=r"^\S+$")
    role: Role


class ChangePassword(Command, NewPasswordMixin):
    current_password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("current_password")
    @classmethod
    def normalized_current_password(cls, value):
        return SecretStr(normalize_password(value.get_secret_value()))


class UpdateStatus(Command):
    expected_revision: int = Field(ge=1)
    status: str = Field(pattern=r"^(active|inactive)$")
    reason: str = Field(min_length=1, max_length=1000)


class ResetPassword(Command, NewPasswordMixin):
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
