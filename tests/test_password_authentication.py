import os
from importlib.resources import files
from uuid import uuid4

import psycopg
import pytest
from conftest import CHANGED_PASSWORD, INITIAL_PASSWORD, command, headers, login
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from bms.app import app
from bms.cli import reset_account_password
from bms.config import settings
from bms.db import migrate
from bms.schemas import CreateUser
from bms.security import hash_password, hasher, token_digest, verify_password


@pytest.mark.parametrize(
    "password",
    ["123456", "short password", "x" * 129, "passwordpassword", " " * 20, "x" * 20],
)
def test_new_password_policy_rejects_weak_values(client, admins, password):
    response = client.post(
        "/api/v1/admin/users",
        headers=headers(admins["ALPHA"]),
        json=command(
            id=str(uuid4()),
            name="Policy Test",
            userId=str(uuid4()),
            role="officer",
            password=password,
        ),
    )
    assert response.status_code == 400
    assert password not in response.text


@pytest.mark.parametrize(
    "password", ["Long passphrase with spaces", "界" * 14 + "a", "z" * 127 + "!"]
)
def test_passwords_accept_spaces_unicode_and_full_length(client, admins, password):
    staff = str(uuid4())
    response = client.post(
        "/api/v1/admin/users",
        headers=headers(admins["ALPHA"]),
        json=command(
            id=str(uuid4()),
            name="Policy Test",
            userId=staff,
            role="officer",
            password=password,
        ),
    )
    assert response.status_code == 200
    assert login(client, staff=staff, password=password)["user"]["userId"] == staff


def test_password_whitespace_case_and_unicode_normalization():
    password = "  A long passphrase with spaces  "
    encoded = hash_password(password)
    assert verify_password(password, encoded)
    assert not verify_password(password.strip(), encoded)
    assert not verify_password(password.lower(), encoded)
    assert verify_password("e\u0301" * 15, hash_password("\u00e9" * 15))


def test_model_keeps_password_whitespace_and_rejects_multiple_roles(client, admins):
    body = command(
        id=str(uuid4()),
        name="Whitespace",
        userId=str(uuid4()),
        role="officer",
        password="  Long password with spaces  ",
    )
    parsed = CreateUser.model_validate(body)
    assert parsed.password.get_secret_value() == body["password"]
    response = client.post(
        "/api/v1/admin/users",
        headers=headers(admins["ALPHA"]),
        json={**body, "role": ["officer", "administrator"]},
    )
    assert response.status_code == 400


def test_legacy_pin_contract_is_not_accepted(client):
    response = client.post(
        "/api/v1/auth/sign-in",
        json=command(schoolCode="ALPHA", loginId="ADMIN", pin="123456"),
    )
    assert response.status_code == 400
    schema = client.get("/openapi.json").json()
    assert "/api/v1/auth/change-pin" not in schema["paths"]
    assert "/api/v1/admin/users/{user_id}/reset-pin" not in schema["paths"]


@pytest.fixture
def legacy_database(database):
    name = f"bms_legacy_test_{uuid4().hex}"
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(database)
    params["dbname"] = name
    url = make_conninfo(**params)
    try:
        with psycopg.connect(url) as conn:
            conn.execute(files("bms").joinpath("migrations", "001_foundation.sql").read_text())
            conn.execute(
                "CREATE TABLE public.bms_schema_versions "
                "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.execute(
                "INSERT INTO public.bms_schema_versions(version) VALUES ('001_foundation.sql')"
            )
        yield url
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_pin_upgrade_preserves_history_revokes_sessions_and_supports_recovery(
    legacy_database, monkeypatch
):
    url = legacy_database
    tenant_id, user_id, event_id = uuid4(), uuid4(), uuid4()
    tokens = ["synthetic-old-interactive", "synthetic-old-sync"]
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO bms.tenants(id,tenant_id,school_code,name,timezone) "
            "VALUES (%s,%s,'LEGACY','Synthetic Legacy','Africa/Nairobi')",
            (tenant_id, tenant_id),
        )
        conn.execute(
            "INSERT INTO bms.users(id,tenant_id,name,login_id,pin_hash,role,must_change_pin) "
            "VALUES (%s,%s,'Legacy Admin','ADMIN',%s,'administrator',false)",
            (user_id, tenant_id, hasher.hash("123456")),
        )
        for kind, token in zip(("interactive", "sync"), tokens, strict=True):
            conn.execute(
                "INSERT INTO bms.sessions(id,tenant_id,user_id,kind,token_hash,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,now()+interval '72 hours')",
                (uuid4(), tenant_id, user_id, kind, token_digest(token)),
            )
        conn.execute(
            "INSERT INTO bms.audit_events "
            "(id,tenant_id,actor_id,actor_role,action,entity_id,operation_id) "
            "VALUES (%s,%s,%s,'administrator','legacy.example',%s,%s)",
            (event_id, tenant_id, user_id, user_id, uuid4()),
        )
    migrate(url)
    migrate(url)
    with psycopg.connect(url, row_factory=dict_row) as conn:
        user = conn.execute("SELECT * FROM bms.users WHERE id=%s", (user_id,)).fetchone()
        assert user["login_id"] == "ADMIN"
        assert user["password_hash"] is None
        assert user["must_change_password"] and user["revision"] == 2
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM bms.sessions WHERE revoked_at IS NULL"
            ).fetchone()["n"]
            == 0
        )
        events = conn.execute("SELECT * FROM bms.audit_events").fetchall()
        assert len(events) == 2
        assert any(row["id"] == event_id for row in events)
        assert any(row["actor_id"] is None and row["actor_role"] == "system" for row in events)
    with monkeypatch.context() as patch:
        patch.setenv("BMS_DATABASE_URL", url)
        settings.cache_clear()
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/auth/sign-in",
                    json=command(schoolCode="LEGACY", userId="ADMIN", password="123456"),
                )
                assert response.status_code == 401
                assert (
                    client.get(
                        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens[0]}"}
                    ).status_code
                    == 401
                )
                reset_account_password(url, "LEGACY", "ADMIN", INITIAL_PASSWORD, "PIN migration")
                session = login(client, "LEGACY", password=INITIAL_PASSWORD)
                assert session["user"]["id"] == str(user_id)
                assert session["user"]["mustChangePassword"]
                response = client.post(
                    "/api/v1/auth/change-password",
                    headers=headers(session),
                    json=command(currentPassword=INITIAL_PASSWORD, password=CHANGED_PASSWORD),
                )
                assert response.status_code == 200
                assert (
                    client.get("/api/v1/admin/users", headers=headers(session)).status_code == 200
                )
                audit = client.get("/api/v1/audit", headers=headers(session))
                assert audit.status_code == 200
                assert any(
                    row["action"] == "users.password_recovery" and row["actorRole"] == "system"
                    for row in audit.json()["data"]["items"]
                )
        finally:
            settings.cache_clear()
    assert os.environ["BMS_DATABASE_URL"] != url
