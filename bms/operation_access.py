from datetime import date, datetime
from uuid import UUID

from psycopg import sql
from pydantic.alias_generators import to_camel

from bms.security import CAPABILITIES
from bms.services import ApiError

TABLES = {
    "facilities",
    "responsibility_assignments",
    "programmes",
    "programme_versions",
    "inspection_occurrences",
    "inspections",
    "findings",
    "corrective_actions",
    "evidence",
    "sensors",
    "school_operations_settings",
}


def public(row):
    def encode(value):
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime | date):
            return value.isoformat()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value

    return {
        to_camel(key): encode(value)
        for key, value in row.items()
        if key not in {"tenant_id", "storage_key"}
    }


def require(actor, capability):
    if capability not in CAPABILITIES[actor["role"]]:
        raise ApiError(403, "FORBIDDEN")


def row_by_id(conn, actor, table, entity_id):
    if table not in TABLES:
        raise ValueError("unapproved_table")
    row = conn.execute(
        sql.SQL("SELECT * FROM bms.{} WHERE tenant_id=%s AND id=%s").format(sql.Identifier(table)),
        (actor["tenant_id"], entity_id),
    ).fetchone()
    if not row:
        raise ApiError(404, "NOT_FOUND")
    return row


def revision(row, expected):
    if row["revision"] != expected:
        raise ApiError(409, "REVISION_CONFLICT", public(row))


def supervisor_in_scope(conn, actor, building_id):
    scopes = conn.execute(
        "SELECT building_id FROM bms.user_scopes WHERE tenant_id=%s AND user_id=%s AND active",
        (actor["tenant_id"], actor["id"]),
    ).fetchall()
    return not scopes or building_id in {row["building_id"] for row in scopes}


def assigned_zone(conn, actor, zone_id):
    return (
        conn.execute(
            "SELECT id FROM bms.responsibility_assignments "
            "WHERE tenant_id=%s AND zone_id=%s AND valid_to IS NULL "
            "AND (primary_user_id=%s OR backup_user_id=%s)",
            (actor["tenant_id"], zone_id, actor["id"], actor["id"]),
        ).fetchone()
        is not None
    )


def facility_access(conn, actor, location_id, *, active=False, occurrence=None, owned=False):
    location = row_by_id(conn, actor, "facilities", location_id)
    if location["level"] != "location":
        raise ApiError(400, "LOCATION_REQUIRED")
    zone = row_by_id(conn, actor, "facilities", location["parent_id"])
    building = row_by_id(conn, actor, "facilities", location["building_id"])
    if active and any(row["status"] != "active" for row in (location, zone, building)):
        raise ApiError(403, "LOCATION_INACTIVE")
    if actor["role"] == "administrator":
        return location
    if actor["role"] == "supervisor":
        if supervisor_in_scope(conn, actor, building["id"]):
            return location
    elif actor["role"] == "officer":
        if owned:
            return location
        if occurrence is not None:
            if actor["id"] in (occurrence["primary_user_id"], occurrence["backup_user_id"]):
                return location
        elif assigned_zone(conn, actor, zone["id"]):
            return location
    raise ApiError(403, "FORBIDDEN")


def finding_access(conn, actor, finding_id):
    finding = row_by_id(conn, actor, "findings", finding_id)
    facility_access(
        conn,
        actor,
        finding["location_id"],
        owned=actor["role"] == "officer" and finding["owner_id"] == actor["id"],
    )
    return finding


def action_access(conn, actor, action_id):
    action = row_by_id(conn, actor, "corrective_actions", action_id)
    finding = finding_access(conn, actor, action["finding_id"])
    return action, finding


def occurrence_access(conn, actor, occurrence_id):
    occurrence = row_by_id(conn, actor, "inspection_occurrences", occurrence_id)
    require(actor, "inspection.execute")
    if occurrence.get("cancelled_at"):
        raise ApiError(403, "CHECK_CANCELLED")
    if (
        occurrence.get("source_kind") == "supervisor_spot"
        and actor["id"] != occurrence["primary_user_id"]
    ):
        raise ApiError(403, "FORBIDDEN")
    facility_access(conn, actor, occurrence["location_id"], active=True, occurrence=occurrence)
    return occurrence


def evidence_access(conn, actor, evidence_id, *, upload=False):
    evidence = row_by_id(conn, actor, "evidence", evidence_id)
    if upload:
        if evidence["creator_id"] != actor["id"]:
            raise ApiError(403, "FORBIDDEN")
        # A creator can finish an already accepted photo after a scope change.
        return evidence
    if evidence["owner_type"] == "incident":
        from bms.emergency_operations import incident_access

        incident_access(conn, actor, evidence["owner_id"])
    elif evidence["owner_type"] == "review":
        require(actor, "setup.manage")
    elif evidence["owner_type"] == "finding":
        finding_access(conn, actor, evidence["owner_id"])
    elif evidence["owner_type"] == "action":
        action_access(conn, actor, evidence["owner_id"])
    else:
        inspection = row_by_id(conn, actor, "inspections", evidence["owner_id"])
        if inspection["lifecycle"] != "completed" and inspection["performer_id"] != actor["id"]:
            raise ApiError(403, "FORBIDDEN")
        occurrence = row_by_id(conn, actor, "inspection_occurrences", inspection["occurrence_id"])
        location = row_by_id(conn, actor, "facilities", occurrence["location_id"])
        owns_finding = conn.execute(
            "SELECT id FROM bms.findings WHERE tenant_id=%s AND inspection_id=%s "
            "AND item_key=%s AND owner_id=%s",
            (actor["tenant_id"], inspection["id"], evidence["item_key"], actor["id"]),
        ).fetchone()
        owned = actor["role"] == "officer" and (
            bool(owns_finding) or assigned_zone(conn, actor, location["parent_id"])
        )
        facility_access(
            conn,
            actor,
            occurrence["location_id"],
            occurrence=occurrence,
            owned=owned,
        )
    return evidence
