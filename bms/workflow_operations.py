from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid5

from psycopg import IntegrityError
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from bms.config import settings
from bms.emergency_operations import MODELS as EMERGENCY_MODELS
from bms.emergency_operations import execute_emergency
from bms.operation_access import (
    action_access,
    facility_access,
    finding_access,
    occurrence_access,
    public,
    require,
    revision,
    row_by_id,
)
from bms.operation_models import (
    ActionComplete,
    ActionReview,
    FindingAssign,
    FindingCreate,
    FindingSeverity,
    InspectionAbandon,
    InspectionComplete,
    InspectionStart,
)
from bms.schemas import Command
from bms.security import CAPABILITIES, request_digest
from bms.services import ApiError, audit, get_user, lock_tenant, remember, replay


def register_evidence(conn, actor, owner_type, owner_id, items):
    seen = set()
    for item in items:
        if item.id in seen or item.byte_size > settings().evidence_max_bytes:
            raise ApiError(400, "INVALID_EVIDENCE")
        seen.add(item.id)
        existing = conn.execute(
            "SELECT * FROM bms.evidence WHERE tenant_id=%s AND id=%s",
            (actor["tenant_id"], item.id),
        ).fetchone()
        if existing:
            if (
                existing["creator_id"] != actor["id"]
                or existing["owner_type"] != owner_type
                or existing["owner_id"] != owner_id
                or existing["sha256"] != item.sha256
                or existing["byte_size"] != item.byte_size
                or existing["item_key"] != item.item_key
                or existing["captured_at"] != item.captured_at
            ):
                raise ApiError(409, "EVIDENCE_CONFLICT")
            continue
        conn.execute(
            "INSERT INTO bms.evidence(id,tenant_id,creator_id,owner_type,owner_id,item_key,"
            "captured_at,sha256,byte_size,storage_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                item.id,
                actor["tenant_id"],
                actor["id"],
                owner_type,
                owner_id,
                item.item_key,
                item.captured_at,
                item.sha256,
                item.byte_size,
                f"{actor['tenant_id']}/{uuid4().hex}.jpg",
            ),
        )


def inspect_start(conn, actor, body):
    occurrence = occurrence_access(conn, actor, body.occurrence_id)
    if body.location_method == "qr":
        token = conn.execute(
            "SELECT location_id FROM bms.qr_tokens WHERE tenant_id=%s AND token=%s AND active",
            (actor["tenant_id"], body.qr_token),
        ).fetchone()
        if not token or token["location_id"] != occurrence["location_id"] or not body.scanned_at:
            raise ApiError(400, "QR_UNKNOWN")
    conn.execute(
        "INSERT INTO bms.inspections(id,tenant_id,occurrence_id,performer_id,lifecycle,"
        "location_method,scanned_at,started_at,device_id) "
        "VALUES (%s,%s,%s,%s,'in_progress',%s,%s,%s,%s)",
        (
            body.id,
            actor["tenant_id"],
            body.occurrence_id,
            actor["id"],
            body.location_method,
            body.scanned_at,
            body.started_at,
            body.device_id,
        ),
    )
    return row_by_id(conn, actor, "inspections", body.id), occurrence


def validate_answers(version, body):
    answers = {item.item_key: item for item in body.answers}
    evidence = {item.id: item for item in body.evidence}
    if len(answers) != len(body.answers) or len(evidence) != len(body.evidence):
        raise ApiError(400, "DUPLICATE_ANSWER")
    keys = {item["key"] for item in version["items"]}
    if set(answers) - keys:
        raise ApiError(400, "UNKNOWN_ANSWER")
    used_evidence = set()
    triggered = []
    for item in version["items"]:
        answer = answers.get(item["key"])
        if answer is None:
            if item["required"]:
                raise ApiError(400, "ANSWER_REQUIRED")
            continue
        allowed = {"yes_no": {"yes", "no"}, "pass_concern_fail": {"pass", "concern", "fail"}}
        if item["type"] in allowed and answer.value not in allowed[item["type"]]:
            raise ApiError(400, "INVALID_ANSWER")
        if item["required"] and not answer.value.strip():
            raise ApiError(400, "ANSWER_REQUIRED")
        flagged = answer.value in item["createsFindingOn"]
        photo_required = (
            item["type"] == "photo"
            or item["photo"] == "always"
            or flagged
            or (
                item["type"] in allowed
                and item["photo"] == "on_fail"
                and answer.value in {"no", "concern", "fail"}
            )
        )
        if flagged and not answer.description:
            raise ApiError(400, "DESCRIPTION_REQUIRED")
        if photo_required and not answer.evidence_id:
            raise ApiError(400, "PHOTO_REQUIRED")
        if answer.evidence_id:
            photo = evidence.get(answer.evidence_id)
            if not photo or photo.item_key != item["key"]:
                raise ApiError(400, "INVALID_EVIDENCE")
            used_evidence.add(photo.id)
        if flagged:
            triggered.append((item, answer))
    if set(evidence) != used_evidence:
        raise ApiError(400, "INVALID_EVIDENCE")
    return triggered


def complete_inspection(conn, actor, body):
    occurrence = occurrence_access(conn, actor, body.occurrence_id)
    existing = conn.execute(
        "SELECT * FROM bms.inspections WHERE tenant_id=%s AND id=%s",
        (actor["tenant_id"], body.id),
    ).fetchone()
    if existing:
        if existing["performer_id"] != actor["id"]:
            raise ApiError(403, "FORBIDDEN")
        revision(existing, body.expected_revision)
        if (
            existing["lifecycle"] != "in_progress"
            or existing["occurrence_id"] != body.occurrence_id
        ):
            raise ApiError(409, "INVALID_TRANSITION")
    else:
        if body.expected_revision != 0:
            raise ApiError(409, "REVISION_CONFLICT", {})
        existing, occurrence = inspect_start(conn, actor, body)
    version = row_by_id(conn, actor, "programme_versions", occurrence["programme_version_id"])
    triggered = validate_answers(version, body)
    duplicate = occurrence["canonical_inspection_id"] is not None
    catch_up = body.completed_at > occurrence["grace_end"]
    flag = (
        abs(body.clock_offset_seconds) > settings().clock_warning_seconds
        or body.completed_at < body.started_at
        or body.completed_at
        > (datetime.now(UTC) + timedelta(seconds=settings().clock_warning_seconds))
    )
    conn.execute(
        "UPDATE bms.inspections SET lifecycle='completed',completed_at=%s,answers=%s,"
        "outcome=%s,clock_offset_seconds=%s,clock_flag=%s,duplicate=%s,catch_up=%s,"
        "revision=revision+1,updated_at=now() WHERE tenant_id=%s AND id=%s",
        (
            body.completed_at,
            Jsonb([a.model_dump(mode="json", by_alias=True) for a in body.answers]),
            "findings_recorded" if triggered else "no_findings",
            body.clock_offset_seconds,
            flag,
            duplicate,
            catch_up,
            actor["tenant_id"],
            body.id,
        ),
    )
    register_evidence(conn, actor, "inspection", body.id, body.evidence)
    finding_ids = []
    if not duplicate:
        for item, answer in triggered:
            finding_id = answer.finding_id or uuid5(body.id, item["key"])
            conn.execute(
                "INSERT INTO bms.findings(id,tenant_id,location_id,source,inspection_id,item_key,"
                "description,severity,reporter_id,reported_at) "
                "VALUES (%s,%s,%s,'inspection',%s,%s,%s,%s,%s,%s)",
                (
                    finding_id,
                    actor["tenant_id"],
                    occurrence["location_id"],
                    body.id,
                    item["key"],
                    answer.description,
                    item["findingSeverity"],
                    actor["id"],
                    body.completed_at,
                ),
            )
            finding_ids.append(str(finding_id))
            audit(
                conn,
                actor,
                "finding.created_from_inspection",
                finding_id,
                body,
                after={"inspectionId": str(body.id), "severity": item["findingSeverity"]},
            )
        state = "closed_late" if body.completed_at > occurrence["due_end"] else "closed_on_time"
        # A catch-up closes the work, but does not erase the missed coverage history.
        conn.execute(
            "UPDATE bms.inspection_occurrences SET canonical_inspection_id=%s,timing_state=%s,"
            "missed_at=CASE WHEN %s THEN coalesce(missed_at,%s) ELSE NULL END,"
            "revision=revision+1,updated_at=now() WHERE tenant_id=%s AND id=%s",
            (
                body.id,
                state,
                catch_up,
                occurrence["grace_end"],
                actor["tenant_id"],
                occurrence["id"],
            ),
        )
        if occurrence["missed_at"] and not catch_up:
            audit(
                conn,
                actor,
                "occurrence.missed_reversed",
                occurrence["id"],
                body,
                before={"missedAt": occurrence["missed_at"].isoformat()},
                after={"state": state},
                reason="Valid device completion before grace ended.",
            )
    result = public(row_by_id(conn, actor, "inspections", body.id))
    result["findingIds"] = finding_ids
    result["evidenceIds"] = [str(item.id) for item in body.evidence]
    return result


def abandon_inspection(conn, actor, entity_id, body):
    row = row_by_id(conn, actor, "inspections", entity_id)
    occurrence_access(conn, actor, row["occurrence_id"])
    if row["performer_id"] != actor["id"]:
        raise ApiError(403, "FORBIDDEN")
    revision(row, body.expected_revision)
    if row["lifecycle"] != "in_progress":
        raise ApiError(409, "INVALID_TRANSITION")
    conn.execute(
        "UPDATE bms.inspections SET lifecycle='abandoned',updated_at=now(),revision=revision+1 "
        "WHERE tenant_id=%s AND id=%s",
        (actor["tenant_id"], entity_id),
    )
    return public(row_by_id(conn, actor, "inspections", entity_id))


def create_finding(conn, actor, body):
    facility_access(conn, actor, body.location_id, active=True)
    require(actor, "finding.create")
    conn.execute(
        "INSERT INTO bms.findings(id,tenant_id,location_id,source,description,severity,"
        "reporter_id,reported_at) VALUES (%s,%s,%s,'manual',%s,%s,%s,%s)",
        (
            body.id,
            actor["tenant_id"],
            body.location_id,
            body.description,
            body.severity,
            actor["id"],
            body.reported_at,
        ),
    )
    register_evidence(conn, actor, "finding", body.id, body.evidence)
    result = public(row_by_id(conn, actor, "findings", body.id))
    result["evidenceIds"] = [str(item.id) for item in body.evidence]
    return result


def assign_finding(conn, actor, entity_id, body):
    finding = finding_access(conn, actor, entity_id)
    owner = get_user(conn, actor["tenant_id"], body.owner_id)
    require(actor, "finding.assign")
    revision(finding, body.expected_revision)
    if finding["status"] not in {"open", "action_required"}:
        raise ApiError(409, "INVALID_TRANSITION")
    if owner["status"] != "active" or "action.complete_owned" not in CAPABILITIES[owner["role"]]:
        raise ApiError(400, "INVALID_OWNER")
    # Supervisors must be able to see the action they are assigned.
    if owner["role"] == "supervisor":
        facility_access(conn, owner, finding["location_id"])
    action = conn.execute(
        "SELECT * FROM bms.corrective_actions WHERE tenant_id=%s AND finding_id=%s",
        (actor["tenant_id"], entity_id),
    ).fetchone()
    if action and action["id"] != body.action_id:
        raise ApiError(409, "ONE_ACTION_PER_FINDING")
    if action:
        conn.execute(
            "UPDATE bms.corrective_actions SET title=%s,owner_id=%s,due_date=%s,"
            "updated_at=now(),revision=revision+1 WHERE tenant_id=%s AND id=%s",
            (body.title, body.owner_id, body.due_date, actor["tenant_id"], body.action_id),
        )
    else:
        conn.execute(
            "INSERT INTO bms.corrective_actions(id,tenant_id,finding_id,title,owner_id,due_date) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                body.action_id,
                actor["tenant_id"],
                entity_id,
                body.title,
                body.owner_id,
                body.due_date,
            ),
        )
    conn.execute(
        "UPDATE bms.findings SET owner_id=%s,status='action_required',"
        "updated_at=now(),revision=revision+1 WHERE tenant_id=%s AND id=%s",
        (body.owner_id, actor["tenant_id"], entity_id),
    )
    return public(row_by_id(conn, actor, "corrective_actions", body.action_id))


def set_severity(conn, actor, entity_id, body):
    finding = finding_access(conn, actor, entity_id)
    require(actor, "finding.assign")
    revision(finding, body.expected_revision)
    if finding["status"] == "verified_closed":
        raise ApiError(409, "VERIFIED_READ_ONLY")
    conn.execute(
        "UPDATE bms.findings SET severity=%s,updated_at=now(),revision=revision+1 "
        "WHERE tenant_id=%s AND id=%s",
        (body.severity, actor["tenant_id"], entity_id),
    )
    return public(row_by_id(conn, actor, "findings", entity_id))


def complete_action(conn, actor, entity_id, body):
    action, finding = action_access(conn, actor, entity_id)
    require(actor, "action.complete_owned")
    revision(action, body.expected_revision)
    if action["owner_id"] != actor["id"]:
        raise ApiError(403, "FORBIDDEN")
    if action["status"] != "assigned" or finding["status"] != "action_required":
        raise ApiError(409, "INVALID_TRANSITION")
    if finding["severity"] in {"critical", "high"} and not body.evidence:
        raise ApiError(400, "PHOTO_REQUIRED")
    register_evidence(conn, actor, "action", entity_id, body.evidence)
    conn.execute(
        "UPDATE bms.corrective_actions SET status='completed',completion_note=%s,"
        "completed_at=%s,completed_by=%s,updated_at=now(),revision=revision+1 "
        "WHERE tenant_id=%s AND id=%s",
        (body.note, body.completed_at, actor["id"], actor["tenant_id"], entity_id),
    )
    conn.execute(
        "UPDATE bms.findings SET status='awaiting_verification',"
        "updated_at=now(),revision=revision+1 WHERE tenant_id=%s AND id=%s",
        (actor["tenant_id"], finding["id"]),
    )
    result = public(row_by_id(conn, actor, "corrective_actions", entity_id))
    result["evidenceIds"] = [str(item.id) for item in body.evidence]
    return result


def review_action(conn, actor, entity_id, body, *, reject=False):
    action, finding = action_access(conn, actor, entity_id)
    require(actor, "action.reject" if reject else "action.verify")
    revision(action, body.expected_revision)
    if action["completed_by"] == actor["id"]:
        raise ApiError(403, "INDEPENDENT_VERIFIER_REQUIRED")
    if action["status"] != "completed" or finding["status"] != "awaiting_verification":
        raise ApiError(409, "INVALID_TRANSITION")
    if not reject:
        pending = conn.execute(
            "SELECT id FROM bms.evidence WHERE tenant_id=%s AND upload_state<>'uploaded' AND "
            "((owner_type='action' AND owner_id=%s) OR (owner_type='finding' AND owner_id=%s) "
            "OR (owner_type='inspection' AND owner_id=%s)) LIMIT 1",
            (actor["tenant_id"], entity_id, finding["id"], finding["inspection_id"]),
        ).fetchone()
        if pending:
            raise ApiError(409, "EVIDENCE_PENDING")
    if reject:
        conn.execute(
            "UPDATE bms.corrective_actions SET status='assigned',rejection_note=%s,"
            "updated_at=now(),revision=revision+1 WHERE tenant_id=%s AND id=%s",
            (body.note, actor["tenant_id"], entity_id),
        )
    else:
        conn.execute(
            "UPDATE bms.corrective_actions SET status='verified',verification_note=%s,"
            "verified_at=now(),verified_by=%s,updated_at=now(),revision=revision+1 "
            "WHERE tenant_id=%s AND id=%s",
            (body.note, actor["id"], actor["tenant_id"], entity_id),
        )
    conn.execute(
        "UPDATE bms.findings SET status=%s,verified_closed_at=%s,"
        "updated_at=now(),revision=revision+1 WHERE tenant_id=%s AND id=%s",
        (
            "action_required" if reject else "verified_closed",
            None if reject else datetime.now(UTC),
            actor["tenant_id"],
            finding["id"],
        ),
    )
    return public(row_by_id(conn, actor, "corrective_actions", entity_id))


COMMAND_MODELS = {
    "inspection.start": InspectionStart,
    "inspection.complete": InspectionComplete,
    "inspection.abandon": InspectionAbandon,
    "finding.create": FindingCreate,
    "finding.assign": FindingAssign,
    "finding.set_severity": FindingSeverity,
    "action.complete": ActionComplete,
    "action.verify": ActionReview,
    "action.reject": ActionReview,
    "incident.report": EMERGENCY_MODELS["incident.report"],
    "incident.evidence": EMERGENCY_MODELS["incident.evidence"],
}


def execute(conn, actor, command, target_id, body):
    if command in {"incident.report", "incident.evidence"}:
        if target_id is not None and target_id != body.id:
            raise ApiError(400, "INVALID_TRANSITION")
        return execute_emergency(conn, actor, command, body)
    key = f"{command}:{target_id}" if target_id else command
    old = replay(conn, actor, key, body)
    if old is not None:
        return old
    before = None
    table = (
        "corrective_actions"
        if command.startswith("action.")
        else "findings"
        if command.startswith("finding.")
        else "inspections"
    )
    if target_id:
        before = public(row_by_id(conn, actor, table, target_id))
    if command == "inspection.start":
        row, _ = inspect_start(conn, actor, body)
        result = public(row)
    elif command == "inspection.complete":
        result = complete_inspection(conn, actor, body)
    elif command == "inspection.abandon":
        result = abandon_inspection(conn, actor, target_id, body)
    elif command == "finding.create":
        result = create_finding(conn, actor, body)
    elif command == "finding.assign":
        result = assign_finding(conn, actor, target_id, body)
    elif command == "finding.set_severity":
        result = set_severity(conn, actor, target_id, body)
    elif command == "action.complete":
        result = complete_action(conn, actor, target_id, body)
    elif command in {"action.verify", "action.reject"}:
        result = review_action(conn, actor, target_id, body, reject=command == "action.reject")
    else:
        raise ApiError(400, "UNKNOWN_COMMAND")
    audit(
        conn,
        actor,
        command,
        target_id or body.id,
        body,
        before=before,
        after=result,
        reason=getattr(body, "reason", None),
    )
    return remember(conn, actor, key, body, result)


def submit_operation(conn, actor, operation, *, review_only=False):
    lock_tenant(conn, actor["tenant_id"])
    raw = operation.model_dump(mode="json", by_alias=True)
    digest = request_digest("sync.submit", raw)
    existing = conn.execute(
        "SELECT * FROM bms.sync_submissions WHERE tenant_id=%s AND id=%s",
        (actor["tenant_id"], operation.client_operation_id),
    ).fetchone()
    if existing:
        if existing["creator_id"] != actor["id"] or existing["request_hash"] != digest:
            raise ApiError(409, "OPERATION_ID_REUSED")
        return existing["outcome"]
    fallback = Command(client_operation_id=operation.client_operation_id)
    try:
        with conn.transaction():
            data = dict(operation.payload)
            supplied = data.get("clientOperationId")
            if supplied is not None and supplied != str(operation.client_operation_id):
                raise ApiError(400, "OPERATION_ID_REUSED")
            data["clientOperationId"] = str(operation.client_operation_id)
            body = COMMAND_MODELS[operation.command].model_validate(data)
            fallback = body
            if review_only:
                raise ApiError(403, "OFFLINE_ACCESS_LOST")
            result = execute(conn, actor, operation.command, operation.target_id, body)
            state = "duplicate_conflict" if result.get("duplicate") else "accepted"
            outcome = {
                "operationId": str(operation.client_operation_id),
                "state": state,
                "httpStatus": 409 if state == "duplicate_conflict" else 200,
                "result": result,
            }
    except ApiError as exc:
        state = (
            "sent_for_review"
            if exc.status == 403
            else "revision_conflict"
            if exc.code == "REVISION_CONFLICT"
            else "rejected"
        )
        outcome = {
            "operationId": str(operation.client_operation_id),
            "state": state,
            "errorCode": exc.code,
            "httpStatus": exc.status,
        }
        if exc.current is not None:
            outcome["current"] = exc.current
    except (ValidationError, IntegrityError):
        outcome = {
            "operationId": str(operation.client_operation_id),
            "state": "rejected",
            "errorCode": "VALIDATION_FAILED",
            "httpStatus": 400,
        }
    if outcome["state"] == "sent_for_review" and hasattr(fallback, "evidence"):
        register_evidence(
            conn,
            actor,
            "review",
            operation.client_operation_id,
            fallback.evidence,
        )
        outcome["evidenceIds"] = [str(item.id) for item in fallback.evidence]
    conn.execute(
        "INSERT INTO bms.sync_submissions "
        "(id,tenant_id,creator_id,request_hash,payload,outcome,state) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            operation.client_operation_id,
            actor["tenant_id"],
            actor["id"],
            digest,
            Jsonb(raw),
            Jsonb(outcome),
            outcome["state"],
        ),
    )
    if outcome["state"] not in {"accepted", "duplicate_conflict"}:
        audit(
            conn,
            actor,
            "sync." + outcome["state"],
            operation.client_operation_id,
            fallback,
            after={"command": operation.command, "errorCode": outcome.get("errorCode")},
        )
    return outcome
