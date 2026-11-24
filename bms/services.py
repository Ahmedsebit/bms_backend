from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from bms.config import settings
from bms.security import request_digest, token_digest, token_for

USER_FIELDS = (
    "id, tenant_id, created_at, updated_at, revision, name, login_id, role, status, "
    "must_change_password"
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, current=None):
        self.status = status
        self.code = code
        self.current = current


def public_user(user):
    return {
        "id": str(user["id"]),
        "name": user["name"],
        "userId": user["login_id"],
        "role": user["role"],
        "status": user["status"],
        "mustChangePassword": user["must_change_password"],
        "revision": user["revision"],
    }


def lock_tenant(conn, tenant_id):
    # M1 serializes mutations per school; later scheduling must use the same lock order.
    conn.execute("SELECT id FROM bms.tenants WHERE tenant_id = %s FOR UPDATE", (tenant_id,))


def get_user(conn, tenant_id, user_id):
    row = conn.execute(
        f"SELECT {USER_FIELDS} FROM bms.users WHERE tenant_id = %s AND id = %s",
        (tenant_id, user_id),
    ).fetchone()
    if not row:
        raise ApiError(404, "NOT_FOUND")
    return row


def check_revision(user, expected):
    if user["revision"] != expected:
        raise ApiError(409, "REVISION_CONFLICT", public_user(user))


def command_payload(body):
    data = body.model_dump(mode="json", by_alias=True)
    if data.get("clientType") == "web":
        data.pop("clientType")
    if not data.get("offline"):
        data.pop("offline", None)
    if data.get("assignmentMode") == "zone":
        data.pop("assignmentMode")
    if not data.get("approved"):
        data.pop("approved", None)
    if not data.get("approvalNote"):
        data.pop("approvalNote", None)
    for key in ("password", "currentPassword"):
        value = getattr(body, "current_password" if key == "currentPassword" else key, None)
        if value is not None:
            data[key] = value.get_secret_value()
    return data


def replay(conn, actor, command, body):
    lock_tenant(conn, actor["tenant_id"])
    row = conn.execute(
        "SELECT actor_id, command, request_hash, result FROM bms.client_operations "
        "WHERE tenant_id = %s AND id = %s",
        (actor["tenant_id"], body.client_operation_id),
    ).fetchone()
    if row:
        if row["actor_id"] != actor["id"]:
            raise ApiError(409, "INVALID_TRANSITION")
        digest = request_digest(command, command_payload(body))
        if row["command"] != command or row["request_hash"] != digest:
            raise ApiError(409, "INVALID_TRANSITION")
        return row["result"]
    return None


def remember(conn, actor, command, body, result):
    conn.execute(
        "INSERT INTO bms.client_operations "
        "(id, tenant_id, actor_id, command, request_hash, result) VALUES (%s,%s,%s,%s,%s,%s)",
        (
            body.client_operation_id,
            actor["tenant_id"],
            actor["id"],
            command,
            request_digest(command, command_payload(body)),
            Jsonb(result),
        ),
    )
    return result


def audit(conn, actor, command, entity_id, body, before=None, after=None, reason=None):
    conn.execute(
        "INSERT INTO bms.audit_events "
        "(id,tenant_id,actor_id,actor_role,action,entity_id,before_data,after_data,reason,"
        "device_occurred_at,device_id,operation_id,offline) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            uuid4(),
            actor["tenant_id"],
            actor["id"],
            actor["role"],
            command,
            entity_id,
            Jsonb(before),
            Jsonb(after),
            reason,
            body.device_occurred_at,
            body.device_id,
            body.client_operation_id,
            body.offline,
        ),
    )


def failed_password(conn, user, body):
    now = datetime.now(UTC)
    attempts = 1 if user["locked_until"] else user["failed_password_attempts"] + 1
    locked = attempts >= settings().failed_password_limit
    conn.execute(
        "UPDATE bms.users SET failed_password_attempts=%s, locked_until=%s, "
        "updated_at=now(), revision=revision+1 WHERE tenant_id=%s AND id=%s",
        (
            attempts,
            now + timedelta(minutes=settings().lockout_minutes) if locked else None,
            user["tenant_id"],
            user["id"],
        ),
    )
    audit(conn, user, "auth.password_failed", user["id"], body, after={"locked": locked})
    # Rejected credentials must still durably update the lockout counter.
    conn.commit()
    raise ApiError(423 if locked else 401, "ACCOUNT_LOCKED" if locked else "UNAUTHENTICATED")


def issue_sessions(conn, user):
    now = datetime.now(UTC)
    result = {"user": public_user(user), "sessions": [], "serverTime": now.isoformat()}
    for kind, hours in (
        ("interactive", settings().interactive_hours),
        ("sync", settings().sync_hours),
    ):
        session_id = uuid4()
        expires = now + timedelta(hours=hours)
        conn.execute(
            "INSERT INTO bms.sessions "
            "(id,tenant_id,user_id,kind,token_hash,expires_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                session_id,
                user["tenant_id"],
                user["id"],
                kind,
                token_digest(token_for(session_id)),
                expires,
            ),
        )
        result["sessions"].append(
            {"id": str(session_id), "kind": kind, "expiresAt": expires.isoformat()}
        )
    result["offlineGrantExpiresAt"] = (
        now + timedelta(hours=settings().offline_grant_hours)
    ).isoformat()
    return result


def session_response(result):
    from uuid import UUID

    return {
        "user": result["user"],
        "offlineGrantExpiresAt": result["offlineGrantExpiresAt"],
        "serverTime": result.get("serverTime"),
        **{
            f"{session['kind']}Credential": {
                "token": token_for(UUID(session["id"])),
                "expiresAt": session["expiresAt"],
            }
            for session in result["sessions"]
        },
    }
