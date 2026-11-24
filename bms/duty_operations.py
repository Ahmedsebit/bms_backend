from datetime import UTC, datetime, timedelta

from bms.operation_access import public
from bms.services import ApiError, get_user


def has_duty(conn, actor, capability, building_id=None, now=None, until=None):
    if actor["status"] != "active":
        return False
    return (
        conn.execute(
            "SELECT id FROM bms.duty_grants WHERE tenant_id=%s AND user_id=%s AND capability=%s "
            "AND active AND starts_at<=%s AND "
            + ("ends_at>=%s " if until else "ends_at>%s ")
            + "AND (building_id IS NULL OR building_id=%s) LIMIT 1",
            (
                actor["tenant_id"],
                actor["id"],
                capability,
                now or datetime.now(UTC),
                until or now or datetime.now(UTC),
                building_id,
            ),
        ).fetchone()
        is not None
    )


def require_duty(conn, actor, capability, building_id=None):
    if not has_duty(conn, actor, capability, building_id):
        raise ApiError(403, "DUTY_GRANT_REQUIRED")


def coverage(conn, actor, building_ids, now=None):
    now = now or datetime.now(UTC)
    horizon = now + timedelta(hours=24)
    shifts = conn.execute(
        "SELECT s.*,p.status AS primary_status,b.status AS backup_status "
        "FROM bms.emergency_shifts s JOIN bms.users p "
        "ON p.tenant_id=s.tenant_id AND p.id=s.primary_user_id "
        "JOIN bms.users b ON b.tenant_id=s.tenant_id AND b.id=s.backup_user_id "
        "WHERE s.tenant_id=%s AND s.building_id=ANY(%s) AND s.ends_at>%s "
        "AND s.starts_at<%s ORDER BY s.starts_at,s.id",
        (actor["tenant_id"], list(building_ids), now, horizon),
    ).fetchall()
    result = []
    for building in building_ids:
        cursor = now
        selected = [s for s in shifts if s["building_id"] == building]
        for shift in selected:
            if shift["starts_at"] > cursor:
                result.append(
                    {
                        "buildingId": str(building),
                        "kind": "coverage_gap",
                        "startsAt": cursor.isoformat(),
                        "endsAt": shift["starts_at"].isoformat(),
                    }
                )
            cursor = max(cursor, shift["ends_at"])
            for role in ("primary", "backup"):
                if shift[f"{role}_status"] != "active" or not shift[f"{role}_accepted_at"]:
                    result.append(
                        {
                            "buildingId": str(building),
                            "shiftId": str(shift["id"]),
                            "kind": f"{role}_unavailable"
                            if shift[f"{role}_status"] != "active"
                            else f"{role}_not_confirmed",
                        }
                    )
            lead = get_user(conn, actor["tenant_id"], shift["lead_user_id"])
            if not has_duty(
                conn,
                lead,
                "incident.command",
                building,
                max(now, shift["starts_at"]),
                min(horizon, shift["ends_at"]),
            ):
                result.append(
                    {
                        "buildingId": str(building),
                        "shiftId": str(shift["id"]),
                        "kind": "lead_authority_unavailable",
                    }
                )
            if any(
                other["building_id"] != building
                and other["lead_user_id"] == shift["lead_user_id"]
                and other["starts_at"] < shift["ends_at"]
                and other["ends_at"] > shift["starts_at"]
                for other in shifts
            ):
                result.append(
                    {
                        "buildingId": str(building),
                        "shiftId": str(shift["id"]),
                        "kind": "lead_capacity_shared",
                    }
                )
        if cursor < horizon:
            result.append(
                {
                    "buildingId": str(building),
                    "kind": "coverage_gap",
                    "startsAt": cursor.isoformat(),
                    "endsAt": horizon.isoformat(),
                }
            )
    pending = conn.execute(
        "SELECT h.id,s.building_id FROM bms.emergency_handovers h "
        "JOIN bms.emergency_shifts s ON s.tenant_id=h.tenant_id AND s.id=h.incoming_shift_id "
        "WHERE h.tenant_id=%s AND s.building_id=ANY(%s) AND h.accepted_at IS NULL "
        "AND s.ends_at>%s AND s.starts_at<%s",
        (actor["tenant_id"], list(building_ids), now, horizon),
    ).fetchall()
    result.extend(
        {
            "buildingId": str(row["building_id"]),
            "handoverId": str(row["id"]),
            "kind": "handover_not_accepted",
        }
        for row in pending
    )
    unassigned = conn.execute(
        "SELECT p.id,l.building_id FROM bms.programmes p JOIN bms.programme_scopes ps "
        "ON ps.tenant_id=p.tenant_id AND ps.programme_id=p.id AND ps.active "
        "JOIN bms.facilities l ON l.tenant_id=ps.tenant_id AND l.id=ps.location_id "
        "LEFT JOIN bms.responsibility_assignments r ON r.tenant_id=l.tenant_id "
        "AND r.zone_id=l.parent_id AND r.valid_to IS NULL "
        "LEFT JOIN bms.users primary_staff ON primary_staff.tenant_id=r.tenant_id "
        "AND primary_staff.id=r.primary_user_id "
        "LEFT JOIN bms.users backup_staff ON backup_staff.tenant_id=r.tenant_id "
        "AND backup_staff.id=r.backup_user_id "
        "WHERE p.tenant_id=%s AND p.enabled AND p.assignment_mode='zone' "
        "AND l.status='active' AND l.building_id=ANY(%s) "
        "AND coalesce(primary_staff.status,'inactive')<>'active' "
        "AND coalesce(backup_staff.status,'inactive')<>'active'",
        (actor["tenant_id"], list(building_ids)),
    ).fetchall()
    result.extend(
        {
            "buildingId": str(row["building_id"]),
            "programmeId": str(row["id"]),
            "kind": "routine_check_unassigned",
        }
        for row in unassigned
    )
    return result


def roster_status(conn, actor, building_ids):
    result = []
    for building in building_ids:
        latest = conn.execute(
            "SELECT * FROM bms.roster_reviews WHERE tenant_id=%s AND building_id=%s "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (actor["tenant_id"], building),
        ).fetchone()
        change = conn.execute(
            "SELECT max(updated_at) AS changed_at,count(*) FILTER (WHERE active) AS total "
            "FROM bms.emergency_people WHERE tenant_id=%s AND building_id=%s",
            (actor["tenant_id"], building),
        ).fetchone()
        current = bool(
            latest
            and latest["created_at"] >= datetime.now(UTC) - timedelta(hours=24)
            and (not change["changed_at"] or latest["created_at"] >= change["changed_at"])
            and latest["expected_count"] == change["total"]
            and change["total"] > 0
        )
        result.append(
            {
                "buildingId": str(building),
                "expectedCount": change["total"],
                "current": current,
                "review": public(latest) if latest else None,
                "lastChangedAt": change["changed_at"].isoformat() if change["changed_at"] else None,
                "timingMode": "unapproved_prototype_24_hour_review",
            }
        )
    return result
