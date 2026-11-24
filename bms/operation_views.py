from datetime import date, datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from bms.emergency_models import EmergencySnapshot
from bms.operation_models import Answer, ChecklistItem, Severity
from bms.responses import UserView
from bms.schemas import Model, Role


class RecordView(Model):
    id: UUID
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class FacilityView(RecordView):
    parent_id: UUID | None
    building_id: UUID
    level: Literal["building", "zone", "location"]
    name: str
    location_type: str | None
    status: Literal["active", "inactive"]


class AssignmentView(RecordView):
    zone_id: UUID
    primary_user_id: UUID
    backup_user_id: UUID | None
    valid_from: datetime
    valid_to: datetime | None


class ProgrammeView(RecordView):
    name: str
    recurrence: Literal["daily", "weekly"]
    start_time: time
    end_time: time
    grace_minutes: int
    life_safety: bool
    enabled: bool
    current_version: int
    assignment_mode: Literal["zone", "supervisor_random"] = "zone"
    sensor_id: UUID | None = None


class ProgrammeVersionView(RecordView):
    programme_id: UUID
    version_number: int
    items: list[ChecklistItem]
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    approval_note: str = ""


class ProgrammeScopeView(RecordView):
    programme_id: UUID
    location_id: UUID
    active: bool


class UserScopeView(RecordView):
    user_id: UUID
    building_id: UUID
    active: bool


class OccurrenceView(RecordView):
    programme_id: UUID
    programme_version_id: UUID
    location_id: UUID
    primary_user_id: UUID
    backup_user_id: UUID | None
    period_key: str
    due_start: datetime
    due_end: datetime
    grace_end: datetime
    timing_state: str
    missed_at: datetime | None
    canonical_inspection_id: UUID | None
    source_kind: Literal["routine", "sensor_fault", "supervisor_spot"]
    cancelled_at: datetime | None
    cancellation_reason: str | None


class DailySettingsView(RecordView):
    supervisor_daily_percentage: int


class SensorView(RecordView):
    location_id: UUID
    name: str
    source: Literal["simulator", "service"]
    status: Literal["unknown", "working", "faulty"]
    fault_programme_id: UUID | None
    status_changed_at: datetime
    gateway_id: str | None = None
    external_device_id: str | None = None
    stale_after_seconds: int = 300
    telemetry_mode: Literal["test", "hardware"] | None = None
    last_observed_at: datetime | None = None
    last_received_at: datetime | None = None
    last_sequence: int | None = None
    device_health: str = "unknown"
    availability_reason: str = "unknown"
    measurements: dict[str, Any] = Field(default_factory=dict)
    measurement_times: dict[str, str] = Field(default_factory=dict)
    active_alarms: list[str] = Field(default_factory=list)


class SensorAlertView(Model):
    id: UUID
    occurrence_id: UUID | None
    sensor_id: UUID
    sensor_name: str
    sensor_status: str
    source: Literal["simulator", "service"]
    location_name: str
    building_name: str
    officer_name: str | None
    due_end: datetime | None
    timing_state: str | None
    telemetry_mode: Literal["test", "hardware"] | None = None
    last_observed_at: datetime | None = None
    last_received_at: datetime | None = None
    availability_reason: str = "unknown"
    active_alarms: list[str] = Field(default_factory=list)
    measurements: dict[str, Any] = Field(default_factory=dict)
    measurement_times: dict[str, str] = Field(default_factory=dict)


class SpotAssignmentView(RecordView):
    sample_id: UUID
    supervisor_user_id: UUID
    building_id: UUID
    building_name: str
    local_date: date
    percentage: int


class InspectionView(RecordView):
    occurrence_id: UUID
    performer_id: UUID
    lifecycle: Literal["in_progress", "completed", "abandoned"]
    location_method: Literal["manual", "qr"]
    scanned_at: datetime | None
    started_at: datetime
    completed_at: datetime | None
    answers: list[Answer]
    outcome: str | None
    device_id: UUID | None
    clock_offset_seconds: int
    clock_flag: bool
    duplicate: bool
    catch_up: bool
    finding_ids: list[UUID] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)


class FindingView(RecordView):
    location_id: UUID
    source: Literal["manual", "inspection"]
    inspection_id: UUID | None
    item_key: str | None
    description: str
    severity: Severity
    status: Literal["open", "action_required", "awaiting_verification", "verified_closed"]
    reporter_id: UUID
    owner_id: UUID | None
    reported_at: datetime
    verified_closed_at: datetime | None
    evidence_ids: list[UUID] = Field(default_factory=list)


class ActionView(RecordView):
    finding_id: UUID
    title: str
    owner_id: UUID
    due_date: date
    status: Literal["assigned", "completed", "verified"]
    completion_note: str | None
    completed_at: datetime | None
    completed_by: UUID | None
    verification_note: str | None
    verified_at: datetime | None
    verified_by: UUID | None
    rejection_note: str | None
    evidence_ids: list[UUID] = Field(default_factory=list)


class EvidenceView(RecordView):
    creator_id: UUID
    owner_type: Literal["inspection", "finding", "action", "review"]
    owner_id: UUID
    item_key: str | None
    captured_at: datetime
    sha256: str
    byte_size: int
    upload_state: Literal["pending", "uploaded"]
    received_at: datetime | None


class RosterUserView(Model):
    id: UUID
    name: str
    role: Role
    status: str
    revision: int
    user_id: str | None = None
    must_change_password: bool | None = None


class SchoolView(Model):
    name: str
    school_code: str
    timezone: str


class QrView(Model):
    location_id: UUID
    token: str


class MobilePolicyView(Model):
    photo_max_bytes: int
    photo_max_dimension: int
    clock_warning_seconds: int


class MobileSnapshotView(Model):
    sync_resolutions: list[dict[str, Any]] = Field(default_factory=list)
    user: UserView
    school: SchoolView
    policy: MobilePolicyView
    server_time: datetime
    cursor: UUID
    full_snapshot: bool
    daily_settings: DailySettingsView
    sensors: list[SensorView]
    sensor_alerts: list[SensorAlertView]
    spot_check_assignments: list[SpotAssignmentView]
    facilities: list[FacilityView]
    assignments: list[AssignmentView]
    programmes: list[ProgrammeView]
    programme_scopes: list[ProgrammeScopeView]
    user_scopes: list[UserScopeView]
    programme_versions: list[ProgrammeVersionView]
    occurrences: list[OccurrenceView]
    inspections: list[InspectionView]
    findings: list[FindingView]
    actions: list[ActionView]
    evidence: list[EvidenceView]
    qr_tokens: list[QrView]
    users: list[RosterUserView]
    emergency: EmergencySnapshot


class SyncOutcomeView(Model):
    operation_id: UUID
    state: Literal[
        "accepted",
        "duplicate_conflict",
        "revision_conflict",
        "sent_for_review",
        "rejected",
    ]
    http_status: int
    result: dict[str, Any] | None = None
    error_code: str | None = None
    current: dict[str, Any] | None = None
    evidence_ids: list[UUID] = Field(default_factory=list)


class SyncPushView(Model):
    outcomes: list[SyncOutcomeView]
    server_time: datetime
