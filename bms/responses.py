from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from bms.schemas import Model, Role


class Envelope[T](Model):
    data: T
    correlation_id: UUID


class UserView(Model):
    id: UUID
    name: str
    user_id: str
    role: Role
    status: str
    must_change_password: bool
    revision: int


class UserList(Model):
    items: list[UserView]


class CredentialView(Model):
    token: str
    expires_at: datetime


class SignInView(Model):
    user: UserView
    interactive_credential: CredentialView
    sync_credential: CredentialView
    offline_grant_expires_at: datetime
    server_time: datetime | None = None


class MeView(Model):
    user: UserView
    capabilities: list[str]
    server_time: datetime


class SignOutView(Model):
    signed_out: bool


class HealthView(Model):
    status: str


class AuditView(Model):
    id: UUID
    tenant_id: UUID
    created_at: datetime
    updated_at: datetime
    revision: int
    actor_id: UUID | None
    actor_role: Role | Literal["system"]
    action: str
    entity_id: UUID
    before_data: dict[str, Any] | None
    after_data: dict[str, Any] | None
    reason: str | None
    device_occurred_at: datetime | None
    received_at: datetime
    device_id: UUID | None
    offline: bool
    operation_id: UUID


class AuditList(Model):
    items: list[AuditView]


class ErrorView(Model):
    code: str
    message_key: str
    current: dict[str, Any] | None = None
    details: list[dict[str, Any]] | None = None


class ErrorEnvelope(Model):
    error: ErrorView
    correlation_id: UUID
