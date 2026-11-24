from datetime import date, datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from bms.schemas import Command, Model
from bms.sensor_contract import Identifier

Severity = Literal["critical", "high", "medium", "low"]


class RevisionCommand(Command):
    expected_revision: int = Field(ge=1)


class FacilityCreate(Command):
    id: UUID
    parent_id: UUID | None = None
    level: Literal["building", "zone", "location"]
    name: str = Field(min_length=1, max_length=200)
    location_type: (
        Literal["door", "emergency_exit", "extinguisher_point", "room", "corridor"] | None
    ) = None


class FacilityUpdate(RevisionCommand):
    name: str = Field(min_length=1, max_length=200)
    status: Literal["active", "inactive"]
    reason: str = Field(min_length=1, max_length=1000)


class ResponsibilitySet(Command):
    primary_user_id: UUID
    backup_user_id: UUID | None = None
    expected_revision: int = Field(ge=0)


class SupervisorScope(Command):
    expected_revision: int = Field(ge=1)
    building_ids: list[UUID] = Field(max_length=1000)


class ChecklistItem(Model):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    type: Literal["yes_no", "pass_concern_fail", "short_text", "photo"]
    text: str = Field(min_length=1, max_length=1000)
    required: bool = True
    photo: Literal["never", "always", "on_fail"] = "never"
    creates_finding_on: list[Literal["yes", "no", "pass", "concern", "fail"]] = Field(
        default_factory=list, max_length=5
    )
    finding_severity: Severity = "medium"

    @model_validator(mode="after")
    def valid_triggers(self):
        allowed = {"yes_no": {"yes", "no"}, "pass_concern_fail": {"pass", "concern", "fail"}}
        if not set(self.creates_finding_on).issubset(allowed.get(self.type, set())):
            raise ValueError("invalid_finding_trigger")
        return self


class ProgrammeCreate(Command):
    id: UUID
    name: str = Field(min_length=1, max_length=200)
    recurrence: Literal["daily", "weekly"]
    assignment_mode: Literal["zone", "supervisor_random"] = "zone"
    start_time: time
    end_time: time
    grace_minutes: int = Field(ge=0, le=10080)
    life_safety: bool = False
    approved: bool = False
    approval_note: str = Field(default="", max_length=1000)
    enabled: bool = True
    location_ids: list[UUID] = Field(min_length=1, max_length=2000)
    items: list[ChecklistItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_items(self):
        if self.assignment_mode == "supervisor_random" and self.recurrence != "daily":
            raise ValueError("spot_checks_must_be_daily")
        if self.start_time == self.end_time or (
            self.recurrence == "weekly" and self.start_time > self.end_time
        ):
            raise ValueError("invalid_window")
        if self.life_safety and self.assignment_mode == "supervisor_random":
            raise ValueError("life_safety_requires_full_coverage")
        if self.approved and not self.approval_note:
            raise ValueError("approval_note_required")
        if len({item.key for item in self.items}) != len(self.items):
            raise ValueError("duplicate_item_key")
        if len(set(self.location_ids)) != len(self.location_ids):
            raise ValueError("duplicate_location")
        return self


class DailyOperationsSettings(RevisionCommand):
    supervisor_daily_percentage: int = Field(ge=1, le=100)


class SensorCreate(Command):
    id: UUID
    location_id: UUID
    name: str = Field(min_length=1, max_length=200)
    status: Literal["unknown", "working", "faulty"] = "unknown"


class SensorServiceCreate(Command):
    id: UUID
    location_id: UUID
    name: str = Field(min_length=1, max_length=200)
    gateway_id: Identifier
    external_device_id: Identifier
    stale_after_seconds: int = Field(default=300, ge=15, le=604800)


class SensorSimulatorStatus(RevisionCommand):
    status: Literal["unknown", "working", "faulty"]
    reason: str = Field(min_length=1, max_length=1000)


class ProgrammeUpdate(ProgrammeCreate):
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class EvidenceDraft(Model):
    id: UUID
    captured_at: datetime
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=1)
    item_key: str | None = Field(default=None, max_length=80)

    @field_validator("captured_at")
    @classmethod
    def aware_capture(cls, value):
        if value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value


class Answer(Model):
    item_key: str = Field(min_length=1, max_length=80)
    value: str = Field(max_length=1000)
    description: str = Field(default="", max_length=2000)
    evidence_id: UUID | None = None
    finding_id: UUID | None = None


class InspectionStart(Command):
    id: UUID
    occurrence_id: UUID
    location_method: Literal["qr", "manual"]
    qr_token: str | None = Field(default=None, max_length=200)
    scanned_at: datetime | None = None
    started_at: datetime

    @field_validator("started_at", "scanned_at")
    @classmethod
    def aware_times(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value


class InspectionComplete(InspectionStart):
    completed_at: datetime
    expected_revision: int = Field(default=0, ge=0)
    clock_offset_seconds: int = Field(default=0, ge=-2147483648, le=2147483647)
    answers: list[Answer] = Field(default_factory=list, max_length=100)
    evidence: list[EvidenceDraft] = Field(default_factory=list, max_length=100)

    @field_validator("completed_at")
    @classmethod
    def aware_completion(cls, value):
        if value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value


class InspectionAbandon(RevisionCommand):
    reason: str = Field(min_length=1, max_length=1000)


class FindingCreate(Command):
    id: UUID
    location_id: UUID
    description: str = Field(min_length=1, max_length=4000)
    severity: Severity
    reported_at: datetime
    evidence: list[EvidenceDraft] = Field(min_length=1, max_length=10)

    @field_validator("reported_at")
    @classmethod
    def aware_report(cls, value):
        if value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value


class FindingAssign(RevisionCommand):
    action_id: UUID
    owner_id: UUID
    title: str = Field(min_length=1, max_length=2000)
    due_date: date


class FindingSeverity(RevisionCommand):
    severity: Severity
    reason: str = Field(min_length=1, max_length=1000)


class ActionComplete(RevisionCommand):
    note: str = Field(min_length=1, max_length=2000)
    completed_at: datetime
    evidence: list[EvidenceDraft] = Field(default_factory=list, max_length=10)

    @field_validator("completed_at")
    @classmethod
    def aware_completion(cls, value):
        if value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value


class ActionReview(RevisionCommand):
    note: str = Field(min_length=1, max_length=2000)


class SyncOperation(Model):
    client_operation_id: UUID
    command: Literal[
        "inspection.start",
        "inspection.complete",
        "inspection.abandon",
        "finding.create",
        "finding.assign",
        "finding.set_severity",
        "action.complete",
        "action.verify",
        "action.reject",
        "incident.report",
        "incident.evidence",
    ]
    target_id: UUID | None = None
    payload: dict[str, Any]


class SyncBatch(Model):
    operations: list[SyncOperation] = Field(min_length=1, max_length=100)
