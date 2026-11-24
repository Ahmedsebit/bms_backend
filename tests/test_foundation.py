import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from conftest import (
    CHANGED_PASSWORD,
    INITIAL_PASSWORD,
    RESET_PASSWORD,
    command,
    headers,
    login,
)
from psycopg.rows import dict_row

from bms.db import connection, set_tenant
from bms.schemas import Command
from bms.security import CAPABILITIES, token_digest
from bms.services import audit


def test_temporary_password_gate_and_password_change_replay(client, new_user):
    _, session, _ = new_user(change=False)
    auth = headers(session)
    assert client.get("/api/v1/admin/users", headers=auth).status_code == 403
    body = command(currentPassword=INITIAL_PASSWORD, password=CHANGED_PASSWORD)
    first = client.post("/api/v1/auth/change-password", headers=auth, json=body)
    second = client.post("/api/v1/auth/change-password", headers=auth, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert not client.get("/api/v1/auth/me", headers=auth).json()["data"]["user"][
        "mustChangePassword"
    ]


def test_sign_in_replay_and_unknown_school_do_not_leak(client, admins):
    body = command(schoolCode="alpha", userId="ADMIN", password=CHANGED_PASSWORD)
    first = client.post("/api/v1/auth/sign-in", json=body)
    second = client.post("/api/v1/auth/sign-in", json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    for changes in (
        {"schoolCode": "UNKNOWN"},
        {"userId": "UNKNOWN"},
        {"password": "Wrong password!"},
    ):
        response = client.post("/api/v1/auth/sign-in", json={**body, **changes})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_password_validation_redacts_secrets_and_rejects_tenant_selector(client, admins):
    for password in ("", "z" * 129):
        response = client.post(
            "/api/v1/auth/sign-in",
            json=command(schoolCode="ALPHA", userId="ADMIN", password=password),
        )
        assert response.status_code == 400
        if password:
            assert password not in response.text
    response = client.post(
        "/api/v1/auth/sign-in",
        json=command(
            schoolCode="ALPHA", userId="ADMIN", password=CHANGED_PASSWORD, tenantId=str(uuid4())
        ),
    )
    assert response.status_code == 400


def test_five_wrong_passwords_lock_account_then_expire(client, new_user, database, schools):
    user, _, staff = new_user()
    for i in range(5):
        response = client.post(
            "/api/v1/auth/sign-in",
            json=command(schoolCode="ALPHA", userId=staff, password="Wrong password!"),
        )
        assert response.status_code == (423 if i == 4 else 401)
    response = client.post(
        "/api/v1/auth/sign-in",
        json=command(schoolCode="ALPHA", userId=staff, password=CHANGED_PASSWORD),
    )
    assert response.status_code == 423
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE bms.users SET locked_until=%s WHERE id=%s",
            (datetime.now(UTC) - timedelta(seconds=1), user["id"]),
        )
    assert login(client, "ALPHA", staff)["user"]["id"] == user["id"]


def test_signout_replay_keeps_creator_sync_credential(client, new_user, database):
    user, session, _ = new_user()
    body = command()
    for _ in range(2):
        response = client.post("/api/v1/auth/sign-out", headers=headers(session), json=body)
        assert response.status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers(session)).status_code == 401
    assert (
        client.post("/api/v1/auth/sign-out", headers=headers(session), json=command()).status_code
        == 401
    )
    sync_digest = token_digest(session["syncCredential"]["token"])
    with psycopg.connect(database) as conn:
        row = conn.execute(
            "SELECT user_id, revoked_at FROM bms.sessions WHERE token_hash=%s", (sync_digest,)
        ).fetchone()
        assert str(row[0]) == user["id"]
        assert row[1] is None
    assert (
        client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {session['syncCredential']['token']}"},
        ).status_code
        == 401
    )


def test_session_expiry_and_tamper(client, new_user, database):
    _, session, _ = new_user()
    token = session["interactiveCredential"]["token"]
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}tamper"}
        ).status_code
        == 401
    )
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE bms.sessions SET expires_at=now()-interval '1 second' WHERE token_hash=%s",
            (token_digest(token),),
        )
    assert client.get("/api/v1/auth/me", headers=headers(session)).status_code == 401


def test_current_password_verification_also_enforces_lockout(client, new_user):
    _, session, _ = new_user()
    for i in range(5):
        response = client.post(
            "/api/v1/auth/change-password",
            headers=headers(session),
            json=command(currentPassword="Wrong password!", password=RESET_PASSWORD),
        )
        assert response.status_code == (423 if i == 4 else 401)
    response = client.post(
        "/api/v1/auth/change-password",
        headers=headers(session),
        json=command(currentPassword=CHANGED_PASSWORD, password=RESET_PASSWORD),
    )
    assert response.status_code == 423


@pytest.mark.parametrize("role", ["officer", "supervisor", "leadership"])
def test_roles_cannot_manage_users(client, new_user, admins, role):
    _, session, _ = new_user(role)
    target = admins["ALPHA"]["user"]["id"]
    auth = headers(session)
    assert client.get("/api/v1/admin/users", headers=auth).status_code == 403
    assert client.get(f"/api/v1/admin/users/{target}", headers=auth).status_code == 403
    for path, body in (
        (
            "/api/v1/admin/users",
            command(
                id=str(uuid4()),
                name="Denied",
                userId=str(uuid4()),
                role="officer",
                password=INITIAL_PASSWORD,
            ),
        ),
        (
            f"/api/v1/admin/users/{target}/set-status",
            command(expectedRevision=1, status="inactive", reason="Denied"),
        ),
        (
            f"/api/v1/admin/users/{target}/reset-password",
            command(expectedRevision=1, password=INITIAL_PASSWORD, reason="Denied"),
        ),
    ):
        assert client.post(path, headers=auth, json=body).status_code == 403
    assert ("inspection.execute" in CAPABILITIES[role]) == (role != "leadership")
    assert "inspection.execute" not in CAPABILITIES["administrator"]
    expected_audit = 200 if role == "leadership" else 403
    assert client.get("/api/v1/audit", headers=auth).status_code == expected_audit


def test_user_create_replay_payload_mismatch_and_atomic_audit(client, admins, database):
    auth = headers(admins["ALPHA"])
    body = command(
        id=str(uuid4()),
        name="Replay",
        userId=str(uuid4()),
        role="officer",
        password=INITIAL_PASSWORD,
    )
    first = client.post("/api/v1/admin/users", headers=auth, json=body)
    second = client.post("/api/v1/admin/users", headers=auth, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    conflict = client.post("/api/v1/admin/users", headers=auth, json={**body, "name": "Changed"})
    assert conflict.status_code == 409
    with psycopg.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM bms.audit_events WHERE operation_id=%s",
                (body["clientOperationId"],),
            ).fetchone()[0]
            == 1
        )
        row = conn.execute(
            "SELECT result::text FROM bms.client_operations WHERE id=%s",
            (body["clientOperationId"],),
        ).fetchone()[0]
        assert INITIAL_PASSWORD not in row
        assert "password_hash" not in row
    duplicate = client.post(
        "/api/v1/admin/users", headers=auth, json={**body, "clientOperationId": str(uuid4())}
    )
    assert duplicate.status_code == 400


def test_concurrent_replay_creates_one_user(client, admins):
    body = command(
        id=str(uuid4()),
        name="Concurrent",
        userId=str(uuid4()),
        role="officer",
        password=INITIAL_PASSWORD,
    )
    auth = headers(admins["ALPHA"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: client.post("/api/v1/admin/users", headers=auth, json=body), range(2)
            )
        )
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json()["data"] == responses[1].json()["data"]


def test_reset_password_revision_replay_and_session_revocation(client, admins, new_user):
    user, session, staff = new_user()
    auth = headers(admins["ALPHA"])
    path = f"/api/v1/admin/users/{user['id']}/reset-password"
    body = command(
        expectedRevision=user["revision"] - 1, password=RESET_PASSWORD, reason="Lost password"
    )
    conflict = client.post(path, headers=auth, json=body)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["current"]["revision"] == user["revision"]
    body = command(
        expectedRevision=user["revision"], password=RESET_PASSWORD, reason="Lost password"
    )
    first = client.post(path, headers=auth, json=body)
    second = client.post(path, headers=auth, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert client.get("/api/v1/auth/me", headers=headers(session)).status_code == 401
    assert login(client, staff=staff, password=RESET_PASSWORD)["user"]["mustChangePassword"]


def test_deactivate_replay_and_history_preserved(client, admins, new_user):
    user, session, staff = new_user()
    auth = headers(admins["ALPHA"])
    path = f"/api/v1/admin/users/{user['id']}/set-status"
    body = command(expectedRevision=user["revision"], status="inactive", reason="No longer active")
    for _ in range(2):
        assert client.post(path, headers=auth, json=body).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers(session)).status_code == 401
    assert client.get(f"/api/v1/admin/users/{user['id']}", headers=auth).status_code == 200
    response = client.post(
        "/api/v1/auth/sign-in",
        json=command(schoolCode="ALPHA", userId=staff, password=CHANGED_PASSWORD),
    )
    assert response.status_code == 401


def test_cannot_deactivate_self(client, admins):
    user = client.get("/api/v1/auth/me", headers=headers(admins["ALPHA"])).json()["data"]["user"]
    response = client.post(
        f"/api/v1/admin/users/{user['id']}/set-status",
        headers=headers(admins["ALPHA"]),
        json=command(expectedRevision=user["revision"], status="inactive", reason="Self"),
    )
    assert response.status_code == 409


@pytest.mark.parametrize("source,target", [("ALPHA", "BETA"), ("BETA", "ALPHA")])
def test_cross_tenant_user_endpoints_are_not_found(client, admins, source, target):
    target_id = admins[target]["user"]["id"]
    auth = headers(admins[source])
    assert client.get(f"/api/v1/admin/users/{target_id}", headers=auth).status_code == 404
    for suffix, body in (
        ("set-status", command(expectedRevision=2, status="inactive", reason="Cross tenant")),
        (
            "reset-password",
            command(expectedRevision=2, password=INITIAL_PASSWORD, reason="Cross tenant"),
        ),
    ):
        response = client.post(f"/api/v1/admin/users/{target_id}/{suffix}", headers=auth, json=body)
        assert response.status_code == 404
    ids = {u["id"] for u in client.get("/api/v1/admin/users", headers=auth).json()["data"]["items"]}
    assert target_id not in ids
    assert client.get("/api/v1/auth/me", headers=auth).json()["data"]["user"]["id"] != target_id
    events = client.get("/api/v1/audit", headers=auth).json()["data"]["items"]
    assert all(event["actorId"] != target_id for event in events)


def test_cross_tenant_operations_do_not_replay_other_school(client, admins):
    operation_id = str(uuid4())
    for code in ("ALPHA", "BETA"):
        body = command(
            id=str(uuid4()),
            name=code,
            userId=str(uuid4()),
            role="officer",
            password=INITIAL_PASSWORD,
        )
        body["clientOperationId"] = operation_id
        response = client.post("/api/v1/admin/users", headers=headers(admins[code]), json=body)
        assert response.status_code == 200
        assert response.json()["data"]["name"] == code


def test_rls_blocks_unfiltered_reads_writes_and_audit_mutation(database, schools):
    alpha, _ = schools["ALPHA"]
    beta, beta_admin = schools["BETA"]
    with connection() as conn:
        assert conn.execute("SELECT * FROM bms.users").fetchall() == []
        set_tenant(conn, alpha)
        rows = conn.execute("SELECT tenant_id FROM bms.users").fetchall()
        assert rows and all(row["tenant_id"] == alpha for row in rows)
        assert conn.execute("SELECT * FROM bms.tenants WHERE id=%s", (beta,)).fetchall() == []
        assert (
            conn.execute(
                "UPDATE bms.users SET name='Wrong' WHERE id=%s RETURNING id", (beta_admin,)
            ).fetchall()
            == []
        )
        assert conn.execute(
            "SELECT rolbypassrls,rolsuper FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == {"rolbypassrls": False, "rolsuper": False}
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connection() as conn:
        set_tenant(conn, alpha)
        conn.execute("UPDATE bms.audit_events SET reason='tampered'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connection() as conn:
        set_tenant(conn, alpha)
        conn.execute("DELETE FROM bms.audit_events")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connection() as conn:
        set_tenant(conn, alpha)
        conn.execute(
            "INSERT INTO bms.users(id,tenant_id,name,login_id,password_hash,role) "
            "VALUES (%s,%s,'Wrong','wrong','no','officer')",
            (uuid4(), beta),
        )


def test_audit_and_state_rollback_together(database, schools):
    alpha, admin_id = schools["ALPHA"]
    operation_id = uuid4()
    with pytest.raises(RuntimeError), connection() as conn:
        set_tenant(conn, alpha)
        audit(
            conn,
            {"id": admin_id, "tenant_id": alpha, "role": "administrator"},
            "rollback.test",
            admin_id,
            Command(client_operation_id=operation_id),
        )
        conn.execute(
            "UPDATE bms.users SET name='Rolled back' WHERE tenant_id=%s AND id=%s",
            (alpha, admin_id),
        )
        raise RuntimeError("simulate failed command")
    with psycopg.connect(database, row_factory=dict_row) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM bms.audit_events WHERE operation_id=%s", (operation_id,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute("SELECT name FROM bms.users WHERE id=%s", (admin_id,)).fetchone()["name"]
            != "Rolled back"
        )


def test_correlation_validation_and_audit_redaction(client, admins):
    auth = headers(admins["ALPHA"])
    for path in ("/api/v1/auth/me", "/api/v1/audit", "/api/v1/admin/users"):
        response = client.get(path, headers=auth)
        assert response.status_code == 200
        assert response.json()["correlationId"] == response.headers["X-Correlation-ID"]
        assert response.headers["Cache-Control"] == "no-store"
    events = client.get("/api/v1/audit?limit=100", headers=auth).json()["data"]["items"]
    encoded = json.dumps(events)
    assert CHANGED_PASSWORD not in encoded and INITIAL_PASSWORD not in encoded
    assert "password_hash" not in encoded and "token_hash" not in encoded
    assert client.get("/api/v1/admin/users?limit=101", headers=auth).status_code == 400
    assert client.get("/api/v1/unknown").status_code == 404


def test_all_protected_routes_reject_missing_auth(client):
    from bms.app import app

    paths = []
    for route in app.routes:
        if route.path.startswith("/api/v1/") and route.path != "/api/v1/auth/sign-in":
            path = re.sub(r"\{[^}]+\}", lambda _: str(uuid4()), route.path)
            paths.append(path)
            method = next(method for method in ("GET", "POST", "PUT") if method in route.methods)
            response = client.request(method, path, json={} if method == "POST" else None)
            assert response.status_code == 401, (path, response.text)
    assert paths


def test_all_m1_tables_force_tenant_rls(database, schools):
    with psycopg.connect(database, row_factory=dict_row) as conn:
        tables = conn.execute(
            "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='bms' AND c.relkind='r'"
        ).fetchall()
    assert tables
    for table in tables:
        assert table["relrowsecurity"] and table["relforcerowsecurity"]
        with connection() as conn:
            query = psycopg.sql.SQL("SELECT tenant_id FROM bms.{}").format(
                psycopg.sql.Identifier(table["relname"])
            )
            assert conn.execute(query).fetchall() == []
            set_tenant(conn, schools["ALPHA"][0])
            assert all(
                row["tenant_id"] == schools["ALPHA"][0] for row in conn.execute(query).fetchall()
            )


def test_api_contract_documents_requests_responses_and_errors(client):
    schema = client.get("/openapi.json").json()
    for path, methods in schema["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for method, operation in methods.items():
            content = operation["responses"]["200"]["content"]
            if "image/jpeg" in content:
                assert content["image/jpeg"]["schema"]["format"] == "binary"
            else:
                assert "$ref" in content["application/json"]["schema"]
            assert "400" in operation["responses"]
            assert "401" in operation["responses"]
            if method == "post" and not path.endswith(("/upload-ticket", "/download-ticket")):
                assert operation["requestBody"]["required"]
