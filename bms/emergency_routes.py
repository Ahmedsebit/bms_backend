from typing import Any

from fastapi import Depends, Request
from pydantic import ValidationError

from bms.emergency_models import EmergencySnapshot, EmergencySubmission, SyncResolution
from bms.emergency_operations import MODELS, emergency_snapshot, execute_emergency
from bms.operation_access import public, revision
from bms.responses import Envelope
from bms.services import ApiError, audit, get_user, remember, replay


def register_emergency_routes(app, authenticated, success):
    dependency = Depends(authenticated, scope="function")

    @app.get("/api/v1/emergency", response_model=Envelope[EmergencySnapshot])
    @app.get("/api/v1/sync/emergency", response_model=Envelope[EmergencySnapshot])
    def pull_emergency(request: Request, context=dependency):
        conn, actor, _ = context
        return success(request, emergency_snapshot(conn, actor))

    @app.post("/api/v1/emergency/commands", response_model=Envelope[dict[str, Any]])
    def emergency_command(request: Request, body: EmergencySubmission, context=dependency):
        conn, actor, _ = context
        try:
            payload = MODELS[body.command].model_validate(body.payload)
        except ValidationError as exc:
            raise ApiError(400, "VALIDATION_FAILED") from exc
        return success(request, execute_emergency(conn, actor, body.command, payload))

    @app.get("/api/v1/leadership", response_model=Envelope[dict[str, Any]])
    def leadership(request: Request, context=dependency):
        conn, actor, _ = context
        if actor["role"] not in {"leadership", "administrator", "supervisor"}:
            raise ApiError(403, "FORBIDDEN")
        school = conn.execute(
            "SELECT name,school_code,timezone FROM bms.tenants WHERE tenant_id=%s",
            (actor["tenant_id"],),
        ).fetchone()
        data = emergency_snapshot(conn, actor)
        buildings = []
        for building in data["buildings"]:
            counts = conn.execute(
                "SELECT count(*) FILTER (WHERE f.status<>'verified_closed') AS open_findings,"
                "count(*) FILTER (WHERE f.severity IN ('critical','high') "
                "AND f.status<>'verified_closed') AS high_risks "
                "FROM bms.findings f JOIN bms.facilities l "
                "ON l.tenant_id=f.tenant_id AND l.id=f.location_id "
                "WHERE f.tenant_id=%s AND l.building_id=%s",
                (actor["tenant_id"], building["id"]),
            ).fetchone()
            buildings.append(building | public(counts))
        return success(
            request, {"school": public(school), "buildings": buildings, "emergency": data}
        )

    @app.post("/api/v1/admin/sync-resolutions", response_model=Envelope[dict[str, Any]])
    def resolve_sync(request: Request, body: SyncResolution, context=dependency):
        conn, actor, _ = context
        if actor["role"] != "administrator" or body.offline:
            raise ApiError(403, "FORBIDDEN")
        key = "sync.resolve"
        old = replay(conn, actor, key, body)
        if old is not None:
            return success(request, old)
        submission = conn.execute(
            "SELECT * FROM bms.sync_submissions WHERE tenant_id=%s AND id=%s",
            (actor["tenant_id"], body.id),
        ).fetchone()
        if not submission:
            raise ApiError(404, "NOT_FOUND")
        if submission["state"] == "accepted":
            raise ApiError(409, "REVIEW_NOT_REQUIRED")
        owner = get_user(conn, actor["tenant_id"], body.owner_id)
        if owner["status"] != "active" or owner["role"] != "administrator":
            raise ApiError(400, "INVALID_REVIEW_OWNER")
        if body.replacement_operation_id:
            replacement = conn.execute(
                "SELECT * FROM bms.sync_submissions WHERE tenant_id=%s AND id=%s",
                (actor["tenant_id"], body.replacement_operation_id),
            ).fetchone()
            if (
                not replacement
                or replacement["state"] != "accepted"
                or (replacement["payload"]["command"] != submission["payload"]["command"])
            ):
                raise ApiError(409, "ACCEPTED_REPLACEMENT_REQUIRED")
        existing = conn.execute(
            "SELECT * FROM bms.sync_resolutions WHERE tenant_id=%s AND submission_id=%s",
            (actor["tenant_id"], body.id),
        ).fetchone()
        if existing:
            revision(existing, body.expected_revision)
            conn.execute(
                "UPDATE bms.sync_resolutions SET owner_id=%s,status=%s,decision=%s,note=%s,"
                "replacement_operation_id=%s,decided_by=%s,revision=revision+1,updated_at=now() "
                "WHERE tenant_id=%s AND submission_id=%s",
                (
                    body.owner_id,
                    body.status,
                    body.decision,
                    body.note,
                    body.replacement_operation_id,
                    actor["id"],
                    actor["tenant_id"],
                    body.id,
                ),
            )
        else:
            if body.expected_revision:
                raise ApiError(409, "REVISION_CONFLICT")
            conn.execute(
                "INSERT INTO bms.sync_resolutions(id,tenant_id,submission_id,owner_id,status,"
                "decision,note,replacement_operation_id,decided_by) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    body.id,
                    actor["tenant_id"],
                    body.id,
                    body.owner_id,
                    body.status,
                    body.decision,
                    body.note,
                    body.replacement_operation_id,
                    actor["id"],
                ),
            )
        result = public(
            conn.execute(
                "SELECT * FROM bms.sync_resolutions WHERE tenant_id=%s AND submission_id=%s",
                (actor["tenant_id"], body.id),
            ).fetchone()
        )
        audit(
            conn,
            actor,
            key,
            body.id,
            body,
            before=public(existing) if existing else None,
            after=result,
            reason=body.note,
        )
        return success(request, remember(conn, actor, key, body, result))
