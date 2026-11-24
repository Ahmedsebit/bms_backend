import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, Request
from fastapi.responses import FileResponse

from bms.config import settings
from bms.daily_operations import save_sensor, save_settings
from bms.evidence_store import check_ticket, file_path, save_photo, ticket
from bms.mobile_snapshot import snapshot
from bms.operation_access import evidence_access, facility_access, public, require, row_by_id
from bms.operation_models import (
    ActionComplete,
    ActionReview,
    DailyOperationsSettings,
    FacilityCreate,
    FacilityUpdate,
    FindingAssign,
    FindingCreate,
    FindingSeverity,
    InspectionAbandon,
    InspectionComplete,
    InspectionStart,
    ProgrammeCreate,
    ProgrammeUpdate,
    ResponsibilitySet,
    SensorCreate,
    SensorServiceCreate,
    SensorSimulatorStatus,
    SupervisorScope,
    SyncBatch,
)
from bms.operation_views import (
    ActionView,
    AssignmentView,
    DailySettingsView,
    FacilityView,
    FindingView,
    InspectionView,
    MobileSnapshotView,
    ProgrammeView,
    SensorView,
    SyncPushView,
)
from bms.responses import Envelope
from bms.schemas import Command
from bms.sensor_operations import create_service_sensor, service_state
from bms.services import ApiError, audit, remember, replay
from bms.setup_operations import (
    create_facility,
    save_programme,
    set_responsibility,
    set_supervisor_scope,
    update_facility,
)
from bms.workflow_operations import execute, submit_operation


def register_routes(app, authenticated, success):
    from bms.emergency_routes import register_emergency_routes

    register_emergency_routes(app, authenticated, success)
    context_dependency = Depends(authenticated, scope="function")
    response = Envelope[dict[str, Any]]

    @app.post("/api/v1/admin/daily-settings", response_model=Envelope[DailySettingsView])
    def daily_settings(request: Request, body: DailyOperationsSettings, context=context_dependency):
        conn, actor, _ = context
        return success(request, save_settings(conn, actor, body))

    @app.post("/api/v1/admin/sensors", response_model=Envelope[SensorView])
    def sensor_create(request: Request, body: SensorCreate, context=context_dependency):
        conn, actor, _ = context
        return success(request, save_sensor(conn, actor, body))

    @app.post("/api/v1/admin/sensors/service", response_model=Envelope[SensorView])
    def sensor_service_create(
        request: Request, body: SensorServiceCreate, context=context_dependency
    ):
        conn, actor, _ = context
        return success(request, create_service_sensor(conn, actor, body))

    @app.get("/api/v1/admin/sensors/service-status", response_model=response)
    def sensor_service_status(request: Request, context=context_dependency):
        conn, actor, _ = context
        require(actor, "setup.manage")
        return success(request, public(service_state(conn, actor["tenant_id"])))

    @app.post("/api/v1/admin/sensors/{sensor_id}/simulate", response_model=Envelope[SensorView])
    def sensor_simulate(
        request: Request, sensor_id: UUID, body: SensorSimulatorStatus, context=context_dependency
    ):
        conn, actor, _ = context
        return success(request, save_sensor(conn, actor, body, sensor_id))

    @app.get("/api/v1/mobile/snapshot", response_model=Envelope[MobileSnapshotView])
    @app.get("/api/v1/sync/pull", response_model=Envelope[MobileSnapshotView])
    def pull(request: Request, context=context_dependency, cursor: str | None = None):
        conn, actor, _ = context
        # MVP cursors identify atomic full replacements; no client infers deletions.
        return success(request, snapshot(conn, actor))

    @app.post("/api/v1/sync/operations", response_model=Envelope[SyncPushView])
    def push(request: Request, body: SyncBatch, context=context_dependency):
        conn, actor, session = context
        if actor["role"] == "leadership":
            raise ApiError(403, "MOBILE_ACCESS_FORBIDDEN")
        outcomes = [
            submit_operation(conn, actor, operation, review_only=session.get("_review_only", False))
            for operation in body.operations
        ]
        return success(
            request,
            {
                "outcomes": outcomes,
                "serverTime": datetime.now(UTC).isoformat(),
            },
        )

    @app.post("/api/v1/facilities", response_model=Envelope[FacilityView])
    def facility_create(request: Request, body: FacilityCreate, context=context_dependency):
        conn, actor, _ = context
        return success(request, create_facility(conn, actor, body))

    @app.post("/api/v1/facilities/{facility_id}/update", response_model=Envelope[FacilityView])
    def facility_update(
        request: Request,
        facility_id: UUID,
        body: FacilityUpdate,
        context=context_dependency,
    ):
        conn, actor, _ = context
        return success(request, update_facility(conn, actor, facility_id, body))

    @app.post("/api/v1/zones/{zone_id}/responsibility", response_model=Envelope[AssignmentView])
    def responsibility(
        request: Request,
        zone_id: UUID,
        body: ResponsibilitySet,
        context=context_dependency,
    ):
        conn, actor, _ = context
        return success(request, set_responsibility(conn, actor, zone_id, body))

    @app.post("/api/v1/admin/users/{user_id}/scope", response_model=response)
    def supervisor_scope(
        request: Request,
        user_id: UUID,
        body: SupervisorScope,
        context=context_dependency,
    ):
        conn, actor, _ = context
        return success(request, set_supervisor_scope(conn, actor, user_id, body))

    @app.post("/api/v1/programmes", response_model=Envelope[ProgrammeView])
    def programme_create(request: Request, body: ProgrammeCreate, context=context_dependency):
        conn, actor, _ = context
        return success(request, save_programme(conn, actor, body))

    @app.post("/api/v1/programmes/{programme_id}/update", response_model=Envelope[ProgrammeView])
    def programme_update(
        request: Request,
        programme_id: UUID,
        body: ProgrammeUpdate,
        context=context_dependency,
    ):
        conn, actor, _ = context
        if programme_id != body.id:
            row_by_id(conn, actor, "programmes", programme_id)
            raise ApiError(400, "VALIDATION_FAILED")
        return success(request, save_programme(conn, actor, body, update=True))

    @app.post("/api/v1/locations/{location_id}/regenerate-qr", response_model=response)
    def regenerate_qr(
        request: Request,
        location_id: UUID,
        body: Command,
        context=context_dependency,
    ):
        import secrets

        conn, actor, _ = context
        location = row_by_id(conn, actor, "facilities", location_id)
        require(actor, "setup.manage")
        if location["level"] != "location":
            raise ApiError(400, "LOCATION_REQUIRED")
        command = f"qr.regenerate:{location_id}"
        old = replay(conn, actor, command, body)
        if old is not None:
            return success(request, old)
        conn.execute(
            "UPDATE bms.qr_tokens SET active=false,revision=revision+1,updated_at=now() "
            "WHERE tenant_id=%s AND location_id=%s AND active",
            (actor["tenant_id"], location_id),
        )
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO bms.qr_tokens(id,tenant_id,location_id,token) VALUES (%s,%s,%s,%s)",
            (uuid4(), actor["tenant_id"], location_id, token),
        )
        result = {"locationId": str(location_id), "token": token}
        audit(conn, actor, "qr.regenerate", location_id, body)
        return success(request, remember(conn, actor, command, body, result))

    @app.get("/api/v1/locations/resolve-qr", response_model=response)
    def resolve_qr(request: Request, token: str, context=context_dependency):
        conn, actor, _ = context
        if len(token) > 200:
            raise ApiError(404, "QR_UNKNOWN")
        row = conn.execute(
            "SELECT location_id FROM bms.qr_tokens WHERE tenant_id=%s AND token=%s AND active",
            (actor["tenant_id"], token),
        ).fetchone()
        if not row:
            raise ApiError(404, "QR_UNKNOWN")
        location = facility_access(conn, actor, row["location_id"], active=True)
        return success(request, public(location))

    @app.post("/api/v1/inspections/start", response_model=Envelope[InspectionView])
    def inspection_start(request: Request, body: InspectionStart, context=context_dependency):
        conn, actor, _ = context
        return success(request, execute(conn, actor, "inspection.start", None, body))

    @app.post("/api/v1/inspections/complete", response_model=Envelope[InspectionView])
    def inspection_complete(
        request: Request,
        body: InspectionComplete,
        context=context_dependency,
    ):
        conn, actor, _ = context
        result = execute(conn, actor, "inspection.complete", None, body)
        if result.get("duplicate"):
            conn.commit()
            raise ApiError(409, "DUPLICATE_INSPECTION", result)
        return success(request, result)

    @app.post(
        "/api/v1/inspections/{inspection_id}/abandon",
        response_model=Envelope[InspectionView],
    )
    def inspection_abandon(
        request: Request,
        inspection_id: UUID,
        body: InspectionAbandon,
        context=context_dependency,
    ):
        conn, actor, _ = context
        return success(request, execute(conn, actor, "inspection.abandon", inspection_id, body))

    @app.post("/api/v1/findings", response_model=Envelope[FindingView])
    def finding_create(request: Request, body: FindingCreate, context=context_dependency):
        conn, actor, _ = context
        return success(request, execute(conn, actor, "finding.create", None, body))

    @app.post("/api/v1/findings/{finding_id}/assign", response_model=Envelope[ActionView])
    def finding_assign(
        request: Request,
        finding_id: UUID,
        body: FindingAssign,
        context=context_dependency,
    ):
        conn, actor, _ = context
        return success(request, execute(conn, actor, "finding.assign", finding_id, body))

    @app.post("/api/v1/findings/{finding_id}/set-severity", response_model=Envelope[FindingView])
    def finding_severity(
        request: Request,
        finding_id: UUID,
        body: FindingSeverity,
        context=context_dependency,
    ):
        conn, actor, _ = context
        return success(request, execute(conn, actor, "finding.set_severity", finding_id, body))

    @app.post("/api/v1/actions/{action_id}/complete", response_model=Envelope[ActionView])
    def action_complete(
        request: Request,
        action_id: UUID,
        body: ActionComplete,
        context=context_dependency,
    ):
        conn, actor, _ = context
        return success(request, execute(conn, actor, "action.complete", action_id, body))

    @app.post("/api/v1/actions/{action_id}/verify", response_model=Envelope[ActionView])
    def action_verify(
        request: Request,
        action_id: UUID,
        body: ActionReview,
        context=context_dependency,
    ):
        conn, actor, _ = context
        return success(request, execute(conn, actor, "action.verify", action_id, body))

    @app.post("/api/v1/actions/{action_id}/reject", response_model=Envelope[ActionView])
    def action_reject(
        request: Request,
        action_id: UUID,
        body: ActionReview,
        context=context_dependency,
    ):
        conn, actor, _ = context
        return success(request, execute(conn, actor, "action.reject", action_id, body))

    @app.get("/api/v1/admin/sync-reviews", response_model=response)
    def sync_reviews(request: Request, context=context_dependency):
        conn, actor, _ = context
        require(actor, "setup.manage")
        rows = conn.execute(
            "SELECT id,created_at,creator_id,payload,outcome,state FROM bms.sync_submissions "
            "WHERE tenant_id=%s AND state IN ('sent_for_review','revision_conflict',"
            "'duplicate_conflict','rejected') ORDER BY created_at,id",
            (actor["tenant_id"],),
        ).fetchall()
        resolutions = {
            row["submission_id"]: public(row)
            for row in conn.execute(
                "SELECT * FROM bms.sync_resolutions WHERE tenant_id=%s",
                (actor["tenant_id"],),
            ).fetchall()
        }
        return success(
            request,
            {"items": [public(row) | {"resolution": resolutions.get(row["id"])} for row in rows]},
        )

    @app.post("/api/v1/evidence/{evidence_id}/upload-ticket", response_model=response)
    def upload_ticket(request: Request, evidence_id: UUID, context=context_dependency):
        conn, actor, _ = context
        evidence = evidence_access(conn, actor, evidence_id, upload=True)
        expires = int(time.time()) + settings().evidence_ticket_seconds
        signature = ticket(actor, evidence, "PUT", expires)
        return success(
            request,
            {
                "url": (
                    f"/api/v1/evidence/{evidence_id}/content?expires={expires}&ticket={signature}"
                ),
                "expiresAt": datetime.fromtimestamp(expires, UTC).isoformat(),
            },
        )

    @app.post("/api/v1/evidence/{evidence_id}/download-ticket", response_model=response)
    def download_ticket(request: Request, evidence_id: UUID, context=context_dependency):
        conn, actor, _ = context
        evidence = evidence_access(conn, actor, evidence_id)
        if evidence["upload_state"] != "uploaded":
            raise ApiError(409, "EVIDENCE_PENDING")
        expires = int(time.time()) + settings().evidence_ticket_seconds
        signature = ticket(actor, evidence, "GET", expires)
        return success(
            request,
            {
                "url": (
                    f"/api/v1/evidence/{evidence_id}/content?expires={expires}&ticket={signature}"
                ),
                "expiresAt": datetime.fromtimestamp(expires, UTC).isoformat(),
            },
        )

    @app.put("/api/v1/evidence/{evidence_id}/content", response_model=response)
    async def upload_photo(
        request: Request,
        evidence_id: UUID,
        expires: int,
        ticket: str,
        context=context_dependency,
    ):
        conn, actor, _ = context
        evidence = evidence_access(conn, actor, evidence_id, upload=True)
        check_ticket(actor, evidence, "PUT", expires, ticket)
        content = bytearray()
        async for part in request.stream():
            content.extend(part)
            if len(content) > settings().evidence_max_bytes:
                raise ApiError(400, "INVALID_EVIDENCE_SIZE")
        save_photo(conn, actor, evidence, bytes(content))
        return success(request, {"id": str(evidence_id), "uploadState": "uploaded"})

    @app.get(
        "/api/v1/evidence/{evidence_id}/content",
        response_class=FileResponse,
        responses={
            200: {
                "content": {
                    "image/jpeg": {
                        "schema": {
                            "type": "string",
                            "format": "binary",
                        }
                    }
                }
            }
        },
    )
    def download_photo(
        evidence_id: UUID,
        expires: int,
        ticket: str,
        context=context_dependency,
    ):
        conn, actor, _ = context
        evidence = evidence_access(conn, actor, evidence_id)
        check_ticket(actor, evidence, "GET", expires, ticket)
        if evidence["upload_state"] != "uploaded" or not file_path(evidence).is_file():
            raise ApiError(404, "NOT_FOUND")
        return FileResponse(file_path(evidence), media_type="image/jpeg")
