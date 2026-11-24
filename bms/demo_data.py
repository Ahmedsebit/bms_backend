from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from bms.daily_operations import save_sensor
from bms.db import connection, set_tenant
from bms.operation_models import (
    FacilityCreate,
    ProgrammeCreate,
    ResponsibilitySet,
    SensorCreate,
    SensorServiceCreate,
)
from bms.scheduling import schedule_school
from bms.sensor_operations import create_service_sensor
from bms.services import USER_FIELDS
from bms.setup_operations import create_facility, save_programme, set_responsibility


def seed_demo_operations(school_code):
    """Only the explicit synthetic demo launcher calls this; never replace user edits."""
    with connection() as conn:
        tenant = conn.execute("SELECT bms.school_tenant(%s) AS id", (school_code,)).fetchone()["id"]
        set_tenant(conn, tenant)
        users = {
            row["login_id"]: row
            for row in conn.execute(
                f"SELECT {USER_FIELDS} FROM bms.users WHERE tenant_id=%s AND login_id=ANY(%s)",
                (tenant, ["ADMIN", "SUPERVISOR", "OFFICER"]),
            ).fetchall()
        }
        actor = users["ADMIN"]
        if actor["status"] != "active":
            return

        def identifier(key):
            return uuid5(NAMESPACE_URL, f"bms:synthetic-demo:{school_code}:{key}")

        def exists(table, key):
            # Call sites use only fixed table names and server-generated IDs.
            from psycopg import sql

            return conn.execute(
                sql.SQL("SELECT id FROM bms.{} WHERE tenant_id=%s AND id=%s").format(
                    sql.Identifier(table)
                ),
                (tenant, key),
            ).fetchone()

        for login in ("ADMIN", "SUPERVISOR"):
            holder = users[login]
            if holder["status"] != "active":
                continue
            for capability in (
                "incident.command",
                "roster.manage",
                "safety.approve",
                "safety.verify",
            ):
                key = identifier(f"duty:{login}:{capability}")
                if not exists("duty_grants", key):
                    from bms.emergency_models import DutyGrantWrite
                    from bms.emergency_operations import execute_emergency

                    execute_emergency(
                        conn,
                        actor,
                        "duty.grant",
                        DutyGrantWrite(
                            id=key,
                            client_operation_id=identifier(f"create:{key}"),
                            user_id=holder["id"],
                            capability=capability,
                            starts_at=datetime.now(UTC) - timedelta(minutes=1),
                            ends_at=datetime.now(UTC) + timedelta(days=365),
                            approval_note="UNAPPROVED DEMO - synthetic practice duty only",
                        ),
                    )
        scopes = {"door": [], "emergency_exit": [], "extinguisher_point": []}
        primary_active = users["OFFICER"]["status"] == "active"
        for dorm in ("Cedar", "Acacia"):
            building_id = identifier(dorm)
            zone_id = identifier(f"{dorm}:zone")
            structures = [
                (building_id, None, "building", f"{dorm} - synthetic demo", None),
                (zone_id, building_id, "zone", f"{dorm} practice zone", None),
            ]
            for kind, title in (
                ("door", "Practice door"),
                ("emergency_exit", "Practice exit"),
                ("extinguisher_point", "Practice extinguisher point"),
            ):
                key = identifier(f"{dorm}:{kind}")
                scopes[kind].append(key)
                structures.append((key, zone_id, "location", f"{dorm} {title}", kind))
            for key, parent, level, title, kind in structures:
                if not exists("facilities", key):
                    create_facility(
                        conn,
                        actor,
                        FacilityCreate(
                            client_operation_id=identifier(f"create:{key}"),
                            id=key,
                            parent_id=parent,
                            level=level,
                            name=title,
                            location_type=kind,
                        ),
                    )
            assignment = conn.execute(
                "SELECT id FROM bms.responsibility_assignments "
                "WHERE tenant_id=%s AND zone_id=%s AND valid_to IS NULL",
                (tenant, zone_id),
            ).fetchone()
            if not assignment and primary_active:
                set_responsibility(
                    conn,
                    actor,
                    zone_id,
                    ResponsibilitySet(
                        client_operation_id=identifier(f"assignment:{zone_id}"),
                        primary_user_id=users["OFFICER"]["id"],
                        backup_user_id=users["SUPERVISOR"]["id"]
                        if users["SUPERVISOR"]["status"] == "active"
                        else None,
                        expected_revision=0,
                    ),
                )
        for kind, title, recurrence in (
            ("emergency_exit", "Emergency openings", "daily"),
            ("door", "Doors not locked from outside", "daily"),
            ("extinguisher_point", "Extinguisher practice", "weekly"),
        ):
            key = identifier(f"programme:{kind}")
            if not exists("programmes", key):
                save_programme(
                    conn,
                    actor,
                    ProgrammeCreate(
                        client_operation_id=identifier(f"create:{key}"),
                        id=key,
                        name=f"UNAPPROVED DEMO - {title}",
                        recurrence=recurrence,
                        start_time="00:00",
                        end_time="23:59:59",
                        grace_minutes=1440 if recurrence == "weekly" else 60,
                        life_safety=True,
                        enabled=primary_active,
                        location_ids=scopes[kind],
                        items=[
                            {
                                "key": "practice_condition",
                                "type": "pass_concern_fail",
                                "text": "UNAPPROVED DEMO: record the marked practice condition.",
                                "required": True,
                                "photo": "on_fail",
                                "createsFindingOn": ["concern", "fail"],
                                "findingSeverity": "high",
                            }
                        ],
                    ),
                )
        path_key = identifier("programme:path-obstruction")
        if not exists("programmes", path_key):
            save_programme(
                conn,
                actor,
                ProgrammeCreate(
                    client_operation_id=identifier(f"create:{path_key}"),
                    id=path_key,
                    name="UNAPPROVED DEMO - Path obstruction",
                    recurrence="daily",
                    start_time="00:00",
                    end_time="23:59:59",
                    grace_minutes=0,
                    enabled=primary_active,
                    location_ids=scopes["emergency_exit"],
                    items=[
                        {
                            "key": "path_clear",
                            "type": "pass_concern_fail",
                            "text": "UNAPPROVED DEMO: is this practice path unobstructed?",
                            "photo": "on_fail",
                            "createsFindingOn": ["concern", "fail"],
                            "findingSeverity": "high",
                        }
                    ],
                ),
            )
        for index, location in enumerate(scopes["door"]):
            sensor_id = identifier(f"sensor:{location}")
            if not exists("sensors", sensor_id):
                save_sensor(
                    conn,
                    actor,
                    SensorCreate(
                        client_operation_id=identifier(f"create:{sensor_id}"),
                        id=sensor_id,
                        location_id=location,
                        name=f"Practice sensor {index + 1} - SIMULATOR",
                        status="faulty" if index == 0 else "working",
                    ),
                )
        if school_code == "DEMO":
            service_id = identifier("sensor:local-service-smoke")
            mapped = conn.execute(
                "SELECT id FROM bms.sensors WHERE tenant_id=%s AND gateway_id='local-test-gateway' "
                "AND external_device_id='local-smoke-01'",
                (tenant,),
            ).fetchone()
            if not mapped and not exists("sensors", service_id):
                create_service_sensor(
                    conn,
                    actor,
                    SensorServiceCreate(
                        client_operation_id=identifier(f"create:{service_id}"),
                        id=service_id,
                        location_id=scopes["door"][0],
                        name="Local gateway smoke sensor - TEST (no hardware)",
                        gateway_id="local-test-gateway",
                        external_device_id="local-smoke-01",
                        stale_after_seconds=300,
                    ),
                )
        spot_key = identifier("programme:supervisor-spot")
        if not exists("programmes", spot_key):
            save_programme(
                conn,
                actor,
                ProgrammeCreate(
                    client_operation_id=identifier(f"create:{spot_key}"),
                    id=spot_key,
                    name="UNAPPROVED DEMO - Supervisor dormitory spot check",
                    recurrence="daily",
                    assignment_mode="supervisor_random",
                    start_time="00:00",
                    end_time="23:59:59",
                    grace_minutes=0,
                    location_ids=[location for group in scopes.values() for location in group],
                    items=[
                        {
                            "key": "spot_condition",
                            "type": "pass_concern_fail",
                            "text": "UNAPPROVED DEMO: is the practice condition satisfactory?",
                            "photo": "on_fail",
                            "createsFindingOn": ["concern", "fail"],
                            "findingSeverity": "high",
                        }
                    ],
                ),
            )
        schedule_school(conn, tenant)
