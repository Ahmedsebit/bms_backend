from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from bms.operation_models import EvidenceDraft
from bms.schemas import Command, Model


class EmergencyWrite(Command):
    id: UUID
    expected_revision: int = Field(default=0, ge=0)

    @field_validator("*")
    @classmethod
    def aware_dates(cls, value):
        if isinstance(value, datetime) and value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value


class ApprovalWrite(EmergencyWrite):
    approved: bool = False
    approval_note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def approval_required(self):
        if self.approved and not self.approval_note.strip():
            raise ValueError("approval_note_required")
        return self


class EmergencySettingsWrite(ApprovalWrite):
    acknowledgement_minutes: int = Field(ge=1, le=60)
    progress_minutes: int = Field(default=5, ge=1, le=60)


class EmergencyContactWrite(ApprovalWrite):
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=80, pattern=r"^[+0-9 ()-]+$")
    purpose: str = Field(min_length=1, max_length=500)
    active: bool = True


class AssemblyPointWrite(EmergencyWrite):
    building_id: UUID
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(default="", max_length=1000)
    active: bool = True


class PersonWrite(EmergencyWrite):
    building_id: UUID
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["student", "staff", "visitor"]
    presence: Literal["on_campus", "temporarily_away"] = "on_campus"
    away_until: datetime | None = None
    assistance: str = Field(default="", max_length=500)
    active: bool = True

    @model_validator(mode="after")
    def absence_required(self):
        if self.presence == "temporarily_away" and self.away_until is None:
            raise ValueError("absence_return_time_required")
        return self


class GuardianWrite(ApprovalWrite):
    person_id: UUID
    name: str = Field(min_length=1, max_length=200)
    relationship: str = Field(min_length=1, max_length=100)
    active: bool = True


class DutyShiftWrite(EmergencyWrite):
    building_id: UUID
    primary_user_id: UUID
    backup_user_id: UUID
    lead_user_id: UUID
    starts_at: datetime
    ends_at: datetime
    approval_note: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def shift_window(self):
        if self.starts_at >= self.ends_at or self.primary_user_id == self.backup_user_id:
            raise ValueError("invalid_duty_shift")
        return self


class HandoverWrite(EmergencyWrite):
    outgoing_shift_id: UUID
    incoming_shift_id: UUID
    note: str = Field(min_length=1, max_length=2000)


class RevisionNote(EmergencyWrite):
    note: str = Field(min_length=1, max_length=2000)


class IncidentReport(EmergencyWrite):
    building_id: UUID
    title: str = Field(min_length=1, max_length=200)
    hazard: Literal["fire", "medical", "flood", "severe_weather", "security", "other"]
    location_note: str = Field(default="", max_length=1000)
    reported_at: datetime


class DrillPlan(IncidentReport):
    planned_at: datetime
    approval_note: str = Field(min_length=1, max_length=1000)
    nighttime: bool = False
    communications_failure: bool = False
    individual_assistance: bool = False
    safe_exercise_plan: str = Field(min_length=1, max_length=2000)


class IncidentPhase(RevisionNote):
    status: Literal["recovery", "closed"]


class IncidentResponders(RevisionNote):
    primary_user_id: UUID
    backup_user_id: UUID
    lead_user_id: UUID

    @model_validator(mode="after")
    def different_responders(self):
        if self.primary_user_id == self.backup_user_id:
            raise ValueError("different_backup_required")
        return self


class IncidentEvidence(EmergencyWrite):
    evidence: list[EvidenceDraft] = Field(min_length=1, max_length=10)


class RollCallConfirm(RevisionNote):
    status: Literal["unaccounted", "at_assembly", "needs_assistance", "confirmed_offsite"]
    point_id: UUID | None = None

    @model_validator(mode="after")
    def assembly_required(self):
        if self.status == "at_assembly" and self.point_id is None:
            raise ValueError("assembly_point_required")
        return self


class IncidentTask(EmergencyWrite):
    incident_id: UUID
    title: str = Field(min_length=1, max_length=500)
    owner_id: UUID
    due_at: datetime
    observation: str = Field(min_length=1, max_length=2000)
    safety_critical: bool = False


class DutyGrantWrite(EmergencyWrite):
    user_id: UUID
    building_id: UUID | None = None
    capability: Literal["incident.command", "roster.manage", "safety.approve", "safety.verify"]
    starts_at: datetime
    ends_at: datetime
    active: bool = True
    approval_note: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def grant_window(self):
        if self.starts_at >= self.ends_at:
            raise ValueError("invalid_duty_window")
        return self


class IncidentTransfer(RevisionNote):
    incoming_user_id: UUID


class ContactAttempt(EmergencyWrite):
    incident_id: UUID
    recipient_user_id: UUID
    channel: Literal["phone", "radio", "in_person"]
    outcome: Literal["attempted", "reached", "failed", "no_response"]
    occurred_at: datetime
    note: str = Field(min_length=1, max_length=2000)


class RosterReview(EmergencyWrite):
    building_id: UUID
    expected_count: int = Field(ge=0)
    note: str = Field(min_length=1, max_length=2000)


class SyncResolution(EmergencyWrite):
    owner_id: UUID
    status: Literal["reviewing", "resolved"]
    decision: Literal["retain", "dismiss", "replace"]
    note: str = Field(min_length=1, max_length=2000)
    replacement_operation_id: UUID | None = None

    @model_validator(mode="after")
    def resolution_valid(self):
        if self.status == "resolved" and self.decision == "retain":
            raise ValueError("resolution_decision_required")
        if (self.decision == "replace") != (self.replacement_operation_id is not None):
            raise ValueError("replacement_reference_required")
        return self


class RecoveryWrite(EmergencyWrite):
    incident_id: UUID
    accommodation: str = Field(min_length=1, max_length=2000)
    welfare: str = Field(min_length=1, max_length=2000)
    learning_continuity: str = Field(min_length=1, max_length=2000)


class ReopeningApproval(RevisionNote):
    authorised: Literal[True]


class GuardianRelease(EmergencyWrite):
    incident_id: UUID
    person_id: UUID
    guardian_id: UUID
    identity_checked: Literal[True]
    verification_note: str = Field(min_length=1, max_length=2000)


EmergencyCommandName = Literal[
    "duty.grant",
    "roster.review",
    "emergency.shift_accept",
    "emergency.settings",
    "emergency.contact",
    "emergency.point",
    "emergency.person",
    "emergency.guardian",
    "emergency.shift",
    "emergency.handover",
    "emergency.handover_accept",
    "incident.report",
    "incident.responders",
    "incident.acknowledge",
    "incident.accept",
    "incident.progress",
    "incident.transfer",
    "incident.transfer_accept",
    "incident.transfer_decline",
    "incident.transfer_cancel",
    "incident.contact",
    "incident.phase",
    "incident.evidence",
    "rollcall.confirm",
    "drill.plan",
    "drill.start",
    "incident.task",
    "incident.task_complete",
    "incident.task_verify",
    "incident.task_reject",
    "recovery.save",
    "recovery.approve",
    "guardian.release",
]


class EmergencySubmission(Model):
    command: EmergencyCommandName
    payload: dict[str, Any]


class EmergencySnapshot(Model):
    server_time: datetime
    notification_mode: Literal["in_app_prototype"] = "in_app_prototype"
    settings: dict[str, Any]
    buildings: list[dict[str, Any]]
    contacts: list[dict[str, Any]]
    points: list[dict[str, Any]]
    people: list[dict[str, Any]]
    guardians: list[dict[str, Any]]
    shifts: list[dict[str, Any]]
    handovers: list[dict[str, Any]]
    incidents: list[dict[str, Any]]
    alerts: list[dict[str, Any]]
    roll_call: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    recovery: list[dict[str, Any]]
    releases: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    staff: list[dict[str, Any]]
    grants: list[dict[str, Any]] = Field(default_factory=list)
    transfers: list[dict[str, Any]] = Field(default_factory=list)
    progress: list[dict[str, Any]] = Field(default_factory=list)
    contact_attempts: list[dict[str, Any]] = Field(default_factory=list)
    roster_reviews: list[dict[str, Any]] = Field(default_factory=list)
    coverage: list[dict[str, Any]] = Field(default_factory=list)
