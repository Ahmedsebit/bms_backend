import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.errors import UniqueViolation

from bms.db import connection, set_tenant
from bms.operation_routes import register_routes
from bms.responses import (
    AuditList,
    Envelope,
    ErrorEnvelope,
    HealthView,
    MeView,
    SignInView,
    SignOutView,
    UserList,
    UserView,
)
from bms.schemas import ChangePassword, Command, CreateUser, ResetPassword, SignIn, UpdateStatus
from bms.security import CAPABILITIES, dummy_hash, hash_password, token_digest, verify_password
from bms.services import (
    USER_FIELDS,
    ApiError,
    audit,
    check_revision,
    failed_password,
    get_user,
    issue_sessions,
    lock_tenant,
    public_user,
    remember,
    replay,
    session_response,
)

app = FastAPI(
    title="BMS Foundation API",
    version="0.2.0",
    responses={
        status: {"model": ErrorEnvelope} for status in (400, 401, 403, 404, 405, 409, 423, 500)
    },
)
bearer = HTTPBearer(auto_error=False)
log = logging.getLogger("bms")


@app.middleware("http")
async def correlation(request: Request, call_next):
    request.state.correlation_id = str(uuid4())
    try:
        response = await call_next(request)
    except Exception:
        # Do not log request bodies, SQL parameters, credentials or exception strings.
        log.error("request_failed correlation_id=%s", request.state.correlation_id)
        response = failure(request, 500, "INTERNAL_ERROR")
    response.headers["X-Correlation-ID"] = request.state.correlation_id
    response.headers["Cache-Control"] = "no-store"
    return response


def success(request, data):
    return {"data": data, "correlationId": request.state.correlation_id}


def failure(request, status, code, current=None, details=None):
    error = {"code": code, "messageKey": f"errors.{code.lower()}"}
    if current is not None:
        error["current"] = current
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status,
        content=jsonable_encoder({"error": error, "correlationId": request.state.correlation_id}),
    )


@app.exception_handler(ApiError)
async def api_error(request, exc):
    return failure(request, exc.status, exc.code, exc.current)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    details = [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
    return failure(request, 400, "VALIDATION_FAILED", details=details)


@app.exception_handler(UniqueViolation)
async def unique_error(request, exc):
    return failure(request, 400, "VALIDATION_FAILED")


@app.exception_handler(404)
async def not_found(request, exc):
    return failure(request, 404, "NOT_FOUND")


@app.exception_handler(405)
async def method_not_allowed(request, exc):
    return failure(request, 405, "METHOD_NOT_ALLOWED")


def authenticated(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    if not credentials:
        raise ApiError(401, "UNAUTHENTICATED")
    digest = token_digest(credentials.credentials)
    with connection() as conn:
        tenant = conn.execute("SELECT bms.session_tenant(%s) AS id", (digest,)).fetchone()
        if not tenant["id"]:
            raise ApiError(401, "UNAUTHENTICATED")
        tenant_id = tenant["id"]
        set_tenant(conn, tenant_id)
        lock_tenant(conn, tenant_id)
        session = conn.execute(
            "SELECT * FROM bms.sessions WHERE tenant_id = %s AND token_hash = %s",
            (tenant_id, digest),
        ).fetchone()
        user = get_user(conn, tenant_id, session["user_id"])
        sign_out = request.url.path == "/api/v1/auth/sign-out"
        expired = session["expires_at"] <= datetime.now(UTC)
        sync_path = request.url.path.startswith(("/api/v1/sync/", "/api/v1/evidence/"))
        sync_session = session["kind"] == "sync"
        if sync_session and not sync_path:
            raise ApiError(401, "UNAUTHENTICATED")
        review_upload = request.url.path.startswith("/api/v1/evidence/") and (
            (request.method == "POST" and request.url.path.endswith("/upload-ticket"))
            or (request.method == "PUT" and request.url.path.endswith("/content"))
        )
        review_only = (
            sync_session
            and not expired
            and (request.url.path == "/api/v1/sync/operations" or review_upload)
            and (user["status"] != "active" or session["revoked_at"] is not None)
        )
        session["_review_only"] = review_only
        if user["status"] != "active" and not review_only:
            raise ApiError(401, "UNAUTHENTICATED")
        if (expired or session["revoked_at"]) and not sign_out and not review_only:
            raise ApiError(401, "UNAUTHENTICATED")
        if (
            user["must_change_password"]
            and not review_only
            and request.url.path
            not in {
                "/api/v1/auth/me",
                "/api/v1/auth/change-password",
                "/api/v1/auth/sign-out",
            }
        ):
            raise ApiError(403, "PASSWORD_CHANGE_REQUIRED")
        yield conn, user, session


Context = Annotated[tuple, Depends(authenticated, scope="function")]


def require(user, capability):
    if capability not in CAPABILITIES[user["role"]]:
        raise ApiError(403, "FORBIDDEN")


@app.get("/health", response_model=Envelope[HealthView])
def health(request: Request):
    return success(request, {"status": "ok"})


@app.post("/api/v1/auth/sign-in", response_model=Envelope[SignInView])
def sign_in(request: Request, body: SignIn):
    with connection() as conn:
        tenant = conn.execute(
            "SELECT bms.school_tenant(%s) AS id", (body.school_code.upper(),)
        ).fetchone()
        if not tenant["id"]:
            verify_password(body.password.get_secret_value(), dummy_hash)
            raise ApiError(401, "UNAUTHENTICATED")
        set_tenant(conn, tenant["id"])
        lock_tenant(conn, tenant["id"])
        user = conn.execute(
            "SELECT * FROM bms.users WHERE tenant_id = %s AND login_id = %s",
            (tenant["id"], body.user_id),
        ).fetchone()
        if not user or user["status"] != "active":
            verify_password(body.password.get_secret_value(), dummy_hash)
            raise ApiError(401, "UNAUTHENTICATED")
        now = datetime.now(UTC)
        if user["locked_until"] and user["locked_until"] > now:
            raise ApiError(423, "ACCOUNT_LOCKED")
        if not verify_password(body.password.get_secret_value(), user["password_hash"]):
            failed_password(conn, user, body)
        if body.client_type == "android" and user["role"] == "leadership":
            raise ApiError(403, "MOBILE_ACCESS_FORBIDDEN")
        old = replay(conn, user, "auth.sign_in", body)
        if old is not None:
            return success(request, session_response(old))
        conn.execute(
            "UPDATE bms.users SET failed_password_attempts=0, locked_until=NULL, "
            "updated_at=now(), revision=revision+1 WHERE tenant_id=%s AND id=%s "
            "AND (failed_password_attempts<>0 OR locked_until IS NOT NULL)",
            (tenant["id"], user["id"]),
        )
        user = get_user(conn, tenant["id"], user["id"])
        result = issue_sessions(conn, user)
        audit(conn, user, "auth.sign_in", user["id"], body)
        remember(conn, user, "auth.sign_in", body, result)
        return success(request, session_response(result))


@app.get("/api/v1/auth/me", response_model=Envelope[MeView])
def me(request: Request, context: Context):
    _, user, _ = context
    return success(
        request,
        {
            "user": public_user(user),
            "capabilities": sorted(CAPABILITIES[user["role"]]),
            "serverTime": datetime.now(UTC).isoformat(),
        },
    )


@app.post("/api/v1/auth/change-password", response_model=Envelope[UserView])
def change_password(request: Request, body: ChangePassword, context: Context):
    conn, user, _ = context
    old = replay(conn, user, "auth.change_password", body)
    if old is not None:
        return success(request, old)
    row = conn.execute(
        "SELECT * FROM bms.users WHERE tenant_id=%s AND id=%s",
        (user["tenant_id"], user["id"]),
    ).fetchone()
    if row["locked_until"] and row["locked_until"] > datetime.now(UTC):
        raise ApiError(423, "ACCOUNT_LOCKED")
    if not verify_password(body.current_password.get_secret_value(), row["password_hash"]):
        failed_password(conn, row, body)
    if body.current_password.get_secret_value() == body.password.get_secret_value():
        raise ApiError(400, "VALIDATION_FAILED")
    conn.execute(
        "UPDATE bms.users SET password_hash=%s, must_change_password=false, "
        "failed_password_attempts=0, "
        "locked_until=NULL, revision=revision+1, "
        "updated_at=now() WHERE tenant_id=%s AND id=%s",
        (hash_password(body.password.get_secret_value()), user["tenant_id"], user["id"]),
    )
    result = public_user(get_user(conn, user["tenant_id"], user["id"]))
    audit(conn, user, "auth.change_password", user["id"], body)
    return success(request, remember(conn, user, "auth.change_password", body, result))


@app.post("/api/v1/auth/sign-out", response_model=Envelope[SignOutView])
def sign_out(request: Request, body: Command, context: Context):
    conn, user, session = context
    command = f"auth.sign_out:{session['id']}"
    old = replay(conn, user, command, body)
    if old is not None:
        return success(request, old)
    if session["revoked_at"] or session["expires_at"] <= datetime.now(UTC):
        raise ApiError(401, "UNAUTHENTICATED")
    conn.execute(
        "UPDATE bms.sessions SET revoked_at=now(), updated_at=now(), revision=revision+1 "
        "WHERE tenant_id=%s AND id=%s",
        (user["tenant_id"], session["id"]),
    )
    audit(conn, user, "auth.sign_out", user["id"], body)
    return success(request, remember(conn, user, command, body, {"signedOut": True}))


@app.get("/api/v1/admin/users", response_model=Envelope[UserList])
def list_users(request: Request, context: Context, limit: int = 50, offset: int = 0):
    conn, user, _ = context
    require(user, "users.manage")
    if not 1 <= limit <= 100 or offset < 0:
        raise ApiError(400, "VALIDATION_FAILED")
    rows = conn.execute(
        f"SELECT {USER_FIELDS} FROM bms.users WHERE tenant_id=%s "
        "ORDER BY created_at, id LIMIT %s OFFSET %s",
        (user["tenant_id"], limit, offset),
    ).fetchall()
    return success(request, {"items": [public_user(row) for row in rows]})


@app.get("/api/v1/admin/users/{user_id}", response_model=Envelope[UserView])
def user_detail(request: Request, user_id: UUID, context: Context):
    conn, user, _ = context
    target = get_user(conn, user["tenant_id"], user_id)
    require(user, "users.manage")
    return success(request, public_user(target))


@app.post("/api/v1/admin/users", response_model=Envelope[UserView])
def create_user(request: Request, body: CreateUser, context: Context):
    conn, actor, _ = context
    require(actor, "users.manage")
    old = replay(conn, actor, "users.create", body)
    if old is not None:
        return success(request, old)
    conn.execute(
        "INSERT INTO bms.users(id,tenant_id,name,login_id,password_hash,role) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (
            body.id,
            actor["tenant_id"],
            body.name,
            body.user_id,
            hash_password(body.password.get_secret_value()),
            body.role.value,
        ),
    )
    result = public_user(get_user(conn, actor["tenant_id"], body.id))
    audit(conn, actor, "users.create", body.id, body, after=result)
    return success(request, remember(conn, actor, "users.create", body, result))


@app.post("/api/v1/admin/users/{user_id}/set-status", response_model=Envelope[UserView])
def set_status(request: Request, user_id: UUID, body: UpdateStatus, context: Context):
    conn, actor, _ = context
    target = get_user(conn, actor["tenant_id"], user_id)
    require(actor, "users.manage")
    command = f"users.set_status:{user_id}"
    old = replay(conn, actor, command, body)
    if old is not None:
        return success(request, old)
    check_revision(target, body.expected_revision)
    if target["role"] == "administrator" and body.status == "inactive":
        count = conn.execute(
            "SELECT count(*) AS n FROM bms.users WHERE tenant_id=%s "
            "AND role='administrator' AND status='active'",
            (actor["tenant_id"],),
        ).fetchone()["n"]
        if target["id"] == actor["id"] or count <= 1:
            raise ApiError(409, "INVALID_TRANSITION")
    before = public_user(target)
    conn.execute(
        "UPDATE bms.users SET status=%s, revision=revision+1, updated_at=now() "
        "WHERE tenant_id=%s AND id=%s",
        (body.status, actor["tenant_id"], user_id),
    )
    if body.status == "inactive":
        revoke_sessions(conn, actor["tenant_id"], user_id)
    result = public_user(get_user(conn, actor["tenant_id"], user_id))
    audit(
        conn,
        actor,
        "users.set_status",
        user_id,
        body,
        before=before,
        after=result,
        reason=body.reason,
    )
    return success(request, remember(conn, actor, command, body, result))


def revoke_sessions(conn, tenant_id, user_id):
    conn.execute(
        "UPDATE bms.sessions SET revoked_at=now(), updated_at=now(), revision=revision+1 "
        "WHERE tenant_id=%s AND user_id=%s AND revoked_at IS NULL",
        (tenant_id, user_id),
    )


@app.post("/api/v1/admin/users/{user_id}/reset-password", response_model=Envelope[UserView])
def reset_password(request: Request, user_id: UUID, body: ResetPassword, context: Context):
    conn, actor, _ = context
    target = get_user(conn, actor["tenant_id"], user_id)
    require(actor, "users.manage")
    command = f"users.reset_password:{user_id}"
    old = replay(conn, actor, command, body)
    if old is not None:
        return success(request, old)
    check_revision(target, body.expected_revision)
    if user_id == actor["id"]:
        raise ApiError(409, "INVALID_TRANSITION")
    conn.execute(
        "UPDATE bms.users SET password_hash=%s, must_change_password=true, "
        "failed_password_attempts=0, "
        "locked_until=NULL, revision=revision+1, updated_at=now() WHERE tenant_id=%s AND id=%s",
        (hash_password(body.password.get_secret_value()), actor["tenant_id"], user_id),
    )
    revoke_sessions(conn, actor["tenant_id"], user_id)
    result = public_user(get_user(conn, actor["tenant_id"], user_id))
    audit(conn, actor, "users.reset_password", user_id, body, after=result, reason=body.reason)
    return success(request, remember(conn, actor, command, body, result))


@app.get("/api/v1/audit", response_model=Envelope[AuditList])
def list_audit(request: Request, context: Context, limit: int = 50, offset: int = 0):
    conn, actor, _ = context
    require(actor, "audit.read")
    if not 1 <= limit <= 100 or offset < 0:
        raise ApiError(400, "VALIDATION_FAILED")
    rows = conn.execute(
        "SELECT * FROM bms.audit_events WHERE tenant_id=%s "
        "ORDER BY received_at DESC, id LIMIT %s OFFSET %s",
        (actor["tenant_id"], limit, offset),
    ).fetchall()
    return success(request, {"items": jsonable_encoder(rows)})


register_routes(app, authenticated, success)
