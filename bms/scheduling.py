import argparse
import time
from datetime import UTC, datetime, timedelta
from datetime import time as local_time
from uuid import uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from bms.config import settings
from bms.daily_operations import ensure_settings, sampled_dorms
from bms.db import connection, set_tenant
from bms.emergency_operations import process_emergency_escalations
from bms.sensor_operations import poll_sensor_service, refresh_sensor_availability
from bms.services import lock_tenant


def schedule_school(conn, tenant_id, now=None):
    now = now or datetime.now(UTC)
    lock_tenant(conn, tenant_id)
    refresh_sensor_availability(conn, tenant_id, now)
    process_emergency_escalations(conn, tenant_id, now)
    tenant = conn.execute(
        "SELECT timezone FROM bms.tenants WHERE tenant_id=%s", (tenant_id,)
    ).fetchone()
    zone = ZoneInfo(tenant["timezone"])
    today = now.astimezone(zone).date()
    ensure_settings(conn, tenant_id)
    scopes = conn.execute(
        "SELECT p.*,s.location_id,l.building_id,v.id AS version_id,"
        "r.primary_user_id,r.backup_user_id,"
        "u.status AS primary_status,u.role AS primary_role,"
        "bu.status AS backup_status,bu.role AS backup_role "
        "FROM bms.programmes p "
        "JOIN bms.programme_scopes s ON s.tenant_id=p.tenant_id AND s.programme_id=p.id "
        "JOIN bms.programme_versions v ON v.tenant_id=p.tenant_id AND v.programme_id=p.id "
        "AND v.version_number=p.current_version "
        "JOIN bms.facilities l ON l.tenant_id=s.tenant_id AND l.id=s.location_id "
        "JOIN bms.facilities z ON z.tenant_id=l.tenant_id AND z.id=l.parent_id "
        "JOIN bms.facilities b ON b.tenant_id=l.tenant_id AND b.id=l.building_id "
        "LEFT JOIN bms.responsibility_assignments r ON r.tenant_id=z.tenant_id AND r.zone_id=z.id "
        "AND r.valid_to IS NULL "
        "LEFT JOIN bms.users u ON u.tenant_id=r.tenant_id AND u.id=r.primary_user_id "
        "LEFT JOIN bms.users bu ON bu.tenant_id=r.tenant_id AND bu.id=r.backup_user_id "
        "WHERE p.tenant_id=%s AND p.enabled AND s.active AND l.status='active' "
        "AND z.status='active' AND b.status='active'",
        (tenant_id,),
    ).fetchall()
    random_dorms = (
        sampled_dorms(conn, tenant_id, today)
        if any(scope["assignment_mode"] == "supervisor_random" for scope in scopes)
        else {}
    )
    for scope in scopes:
        random = scope["assignment_mode"] == "supervisor_random"
        primary, backup = scope["primary_user_id"], scope["backup_user_id"]
        if not random:
            if scope["sensor_id"] and (
                scope["primary_role"] != "officer" or scope["primary_status"] != "active"
            ):
                primary = (
                    backup
                    if scope["backup_role"] == "officer" and scope["backup_status"] == "active"
                    else None
                )
                backup = None
            elif scope["primary_status"] != "active":
                primary = (
                    backup
                    if scope["backup_status"] == "active"
                    and scope["backup_role"] in {"officer", "supervisor"}
                    else None
                )
                backup = None
            if primary is None:
                continue
        periods = set()
        overnight = scope["recurrence"] == "daily" and scope["end_time"] < scope["start_time"]
        for offset in range(
            -1 if overnight else 0, 1 if random else settings().scheduling_horizon_days + 1
        ):
            day = today + timedelta(days=offset)
            if scope["recurrence"] == "weekly":
                day -= timedelta(days=day.weekday())
                iso = day.isocalendar()
                key = f"{iso.year}-W{iso.week:02d}"
                last = day + timedelta(days=6)
            else:
                key, last = day.isoformat(), day
            if key in periods:
                continue
            periods.add(key)
            weekly = scope["recurrence"] == "weekly"
            start = datetime.combine(day, local_time.min if weekly else scope["start_time"], zone)
            end = datetime.combine(
                last + timedelta(days=1) if overnight else last,
                local_time(23, 59, 59) if weekly else scope["end_time"],
                zone,
            )
            performers = random_dorms.get(scope["building_id"], []) if random else [primary]
            for performer in performers:
                conn.execute(
                    "INSERT INTO bms.inspection_occurrences "
                    "(id,tenant_id,programme_id,programme_version_id,location_id,primary_user_id,"
                    "backup_user_id,period_key,due_start,due_end,grace_end,source_kind) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (tenant_id,programme_id,location_id,period_key) DO NOTHING",
                    (
                        uuid4(),
                        tenant_id,
                        scope["id"],
                        scope["version_id"],
                        scope["location_id"],
                        performer,
                        None if random else backup,
                        f"{key}:supervisor:{performer}" if random else key,
                        start,
                        end,
                        end + timedelta(minutes=scope["grace_minutes"]),
                        "supervisor_spot"
                        if random
                        else "sensor_fault"
                        if scope["sensor_id"]
                        else "routine",
                    ),
                )
    candidates = conn.execute(
        "SELECT id,timing_state,grace_end FROM bms.inspection_occurrences "
        "WHERE tenant_id=%s AND canonical_inspection_id IS NULL AND cancelled_at IS NULL",
        (tenant_id,),
    ).fetchall()
    conn.execute(
        "UPDATE bms.inspection_occurrences SET timing_state=CASE "
        "WHEN %s<due_start THEN 'not_yet_due' WHEN %s<=due_end THEN 'due' "
        "WHEN %s<=grace_end THEN 'in_grace' ELSE 'missed' END, "
        "missed_at=CASE WHEN %s>grace_end THEN coalesce(missed_at,%s) ELSE missed_at END, "
        "updated_at=now(),revision=revision+1 WHERE tenant_id=%s "
        "AND canonical_inspection_id IS NULL AND cancelled_at IS NULL AND timing_state<>CASE "
        "WHEN %s<due_start THEN 'not_yet_due' WHEN %s<=due_end THEN 'due' "
        "WHEN %s<=grace_end THEN 'in_grace' ELSE 'missed' END",
        (now, now, now, now, now, tenant_id, now, now, now),
    )
    for row in candidates:
        if row["timing_state"] != "missed" and now > row["grace_end"]:
            conn.execute(
                "INSERT INTO bms.audit_events "
                "(id,tenant_id,actor_id,actor_role,action,entity_id,after_data,operation_id) "
                "VALUES (%s,%s,NULL,'system','occurrence.missed',%s,%s,%s)",
                (uuid4(), tenant_id, row["id"], Jsonb({"state": "missed"}), uuid4()),
            )


def main():
    parser = argparse.ArgumentParser(description="Run the single BMS scheduler for one school.")
    parser.add_argument("--school-code", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    # One session-level advisory lock per school prevents overlapping workers.
    with connection() as conn:
        tenant = conn.execute(
            "SELECT bms.school_tenant(%s) AS id", (args.school_code.upper(),)
        ).fetchone()["id"]
        if tenant is None:
            raise SystemExit("Unknown school.")
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked",
            (f"bms.scheduler:{tenant}",),
        ).fetchone()["locked"]
        if not locked:
            raise SystemExit("A scheduler is already running for this school.")
        next_routine = 0
        while True:
            conn.execute("SET LOCAL ROLE bms_runtime")
            set_tenant(conn, tenant)
            if settings().sensor_service_url:
                poll_sensor_service(conn, tenant)
            if time.monotonic() >= next_routine:
                schedule_school(conn, tenant)
                next_routine = time.monotonic() + settings().scheduler_interval_minutes * 60
            else:
                process_emergency_escalations(conn, tenant)
            conn.commit()
            if args.once:
                break
            time.sleep(
                min(15, settings().sensor_poll_seconds, settings().scheduler_interval_minutes * 60)
            )


if __name__ == "__main__":
    main()
