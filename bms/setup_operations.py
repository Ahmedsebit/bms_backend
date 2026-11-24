import secrets
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from bms.operation_access import (
    facility_access,
    public,
    require,
    revision,
    row_by_id,
    supervisor_in_scope,
)
from bms.scheduling import schedule_school
from bms.security import CAPABILITIES
from bms.services import ApiError, audit, get_user, remember, replay


def create_facility(conn, actor, body):
    require(actor, "setup.manage")
    old = replay(conn, actor, "facility.create", body)
    if old is not None:
        return old
    if body.level == "building":
        if body.parent_id is not None or body.location_type is not None:
            raise ApiError(400, "VALIDATION_FAILED")
        building_id = body.id
    else:
        parent = row_by_id(conn, actor, "facilities", body.parent_id)
        expected = "building" if body.level == "zone" else "zone"
        if parent["level"] != expected or parent["status"] != "active":
            raise ApiError(400, "INVALID_PARENT")
        if (body.level == "location") != (body.location_type is not None):
            raise ApiError(400, "VALIDATION_FAILED")
        building_id = parent["building_id"]
    conn.execute(
        "INSERT INTO bms.facilities "
        "(id,tenant_id,parent_id,building_id,level,name,location_type) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            body.id,
            actor["tenant_id"],
            body.parent_id,
            building_id,
            body.level,
            body.name,
            body.location_type,
        ),
    )
    if body.level == "location":
        conn.execute(
            "INSERT INTO bms.qr_tokens(id,tenant_id,location_id,token) VALUES (%s,%s,%s,%s)",
            (uuid4(), actor["tenant_id"], body.id, secrets.token_urlsafe(32)),
        )
    result = public(row_by_id(conn, actor, "facilities", body.id))
    audit(conn, actor, "facility.create", body.id, body, after=result)
    return remember(conn, actor, "facility.create", body, result)


def update_facility(conn, actor, entity_id, body):
    before = row_by_id(conn, actor, "facilities", entity_id)
    require(actor, "setup.manage")
    command = f"facility.update:{entity_id}"
    old = replay(conn, actor, command, body)
    if old is not None:
        return old
    revision(before, body.expected_revision)
    if body.status == "active" and before["parent_id"]:
        parent = row_by_id(conn, actor, "facilities", before["parent_id"])
        building = row_by_id(conn, actor, "facilities", before["building_id"])
        if parent["status"] != "active" or building["status"] != "active":
            raise ApiError(400, "INVALID_PARENT")
    conn.execute(
        "UPDATE bms.facilities SET name=%s,status=%s,revision=revision+1,updated_at=now() "
        "WHERE tenant_id=%s AND id=%s",
        (body.name, body.status, actor["tenant_id"], entity_id),
    )
    result = public(row_by_id(conn, actor, "facilities", entity_id))
    audit(conn, actor, "facility.update", entity_id, body, public(before), result, body.reason)
    return remember(conn, actor, command, body, result)


def set_responsibility(conn, actor, zone_id, body):
    zone = row_by_id(conn, actor, "facilities", zone_id)
    require(actor, "setup.manage")
    command = f"responsibility.set:{zone_id}"
    old = replay(conn, actor, command, body)
    if old is not None:
        return old
    if zone["level"] != "zone" or zone["status"] != "active":
        raise ApiError(400, "ZONE_REQUIRED")
    for user_id in (body.primary_user_id, body.backup_user_id):
        if user_id is not None:
            user = get_user(conn, actor["tenant_id"], user_id)
            if user["status"] != "active" or "inspection.execute" not in CAPABILITIES[user["role"]]:
                raise ApiError(400, "INVALID_PERFORMER")
            if user["role"] == "supervisor" and not supervisor_in_scope(
                conn, user, zone["building_id"]
            ):
                raise ApiError(400, "INVALID_PERFORMER")
    if body.primary_user_id == body.backup_user_id:
        raise ApiError(400, "INVALID_PERFORMER")
    before = conn.execute(
        "SELECT * FROM bms.responsibility_assignments "
        "WHERE tenant_id=%s AND zone_id=%s AND valid_to IS NULL",
        (actor["tenant_id"], zone_id),
    ).fetchone()
    if body.expected_revision != (before["revision"] if before else 0):
        raise ApiError(409, "REVISION_CONFLICT", public(before) if before else {})
    if before:
        conn.execute(
            "UPDATE bms.responsibility_assignments SET valid_to=now(),updated_at=now(),"
            "revision=revision+1 WHERE tenant_id=%s AND id=%s",
            (actor["tenant_id"], before["id"]),
        )
    entity_id = uuid4()
    conn.execute(
        "INSERT INTO bms.responsibility_assignments "
        "(id,tenant_id,zone_id,primary_user_id,backup_user_id) VALUES (%s,%s,%s,%s,%s)",
        (entity_id, actor["tenant_id"], zone_id, body.primary_user_id, body.backup_user_id),
    )
    result = public(row_by_id(conn, actor, "responsibility_assignments", entity_id))
    audit(
        conn, actor, "responsibility.set", zone_id, body, public(before) if before else None, result
    )
    return remember(conn, actor, command, body, result)


def set_supervisor_scope(conn, actor, user_id, body):
    target = get_user(conn, actor["tenant_id"], user_id)
    require(actor, "setup.manage")
    command = f"scope.set:{user_id}"
    old = replay(conn, actor, command, body)
    if old is not None:
        return old
    if target["role"] != "supervisor":
        raise ApiError(400, "SUPERVISOR_REQUIRED")
    revision(target, body.expected_revision)
    for building_id in body.building_ids:
        building = row_by_id(conn, actor, "facilities", building_id)
        if building["level"] != "building":
            raise ApiError(400, "BUILDING_REQUIRED")
    conn.execute(
        "UPDATE bms.user_scopes SET active=false,revision=revision+1,updated_at=now() "
        "WHERE tenant_id=%s AND user_id=%s AND active",
        (actor["tenant_id"], user_id),
    )
    for building_id in set(body.building_ids):
        conn.execute(
            "INSERT INTO bms.user_scopes(id,tenant_id,user_id,building_id) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (tenant_id,user_id,building_id) DO UPDATE "
            "SET active=true,revision=bms.user_scopes.revision+1,updated_at=now()",
            (uuid4(), actor["tenant_id"], user_id, building_id),
        )
    conn.execute(
        "UPDATE bms.users SET revision=revision+1,updated_at=now() WHERE tenant_id=%s AND id=%s",
        (actor["tenant_id"], user_id),
    )
    result = {
        "buildingIds": [str(value) for value in body.building_ids],
        "revision": target["revision"] + 1,
    }
    audit(conn, actor, "scope.set", user_id, body, after=result)
    return remember(conn, actor, command, body, result)


def save_programme(conn, actor, body, *, update=False):
    from bms.duty_operations import require_duty

    before = row_by_id(conn, actor, "programmes", body.id) if update else None
    require(actor, "setup.manage")
    command = f"programme.{'update' if update else 'create'}:{body.id}"
    old = replay(conn, actor, command, body)
    if old is not None:
        return old
    if before:
        revision(before, body.expected_revision)
        if before["sensor_id"]:
            raise ApiError(409, "SENSOR_MANAGED_PROGRAMME")
    for location_id in body.location_ids:
        location = row_by_id(conn, actor, "facilities", location_id)
        if location["level"] != "location":
            raise ApiError(400, "LOCATION_REQUIRED")
        if body.approved:
            require_duty(conn, actor, "safety.approve", location["building_id"])
        if body.enabled:
            facility_access(conn, actor, location_id, active=True)
            primary = conn.execute(
                "SELECT r.id FROM bms.responsibility_assignments r "
                "JOIN bms.users u ON u.tenant_id=r.tenant_id AND u.id=r.primary_user_id "
                "WHERE r.tenant_id=%s AND r.zone_id=%s AND r.valid_to IS NULL "
                "AND u.status='active'",
                (actor["tenant_id"], location["parent_id"]),
            ).fetchone()
            if not primary and body.assignment_mode == "zone":
                raise ApiError(400, "PRIMARY_REQUIRED")
    version_number = before["current_version"] + 1 if before else 1
    if before:
        conn.execute(
            "UPDATE bms.programmes SET name=%s,recurrence=%s,start_time=%s,end_time=%s,"
            "grace_minutes=%s,life_safety=%s,enabled=%s,current_version=%s,assignment_mode=%s,"
            "updated_at=now(),revision=revision+1 WHERE tenant_id=%s AND id=%s",
            (
                body.name,
                body.recurrence,
                body.start_time,
                body.end_time,
                body.grace_minutes,
                body.life_safety,
                body.enabled,
                version_number,
                body.assignment_mode,
                actor["tenant_id"],
                body.id,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO bms.programmes(id,tenant_id,name,recurrence,start_time,end_time,"
            "grace_minutes,life_safety,enabled,assignment_mode) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                body.id,
                actor["tenant_id"],
                body.name,
                body.recurrence,
                body.start_time,
                body.end_time,
                body.grace_minutes,
                body.life_safety,
                body.enabled,
                body.assignment_mode,
            ),
        )
    version_id = uuid4()
    conn.execute(
        "INSERT INTO bms.programme_versions(id,tenant_id,programme_id,version_number,items,"
        "approved_by,approved_at,approval_note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            version_id,
            actor["tenant_id"],
            body.id,
            version_number,
            Jsonb([item.model_dump(mode="json", by_alias=True) for item in body.items]),
            actor["id"] if body.approved else None,
            datetime.now(UTC) if body.approved else None,
            body.approval_note,
        ),
    )
    conn.execute(
        "UPDATE bms.programme_scopes SET active=false,updated_at=now(),revision=revision+1 "
        "WHERE tenant_id=%s AND programme_id=%s AND active",
        (actor["tenant_id"], body.id),
    )
    for location_id in body.location_ids:
        conn.execute(
            "INSERT INTO bms.programme_scopes(id,tenant_id,programme_id,location_id) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (tenant_id,programme_id,location_id) "
            "DO UPDATE SET active=true,updated_at=now(),revision=bms.programme_scopes.revision+1",
            (uuid4(), actor["tenant_id"], body.id, location_id),
        )
    result = public(row_by_id(conn, actor, "programmes", body.id))
    audit(
        conn,
        actor,
        "programme.update" if before else "programme.create",
        body.id,
        body,
        public(before) if before else None,
        result,
        getattr(body, "reason", None),
    )
    schedule_school(conn, actor["tenant_id"])
    return remember(conn, actor, command, body, result)
