import math
import secrets
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from bms.operation_access import facility_access, public, require, revision, row_by_id
from bms.schemas import Command
from bms.services import ApiError, audit, remember, replay


def ensure_settings(conn, tenant):
    conn.execute(
        "INSERT INTO bms.school_operations_settings(id,tenant_id) VALUES (%s,%s) "
        "ON CONFLICT (tenant_id) DO NOTHING",
        (uuid4(), tenant),
    )
    return conn.execute(
        "SELECT * FROM bms.school_operations_settings WHERE tenant_id=%s", (tenant,)
    ).fetchone()


def save_settings(conn, actor, body):
    require(actor, "setup.manage")
    key = "daily_settings.update"
    old = replay(conn, actor, key, body)
    if old is not None:
        return old
    before = ensure_settings(conn, actor["tenant_id"])
    revision(before, body.expected_revision)
    conn.execute(
        "UPDATE bms.school_operations_settings SET supervisor_daily_percentage=%s,"
        "revision=revision+1,updated_at=now() WHERE tenant_id=%s",
        (body.supervisor_daily_percentage, actor["tenant_id"]),
    )
    result = public(ensure_settings(conn, actor["tenant_id"]))
    audit(conn, actor, key, before["id"], body, before=public(before), after=result)
    return remember(conn, actor, key, body, result)


def sampled_dorms(conn, tenant, day):
    config = ensure_settings(conn, tenant)
    buildings = conn.execute(
        "SELECT id FROM bms.facilities WHERE tenant_id=%s AND level='building' "
        "AND status='active' ORDER BY id",
        (tenant,),
    ).fetchall()
    supervisors = conn.execute(
        "SELECT id FROM bms.users WHERE tenant_id=%s AND role='supervisor' AND status='active'",
        (tenant,),
    ).fetchall()
    result = {}
    for user in supervisors:
        sample = conn.execute(
            "SELECT id FROM bms.supervisor_daily_samples WHERE tenant_id=%s "
            "AND supervisor_user_id=%s AND local_date=%s",
            (tenant, user["id"], day),
        ).fetchone()
        if not sample:
            eligible = conn.execute(
                "SELECT DISTINCT b.id FROM bms.facilities b "
                "JOIN bms.facilities l ON l.tenant_id=b.tenant_id AND l.building_id=b.id "
                "AND l.level='location' AND l.status='active' "
                "JOIN bms.facilities z ON z.tenant_id=l.tenant_id AND z.id=l.parent_id "
                "AND z.status='active' "
                "JOIN bms.programme_scopes ps ON ps.tenant_id=l.tenant_id AND ps.location_id=l.id "
                "AND ps.active JOIN bms.programmes p ON p.tenant_id=ps.tenant_id "
                "AND p.id=ps.programme_id AND p.enabled AND p.assignment_mode='supervisor_random' "
                "WHERE b.tenant_id=%s AND b.level='building' AND b.status='active' "
                "AND (NOT EXISTS (SELECT 1 FROM bms.user_scopes s WHERE s.tenant_id=b.tenant_id "
                "AND s.user_id=%s AND s.active) OR b.id IN (SELECT s.building_id "
                "FROM bms.user_scopes s WHERE s.tenant_id=b.tenant_id "
                "AND s.user_id=%s AND s.active)) "
                "ORDER BY b.id",
                (tenant, user["id"], user["id"]),
            ).fetchall()
            count = min(
                len(eligible),
                math.ceil(len(buildings) * config["supervisor_daily_percentage"] / 100),
            )
            chosen = secrets.SystemRandom().sample(eligible, count)
            sample = {"id": uuid4()}
            conn.execute(
                "INSERT INTO bms.supervisor_daily_samples "
                "(id,tenant_id,supervisor_user_id,local_date,percentage,total_dormitories) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    sample["id"],
                    tenant,
                    user["id"],
                    day,
                    config["supervisor_daily_percentage"],
                    len(buildings),
                ),
            )
            for building in chosen:
                conn.execute(
                    "INSERT INTO bms.supervisor_dorm_assignments"
                    "(id,tenant_id,sample_id,building_id) "
                    "VALUES (%s,%s,%s,%s)",
                    (uuid4(), tenant, sample["id"], building["id"]),
                )
            audit(
                conn,
                {"id": None, "tenant_id": tenant, "role": "system"},
                "supervisor.daily_sample",
                sample["id"],
                Command(client_operation_id=uuid4()),
                after={
                    "supervisorId": str(user["id"]),
                    "localDate": day.isoformat(),
                    "percentage": config["supervisor_daily_percentage"],
                    "buildingIds": [str(row["id"]) for row in chosen],
                },
            )
        selected = conn.execute(
            "SELECT building_id FROM bms.supervisor_dorm_assignments "
            "WHERE tenant_id=%s AND sample_id=%s",
            (tenant, sample["id"]),
        ).fetchall()
        for building in selected:
            result.setdefault(building["building_id"], []).append(user["id"])
    return result


def _fault_programme(conn, actor, sensor):
    programme, version = uuid4(), uuid4()
    prefix = "SIMULATED " if sensor["source"] == "simulator" else ""
    conn.execute(
        "INSERT INTO bms.programmes(id,tenant_id,name,recurrence,start_time,end_time,"
        "grace_minutes,sensor_id) VALUES (%s,%s,%s,'daily','00:00','23:59:59',0,%s)",
        (
            programme,
            actor["tenant_id"],
            f"{prefix}sensor follow-up - {sensor['name']}",
            sensor["id"],
        ),
    )
    items = [
        {
            "key": "manual_safety_check",
            "type": "pass_concern_fail",
            "text": f"{prefix}sensor unavailable. Follow the school's approved manual-check "
            "procedure for this location. Is the checked condition satisfactory?",
            "required": True,
            "photo": "on_fail",
            "createsFindingOn": ["concern", "fail"],
            "findingSeverity": "high",
        }
    ]
    conn.execute(
        "INSERT INTO bms.programme_versions(id,tenant_id,programme_id,version_number,items) "
        "VALUES (%s,%s,%s,1,%s)",
        (version, actor["tenant_id"], programme, Jsonb(items)),
    )
    conn.execute(
        "INSERT INTO bms.programme_scopes(id,tenant_id,programme_id,location_id) "
        "VALUES (%s,%s,%s,%s)",
        (uuid4(), actor["tenant_id"], programme, sensor["location_id"]),
    )
    conn.execute(
        "UPDATE bms.sensors SET fault_programme_id=%s WHERE tenant_id=%s AND id=%s",
        (programme, actor["tenant_id"], sensor["id"]),
    )


def transition_sensor(conn, actor, before, current):
    if current["status"] != "working" and (before is None or before["status"] == "working"):
        _fault_programme(conn, actor, current)
    elif current["status"] == "working" and current["fault_programme_id"]:
        conn.execute(
            "UPDATE bms.programmes SET enabled=false,revision=revision+1,updated_at=now() "
            "WHERE tenant_id=%s AND id=%s AND enabled",
            (actor["tenant_id"], current["fault_programme_id"]),
        )
        conn.execute(
            "UPDATE bms.inspection_occurrences SET cancelled_at=now(),"
            "cancellation_reason='sensor_restored',revision=revision+1,updated_at=now() "
            "WHERE tenant_id=%s AND programme_id=%s AND canonical_inspection_id IS NULL "
            "AND cancelled_at IS NULL",
            (actor["tenant_id"], current["fault_programme_id"]),
        )


def save_sensor(conn, actor, body, sensor_id=None):
    require(actor, "setup.manage")
    before = row_by_id(conn, actor, "sensors", sensor_id) if sensor_id else None
    key = f"sensor.simulator_status:{sensor_id}" if sensor_id else "sensor.simulator_create"
    old = replay(conn, actor, key, body)
    if old is not None:
        return old
    if before:
        if before["source"] != "simulator":
            raise ApiError(409, "SERVICE_SENSOR_CANNOT_BE_SIMULATED")
        revision(before, body.expected_revision)
        conn.execute(
            "UPDATE bms.sensors SET status=%s,status_changed_at=CASE WHEN status<>%s "
            "THEN now() ELSE status_changed_at END,revision=revision+1,updated_at=now() "
            "WHERE tenant_id=%s AND id=%s",
            (body.status, body.status, actor["tenant_id"], sensor_id),
        )
    else:
        facility_access(conn, actor, body.location_id, active=True)
        sensor_id = body.id
        conn.execute(
            "INSERT INTO bms.sensors(id,tenant_id,location_id,name,status) VALUES (%s,%s,%s,%s,%s)",
            (body.id, actor["tenant_id"], body.location_id, body.name, body.status),
        )
    current = row_by_id(conn, actor, "sensors", sensor_id)
    transition_sensor(conn, actor, before, current)
    from bms.scheduling import schedule_school

    schedule_school(conn, actor["tenant_id"])
    result = public(row_by_id(conn, actor, "sensors", sensor_id))
    audit(
        conn,
        actor,
        key.split(":")[0],
        sensor_id,
        body,
        before=public(before) if before else None,
        after=result,
        reason=getattr(body, "reason", None),
    )
    return remember(conn, actor, key, body, result)


def sensor_alerts(conn, actor):
    if actor["role"] not in {"supervisor", "administrator"}:
        return []
    now = datetime.now(UTC)
    rows = conn.execute(
        "SELECT o.id,o.id AS occurrence_id,s.id AS sensor_id,s.name AS sensor_name,"
        "s.status AS sensor_status,s.source,s.telemetry_mode,s.last_observed_at,"
        "s.last_received_at,s.availability_reason,s.active_alarms,s.measurements,s.measurement_times,"
        "l.name AS location_name,b.name AS building_name,"
        "u.name AS officer_name,o.due_end,o.timing_state "
        "FROM bms.sensors s LEFT JOIN bms.programmes p ON p.tenant_id=s.tenant_id "
        "AND p.id=s.fault_programme_id AND p.enabled "
        "JOIN bms.facilities l ON l.tenant_id=s.tenant_id AND l.id=s.location_id "
        "JOIN bms.facilities z ON z.tenant_id=l.tenant_id AND z.id=l.parent_id "
        "JOIN bms.facilities b ON b.tenant_id=l.tenant_id AND b.id=l.building_id "
        "LEFT JOIN bms.inspection_occurrences o ON o.tenant_id=p.tenant_id AND o.programme_id=p.id "
        "AND o.cancelled_at IS NULL AND o.due_start<=%s AND o.canonical_inspection_id IS NULL "
        "LEFT JOIN bms.users u ON u.tenant_id=o.tenant_id AND u.id=o.primary_user_id "
        "WHERE s.tenant_id=%s AND l.status='active' AND z.status='active' "
        "AND b.status='active' AND (cardinality(s.active_alarms)>0 OR "
        "(s.status<>'working' AND p.id IS NOT NULL AND (o.id IS NOT NULL OR NOT EXISTS "
        "(SELECT 1 FROM bms.inspection_occurrences c WHERE c.tenant_id=p.tenant_id "
        "AND c.programme_id=p.id AND c.due_start<=%s AND c.due_end>=%s)))) "
        "ORDER BY o.due_end NULLS FIRST,s.id,o.id",
        (now, actor["tenant_id"], now, now),
    ).fetchall()
    return [public({**row, "id": row["id"] or row["sensor_id"]}) for row in rows]
