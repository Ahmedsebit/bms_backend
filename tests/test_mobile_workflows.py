import hashlib
import io
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from conftest import CHANGED_PASSWORD, command, headers
from PIL import Image
from psycopg.rows import dict_row

from bms.config import settings
from bms.db import connection, set_tenant
from bms.scheduling import schedule_school


def post(client, session, path, body, status=200):
    response = client.post(path, headers=headers(session) if session else {}, json=body)
    assert response.status_code == status, response.text
    return response.json().get("data", response.json().get("error"))


@pytest.fixture(autouse=True)
def private_evidence_directory(tmp_path, monkeypatch, database):
    monkeypatch.setattr(settings(), "evidence_directory", tmp_path / "evidence")


def photo(item_key=None):
    output = io.BytesIO()
    Image.new("RGB", (40, 30), (100, 150, 100)).save(output, "JPEG")
    content = output.getvalue()
    return {
        "id": str(uuid4()),
        "capturedAt": datetime.now(UTC).isoformat(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "byteSize": len(content),
        "itemKey": item_key,
    }, content


def upload(client, session, descriptor, content):
    result = post(
        client,
        session,
        f"/api/v1/evidence/{descriptor['id']}/upload-ticket",
        None,
    )
    response = client.put(
        result["url"],
        headers={**headers(session), "Content-Type": "image/jpeg"},
        content=content,
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def workspace(client, admins, new_user):
    officer, officer_session, _ = new_user()
    supervisor, supervisor_session, _ = new_user("supervisor")
    admin = admins["ALPHA"]
    building = post(
        client,
        admin,
        "/api/v1/facilities",
        command(
            id=str(uuid4()),
            level="building",
            name="Synthetic mobile dormitory",
        ),
    )
    for holder, scope in ((admin["user"]["id"], None), (supervisor["id"], building["id"])):
        for capability in ("incident.command", "roster.manage", "safety.approve", "safety.verify"):
            post(
                client,
                admin,
                "/api/v1/emergency/commands",
                {
                    "command": "duty.grant",
                    "payload": command(
                        id=str(uuid4()),
                        userId=holder,
                        buildingId=scope,
                        capability=capability,
                        startsAt=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                        endsAt=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
                        approvalNote="Synthetic approved test duty",
                    ),
                },
            )
    zone = post(
        client,
        admin,
        "/api/v1/facilities",
        command(
            id=str(uuid4()),
            level="zone",
            parentId=building["id"],
            name="Ground floor",
        ),
    )
    location = post(
        client,
        admin,
        "/api/v1/facilities",
        command(
            id=str(uuid4()),
            level="location",
            parentId=zone["id"],
            name="Synthetic door",
            locationType="door",
        ),
    )
    assignment = post(
        client,
        admin,
        f"/api/v1/zones/{zone['id']}/responsibility",
        command(
            primaryUserId=officer["id"],
            backupUserId=supervisor["id"],
            expectedRevision=0,
        ),
    )
    programme_body = command(
        id=str(uuid4()),
        name="UNAPPROVED synthetic practice check",
        recurrence="daily",
        startTime="00:00:00",
        endTime="23:59:00",
        graceMinutes=60,
        lifeSafety=True,
        locationIds=[location["id"]],
        items=[
            {
                "key": "condition",
                "type": "pass_concern_fail",
                "text": "Synthetic practice condition",
                "required": True,
                "photo": "on_fail",
                "createsFindingOn": ["concern", "fail"],
                "findingSeverity": "high",
            }
        ],
    )
    programme = post(client, admin, "/api/v1/programmes", programme_body)
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(officer_session)).json()["data"]
    occurrence = next(row for row in snap["occurrences"] if row["programmeId"] == programme["id"])
    return {
        "admin": admin,
        "officer": officer,
        "officerSession": officer_session,
        "supervisor": supervisor,
        "supervisorSession": supervisor_session,
        "building": building,
        "zone": zone,
        "location": location,
        "assignment": assignment,
        "programme": programme,
        "programmeBody": programme_body,
        "occurrence": occurrence,
    }


def inspection_body(workspace, value="pass", evidence=None):
    now = datetime.now(UTC).isoformat()
    answer = {"itemKey": "condition", "value": value}
    if evidence:
        answer.update(description="Synthetic concern, no people.", evidenceId=evidence["id"])
    return command(
        id=str(uuid4()),
        occurrenceId=workspace["occurrence"]["id"],
        locationMethod="manual",
        startedAt=now,
        completedAt=now,
        answers=[answer],
        evidence=[evidence] if evidence else [],
    )


def test_real_roles_scopes_and_cross_tenant_ids(client, admins, new_user, workspace):
    w = workspace
    outsider, outsider_session, outsider_login = new_user()
    body = inspection_body(w)
    post(client, w["admin"], "/api/v1/inspections/complete", body, 403)
    post(client, outsider_session, "/api/v1/inspections/complete", body, 403)
    post(client, admins["BETA"], "/api/v1/inspections/complete", body, 404)
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(outsider_session)).json()["data"]
    assert w["location"]["id"] not in {row["id"] for row in snap["facilities"]}
    leadership, leadership_session, staff = new_user("leadership")
    post(
        client,
        None,
        "/api/v1/auth/sign-in",
        command(
            schoolCode="ALPHA",
            userId=staff,
            password=CHANGED_PASSWORD,
            clientType="android",
        ),
        403,
    )
    assert (
        client.get(
            "/api/v1/mobile/snapshot",
            headers=headers(leadership_session),
        ).status_code
        == 403
    )
    other_building = post(
        client,
        w["admin"],
        "/api/v1/facilities",
        command(
            id=str(uuid4()),
            level="building",
            name="Different scope",
        ),
    )
    current = client.get(
        f"/api/v1/admin/users/{w['supervisor']['id']}",
        headers=headers(w["admin"]),
    ).json()["data"]
    result = post(
        client,
        w["admin"],
        f"/api/v1/admin/users/{current['id']}/scope",
        command(
            expectedRevision=current["revision"],
            buildingIds=[other_building["id"]],
        ),
    )
    post(client, w["supervisorSession"], "/api/v1/inspections/complete", body, 403)
    scoped = client.get(
        "/api/v1/mobile/snapshot",
        headers=headers(w["supervisorSession"]),
    ).json()["data"]
    assert not scoped["occurrences"] and not scoped["findings"]
    post(
        client,
        w["admin"],
        f"/api/v1/admin/users/{current['id']}/scope",
        command(
            expectedRevision=result["revision"],
            buildingIds=[],
        ),
    )
    post(client, w["supervisorSession"], "/api/v1/inspections/complete", body)


def test_required_concern_evidence_atomic_findings_and_independent_closure(client, workspace):
    w = workspace
    body = inspection_body(w, "concern")
    post(client, w["officerSession"], "/api/v1/inspections/complete", body, 400)
    evidence, content = photo("condition")
    body = inspection_body(w, "concern", evidence)
    result = post(client, w["officerSession"], "/api/v1/inspections/complete", body)
    replayed = post(client, w["officerSession"], "/api/v1/inspections/complete", body)
    assert result == replayed and len(result["findingIds"]) == 1
    finding_id = result["findingIds"][0]
    action = post(
        client,
        w["supervisorSession"],
        f"/api/v1/findings/{finding_id}/assign",
        command(
            expectedRevision=1,
            actionId=str(uuid4()),
            ownerId=w["supervisor"]["id"],
            title="Synthetic corrective action",
            dueDate=datetime.now(UTC).date().isoformat(),
        ),
    )
    completion = command(
        expectedRevision=action["revision"],
        note="Synthetic repair",
        completedAt=datetime.now(UTC).isoformat(),
    )
    post(
        client, w["supervisorSession"], f"/api/v1/actions/{action['id']}/complete", completion, 400
    )
    completion_photo, completion_bytes = photo()
    completion["evidence"] = [completion_photo]
    completed = post(
        client,
        w["supervisorSession"],
        f"/api/v1/actions/{action['id']}/complete",
        completion,
    )
    review = command(expectedRevision=completed["revision"], note="Independent review")
    for action_name in ("verify", "reject"):
        error = post(
            client,
            w["supervisorSession"],
            f"/api/v1/actions/{action['id']}/{action_name}",
            review,
            403,
        )
        assert error["code"] == "INDEPENDENT_VERIFIER_REQUIRED"
    post(client, w["admin"], f"/api/v1/actions/{action['id']}/verify", review, 409)
    upload(client, w["officerSession"], evidence, content)
    upload(client, w["officerSession"], evidence, content)
    upload(client, w["supervisorSession"], completion_photo, completion_bytes)
    events = client.get("/api/v1/audit?limit=100", headers=headers(w["admin"])).json()["data"][
        "items"
    ]
    assert sum(entry["action"] == "evidence.uploaded" for entry in events) == 2
    rejected = post(client, w["admin"], f"/api/v1/actions/{action['id']}/reject", review)
    completion.update(
        clientOperationId=str(uuid4()),
        expectedRevision=rejected["revision"],
        note="Synthetic second completion",
    )
    completed = post(
        client,
        w["supervisorSession"],
        f"/api/v1/actions/{action['id']}/complete",
        completion,
    )
    verified = post(
        client,
        w["admin"],
        f"/api/v1/actions/{action['id']}/verify",
        command(
            expectedRevision=completed["revision"],
            note="Different person checked the evidence",
        ),
    )
    assert verified["status"] == "verified"
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(w["admin"])).json()["data"]
    finding = next(row for row in snap["findings"] if row["id"] == finding_id)
    assert finding["status"] == "verified_closed"
    post(
        client,
        w["admin"],
        f"/api/v1/findings/{finding_id}/set-severity",
        command(
            expectedRevision=finding["revision"],
            severity="low",
            reason="Cannot change closed work",
        ),
        409,
    )


def test_creator_sync_survives_signout_and_retains_revoked_work_for_review(
    client,
    workspace,
    new_user,
):
    w = workspace
    session = w["officerSession"]
    sync_headers = {"Authorization": f"Bearer {session['syncCredential']['token']}"}
    post(client, session, "/api/v1/auth/sign-out", command())
    assert client.get("/api/v1/auth/me", headers=sync_headers).status_code == 401
    body = inspection_body(w)
    operation = {
        "clientOperationId": body["clientOperationId"],
        "command": "inspection.complete",
        "payload": body,
    }
    response = client.post(
        "/api/v1/sync/operations", headers=sync_headers, json={"operations": [operation]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["outcomes"][0]["state"] == "accepted"
    again = client.post(
        "/api/v1/sync/operations", headers=sync_headers, json={"operations": [operation]}
    )
    assert again.json()["data"]["outcomes"] == response.json()["data"]["outcomes"]
    unrelated, unrelated_session, _ = new_user()
    other_sync = {"Authorization": f"Bearer {unrelated_session['syncCredential']['token']}"}
    assert (
        client.post(
            "/api/v1/sync/operations", headers=other_sync, json={"operations": [operation]}
        ).status_code
        == 409
    )
    current = client.get(
        f"/api/v1/admin/users/{w['officer']['id']}",
        headers=headers(w["admin"]),
    ).json()["data"]
    post(
        client,
        w["admin"],
        f"/api/v1/admin/users/{current['id']}/set-status",
        command(
            expectedRevision=current["revision"],
            status="inactive",
            reason="Synthetic access loss",
        ),
    )
    descriptor, image = photo()
    pending = command(
        id=str(uuid4()),
        locationId=w["location"]["id"],
        description="Retained offline report",
        severity="medium",
        reportedAt=datetime.now(UTC).isoformat(),
        evidence=[descriptor],
    )
    operation = {
        "clientOperationId": pending["clientOperationId"],
        "command": "finding.create",
        "payload": pending,
    }
    result = client.post(
        "/api/v1/sync/operations", headers=sync_headers, json={"operations": [operation]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["outcomes"][0]["state"] == "sent_for_review"
    assert client.get("/api/v1/sync/pull", headers=sync_headers).status_code == 401
    upload_session = {"interactiveCredential": session["syncCredential"]}
    upload(client, upload_session, descriptor, image)
    assert (
        client.post(
            f"/api/v1/evidence/{descriptor['id']}/download-ticket", headers=sync_headers
        ).status_code
        == 401
    )
    reviews = client.get("/api/v1/admin/sync-reviews", headers=headers(w["admin"])).json()["data"]
    assert any(row["id"] == pending["clientOperationId"] for row in reviews["items"])


def test_duplicate_inspections_and_delayed_missed_reversal(client, workspace, database, schools):
    w = workspace
    now = datetime.now(UTC)
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE bms.inspection_occurrences SET due_start=%s,due_end=%s,grace_end=%s,"
            "timing_state='missed',missed_at=%s WHERE id=%s",
            (
                now - timedelta(hours=5),
                now - timedelta(hours=2),
                now - timedelta(hours=1),
                now - timedelta(minutes=59),
                w["occurrence"]["id"],
            ),
        )
    first = inspection_body(w)
    first["completedAt"] = (now - timedelta(minutes=90)).isoformat()
    first["startedAt"] = (now - timedelta(minutes=95)).isoformat()
    first["clockOffsetSeconds"] = 400
    first_result = post(client, w["officerSession"], "/api/v1/inspections/complete", first)
    assert first_result["clockFlag"] and not first_result["catchUp"]
    second = inspection_body(w)
    operation = {
        "clientOperationId": second["clientOperationId"],
        "command": "inspection.complete",
        "payload": second,
    }
    sync_headers = {"Authorization": f"Bearer {w['supervisorSession']['syncCredential']['token']}"}
    result = client.post(
        "/api/v1/sync/operations", headers=sync_headers, json={"operations": [operation]}
    ).json()["data"]["outcomes"][0]
    assert result["state"] == "duplicate_conflict"
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(w["admin"])).json()["data"]
    stored = [row for row in snap["inspections"] if row["occurrenceId"] == w["occurrence"]["id"]]
    assert len(stored) == 2 and sum(row["duplicate"] for row in stored) == 1
    occurrence = next(row for row in snap["occurrences"] if row["id"] == w["occurrence"]["id"])
    assert occurrence["missedAt"] is None and occurrence["timingState"] == "closed_late"
    with psycopg.connect(database, row_factory=dict_row) as conn:
        audit = conn.execute(
            "SELECT action FROM bms.audit_events WHERE tenant_id=%s AND entity_id=%s",
            (schools["ALPHA"][0], w["occurrence"]["id"]),
        ).fetchall()
    assert "occurrence.missed_reversed" in {row["action"] for row in audit}


def test_scheduler_versions_and_responsibility_snapshots(
    client, workspace, database, schools, new_user
):
    w = workspace
    replacement, _, _ = new_user()
    post(
        client,
        w["admin"],
        f"/api/v1/zones/{w['zone']['id']}/responsibility",
        command(
            expectedRevision=w["assignment"]["revision"],
            primaryUserId=replacement["id"],
        ),
    )
    updated = {
        **w["programmeBody"],
        "clientOperationId": str(uuid4()),
        "expectedRevision": 1,
        "reason": "Synthetic version change",
        "name": "UNAPPROVED practice version two",
    }
    updated["items"] = [{**updated["items"][0], "text": "New version, future checks only"}]
    post(client, w["admin"], f"/api/v1/programmes/{w['programme']['id']}/update", updated)
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        schedule_school(conn, schools["ALPHA"][0], datetime.now(UTC) + timedelta(days=8))
    with psycopg.connect(database, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT o.*,v.version_number FROM bms.inspection_occurrences o "
            "JOIN bms.programme_versions v ON v.id=o.programme_version_id "
            "WHERE o.programme_id=%s ORDER BY o.due_start",
            (w["programme"]["id"],),
        ).fetchall()
        history = conn.execute(
            "SELECT valid_to FROM bms.responsibility_assignments WHERE zone_id=%s",
            (w["zone"]["id"],),
        ).fetchall()
    assert (
        rows[0]["primary_user_id"] == w["officer"]["id"]
        or str(rows[0]["primary_user_id"]) == w["officer"]["id"]
    )
    assert rows[0]["version_number"] == 1 and rows[-1]["version_number"] == 2
    assert str(rows[-1]["primary_user_id"]) == replacement["id"]
    assert len(history) == 2 and sum(row["valid_to"] is None for row in history) == 1


def test_evidence_tickets_are_creator_and_tenant_bound(client, workspace, admins):
    w = workspace
    descriptor, image = photo()
    finding = command(
        id=str(uuid4()),
        locationId=w["location"]["id"],
        description="Synthetic manual finding",
        severity="low",
        reportedAt=datetime.now(UTC).isoformat(),
        evidence=[descriptor],
    )
    post(client, w["officerSession"], "/api/v1/findings", finding)
    path = f"/api/v1/evidence/{descriptor['id']}"
    post(client, admins["BETA"], path + "/upload-ticket", None, 404)
    post(client, w["supervisorSession"], path + "/upload-ticket", None, 403)
    result = post(client, w["officerSession"], path + "/upload-ticket", None)
    assert (
        client.put(
            result["url"], headers=headers(w["officerSession"]), content=image[:-1] + b"x"
        ).status_code
        == 400
    )
    upload(client, w["officerSession"], descriptor, image)
    download = post(client, w["supervisorSession"], path + "/download-ticket", None)
    assert client.get(download["url"], headers=headers(w["supervisorSession"])).content == image
    assert client.get(download["url"], headers=headers(w["officerSession"])).status_code == 403
    assert client.get(download["url"], headers=headers(admins["BETA"])).status_code == 404


def test_owned_action_does_not_expose_other_findings_in_an_unassigned_zone(
    client,
    workspace,
    new_user,
):
    w = workspace
    owner, owner_session, _ = new_user()
    descriptor, image = photo("condition")
    inspection = post(
        client,
        w["officerSession"],
        "/api/v1/inspections/complete",
        inspection_body(w, "fail", descriptor),
    )
    finding_id = inspection["findingIds"][0]
    post(
        client,
        w["supervisorSession"],
        f"/api/v1/findings/{finding_id}/assign",
        command(
            expectedRevision=1,
            actionId=str(uuid4()),
            ownerId=owner["id"],
            title="Owned work outside responsibility zone",
            dueDate="2027-01-01",
        ),
    )
    other_photo, _ = photo()
    unrelated = post(
        client,
        w["officerSession"],
        "/api/v1/findings",
        command(
            id=str(uuid4()),
            locationId=w["location"]["id"],
            description="Not the owner's work",
            severity="low",
            reportedAt=datetime.now(UTC).isoformat(),
            evidence=[other_photo],
        ),
    )
    snapshot = client.get(
        "/api/v1/mobile/snapshot",
        headers=headers(owner_session),
    ).json()["data"]
    assert finding_id in {row["id"] for row in snapshot["findings"]}
    assert unrelated["id"] not in {row["id"] for row in snapshot["findings"]}
    assert descriptor["id"] in {row["id"] for row in snapshot["evidence"]}
    assert not snapshot["occurrences"]
    upload(client, w["officerSession"], descriptor, image)
    ticket = post(
        client, owner_session, f"/api/v1/evidence/{descriptor['id']}/download-ticket", None
    )
    assert client.get(ticket["url"], headers=headers(owner_session)).content == image
    post(client, owner_session, "/api/v1/inspections/complete", inspection_body(w), 403)


def test_weekly_iso_window_is_tenant_local_and_inactive_locations_stop_scheduling(
    client,
    workspace,
    database,
    schools,
):
    w = workspace
    programme_id = str(uuid4())
    weekly = {
        **w["programmeBody"],
        "clientOperationId": str(uuid4()),
        "id": programme_id,
        "recurrence": "weekly",
        "startTime": "08:00",
        "endTime": "17:00",
        "graceMinutes": 1440,
    }
    post(client, w["admin"], "/api/v1/programmes", weekly)
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        schedule_school(conn, schools["ALPHA"][0], datetime(2027, 1, 3, 21, 30, tzinfo=UTC))
    with psycopg.connect(database, row_factory=dict_row) as conn:
        occurrence = conn.execute(
            "SELECT * FROM bms.inspection_occurrences WHERE programme_id=%s AND period_key=%s",
            (programme_id, "2027-W01"),
        ).fetchone()
    assert occurrence["due_start"] == datetime(2027, 1, 3, 21, tzinfo=UTC)
    assert occurrence["due_end"] == datetime(2027, 1, 10, 20, 59, 59, tzinfo=UTC)
    assert occurrence["grace_end"] == datetime(2027, 1, 11, 20, 59, 59, tzinfo=UTC)
    post(
        client,
        w["admin"],
        f"/api/v1/facilities/{w['location']['id']}/update",
        command(
            expectedRevision=1,
            name=w["location"]["name"],
            status="inactive",
            reason="Practice ended",
        ),
    )
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        schedule_school(conn, schools["ALPHA"][0], datetime(2027, 2, 1, 12, tzinfo=UTC))
    with psycopg.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM bms.inspection_occurrences "
                "WHERE programme_id=%s AND period_key='2027-W05'",
                (programme_id,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("required,value", [(False, None), (True, "fail")])
def test_optional_empty_and_short_text_do_not_infer_failed_checks(
    client, workspace, required, value
):
    w = workspace
    programme = post(
        client,
        w["admin"],
        "/api/v1/programmes",
        {
            **w["programmeBody"],
            "id": str(uuid4()),
            "clientOperationId": str(uuid4()),
            "items": [
                {
                    "key": "condition",
                    "type": "short_text",
                    "text": "Synthetic optional note",
                    "required": required,
                    "photo": "on_fail",
                    "createsFindingOn": [],
                }
            ],
        },
    )
    snapshot = client.get("/api/v1/mobile/snapshot", headers=headers(w["officerSession"])).json()[
        "data"
    ]
    occurrence = next(
        row for row in snapshot["occurrences"] if row["programmeId"] == programme["id"]
    )
    body = inspection_body({**w, "occurrence": occurrence})
    body["answers"] = [] if value is None else [{"itemKey": "condition", "value": value}]
    result = post(client, w["officerSession"], "/api/v1/inspections/complete", body)
    assert result["lifecycle"] == "completed" and result["findingIds"] == []


def extra_dorm(client, w, name):
    building = post(
        client,
        w["admin"],
        "/api/v1/facilities",
        command(id=str(uuid4()), level="building", name=name),
    )
    zone = post(
        client,
        w["admin"],
        "/api/v1/facilities",
        command(id=str(uuid4()), level="zone", parentId=building["id"], name=name + " zone"),
    )
    location = post(
        client,
        w["admin"],
        "/api/v1/facilities",
        command(
            id=str(uuid4()),
            level="location",
            parentId=zone["id"],
            name=name + " path",
            locationType="emergency_exit",
        ),
    )
    return building, zone, location


def test_sensor_daily_followups_restore_refault_and_retained_offline_work(client, workspace):
    w = workspace
    create = command(
        id=str(uuid4()),
        locationId=w["location"]["id"],
        name="Synthetic simulated sensor",
        status="faulty",
    )
    sensor = post(client, w["admin"], "/api/v1/admin/sensors", create)
    assert sensor == post(client, w["admin"], "/api/v1/admin/sensors", create)
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(w["officerSession"])).json()[
        "data"
    ]
    checks = [
        row for row in snap["occurrences"] if row["programmeId"] == sensor["faultProgrammeId"]
    ]
    assert len({row["periodKey"] for row in checks}) == 8
    assert all(row["sourceKind"] == "sensor_fault" for row in checks)
    today = min(checks, key=lambda row: row["dueStart"])
    complete = inspection_body({**w, "occurrence": today})
    complete["answers"] = [{"itemKey": "manual_safety_check", "value": "pass"}]
    post(client, w["officerSession"], "/api/v1/inspections/complete", complete)
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(w["supervisorSession"])).json()[
        "data"
    ]
    assert not [row for row in snap["sensorAlerts"] if row["sensorId"] == sensor["id"]]
    assert next(row for row in snap["sensors"] if row["id"] == sensor["id"])["status"] == "faulty"
    restored = post(
        client,
        w["admin"],
        f"/api/v1/admin/sensors/{sensor['id']}/simulate",
        command(expectedRevision=1, status="working", reason="Synthetic restoration"),
    )
    officer = client.get("/api/v1/mobile/snapshot", headers=headers(w["officerSession"])).json()[
        "data"
    ]
    old = [
        row for row in officer["occurrences"] if row["programmeId"] == sensor["faultProgrammeId"]
    ]
    assert sum(row["cancelledAt"] is not None for row in old) == 7
    assert (
        next(row for row in old if row["id"] == today["id"])["canonicalInspectionId"]
        == complete["id"]
    )
    descriptor, image = photo("manual_safety_check")
    pending = inspection_body({**w, "occurrence": max(checks, key=lambda row: row["dueStart"])})
    pending["answers"] = [
        {
            "itemKey": "manual_safety_check",
            "value": "concern",
            "description": "Retained offline synthetic observation",
            "evidenceId": descriptor["id"],
        }
    ]
    pending["evidence"] = [descriptor]
    operation = {
        "clientOperationId": pending["clientOperationId"],
        "command": "inspection.complete",
        "payload": pending,
    }
    result = post(
        client, w["officerSession"], "/api/v1/sync/operations", {"operations": [operation]}
    )["outcomes"][0]
    assert result["state"] == "sent_for_review" and result["errorCode"] == "CHECK_CANCELLED"
    upload(client, w["officerSession"], descriptor, image)
    repeated = post(
        client,
        w["admin"],
        f"/api/v1/admin/sensors/{sensor['id']}/simulate",
        command(expectedRevision=restored["revision"], status="faulty", reason="New fault"),
    )
    assert repeated["faultProgrammeId"] != sensor["faultProgrammeId"]
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(w["supervisorSession"])).json()[
        "data"
    ]
    assert len([row for row in snap["sensorAlerts"] if row["sensorId"] == sensor["id"]]) == 1


def test_supervisor_schoolwide_sensor_alerts_do_not_expand_inspection_access(
    client,
    workspace,
    new_user,
    admins,
):
    w = workspace
    building, zone, location = extra_dorm(client, w, "Outside permitted dormitory")
    supervisor = client.get(
        f"/api/v1/admin/users/{w['supervisor']['id']}", headers=headers(w["admin"])
    ).json()["data"]
    post(
        client,
        w["admin"],
        f"/api/v1/admin/users/{supervisor['id']}/scope",
        command(expectedRevision=supervisor["revision"], buildingIds=[w["building"]["id"]]),
    )
    sensor = post(
        client,
        w["admin"],
        "/api/v1/admin/sensors",
        command(id=str(uuid4()), locationId=location["id"], name="Outside fault", status="faulty"),
    )
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(w["supervisorSession"])).json()[
        "data"
    ]
    alert = next(row for row in snap["sensorAlerts"] if row["sensorId"] == sensor["id"])
    assert alert["occurrenceId"] is None and alert["officerName"] is None
    assert alert["buildingName"] == building["name"]
    assert sensor["id"] not in {row["id"] for row in snap["sensors"]}
    assert location["id"] not in {row["id"] for row in snap["facilities"]}
    officer, session, _ = new_user()
    post(
        client,
        w["admin"],
        f"/api/v1/zones/{zone['id']}/responsibility",
        command(expectedRevision=0, primaryUserId=officer["id"]),
    )
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(w["supervisorSession"])).json()[
        "data"
    ]
    alert = next(row for row in snap["sensorAlerts"] if row["sensorId"] == sensor["id"])
    assert alert["occurrenceId"] and alert["officerName"] == officer["name"]
    outside = client.get("/api/v1/mobile/snapshot", headers=headers(session)).json()["data"]
    occurrence = next(row for row in outside["occurrences"] if row["id"] == alert["occurrenceId"])
    body = inspection_body({**w, "occurrence": occurrence})
    body["answers"] = [{"itemKey": "manual_safety_check", "value": "pass"}]
    post(client, w["supervisorSession"], "/api/v1/inspections/complete", body, 403)
    post(client, w["officerSession"], "/api/v1/inspections/complete", body, 403)
    post(
        client,
        admins["BETA"],
        f"/api/v1/admin/sensors/{sensor['id']}/simulate",
        command(expectedRevision=1, status="working", reason="Cross tenant"),
        404,
    )
    post(
        client,
        w["officerSession"],
        f"/api/v1/admin/sensors/{sensor['id']}/simulate",
        command(expectedRevision=1, status="working", reason="Officer cannot simulate"),
        403,
    )
    post(client, session, "/api/v1/inspections/complete", body)


def test_thirty_percent_random_supervisor_dorms_are_stable_scoped_and_daily(
    client,
    workspace,
    new_user,
    database,
    schools,
):
    import math
    from zoneinfo import ZoneInfo

    w = workspace
    dorms = [extra_dorm(client, w, f"Sample dorm {index}") for index in range(9)]
    other, other_session, _ = new_user("supervisor")
    current = client.get(f"/api/v1/admin/users/{other['id']}", headers=headers(w["admin"])).json()[
        "data"
    ]
    post(
        client,
        w["admin"],
        f"/api/v1/admin/users/{other['id']}/scope",
        command(expectedRevision=current["revision"], buildingIds=[dorms[0][0]["id"]]),
    )
    locations = [w["location"]["id"]] + [row[2]["id"] for row in dorms]
    programme = post(
        client,
        w["admin"],
        "/api/v1/programmes",
        {
            **w["programmeBody"],
            "id": str(uuid4()),
            "clientOperationId": str(uuid4()),
            "assignmentMode": "supervisor_random",
            "lifeSafety": False,
            "locationIds": locations,
        },
    )

    def own_snapshot(session):
        return client.get("/api/v1/mobile/snapshot", headers=headers(session)).json()["data"]

    first = own_snapshot(w["supervisorSession"])
    with psycopg.connect(database) as conn:
        total = conn.execute(
            "SELECT count(*) FROM bms.facilities WHERE tenant_id=%s AND level='building' "
            "AND status='active'",
            (schools["ALPHA"][0],),
        ).fetchone()[0]
    own = [row for row in first["occurrences"] if row["programmeId"] == programme["id"]]
    expected = min(10, math.ceil(total * 30 / 100))
    assert len(own) == expected and all(
        row["primaryUserId"] == w["supervisor"]["id"] for row in own
    )
    assert len(first["spotCheckAssignments"]) == expected
    assert first["dailySettings"]["supervisorDailyPercentage"] == 30
    other_snap = own_snapshot(other_session)
    other_checks = [
        row for row in other_snap["occurrences"] if row["programmeId"] == programme["id"]
    ]
    assert len(other_checks) == 1 and other_checks[0]["locationId"] == dorms[0][2]["id"]
    post(
        client,
        w["supervisorSession"],
        "/api/v1/inspections/complete",
        inspection_body({**w, "occurrence": other_checks[0]}),
        403,
    )
    post(
        client,
        w["officerSession"],
        "/api/v1/inspections/complete",
        inspection_body({**w, "occurrence": own[0]}),
        403,
    )
    post(
        client,
        w["supervisorSession"],
        "/api/v1/inspections/complete",
        inspection_body({**w, "occurrence": own[0]}),
    )
    post(
        client,
        w["admin"],
        "/api/v1/admin/daily-settings",
        command(expectedRevision=first["dailySettings"]["revision"], supervisorDailyPercentage=90),
    )
    again = own_snapshot(w["supervisorSession"])
    assert {row["id"] for row in first["spotCheckAssignments"]} == {
        row["id"] for row in again["spotCheckAssignments"]
    }
    tomorrow = datetime.now(UTC).astimezone(ZoneInfo("Africa/Nairobi")).date() + timedelta(days=1)
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        schedule_school(
            conn,
            schools["ALPHA"][0],
            datetime.combine(tomorrow, datetime.min.time(), ZoneInfo("Africa/Nairobi")),
        )
    with psycopg.connect(database) as conn:
        count = conn.execute(
            "SELECT count(*) FROM bms.supervisor_dorm_assignments a "
            "JOIN bms.supervisor_daily_samples s ON s.id=a.sample_id AND s.tenant_id=a.tenant_id "
            "WHERE s.tenant_id=%s AND s.supervisor_user_id=%s AND s.local_date=%s",
            (schools["ALPHA"][0], w["supervisor"]["id"], tomorrow),
        ).fetchone()[0]
    assert count == min(10, math.ceil(total * 90 / 100))
