from datetime import UTC, datetime
from uuid import uuid4

from bms.config import settings
from bms.daily_operations import ensure_settings, sensor_alerts
from bms.emergency_operations import emergency_snapshot
from bms.operation_access import public
from bms.services import USER_FIELDS, ApiError, lock_tenant, public_user


def snapshot(conn, actor):
    if actor["role"] == "leadership":
        raise ApiError(403, "MOBILE_ACCESS_FORBIDDEN")
    lock_tenant(conn, actor["tenant_id"])
    from bms.scheduling import schedule_school

    schedule_school(conn, actor["tenant_id"])
    args = [actor["tenant_id"]]
    restriction = ""
    if actor["role"] == "supervisor":
        restriction = (
            " AND (NOT EXISTS (SELECT 1 FROM bms.user_scopes s WHERE "
            "s.tenant_id=l.tenant_id AND s.user_id=%s AND s.active) OR l.building_id IN "
            "(SELECT s.building_id FROM bms.user_scopes s WHERE "
            "s.tenant_id=l.tenant_id AND s.user_id=%s AND s.active))"
        )
        args += [actor["id"], actor["id"]]
    elif actor["role"] == "officer":
        restriction = (
            " AND (EXISTS (SELECT 1 FROM bms.responsibility_assignments r WHERE "
            "r.tenant_id=l.tenant_id AND r.zone_id=l.parent_id AND r.valid_to IS NULL "
            "AND (r.primary_user_id=%s OR r.backup_user_id=%s)) OR EXISTS "
            "(SELECT 1 FROM bms.inspection_occurrences o WHERE o.tenant_id=l.tenant_id "
            "AND o.location_id=l.id AND (o.primary_user_id=%s OR o.backup_user_id=%s)) "
            "OR EXISTS (SELECT 1 FROM bms.findings f WHERE f.tenant_id=l.tenant_id "
            "AND f.location_id=l.id AND f.owner_id=%s))"
        )
        args += [actor["id"]] * 5
    locations = conn.execute(
        "SELECT l.* FROM bms.facilities l WHERE l.tenant_id=%s AND l.level='location'"
        + restriction,
        args,
    ).fetchall()
    location_ids = [row["id"] for row in locations]
    facility_ids = list(
        {value for row in locations for value in (row["id"], row["parent_id"], row["building_id"])}
    )
    if actor["role"] == "administrator":
        facilities = conn.execute(
            "SELECT * FROM bms.facilities WHERE tenant_id=%s ORDER BY level,name,id",
            (actor["tenant_id"],),
        ).fetchall()
    else:
        facilities = conn.execute(
            "SELECT * FROM bms.facilities WHERE tenant_id=%s AND id=ANY(%s) ORDER BY level,name,id",
            (actor["tenant_id"], facility_ids),
        ).fetchall()
    occurrence_args = [actor["tenant_id"], location_ids]
    occurrence_scope = ""
    if actor["role"] == "officer":
        occurrence_scope = " AND (primary_user_id=%s OR backup_user_id=%s)"
        occurrence_args += [actor["id"], actor["id"]]
    elif actor["role"] == "supervisor":
        occurrence_scope = " AND (source_kind<>'supervisor_spot' OR primary_user_id=%s)"
        occurrence_args += [actor["id"]]
    occurrences = conn.execute(
        "SELECT * FROM bms.inspection_occurrences WHERE tenant_id=%s AND location_id=ANY(%s)"
        + occurrence_scope
        + " ORDER BY due_end,id",
        occurrence_args,
    ).fetchall()
    occurrence_ids = [row["id"] for row in occurrences]
    versions = conn.execute(
        "SELECT * FROM bms.programme_versions WHERE tenant_id=%s AND "
        "(id=ANY(%s) OR programme_id IN (SELECT programme_id FROM bms.programme_scopes "
        "WHERE tenant_id=%s AND active AND location_id=ANY(%s))) ORDER BY created_at,id",
        (
            actor["tenant_id"],
            [row["programme_version_id"] for row in occurrences],
            actor["tenant_id"],
            location_ids,
        ),
    ).fetchall()
    if actor["role"] == "administrator":
        versions = conn.execute(
            "SELECT * FROM bms.programme_versions WHERE tenant_id=%s ORDER BY created_at,id",
            (actor["tenant_id"],),
        ).fetchall()
    programme_ids = list({row["programme_id"] for row in versions})
    programmes = conn.execute(
        "SELECT * FROM bms.programmes WHERE tenant_id=%s "
        + ("" if actor["role"] == "administrator" else "AND id=ANY(%s) ")
        + "ORDER BY name,id",
        (actor["tenant_id"],)
        if actor["role"] == "administrator"
        else (actor["tenant_id"], programme_ids),
    ).fetchall()
    inspections = conn.execute(
        "SELECT * FROM bms.inspections WHERE tenant_id=%s AND occurrence_id=ANY(%s) "
        "AND (lifecycle='completed' OR performer_id=%s) ORDER BY created_at,id",
        (actor["tenant_id"], occurrence_ids, actor["id"]),
    ).fetchall()
    finding_scope = ""
    finding_args = [actor["tenant_id"], location_ids]
    if actor["role"] == "officer":
        finding_scope = (
            "AND (f.owner_id=%s OR EXISTS (SELECT 1 FROM bms.facilities l "
            "JOIN bms.responsibility_assignments r ON r.tenant_id=l.tenant_id "
            "AND r.zone_id=l.parent_id AND r.valid_to IS NULL "
            "WHERE l.tenant_id=f.tenant_id AND l.id=f.location_id "
            "AND (r.primary_user_id=%s OR r.backup_user_id=%s))) "
        )
        finding_args += [actor["id"]] * 3
    findings = conn.execute(
        "SELECT f.* FROM bms.findings f WHERE f.tenant_id=%s AND f.location_id=ANY(%s) "
        + finding_scope
        + "ORDER BY (f.owner_id IS NOT NULL),CASE f.severity WHEN 'critical' THEN 0 "
        "WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,f.reported_at,f.id",
        finding_args,
    ).fetchall()
    actions = conn.execute(
        "SELECT * FROM bms.corrective_actions WHERE tenant_id=%s AND finding_id=ANY(%s) "
        "ORDER BY due_date,id",
        (actor["tenant_id"], [row["id"] for row in findings]),
    ).fetchall()
    evidence = conn.execute(
        "SELECT * FROM bms.evidence WHERE tenant_id=%s AND "
        "((owner_type='inspection' AND (owner_id=ANY(%s) OR EXISTS "
        "(SELECT 1 FROM bms.findings f WHERE f.tenant_id=bms.evidence.tenant_id "
        "AND f.id=ANY(%s) AND f.inspection_id=bms.evidence.owner_id "
        "AND f.item_key=bms.evidence.item_key))) OR "
        "(owner_type='finding' AND owner_id=ANY(%s)) OR "
        "(owner_type='action' AND owner_id=ANY(%s))) ORDER BY created_at,id",
        (
            actor["tenant_id"],
            [row["id"] for row in inspections],
            [row["id"] for row in findings],
            [row["id"] for row in findings],
            [row["id"] for row in actions],
        ),
    ).fetchall()
    assignments = conn.execute(
        "SELECT * FROM bms.responsibility_assignments WHERE tenant_id=%s AND valid_to IS NULL "
        + ("" if actor["role"] == "administrator" else "AND zone_id=ANY(%s) "),
        (actor["tenant_id"],)
        if actor["role"] == "administrator"
        else (
            actor["tenant_id"],
            [row["id"] for row in facilities if row["level"] == "zone"],
        ),
    ).fetchall()
    qr_tokens = conn.execute(
        "SELECT location_id,token FROM bms.qr_tokens "
        "WHERE tenant_id=%s AND active AND location_id=ANY(%s)",
        (actor["tenant_id"], location_ids),
    ).fetchall()
    users = conn.execute(
        f"SELECT {USER_FIELDS} FROM bms.users WHERE tenant_id=%s AND "
        "(%s OR id=%s OR id=ANY(%s)) ORDER BY name,id",
        (
            actor["tenant_id"],
            actor["role"] in {"administrator", "supervisor"},
            actor["id"],
            list(
                {
                    value
                    for row in assignments
                    for value in (row["primary_user_id"], row["backup_user_id"])
                    if value
                }
            ),
        ),
    ).fetchall()
    tenant = conn.execute(
        "SELECT name,school_code,timezone FROM bms.tenants WHERE tenant_id=%s",
        (actor["tenant_id"],),
    ).fetchone()
    programme_scopes = conn.execute(
        "SELECT * FROM bms.programme_scopes WHERE tenant_id=%s AND active AND location_id=ANY(%s)",
        (actor["tenant_id"], location_ids),
    ).fetchall()
    user_scopes = conn.execute(
        "SELECT * FROM bms.user_scopes WHERE tenant_id=%s AND active AND (%s OR user_id=%s)",
        (actor["tenant_id"], actor["role"] == "administrator", actor["id"]),
    ).fetchall()
    sensors = conn.execute(
        "SELECT * FROM bms.sensors WHERE tenant_id=%s AND location_id=ANY(%s) ORDER BY name,id",
        (actor["tenant_id"], location_ids),
    ).fetchall()
    dorm_assignments = conn.execute(
        "SELECT a.*,s.supervisor_user_id,s.local_date,s.percentage,b.name AS building_name "
        "FROM bms.supervisor_dorm_assignments a JOIN bms.supervisor_daily_samples s "
        "ON s.tenant_id=a.tenant_id AND s.id=a.sample_id JOIN bms.facilities b "
        "ON b.tenant_id=a.tenant_id AND b.id=a.building_id "
        "WHERE a.tenant_id=%s AND (%s OR s.supervisor_user_id=%s) "
        "AND (%s OR a.building_id=ANY(%s)) ORDER BY s.local_date DESC,b.name",
        (
            actor["tenant_id"],
            actor["role"] == "administrator",
            actor["id"],
            actor["role"] == "administrator",
            facility_ids,
        ),
    ).fetchall()
    return {
        "user": public_user(actor),
        "school": public(tenant),
        "policy": {
            "photoMaxBytes": settings().evidence_max_bytes,
            "photoMaxDimension": settings().evidence_max_dimension,
            "clockWarningSeconds": settings().clock_warning_seconds,
        },
        "serverTime": datetime.now(UTC).isoformat(),
        "syncResolutions": [
            public(row)
            for row in conn.execute(
                "SELECT r.* FROM bms.sync_resolutions r JOIN bms.sync_submissions s "
                "ON s.tenant_id=r.tenant_id AND s.id=r.submission_id "
                "WHERE r.tenant_id=%s AND s.creator_id=%s",
                (actor["tenant_id"], actor["id"]),
            ).fetchall()
        ],
        "emergency": emergency_snapshot(conn, actor),
        "cursor": str(uuid4()),
        "fullSnapshot": True,
        "dailySettings": public(ensure_settings(conn, actor["tenant_id"])),
        "sensors": [public(row) for row in sensors],
        "sensorAlerts": sensor_alerts(conn, actor),
        "spotCheckAssignments": [public(row) for row in dorm_assignments],
        "facilities": [public(row) for row in facilities],
        "assignments": [public(row) for row in assignments],
        "programmes": [public(row) for row in programmes],
        "programmeScopes": [public(row) for row in programme_scopes],
        "userScopes": [public(row) for row in user_scopes],
        "programmeVersions": [public(row) for row in versions],
        "occurrences": [public(row) for row in occurrences],
        "inspections": [public(row) for row in inspections],
        "findings": [public(row) for row in findings],
        "actions": [public(row) for row in actions],
        "evidence": [public(row) for row in evidence],
        "qrTokens": [public(row) for row in qr_tokens],
        "users": [
            public_user(row)
            if actor["role"] == "administrator"
            else {
                "id": str(row["id"]),
                "name": row["name"],
                "role": row["role"],
                "status": row["status"],
                "revision": row["revision"],
            }
            for row in users
        ],
    }
