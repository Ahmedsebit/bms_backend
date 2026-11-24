import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from bms.app import app
from bms.cli import bootstrap
from bms.config import settings
from bms.db import migrate

INITIAL_PASSWORD = "Initial synthetic password!"
CHANGED_PASSWORD = "Changed synthetic password!"
RESET_PASSWORD = "Reset synthetic password!"


@pytest.fixture(scope="session")
def database():
    admin_url = os.environ.get("BMS_TEST_ADMIN_DATABASE_URL")
    if not admin_url:
        pytest.fail(
            "BMS_TEST_ADMIN_DATABASE_URL is required. Tests create and drop their own database."
        )
    name = f"bms_test_{uuid4().hex}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(admin_url)
    params["dbname"] = name
    url = make_conninfo(**params)
    os.environ["BMS_DATABASE_URL"] = url
    os.environ["BMS_SECRET_KEY"] = "synthetic-tests-only-secret-not-for-deployment"
    settings.cache_clear()
    try:
        migrate(url)
        migrate(url)
        yield url
    finally:
        settings.cache_clear()
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def client(database):
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def schools(database):
    return {
        code: bootstrap(
            database,
            code,
            f"Synthetic {code}",
            "Africa/Nairobi",
            "ADMIN",
            "Test Admin",
            INITIAL_PASSWORD,
        )
        for code in ("ALPHA", "BETA")
    }


def command(**kwargs):
    return {"clientOperationId": str(uuid4()), **kwargs}


def login(client, code="ALPHA", staff="ADMIN", password=CHANGED_PASSWORD):
    response = client.post(
        "/api/v1/auth/sign-in", json=command(schoolCode=code, userId=staff, password=password)
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def headers(session):
    return {"Authorization": f"Bearer {session['interactiveCredential']['token']}"}


@pytest.fixture(scope="session")
def admins(client, schools):
    result = {}
    for code in schools:
        session = login(client, code, password=INITIAL_PASSWORD)
        response = client.post(
            "/api/v1/auth/change-password",
            headers=headers(session),
            json=command(currentPassword=INITIAL_PASSWORD, password=CHANGED_PASSWORD),
        )
        assert response.status_code == 200, response.text
        result[code] = login(client, code)
    return result


@pytest.fixture
def new_user(client, admins):
    def create(role="officer", school="ALPHA", change=True):
        staff_id = f"STAFF-{uuid4().hex[:12]}"
        body = command(
            id=str(uuid4()),
            name="Synthetic Staff",
            userId=staff_id,
            password=INITIAL_PASSWORD,
            role=role,
        )
        response = client.post("/api/v1/admin/users", json=body, headers=headers(admins[school]))
        assert response.status_code == 200, response.text
        user = response.json()["data"]
        session = login(client, school, staff_id, INITIAL_PASSWORD)
        if change:
            response = client.post(
                "/api/v1/auth/change-password",
                headers=headers(session),
                json=command(currentPassword=INITIAL_PASSWORD, password=CHANGED_PASSWORD),
            )
            assert response.status_code == 200, response.text
            user = response.json()["data"]
        return user, session, staff_id

    return create
