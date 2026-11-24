from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid5

from psycopg import sql
from psycopg.types.json import Jsonb

from bms.duty_operations import coverage, has_duty, require_duty, roster_status
from bms.emergency_models import (
    AssemblyPointWrite,
    ContactAttempt,
    DrillPlan,
    DutyGrantWrite,
    DutyShiftWrite,
    EmergencyContactWrite,
    EmergencySettingsWrite,
    GuardianRelease,
    GuardianWrite,
    HandoverWrite,
    IncidentEvidence,
    IncidentPhase,
    IncidentReport,
    IncidentResponders,
    IncidentTask,
    IncidentTransfer,
    PersonWrite,
    RecoveryWrite,
    ReopeningApproval,
    RevisionNote,
    RollCallConfirm,
    RosterReview,
)
from bms.operation_access import assigned_zone, public, revision, supervisor_in_scope
from bms.schemas import Command
from bms.services import ApiError, audit, get_user, lock_tenant, remember, replay

MODELS = {
    "duty.grant": DutyGrantWrite,
    "roster.review": RosterReview,
    "emergency.shift_accept": RevisionNote,
    "incident.accept": RevisionNote,
    "incident.progress": RevisionNote,
    "incident.transfer": IncidentTransfer,
    "incident.transfer_accept": RevisionNote,
    "incident.transfer_decline": RevisionNote,
    "incident.transfer_cancel": RevisionNote,
    "incident.contact": ContactAttempt,
    "incident.task_verify": RevisionNote,
    "incident.task_reject": RevisionNote,
    "emergency.settings": EmergencySettingsWrite,
    "emergency.contact": EmergencyContactWrite,
    "emergency.point": AssemblyPointWrite,
    "emergency.person": PersonWrite,
    "emergency.guardian": GuardianWrite,
    "emergency.shift": DutyShiftWrite,
    "emergency.handover": HandoverWrite,
    "emergency.handover_accept": RevisionNote,
    "incident.report": IncidentReport,
    "incident.responders": IncidentResponders,
    "incident.acknowledge": RevisionNote,
    "incident.phase": IncidentPhase,
    "incident.evidence": IncidentEvidence,
    "rollcall.confirm": RollCallConfirm,
    "drill.plan": DrillPlan,
    "drill.start": RevisionNote,
    "incident.task": IncidentTask,
    "incident.task_complete": RevisionNote,
    "recovery.save": RecoveryWrite,
    "recovery.approve": ReopeningApproval,
    "guardian.release": GuardianRelease,
}
TABLES = {
    "grants": "duty_grants",
    "transfers": "incident_transfers",
    "progress": "incident_progress",
    "contactAttempts": "incident_contacts",
    "rosterReviews": "roster_reviews",
    "settings": "emergency_settings",
    "contacts": "emergency_contacts",
    "points": "emergency_points",
    "people": "emergency_people",
    "guardians": "emergency_guardians",
    "shifts": "emergency_shifts",
    "handovers": "emergency_handovers",
    "incidents": "emergency_incidents",
    "alerts": "emergency_alerts",
    "rollCall": "emergency_rollcall",
    "tasks": "emergency_tasks",
    "recovery": "emergency_recovery",
    "releases": "emergency_releases",
}


def _lead(actor):
    return actor["role"] in {"supervisor", "administrator"}


def _require_lead(actor):
    if actor["role"] not in {"supervisor", "administrator", "leadership"}:
        raise ApiError(403, "FORBIDDEN")


def _get(conn, actor, kind, entity_id):
    row = conn.execute(
        sql.SQL("SELECT * FROM bms.{} WHERE tenant_id=%s AND id=%s").format(
            sql.Identifier(TABLES[kind])
        ),
        (actor["tenant_id"], entity_id),
    ).fetchone()
    if not row:
        raise ApiError(404, "NOT_FOUND")
    return row


def _save(conn, actor, kind, body, values):
    existing = conn.execute(
        sql.SQL("SELECT * FROM bms.{} WHERE tenant_id=%s AND id=%s").format(
            sql.Identifier(TABLES[kind])
        ),
        (actor["tenant_id"], body.id),
    ).fetchone()
    if existing:
        revision(existing, body.expected_revision)
        for key in {
            "points": ("building_id",),
            "guardians": ("person_id",),
            "tasks": ("incident_id",),
            "recovery": ("incident_id",),
        }.get(kind, ()):
            if values.get(key, existing[key]) != existing[key]:
                raise ApiError(409, "EMERGENCY_PARENT_IMMUTABLE")
        if kind == "tasks" and existing["status"] == "completed":
            raise ApiError(409, "INVALID_TRANSITION")
        if kind == "shifts" and existing["starts_at"] <= datetime.now(UTC):
            raise ApiError(409, "DUTY_SHIFT_ALREADY_STARTED")
        _update(conn, actor, kind, body.id, values)
    else:
        if body.expected_revision:
            raise ApiError(409, "REVISION_CONFLICT")
        keys = ["id", "tenant_id", *values]
        conn.execute(
            sql.SQL("INSERT INTO bms.{} ({}) VALUES ({})").format(
                sql.Identifier(TABLES[kind]),
                sql.SQL(",").join(map(sql.Identifier, keys)),
                sql.SQL(",").join(sql.Placeholder() for _ in keys),
            ),
            (body.id, actor["tenant_id"], *values.values()),
        )
    return _get(conn, actor, kind, body.id)


def _update(conn, actor, kind, entity_id, values):
    conn.execute(
        sql.SQL(
            "UPDATE bms.{} SET {},revision=revision+1,updated_at=now() WHERE tenant_id=%s AND id=%s"
        ).format(
            sql.Identifier(TABLES[kind]),
            sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in values),
        ),
        (*values.values(), actor["tenant_id"], entity_id),
    )


def ensure_emergency_settings(conn, tenant_id):
    conn.execute(
        "INSERT INTO bms.emergency_settings(id,tenant_id) VALUES (%s,%s) "
        "ON CONFLICT (tenant_id) DO NOTHING",
        (uuid5(tenant_id, "emergency-settings"), tenant_id),
    )
    return conn.execute(
        "SELECT * FROM bms.emergency_settings WHERE tenant_id=%s",
        (tenant_id,),
    ).fetchone()


def _building(conn, actor, building_id):
    row = conn.execute(
        "SELECT * FROM bms.facilities WHERE tenant_id=%s AND id=%s AND level='building'",
        (actor["tenant_id"], building_id),
    ).fetchone()
    if not row:
        raise ApiError(404, "NOT_FOUND")
    return row


def _scoped_building(conn, actor, building_id):
    if any(
        has_duty(conn, actor, capability, building_id)
        for capability in ("incident.command", "roster.manage")
    ):
        return True
    if actor["role"] == "leadership":
        return False
    if actor["role"] == "administrator":
        return True
    if actor["role"] == "supervisor":
        return supervisor_in_scope(conn, actor, building_id)
    zones = conn.execute(
        "SELECT id FROM bms.facilities WHERE tenant_id=%s AND building_id=%s AND level='zone'",
        (actor["tenant_id"], building_id),
    ).fetchall()
    return any(assigned_zone(conn, actor, row["id"]) for row in zones)


def can_account(conn, actor, incident):
    return (
        actor["id"]
        in (
            incident["primary_user_id"],
            incident["backup_user_id"],
            incident["response_owner_id"],
        )
        or has_duty(conn, actor, "incident.command", incident["building_id"])
        or has_duty(conn, actor, "roster.manage", incident["building_id"])
        or (
            actor["role"] in {"officer", "supervisor"}
            and _scoped_building(conn, actor, incident["building_id"])
        )
    )


def can_lead(conn, actor, incident):
    return has_duty(conn, actor, "incident.command", incident["building_id"])


def _require_incident_lead(conn, actor, incident):
    if not can_lead(conn, actor, incident):
        raise ApiError(403, "FORBIDDEN")


def _require_building_lead(conn, actor, building_id):
    _require_lead(actor)
    require_duty(conn, actor, "incident.command", building_id)


def incident_access(conn, actor, entity_id, *, account=False):
    if actor["role"] not in {"officer", "supervisor", "administrator", "leadership"}:
        raise ApiError(403, "FORBIDDEN")
    incident = _get(conn, actor, "incidents", entity_id)
    allowed = can_account(conn, actor, incident)
    owns_task = conn.execute(
        "SELECT id FROM bms.emergency_tasks WHERE tenant_id=%s AND incident_id=%s AND owner_id=%s",
        (actor["tenant_id"], entity_id, actor["id"]),
    ).fetchone()
    delegated = has_duty(conn, actor, "safety.approve", incident["building_id"]) or has_duty(
        conn, actor, "safety.verify", incident["building_id"]
    )
    incoming = conn.execute(
        "SELECT id FROM bms.incident_transfers WHERE tenant_id=%s AND incident_id=%s "
        "AND incoming_user_id=%s AND status='pending'",
        (actor["tenant_id"], entity_id, actor["id"]),
    ).fetchone()
    if not allowed and (
        account
        or (
            actor["role"] != "leadership"
            and incident["reported_by"] != actor["id"]
            and not owns_task
            and not delegated
            and not incoming
        )
        or (actor["role"] == "leadership" and not delegated and not incoming)
    ):
        raise ApiError(403, "FORBIDDEN")
    return incident


def _active(incident):
    if incident["status"] not in {"active", "recovery"}:
        raise ApiError(409, "INCIDENT_NOT_ACTIVE")


def _approval(actor, body):
    return {
        "approved_by": actor["id"] if body.approved else None,
        "approved_at": datetime.now(UTC) if body.approved else None,
        "approval_note": body.approval_note,
    }


def _responder(conn, actor, user_id, *, lead=False, building_id=None):
    user = get_user(conn, actor["tenant_id"], user_id)
    roles = {"supervisor", "administrator", "leadership"} if lead else {"officer", "supervisor"}
    if user["status"] != "active" or user["role"] not in roles:
        raise ApiError(400, "INVALID_EMERGENCY_RESPONDER")
    if lead:
        require_duty(conn, user, "incident.command", building_id)
    return user


def _invalidate_reopening(conn, actor, incident_id):
    conn.execute(
        "UPDATE bms.emergency_recovery SET approved_by=NULL,approved_at=NULL,"
        "reopening_note='',revision=revision+1,updated_at=now() "
        "WHERE tenant_id=%s AND incident_id=%s AND approved_at IS NOT NULL",
        (actor["tenant_id"], incident_id),
    )


def _add_person(conn, actor, incident, person):
    conn.execute(
        "INSERT INTO bms.emergency_rollcall(id,tenant_id,incident_id,person_id,person_name,"
        "person_kind,expected_presence,away_until,assistance,roster_updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (tenant_id,incident_id,person_id) DO NOTHING",
        (
            uuid4(),
            actor["tenant_id"],
            incident["id"],
            person["id"],
            person["name"],
            person["kind"],
            person["presence"],
            person["away_until"],
            person["assistance"],
            person["updated_at"],
        ),
    )


def _activate(conn, actor, incident, now):
    duty = conn.execute(
        "SELECT * FROM bms.emergency_shifts WHERE tenant_id=%s AND building_id=%s "
        "AND starts_at<=%s AND ends_at>%s ORDER BY starts_at DESC LIMIT 1",
        (actor["tenant_id"], incident["building_id"], now, now),
    ).fetchone()
    settings = ensure_emergency_settings(conn, actor["tenant_id"])
    _update(
        conn,
        actor,
        "incidents",
        incident["id"],
        {
            "status": "active",
            "started_at": now,
            "lead_user_id": duty["lead_user_id"]
            if duty
            else actor["id"]
            if can_lead(conn, actor, incident)
            else None,
            "primary_user_id": duty["primary_user_id"] if duty else None,
            "backup_user_id": duty["backup_user_id"] if duty else None,
            "timing_approved": settings["approved_at"] is not None,
            "acknowledgement_minutes": settings["acknowledgement_minutes"],
            "progress_minutes": settings["progress_minutes"],
        },
    )
    incident = _get(conn, actor, "incidents", incident["id"])
    people = conn.execute(
        "SELECT * FROM bms.emergency_people WHERE tenant_id=%s AND building_id=%s AND active",
        (actor["tenant_id"], incident["building_id"]),
    ).fetchall()
    for person in people:
        _add_person(conn, actor, incident, person)
    _new_alert(conn, actor, incident, "primary", now)
    return incident


def _new_alert(conn, actor, incident, stage, now):
    recipient = incident[f"{stage}_user_id"]
    failure = ""
    if recipient is None:
        failure = {
            "primary": "No duty responder configured",
            "backup": "No backup configured",
            "lead": "No incident lead configured",
        }[stage]
    else:
        user = get_user(conn, actor["tenant_id"], recipient)
        if stage in {"backup", "lead"} and recipient == incident["response_owner_id"]:
            failure = f"{stage.title()} is the stalled response owner"
        valid = (
            has_duty(conn, user, "incident.command", incident["building_id"], now)
            if stage == "lead"
            else user["role"] in {"officer", "supervisor"}
        )
        if user["status"] != "active" or not valid:
            failure = "Responder account unavailable"
    conn.execute(
        "INSERT INTO bms.emergency_alerts(id,tenant_id,incident_id,recipient_id,stage,status,"
        "deadline,failure_reason,notification_round) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (tenant_id,incident_id,stage,notification_round) DO NOTHING",
        (
            uuid4(),
            actor["tenant_id"],
            incident["id"],
            recipient,
            stage,
            "failed" if failure else "queued",
            now + timedelta(minutes=incident["acknowledgement_minutes"]),
            failure,
            incident["notification_round"],
        ),
    )


def process_emergency_escalations(conn, tenant_id, now=None):
    now = now or datetime.now(UTC)
    lock_tenant(conn, tenant_id)
    actor = {"tenant_id": tenant_id, "id": None, "role": "system"}
    incidents = conn.execute(
        "SELECT * FROM bms.emergency_incidents WHERE tenant_id=%s AND status='active' ",
        (tenant_id,),
    ).fetchall()
    for incident in incidents:
        if incident["response_owner_id"]:
            owner = get_user(conn, tenant_id, incident["response_owner_id"])
            qualified = owner["role"] in {"officer", "supervisor"} or has_duty(
                conn, owner, "incident.command", incident["building_id"], now
            )
            if owner["status"] == "active" and qualified and now < incident["progress_deadline"]:
                continue
            _new_alert(conn, actor, incident, "backup", now)
            reason = (
                "Response progress deadline elapsed"
                if owner["status"] == "active" and qualified
                else ("Response owner unavailable")
            )
            conn.execute(
                "UPDATE bms.emergency_alerts SET status='escalated',failure_reason=%s "
                "WHERE tenant_id=%s AND incident_id=%s AND notification_round=%s "
                "AND stage='primary' AND status NOT IN ('failed','cancelled','escalated')",
                (reason, tenant_id, incident["id"], incident["notification_round"]),
            )
        alerts = conn.execute(
            "SELECT * FROM bms.emergency_alerts WHERE tenant_id=%s AND incident_id=%s "
            "AND notification_round=%s",
            (tenant_id, incident["id"], incident["notification_round"]),
        ).fetchall()
        for alert in alerts:
            if alert["status"] in {"cancelled", "escalated"}:
                continue
            reason = alert["failure_reason"]
            if alert["recipient_id"]:
                user = get_user(conn, tenant_id, alert["recipient_id"])
                valid = (
                    has_duty(conn, user, "incident.command", incident["building_id"], now)
                    if alert["stage"] == "lead"
                    else user["role"] in {"officer", "supervisor"}
                )
                if user["status"] != "active" or not valid:
                    reason = "Responder account unavailable"
            expired = now >= alert["deadline"]
            if expired and alert["status"] == "acknowledged" and not incident["response_owner_id"]:
                reason = "Responsibility acceptance deadline elapsed"
            if not reason and not expired:
                continue
            if alert["stage"] == "primary":
                new_status = "failed" if reason else "escalated"
                reason = reason or "Primary acknowledgement deadline elapsed"
                _new_alert(conn, actor, incident, "backup", now)
            elif alert["stage"] == "backup":
                new_status = "failed"
                reason = reason or "Backup acknowledgement deadline elapsed"
                _new_alert(conn, actor, incident, "lead", now)
            else:
                new_status = "failed"
                reason = reason or "Lead acknowledgement deadline elapsed; use approved fallback"
            if alert["status"] != new_status or alert["failure_reason"] != reason:
                _update(
                    conn,
                    actor,
                    "alerts",
                    alert["id"],
                    {
                        "status": new_status,
                        "failure_reason": reason,
                    },
                )
                audit(
                    conn,
                    actor,
                    "incident.alert_" + new_status,
                    alert["id"],
                    Command(client_operation_id=uuid4()),
                    after={
                        "incidentId": str(incident["id"]),
                        "stage": alert["stage"],
                        "reason": reason,
                    },
                )


def _record_progress(conn, actor, incident, kind, note, now):
    _update(
        conn,
        actor,
        "incidents",
        incident["id"],
        {
            "response_owner_id": actor["id"],
            "responsibility_accepted_at": now
            if kind != "progress"
            else incident["responsibility_accepted_at"],
            "progress_at": now,
            "progress_deadline": now + timedelta(minutes=incident["progress_minutes"]),
            "notification_round": incident["notification_round"] + 1,
        },
    )
    conn.execute(
        "UPDATE bms.emergency_alerts SET status='cancelled',revision=revision+1,updated_at=now() "
        "WHERE tenant_id=%s AND incident_id=%s AND status IN ('queued','available_in_app')",
        (actor["tenant_id"], incident["id"]),
    )
    conn.execute(
        "INSERT INTO bms.incident_progress(id,tenant_id,incident_id,recorded_by,kind,note) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (uuid4(), actor["tenant_id"], incident["id"], actor["id"], kind, note),
    )


def _check_accounted(conn, actor, incident):
    if conn.execute(
        "SELECT id FROM bms.incident_transfers WHERE tenant_id=%s AND incident_id=%s "
        "AND status='pending' LIMIT 1",
        (actor["tenant_id"], incident["id"]),
    ).fetchone():
        raise ApiError(409, "TRANSFER_PENDING")
    rows = conn.execute(
        "SELECT status FROM bms.emergency_rollcall WHERE tenant_id=%s AND incident_id=%s",
        (actor["tenant_id"], incident["id"]),
    ).fetchall()
    if not rows:
        raise ApiError(409, "EMERGENCY_ROSTER_EMPTY")
    if any(row["status"] in {"unaccounted", "needs_assistance"} for row in rows):
        raise ApiError(409, "PEOPLE_UNACCOUNTED")
    if conn.execute(
        "SELECT id FROM bms.emergency_tasks WHERE tenant_id=%s AND incident_id=%s "
        "AND (status='open' OR (safety_critical AND verified_at IS NULL)) LIMIT 1",
        (actor["tenant_id"], incident["id"]),
    ).fetchone():
        raise ApiError(409, "EMERGENCY_TASKS_OPEN")


def _check_reopening(conn, actor, incident):
    _check_accounted(conn, actor, incident)
    if not roster_status(conn, actor, [incident["building_id"]])[0]["current"]:
        raise ApiError(409, "ROSTER_RECONCILIATION_REQUIRED")
    if conn.execute(
        "SELECT f.id FROM bms.findings f JOIN bms.facilities l "
        "ON l.tenant_id=f.tenant_id AND l.id=f.location_id WHERE f.tenant_id=%s "
        "AND l.building_id=%s AND f.severity IN ('critical','high') "
        "AND f.status<>'verified_closed' LIMIT 1",
        (actor["tenant_id"], incident["building_id"]),
    ).fetchone():
        raise ApiError(409, "UNRESOLVED_LIFE_SAFETY_FINDINGS")


def execute_emergency(conn, actor, name, body):
    if actor["role"] not in {"officer", "supervisor", "administrator", "leadership"}:
        raise ApiError(403, "FORBIDDEN")
    if body.offline and name not in {"incident.report", "incident.evidence"}:
        raise ApiError(400, "EMERGENCY_ONLINE_REQUIRED")
    old = replay(conn, actor, name, body)
    if old is not None:
        return old
    now = datetime.now(UTC)
    before = None
    audit_kind = {
        "emergency.settings": "settings",
        "emergency.contact": "contacts",
        "emergency.point": "points",
        "emergency.person": "people",
        "emergency.guardian": "guardians",
        "emergency.shift": "shifts",
        "emergency.handover": "handovers",
        "emergency.handover_accept": "handovers",
        "incident.report": "incidents",
        "incident.responders": "incidents",
        "incident.acknowledge": "incidents",
        "incident.phase": "incidents",
        "incident.evidence": "incidents",
        "drill.plan": "incidents",
        "drill.start": "incidents",
        "rollcall.confirm": "rollCall",
        "incident.task": "tasks",
        "incident.task_complete": "tasks",
        "recovery.save": "recovery",
        "recovery.approve": "recovery",
        "guardian.release": "releases",
    }.get(name)
    if audit_kind:
        prior = conn.execute(
            sql.SQL("SELECT * FROM bms.{} WHERE tenant_id=%s AND id=%s").format(
                sql.Identifier(TABLES[audit_kind])
            ),
            (actor["tenant_id"], body.id),
        ).fetchone()
        before = public(prior) if prior else None
    result = None
    if name == "duty.grant":
        if actor["role"] != "administrator":
            raise ApiError(403, "FORBIDDEN")
        target = get_user(conn, actor["tenant_id"], body.user_id)
        if target["status"] != "active" or (
            body.capability != "roster.manage"
            and target["role"] not in {"administrator", "supervisor", "leadership"}
        ):
            raise ApiError(400, "INVALID_DUTY_HOLDER")
        if body.building_id:
            _building(conn, actor, body.building_id)
        result = _save(
            conn,
            actor,
            "grants",
            body,
            {
                key: getattr(body, key)
                for key in (
                    "user_id",
                    "building_id",
                    "capability",
                    "starts_at",
                    "ends_at",
                    "active",
                    "approval_note",
                )
            }
            | {"approved_by": actor["id"]},
        )
    elif name == "roster.review":
        require_duty(conn, actor, "roster.manage", body.building_id)
        _building(conn, actor, body.building_id)
        total = conn.execute(
            "SELECT count(*) AS total FROM bms.emergency_people "
            "WHERE tenant_id=%s AND building_id=%s AND active",
            (actor["tenant_id"], body.building_id),
        ).fetchone()["total"]
        if total != body.expected_count:
            raise ApiError(409, "ROSTER_COUNT_CONFLICT")
        if body.expected_revision:
            raise ApiError(409, "APPEND_ONLY")
        result = _save(
            conn,
            actor,
            "rosterReviews",
            body,
            {
                "building_id": body.building_id,
                "reviewed_by": actor["id"],
                "expected_count": total,
                "note": body.note,
            },
        )
    elif name in {
        "emergency.settings",
        "emergency.contact",
        "emergency.point",
        "emergency.person",
        "emergency.guardian",
    }:
        if name == "emergency.person":
            require_duty(conn, actor, "roster.manage", body.building_id)
            existing = conn.execute(
                "SELECT building_id FROM bms.emergency_people WHERE tenant_id=%s AND id=%s",
                (actor["tenant_id"], body.id),
            ).fetchone()
            if existing:
                require_duty(conn, actor, "roster.manage", existing["building_id"])
        elif actor["role"] != "administrator":
            raise ApiError(403, "FORBIDDEN")
        values = body.model_dump(
            exclude={
                "id",
                "expected_revision",
                "client_operation_id",
                "device_id",
                "device_occurred_at",
                "offline",
                "approved",
                "approval_note",
            }
        )
        kind = {
            "emergency.settings": "settings",
            "emergency.contact": "contacts",
            "emergency.point": "points",
            "emergency.person": "people",
            "emergency.guardian": "guardians",
        }[name]
        if hasattr(body, "approved"):
            if body.approved:
                approval_building = None
                if name == "emergency.guardian":
                    approval_building = _get(conn, actor, "people", body.person_id)["building_id"]
                require_duty(conn, actor, "safety.approve", approval_building)
            values.update(_approval(actor, body))
        if kind == "people":
            values["maintained_by"] = actor["id"]
        if kind == "settings":
            current = ensure_emergency_settings(conn, actor["tenant_id"])
            if body.id != current["id"]:
                raise ApiError(400, "INVALID_SETTINGS_ID")
        if hasattr(body, "building_id"):
            _building(conn, actor, body.building_id)
        if kind == "guardians":
            person = _get(conn, actor, "people", body.person_id)
            if person["kind"] != "student":
                raise ApiError(400, "GUARDIAN_STUDENT_REQUIRED")
        result = _save(conn, actor, kind, body, values)
        if kind == "people":
            old_building = existing["building_id"] if existing else body.building_id
            incidents = conn.execute(
                "SELECT * FROM bms.emergency_incidents WHERE tenant_id=%s AND building_id=ANY(%s) "
                "AND status IN ('active','recovery')",
                (actor["tenant_id"], list({body.building_id, old_building})),
            ).fetchall()
            for incident in incidents:
                present = conn.execute(
                    "SELECT id FROM bms.emergency_rollcall WHERE tenant_id=%s AND incident_id=%s "
                    "AND person_id=%s",
                    (actor["tenant_id"], incident["id"], body.id),
                ).fetchone()
                if not present and body.active and incident["building_id"] == body.building_id:
                    _add_person(conn, actor, incident, result)
                elif present:
                    conn.execute(
                        "UPDATE bms.emergency_rollcall SET roster_updated_at=now(),"
                        "assistance=%s,expected_presence=%s,away_until=%s,"
                        "status=CASE WHEN status='released' THEN status ELSE 'unaccounted' END,"
                        "note='Roster changed; confirm whereabouts again',revision=revision+1 "
                        "WHERE tenant_id=%s AND incident_id=%s AND person_id=%s",
                        (
                            body.assistance,
                            body.presence,
                            body.away_until,
                            actor["tenant_id"],
                            incident["id"],
                            body.id,
                        ),
                    )
                _invalidate_reopening(conn, actor, incident["id"])
    elif name == "emergency.shift":
        _require_building_lead(conn, actor, body.building_id)
        _building(conn, actor, body.building_id)
        _responder(conn, actor, body.primary_user_id)
        _responder(conn, actor, body.backup_user_id)
        _responder(conn, actor, body.lead_user_id, lead=True, building_id=body.building_id)
        overlaps = conn.execute(
            "SELECT id FROM bms.emergency_shifts WHERE tenant_id=%s AND building_id=%s "
            "AND id<>%s AND starts_at<%s AND ends_at>%s LIMIT 1",
            (actor["tenant_id"], body.building_id, body.id, body.ends_at, body.starts_at),
        ).fetchone()
        if overlaps:
            raise ApiError(409, "DUTY_SHIFT_OVERLAP")
        if conn.execute(
            "SELECT id FROM bms.emergency_shifts WHERE tenant_id=%s AND id<>%s "
            "AND starts_at<%s AND ends_at>%s "
            "AND (primary_user_id=ANY(%s) OR backup_user_id=ANY(%s)) LIMIT 1",
            (
                actor["tenant_id"],
                body.id,
                body.ends_at,
                body.starts_at,
                [body.primary_user_id, body.backup_user_id],
                [body.primary_user_id, body.backup_user_id],
            ),
        ).fetchone():
            raise ApiError(409, "RESPONDER_DOUBLE_BOOKED")
        result = _save(
            conn,
            actor,
            "shifts",
            body,
            {
                key: getattr(body, key)
                for key in (
                    "building_id",
                    "primary_user_id",
                    "backup_user_id",
                    "lead_user_id",
                    "starts_at",
                    "ends_at",
                    "approval_note",
                )
            },
        )
    elif name == "emergency.shift_accept":
        row = _get(conn, actor, "shifts", body.id)
        revision(row, body.expected_revision)
        role = (
            "primary"
            if actor["id"] == row["primary_user_id"]
            else ("backup" if actor["id"] == row["backup_user_id"] else None)
        )
        if role is None or row["ends_at"] <= now:
            raise ApiError(403, "FORBIDDEN")
        _update(conn, actor, "shifts", body.id, {f"{role}_accepted_at": now})
        result = _get(conn, actor, "shifts", body.id)
    elif name == "emergency.handover":
        outgoing = _get(conn, actor, "shifts", body.outgoing_shift_id)
        incoming = _get(conn, actor, "shifts", body.incoming_shift_id)
        if not (
            actor["role"] == "administrator"
            or (
                actor["role"] == "supervisor"
                and supervisor_in_scope(conn, actor, outgoing["building_id"])
            )
        ) and actor["id"] not in (
            outgoing["primary_user_id"],
            outgoing["backup_user_id"],
        ):
            raise ApiError(403, "FORBIDDEN")
        if outgoing["building_id"] != incoming["building_id"] or (
            incoming["starts_at"] != outgoing["ends_at"]
        ):
            raise ApiError(400, "INVALID_HANDOVER")
        if body.expected_revision:
            raise ApiError(409, "HANDOVER_APPEND_ONLY")
        result = _save(
            conn,
            actor,
            "handovers",
            body,
            {
                "outgoing_shift_id": body.outgoing_shift_id,
                "incoming_shift_id": body.incoming_shift_id,
                "note": body.note,
                "created_by": actor["id"],
            },
        )
    elif name == "emergency.handover_accept":
        row = _get(conn, actor, "handovers", body.id)
        incoming = _get(conn, actor, "shifts", row["incoming_shift_id"])
        if actor["id"] != incoming["primary_user_id"]:
            raise ApiError(403, "FORBIDDEN")
        revision(row, body.expected_revision)
        if row["accepted_at"]:
            raise ApiError(409, "INVALID_TRANSITION")
        _update(conn, actor, "shifts", incoming["id"], {"primary_accepted_at": now})
        _update(
            conn,
            actor,
            "handovers",
            body.id,
            {
                "accepted_by": actor["id"],
                "accepted_at": now,
            },
        )
        result = _get(conn, actor, "handovers", body.id)
    elif name in {"incident.report", "drill.plan"}:
        _building(conn, actor, body.building_id)
        if body.expected_revision:
            raise ApiError(409, "INVALID_TRANSITION")
        drill = name == "drill.plan"
        if drill:
            _require_building_lead(conn, actor, body.building_id)
        settings = ensure_emergency_settings(conn, actor["tenant_id"])
        drill_plan = {}
        if drill:
            drill_plan = {
                "nighttime": body.nighttime,
                "communicationsFailure": body.communications_failure,
                "individualAssistance": body.individual_assistance,
                "safeExercisePlan": body.safe_exercise_plan,
                "approvalNote": body.approval_note,
                "approvedBy": str(actor["id"]),
                "approvedAt": now.isoformat(),
            }
        result = _save(
            conn,
            actor,
            "incidents",
            body,
            {
                "building_id": body.building_id,
                "title": body.title,
                "hazard": body.hazard,
                "location_note": body.location_note,
                "is_drill": drill,
                "status": "planned" if drill else "active",
                "reported_by": actor["id"],
                "reported_at": body.reported_at,
                "planned_at": body.planned_at if drill else None,
                "drill_plan": Jsonb(drill_plan),
                "acknowledgement_minutes": settings["acknowledgement_minutes"],
            },
        )
        if not drill:
            result = _activate(conn, actor, result, now)
            process_emergency_escalations(conn, actor["tenant_id"], now)
    elif name == "drill.start":
        _require_lead(actor)
        row = incident_access(conn, actor, body.id)
        _require_incident_lead(conn, actor, row)
        revision(row, body.expected_revision)
        if not row["is_drill"] or row["status"] != "planned":
            raise ApiError(409, "INVALID_TRANSITION")
        result = _activate(conn, actor, row, now)
        process_emergency_escalations(conn, actor["tenant_id"], now)
    elif name == "incident.responders":
        _require_lead(actor)
        row = incident_access(conn, actor, body.id)
        _require_incident_lead(conn, actor, row)
        revision(row, body.expected_revision)
        if row["status"] != "active" or (
            row["response_owner_id"] and body.primary_user_id != row["primary_user_id"]
        ):
            raise ApiError(409, "INVALID_TRANSITION")
        _responder(conn, actor, body.primary_user_id)
        _responder(conn, actor, body.backup_user_id)
        _responder(conn, actor, body.lead_user_id, lead=True, building_id=row["building_id"])
        _update(
            conn,
            actor,
            "incidents",
            body.id,
            {
                "primary_user_id": body.primary_user_id,
                "backup_user_id": body.backup_user_id,
                "lead_user_id": body.lead_user_id,
                "notification_round": row["notification_round"] + 1,
            },
        )
        conn.execute(
            "UPDATE bms.emergency_alerts SET status='cancelled',revision=revision+1,"
            "updated_at=now() "
            "WHERE tenant_id=%s AND incident_id=%s AND status IN ('queued','available_in_app')",
            (actor["tenant_id"], body.id),
        )
        result = _get(conn, actor, "incidents", body.id)
        if not row["response_owner_id"]:
            _new_alert(conn, actor, result, "primary", now)
    elif name == "incident.acknowledge":
        row = incident_access(conn, actor, body.id)
        _active(row)
        revision(row, body.expected_revision)
        if row["acknowledged_at"]:
            raise ApiError(409, "INCIDENT_ALREADY_ACKNOWLEDGED")
        alert = conn.execute(
            "SELECT * FROM bms.emergency_alerts WHERE tenant_id=%s AND incident_id=%s "
            "AND recipient_id=%s AND notification_round=%s AND status<>'cancelled' "
            "ORDER BY created_at LIMIT 1",
            (actor["tenant_id"], body.id, actor["id"], row["notification_round"]),
        ).fetchone()
        if not alert:
            raise ApiError(403, "FORBIDDEN")
        _update(
            conn,
            actor,
            "incidents",
            body.id,
            {
                "acknowledged_by": actor["id"],
                "acknowledged_at": now,
            },
        )
        _update(
            conn,
            actor,
            "alerts",
            alert["id"],
            {
                "status": "acknowledged",
                "acknowledged_at": now,
            },
        )
        result = _get(conn, actor, "incidents", body.id)
    elif name in {"incident.accept", "incident.progress"}:
        row = incident_access(conn, actor, body.id)
        _active(row)
        revision(row, body.expected_revision)
        if name == "incident.accept":
            if row["response_owner_id"]:
                raise ApiError(409, "RESPONSIBILITY_TRANSFER_REQUIRED")
            alert = conn.execute(
                "SELECT id FROM bms.emergency_alerts WHERE tenant_id=%s AND incident_id=%s "
                "AND recipient_id=%s AND notification_round=%s AND status<>'cancelled'",
                (actor["tenant_id"], body.id, actor["id"], row["notification_round"]),
            ).fetchone()
            if not alert and not can_lead(conn, actor, row):
                raise ApiError(403, "FORBIDDEN")
        elif row["response_owner_id"] != actor["id"]:
            raise ApiError(403, "RESPONSE_OWNER_REQUIRED")
        _record_progress(
            conn,
            actor,
            row,
            "accepted" if name == "incident.accept" else "progress",
            body.note,
            now,
        )
        result = _get(conn, actor, "incidents", body.id)
    elif name == "incident.transfer":
        row = incident_access(conn, actor, body.id)
        _active(row)
        revision(row, body.expected_revision)
        if not can_lead(conn, actor, row) and row["response_owner_id"] != actor["id"]:
            raise ApiError(403, "FORBIDDEN")
        incoming = get_user(conn, actor["tenant_id"], body.incoming_user_id)
        if (
            incoming["status"] != "active"
            or (
                incoming["role"] not in {"officer", "supervisor"}
                and not can_lead(conn, incoming, row)
            )
            or incoming["id"] == row["response_owner_id"]
        ):
            raise ApiError(400, "INVALID_EMERGENCY_RESPONDER")
        if conn.execute(
            "SELECT id FROM bms.incident_transfers WHERE tenant_id=%s AND incident_id=%s "
            "AND status='pending'",
            (actor["tenant_id"], body.id),
        ).fetchone():
            raise ApiError(409, "TRANSFER_PENDING")
        result = _save(
            conn,
            actor,
            "transfers",
            RevisionNote(id=uuid4(), client_operation_id=body.client_operation_id, note=body.note),
            {
                "incident_id": row["id"],
                "outgoing_user_id": row["response_owner_id"],
                "incoming_user_id": body.incoming_user_id,
                "requested_by": actor["id"],
                "note": body.note,
            },
        )
    elif name in {
        "incident.transfer_accept",
        "incident.transfer_decline",
        "incident.transfer_cancel",
    }:
        transfer = _get(conn, actor, "transfers", body.id)
        revision(transfer, body.expected_revision)
        row = incident_access(conn, actor, transfer["incident_id"])
        _active(row)
        cancel = name == "incident.transfer_cancel"
        permitted = (
            can_lead(conn, actor, row) or transfer["requested_by"] == actor["id"]
            if cancel
            else transfer["incoming_user_id"] == actor["id"]
        )
        if not permitted or transfer["status"] != "pending":
            raise ApiError(403, "FORBIDDEN")
        if transfer["outgoing_user_id"] != row["response_owner_id"]:
            raise ApiError(409, "TRANSFER_OWNER_CHANGED")
        accepted = name == "incident.transfer_accept"
        _update(
            conn,
            actor,
            "transfers",
            body.id,
            {
                "status": "accepted" if accepted else "cancelled" if cancel else "declined",
                "decided_at": now,
                "decision_note": body.note,
            },
        )
        if accepted:
            _record_progress(conn, actor, row, "transfer", body.note, now)
        result = _get(conn, actor, "transfers", body.id)
    elif name == "incident.contact":
        row = incident_access(conn, actor, body.incident_id)
        _active(row)
        if not can_account(conn, actor, row):
            raise ApiError(403, "FORBIDDEN")
        get_user(conn, actor["tenant_id"], body.recipient_user_id)
        if body.expected_revision or body.occurred_at > now + timedelta(seconds=5):
            raise ApiError(400, "INVALID_CONTACT_RECORD")
        result = _save(
            conn,
            actor,
            "contactAttempts",
            body,
            {
                key: getattr(body, key)
                for key in (
                    "incident_id",
                    "recipient_user_id",
                    "channel",
                    "outcome",
                    "occurred_at",
                    "note",
                )
            }
            | {"recorded_by": actor["id"]},
        )
    elif name == "incident.phase":
        _require_lead(actor)
        row = incident_access(conn, actor, body.id)
        _require_incident_lead(conn, actor, row)
        revision(row, body.expected_revision)
        _active(row)
        if body.status == "closed":
            if row["status"] != "recovery":
                raise ApiError(409, "RECOVERY_REQUIRED")
            if row["is_drill"]:
                _check_accounted(conn, actor, row)
            else:
                _check_reopening(conn, actor, row)
                recovery = conn.execute(
                    "SELECT id FROM bms.emergency_recovery WHERE tenant_id=%s AND incident_id=%s "
                    "AND approved_at IS NOT NULL",
                    (actor["tenant_id"], body.id),
                ).fetchone()
                if not recovery:
                    raise ApiError(409, "REOPENING_APPROVAL_REQUIRED")
        elif row["status"] != "active":
            raise ApiError(409, "INVALID_TRANSITION")
        _update(
            conn,
            actor,
            "incidents",
            body.id,
            {
                "status": body.status,
                "phase_note": body.note,
            },
        )
        conn.execute(
            "UPDATE bms.emergency_alerts SET status='cancelled',updated_at=now(),"
            "revision=revision+1 WHERE tenant_id=%s AND incident_id=%s "
            "AND status IN ('queued','available_in_app')",
            (actor["tenant_id"], body.id),
        )
        result = _get(conn, actor, "incidents", body.id)
    elif name == "incident.evidence":
        row = incident_access(conn, actor, body.id)
        _active(row)
        from bms.workflow_operations import register_evidence

        register_evidence(conn, actor, "incident", body.id, body.evidence)
        result = dict(row)
        result["evidence_ids"] = [str(item.id) for item in body.evidence]
    elif name == "rollcall.confirm":
        row = _get(conn, actor, "rollCall", body.id)
        incident = incident_access(conn, actor, row["incident_id"], account=True)
        _active(incident)
        revision(row, body.expected_revision)
        if row["status"] == "released":
            raise ApiError(409, "PERSON_ALREADY_RELEASED")
        point_id = body.point_id if body.status == "at_assembly" else None
        if point_id:
            point = _get(conn, actor, "points", point_id)
            if not point["active"] or point["building_id"] != incident["building_id"]:
                raise ApiError(400, "INVALID_ASSEMBLY_POINT")
        _update(
            conn,
            actor,
            "rollCall",
            body.id,
            {
                "status": body.status,
                "point_id": point_id,
                "note": body.note,
                "last_confirmed_at": now,
                "confirmed_by": actor["id"],
            },
        )
        if body.status in {"unaccounted", "needs_assistance"}:
            _invalidate_reopening(conn, actor, incident["id"])
        result = _get(conn, actor, "rollCall", body.id)
    elif name == "incident.task":
        _require_lead(actor)
        incident = incident_access(conn, actor, body.incident_id)
        _require_incident_lead(conn, actor, incident)
        _active(incident)
        owner = get_user(conn, actor["tenant_id"], body.owner_id)
        if owner["status"] != "active" or owner["role"] == "leadership":
            raise ApiError(400, "INVALID_OWNER")
        result = _save(
            conn,
            actor,
            "tasks",
            body,
            {
                key: getattr(body, key)
                for key in (
                    "incident_id",
                    "title",
                    "owner_id",
                    "due_at",
                    "observation",
                    "safety_critical",
                )
            },
        )
        _invalidate_reopening(conn, actor, body.incident_id)
    elif name == "incident.task_complete":
        row = _get(conn, actor, "tasks", body.id)
        incident = incident_access(conn, actor, row["incident_id"])
        _active(incident)
        revision(row, body.expected_revision)
        if actor["id"] != row["owner_id"]:
            raise ApiError(403, "FORBIDDEN")
        if row["status"] != "open":
            raise ApiError(409, "INVALID_TRANSITION")
        _update(
            conn,
            actor,
            "tasks",
            body.id,
            {
                "status": "completed",
                "completion_note": body.note,
                "completed_by": actor["id"],
                "completed_at": now,
            },
        )
        result = _get(conn, actor, "tasks", body.id)
    elif name in {"incident.task_verify", "incident.task_reject"}:
        row = _get(conn, actor, "tasks", body.id)
        incident = incident_access(conn, actor, row["incident_id"])
        _active(incident)
        require_duty(conn, actor, "safety.verify", incident["building_id"])
        revision(row, body.expected_revision)
        if row["status"] != "completed" or actor["id"] == row["completed_by"]:
            raise ApiError(409, "INDEPENDENT_VERIFIER_REQUIRED")
        reject = name == "incident.task_reject"
        _update(
            conn,
            actor,
            "tasks",
            body.id,
            {
                "status": "open" if reject else "completed",
                "verification_note": body.note,
                "verified_by": None if reject else actor["id"],
                "verified_at": None if reject else now,
            },
        )
        _invalidate_reopening(conn, actor, incident["id"])
        result = _get(conn, actor, "tasks", body.id)
    elif name == "recovery.save":
        _require_lead(actor)
        incident = incident_access(conn, actor, body.incident_id)
        _require_incident_lead(conn, actor, incident)
        _active(incident)
        if incident["is_drill"]:
            raise ApiError(409, "DRILL_NOT_REAL_RECOVERY")
        result = _save(
            conn,
            actor,
            "recovery",
            body,
            {
                "incident_id": body.incident_id,
                "accommodation": body.accommodation,
                "welfare": body.welfare,
                "learning_continuity": body.learning_continuity,
                "approved_by": None,
                "approved_at": None,
                "reopening_note": "",
                "authored_by": actor["id"],
            },
        )
    elif name == "recovery.approve":
        _require_lead(actor)
        row = _get(conn, actor, "recovery", body.id)
        incident = incident_access(conn, actor, row["incident_id"])
        require_duty(conn, actor, "safety.approve", incident["building_id"])
        revision(row, body.expected_revision)
        if incident["is_drill"] or incident["status"] != "recovery":
            raise ApiError(409, "RECOVERY_REQUIRED")
        _check_reopening(conn, actor, incident)
        if not row["authored_by"] or row["authored_by"] == actor["id"]:
            raise ApiError(409, "INDEPENDENT_APPROVER_REQUIRED")
        _update(
            conn,
            actor,
            "recovery",
            body.id,
            {
                "approved_by": actor["id"],
                "approved_at": now,
                "reopening_note": body.note,
            },
        )
        result = _get(conn, actor, "recovery", body.id)
    elif name == "guardian.release":
        _require_lead(actor)
        incident = incident_access(conn, actor, body.incident_id)
        _require_incident_lead(conn, actor, incident)
        if incident["is_drill"] or incident["status"] != "recovery":
            raise ApiError(409, "RECOVERY_REQUIRED")
        guardian = _get(conn, actor, "guardians", body.guardian_id)
        if (
            not guardian["active"]
            or not guardian["approved_at"]
            or (guardian["person_id"] != body.person_id)
        ):
            raise ApiError(403, "GUARDIAN_NOT_AUTHORISED")
        roll = conn.execute(
            "SELECT * FROM bms.emergency_rollcall WHERE tenant_id=%s AND incident_id=%s "
            "AND person_id=%s",
            (actor["tenant_id"], body.incident_id, body.person_id),
        ).fetchone()
        if not roll or roll["status"] != "at_assembly" or roll["person_kind"] != "student":
            raise ApiError(409, "PERSON_NOT_READY_FOR_HANDOVER")
        result = _save(
            conn,
            actor,
            "releases",
            body,
            {
                "incident_id": body.incident_id,
                "person_id": body.person_id,
                "guardian_id": body.guardian_id,
                "guardian_name": guardian["name"],
                "person_name": roll["person_name"],
                "verification_note": body.verification_note,
                "released_by": actor["id"],
            },
        )
        _update(
            conn,
            actor,
            "rollCall",
            roll["id"],
            {
                "status": "released",
                "last_confirmed_at": now,
                "confirmed_by": actor["id"],
                "note": "Authorised guardian handover recorded",
            },
        )
    else:
        raise ApiError(400, "UNKNOWN_COMMAND")
    result = public(result)
    audit(
        conn,
        actor,
        name,
        body.id,
        body,
        before=before,
        after=result,
        reason=getattr(body, "note", None),
    )
    return remember(conn, actor, name, body, result)


def emergency_snapshot(conn, actor):
    if actor["role"] not in {"officer", "supervisor", "administrator", "leadership"}:
        raise ApiError(403, "FORBIDDEN")
    lock_tenant(conn, actor["tenant_id"])
    process_emergency_escalations(conn, actor["tenant_id"])
    settings = ensure_emergency_settings(conn, actor["tenant_id"])
    buildings = conn.execute(
        "SELECT id,name,status FROM bms.facilities WHERE tenant_id=%s AND level='building' "
        "ORDER BY name,id",
        (actor["tenant_id"],),
    ).fetchall()
    allowed_buildings = {row["id"] for row in buildings if _scoped_building(conn, actor, row["id"])}
    incidents = conn.execute(
        "SELECT i.* FROM bms.emergency_incidents i WHERE i.tenant_id=%s AND "
        "(%s OR i.building_id=ANY(%s) OR i.reported_by=%s OR i.primary_user_id=%s "
        "OR i.backup_user_id=%s OR i.lead_user_id=%s OR i.response_owner_id=%s OR EXISTS "
        "(SELECT 1 FROM bms.emergency_tasks t "
        "WHERE t.tenant_id=i.tenant_id AND t.incident_id=i.id AND t.owner_id=%s) OR EXISTS "
        "(SELECT 1 FROM bms.duty_grants g WHERE g.tenant_id=i.tenant_id AND g.user_id=%s "
        "AND g.active AND g.starts_at<=now() AND g.ends_at>now() "
        "AND (g.building_id IS NULL OR g.building_id=i.building_id)) OR EXISTS "
        "(SELECT 1 FROM bms.incident_transfers t WHERE t.tenant_id=i.tenant_id "
        "AND t.incident_id=i.id AND t.incoming_user_id=%s AND t.status='pending')) "
        "ORDER BY i.created_at DESC,i.id",
        (
            actor["tenant_id"],
            actor["role"] in {"administrator", "leadership"},
            list(allowed_buildings),
            actor["id"],
            actor["id"],
            actor["id"],
            actor["id"],
            actor["id"],
            actor["id"],
            actor["id"],
            actor["id"],
        ),
    ).fetchall()
    ids = [row["id"] for row in incidents]
    account_ids = [row["id"] for row in incidents if can_account(conn, actor, row)]
    lead_ids = [row["id"] for row in incidents if can_lead(conn, actor, row)]
    approval_ids = [
        row["id"]
        for row in incidents
        if has_duty(conn, actor, "safety.approve", row["building_id"])
    ]
    verify_ids = [
        row["id"] for row in incidents if has_duty(conn, actor, "safety.verify", row["building_id"])
    ]
    conn.execute(
        "UPDATE bms.emergency_alerts SET status='available_in_app',available_at=now(),"
        "updated_at=now(),revision=revision+1 WHERE tenant_id=%s AND recipient_id=%s "
        "AND incident_id=ANY(%s) AND status='queued'",
        (actor["tenant_id"], actor["id"], ids),
    )
    result = {
        "serverTime": datetime.now(UTC).isoformat(),
        "notificationMode": "in_app_prototype",
        "settings": public(settings),
        "buildings": [public(row) for row in buildings],
        "incidents": [
            {
                **public(row),
                "canLead": can_lead(conn, actor, row),
                "canAccount": can_account(conn, actor, row),
                "canApprove": row["id"] in approval_ids,
                "canVerify": row["id"] in verify_ids,
                "accountability": {
                    status: conn.execute(
                        "SELECT count(*) AS total FROM bms.emergency_rollcall "
                        "WHERE tenant_id=%s AND incident_id=%s AND status=%s",
                        (actor["tenant_id"], row["id"], status),
                    ).fetchone()["total"]
                    for status in (
                        "unaccounted",
                        "needs_assistance",
                        "at_assembly",
                        "confirmed_offsite",
                        "released",
                    )
                },
            }
            for row in incidents
        ],
    }
    for kind in ("contacts", "points", "people", "guardians", "shifts", "handovers"):
        restriction, params = "", [actor["tenant_id"]]
        if kind == "contacts" and actor["role"] != "administrator":
            restriction = " AND active AND approved_at IS NOT NULL"
        elif kind == "people":
            restriction = " AND building_id=ANY(%s)"
            params.append(list(allowed_buildings))
        elif kind == "guardians" and actor["role"] != "administrator":
            restriction = (
                " AND (person_id IN (SELECT id FROM bms.emergency_people WHERE tenant_id=%s "
                "AND building_id=ANY(%s)) OR person_id IN "
                "(SELECT person_id FROM bms.emergency_rollcall WHERE tenant_id=%s "
                "AND incident_id=ANY(%s)))"
            )
            command_buildings = [
                row["id"]
                for row in buildings
                if has_duty(conn, actor, "incident.command", row["id"])
            ]
            params += [actor["tenant_id"], command_buildings, actor["tenant_id"], lead_ids]
        elif kind == "shifts" and actor["role"] != "administrator":
            restriction = (
                " AND (primary_user_id=%s OR backup_user_id=%s OR lead_user_id=%s "
                "OR building_id=ANY(%s))"
            )
            params += [actor["id"], actor["id"], actor["id"], list(allowed_buildings)]
        elif kind == "handovers" and actor["role"] != "administrator":
            restriction = (
                " AND (created_by=%s OR incoming_shift_id IN "
                "(SELECT id FROM bms.emergency_shifts WHERE tenant_id=%s AND "
                "(primary_user_id=%s OR lead_user_id=%s OR building_id=ANY(%s))))"
            )
            params += [
                actor["id"],
                actor["tenant_id"],
                actor["id"],
                actor["id"],
                list(allowed_buildings),
            ]
        rows = conn.execute(
            sql.SQL(
                "SELECT * FROM bms.{} WHERE tenant_id=%s" + restriction + " ORDER BY created_at,id"
            ).format(sql.Identifier(TABLES[kind])),
            params,
        ).fetchall()
        result[kind] = [public(row) for row in rows]
    for kind in ("alerts", "rollCall", "tasks", "recovery", "releases"):
        visible_ids = account_ids if kind == "rollCall" else ids
        if kind in {"recovery", "releases"}:
            visible_ids = list(set(lead_ids + approval_ids)) if kind == "recovery" else lead_ids
        rows = conn.execute(
            sql.SQL(
                "SELECT * FROM bms.{} WHERE tenant_id=%s AND incident_id=ANY(%s) "
                "ORDER BY created_at,id"
            ).format(sql.Identifier(TABLES[kind])),
            (actor["tenant_id"], visible_ids),
        ).fetchall()
        result[kind] = [public(row) for row in rows]
    evidence = conn.execute(
        "SELECT * FROM bms.evidence WHERE tenant_id=%s AND owner_type='incident' "
        "AND owner_id=ANY(%s)",
        (actor["tenant_id"], ids),
    ).fetchall()
    result["evidence"] = [public(row) for row in evidence]
    if actor["role"] == "leadership":
        result["evidence"] = [
            row
            for row in result["evidence"]
            if row["ownerId"] in {str(value) for value in account_ids}
        ]
    for kind in ("transfers", "progress", "contactAttempts"):
        rows = conn.execute(
            sql.SQL(
                "SELECT * FROM bms.{} WHERE tenant_id=%s AND incident_id=ANY(%s) "
                "ORDER BY created_at,id"
            ).format(sql.Identifier(TABLES[kind])),
            (actor["tenant_id"], list(set(account_ids + lead_ids)) if kind != "transfers" else ids),
        ).fetchall()
        result[kind] = [public(row) for row in rows]
    grants = conn.execute(
        "SELECT * FROM bms.duty_grants WHERE tenant_id=%s AND (%s OR user_id=%s) "
        "ORDER BY created_at,id",
        (actor["tenant_id"], actor["role"] == "administrator", actor["id"]),
    ).fetchall()
    result["grants"] = [public(row) for row in grants]
    result["rosterReviews"] = roster_status(conn, actor, allowed_buildings)
    result["coverage"] = coverage(
        conn,
        actor,
        {
            row["id"]
            for row in buildings
            if actor["role"] in {"administrator", "leadership"} or row["id"] in allowed_buildings
        },
    )
    staff = conn.execute(
        "SELECT id,name,role,status FROM bms.users WHERE tenant_id=%s AND "
        "(%s OR id=%s OR id=ANY(%s)) ORDER BY name,id",
        (
            actor["tenant_id"],
            _lead(actor) or actor["role"] == "leadership",
            actor["id"],
            list(
                {
                    value
                    for row in incidents
                    for value in (
                        row["lead_user_id"],
                        row["primary_user_id"],
                        row["backup_user_id"],
                    )
                    if value
                }
            ),
        ),
    ).fetchall()
    result["staff"] = [public(row) for row in staff]
    return result
