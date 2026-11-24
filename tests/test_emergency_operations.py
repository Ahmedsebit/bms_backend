from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from conftest import command, headers
from test_mobile_workflows import photo, post
from test_mobile_workflows import workspace as workspace

from bms.db import connection, set_tenant
from bms.emergency_operations import process_emergency_escalations
from bms.scheduling import schedule_school


def emergency(client, session, command_name, expected_http_status=200, **values):
    return post(
        client,
        session,
        "/api/v1/emergency/commands",
        {"command": command_name, "payload": command(**values)},
        expected_http_status,
    )


def snapshot(client, session):
    response = client.get("/api/v1/emergency", headers=headers(session))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def report(client, w):
    return emergency(
        client,
        w["officerSession"],
        "incident.report",
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        title="Synthetic emergency",
        hazard="fire",
        reportedAt=datetime.now(UTC).isoformat(),
    )


def person(client, w, kind="student", away=False):
    return emergency(
        client,
        w["admin"],
        "emergency.person",
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        name=f"Synthetic {kind}",
        kind=kind,
        presence="temporarily_away" if away else "on_campus",
        awayUntil=(datetime.now(UTC) + timedelta(hours=2)).isoformat() if away else None,
        assistance="Use the school-approved assistance plan" if kind == "student" else "",
    )


def point(client, w):
    return emergency(
        client,
        w["admin"],
        "emergency.point",
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        name="Synthetic assembly point",
    )


def confirm_all(client, w, incident, assembly):
    for row in snapshot(client, w["admin"])["rollCall"]:
        if row["incidentId"] == incident["id"]:
            emergency(
                client,
                w["officerSession"],
                "rollcall.confirm",
                id=row["id"],
                expectedRevision=row["revision"],
                status="at_assembly",
                pointId=assembly["id"],
                note="Physically confirmed in this synthetic exercise",
            )
    people = [
        row
        for row in snapshot(client, w["admin"])["people"]
        if row["buildingId"] == w["building"]["id"] and row["active"]
    ]
    emergency(
        client,
        w["admin"],
        "roster.review",
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        expectedCount=len(people),
        note="Synthetic attendance and visitor register reconciled",
    )


def test_rapid_report_accepts_no_photo_and_keeps_manual_finding_rules(client, workspace):
    w = workspace
    body = command(
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        title="Synthetic rapid report",
        hazard="other",
        reportedAt=datetime.now(UTC).isoformat(),
        offline=True,
    )
    operation = {
        "clientOperationId": body["clientOperationId"],
        "command": "incident.report",
        "payload": body,
    }
    token = w["officerSession"]["syncCredential"]["token"]
    response = client.post(
        "/api/v1/sync/operations",
        headers={"Authorization": f"Bearer {token}"},
        json={"operations": [operation]},
    )
    assert response.status_code == 200, response.text
    outcome = response.json()["data"]["outcomes"][0]
    assert outcome["state"] == "accepted"
    assert outcome["result"]["isDrill"] is False
    replay = client.post(
        "/api/v1/sync/operations",
        headers={"Authorization": f"Bearer {token}"},
        json={"operations": [operation]},
    ).json()["data"]["outcomes"][0]
    assert replay == outcome
    snap = snapshot(client, w["admin"])
    assert snap["notificationMode"] == "in_app_prototype"
    assert next(a for a in snap["alerts"] if a["incidentId"] == body["id"])["status"] == "failed"
    post(
        client,
        w["officerSession"],
        "/api/v1/findings",
        command(
            id=str(uuid4()),
            locationId=w["location"]["id"],
            description="Synthetic",
            severity="critical",
            reportedAt=datetime.now(UTC).isoformat(),
            evidence=[],
        ),
        400,
    )
    descriptor, _ = photo()
    evidence = emergency(
        client,
        w["officerSession"],
        "incident.evidence",
        id=body["id"],
        evidence=[descriptor],
        offline=True,
    )
    assert evidence["evidenceIds"] == [descriptor["id"]]
    assert any(e["ownerId"] == body["id"] for e in snapshot(client, w["admin"])["evidence"])


def test_in_app_acknowledgement_and_backup_escalation(client, workspace, schools):
    w = workspace
    now = datetime.now(UTC)
    emergency(
        client,
        w["admin"],
        "emergency.shift",
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        leadUserId=w["admin"]["user"]["id"],
        startsAt=(now - timedelta(hours=1)).isoformat(),
        endsAt=(now + timedelta(hours=8)).isoformat(),
        approvalNote="Synthetic approved duty",
    )
    incident = report(client, w)
    snap = snapshot(client, w["admin"])
    primary = next(a for a in snap["alerts"] if a["incidentId"] == incident["id"])
    assert primary["status"] == "queued"
    assert incident["timingApproved"] is False
    officer_snap = snapshot(client, w["officerSession"])
    assert next(a for a in officer_snap["alerts"] if a["id"] == primary["id"])["status"] == (
        "available_in_app"
    )
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        deadline = datetime.fromisoformat(primary["deadline"])
        process_emergency_escalations(conn, schools["ALPHA"][0], deadline - timedelta(seconds=1))
    assert not any(
        a["stage"] == "backup" and a["incidentId"] == incident["id"]
        for a in snapshot(client, w["admin"])["alerts"]
    )
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        process_emergency_escalations(conn, schools["ALPHA"][0], deadline + timedelta(seconds=1))
    snap = snapshot(client, w["supervisorSession"])
    assert any(a["stage"] == "backup" and a["incidentId"] == incident["id"] for a in snap["alerts"])
    current = next(i for i in snap["incidents"] if i["id"] == incident["id"])
    emergency(
        client,
        w["admin"],
        "incident.acknowledge",
        403,
        id=current["id"],
        expectedRevision=current["revision"],
        note="Not the allocated responder",
    )
    ack = emergency(
        client,
        w["supervisorSession"],
        "incident.acknowledge",
        id=current["id"],
        expectedRevision=current["revision"],
        note="Backup responder taking responsibility",
    )
    assert ack["acknowledgedBy"] == w["supervisor"]["id"]
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        process_emergency_escalations(conn, schools["ALPHA"][0], deadline + timedelta(hours=1))
    assert (
        next(i for i in snapshot(client, w["admin"])["incidents"] if i["id"] == incident["id"])[
            "acknowledgedBy"
        ]
        == w["supervisor"]["id"]
    )


def test_roll_call_absence_reunification_and_independent_reopening(client, workspace):
    w = workspace
    student = person(client, w)
    person(client, w, "staff")
    person(client, w, "visitor", away=True)
    assembly = point(client, w)
    guardian = emergency(
        client,
        w["admin"],
        "emergency.guardian",
        id=str(uuid4()),
        personId=student["id"],
        name="Synthetic authorised guardian",
        relationship="Guardian",
        approved=True,
        approvalNote="Synthetic authorisation",
    )
    incident = report(client, w)
    rolls = [
        r for r in snapshot(client, w["admin"])["rollCall"] if r["incidentId"] == incident["id"]
    ]
    assert len(rolls) == 3 and all(r["status"] == "unaccounted" for r in rolls)
    assert any(r["expectedPresence"] == "temporarily_away" for r in rolls)
    recovery = emergency(
        client,
        w["admin"],
        "recovery.save",
        id=str(uuid4()),
        incidentId=incident["id"],
        accommodation="Synthetic temporary dormitory",
        welfare="Synthetic welfare check",
        learningContinuity="Synthetic lessons plan",
    )
    incident = emergency(
        client,
        w["admin"],
        "incident.phase",
        id=incident["id"],
        expectedRevision=incident["revision"],
        status="recovery",
        note="School incident lead authorised recovery phase",
    )
    blocked = emergency(
        client,
        w["admin"],
        "recovery.approve",
        409,
        id=recovery["id"],
        expectedRevision=recovery["revision"],
        authorised=True,
        note="Synthetic reopening decision",
    )
    assert blocked["code"] == "PEOPLE_UNACCOUNTED"
    confirm_all(client, w, incident, assembly)
    stale = emergency(
        client,
        w["officerSession"],
        "rollcall.confirm",
        409,
        id=rolls[0]["id"],
        expectedRevision=rolls[0]["revision"],
        status="confirmed_offsite",
        note="Stale update must not overwrite headcount",
    )
    assert stale["code"] == "REVISION_CONFLICT"
    emergency(
        client,
        w["admin"],
        "guardian.release",
        400,
        id=str(uuid4()),
        incidentId=incident["id"],
        personId=student["id"],
        guardianId=guardian["id"],
        identityChecked=False,
        verificationNote="No verification",
    )
    release = emergency(
        client,
        w["admin"],
        "guardian.release",
        id=str(uuid4()),
        incidentId=incident["id"],
        personId=student["id"],
        guardianId=guardian["id"],
        identityChecked=True,
        verificationNote="Staff checked approved guardian identity",
    )
    assert release["releasedBy"] == w["admin"]["user"]["id"]
    assert release["guardianName"] == guardian["name"]
    assert release["personName"] == student["name"]
    emergency(
        client,
        w["admin"],
        "emergency.guardian",
        id=guardian["id"],
        expectedRevision=guardian["revision"],
        personId=student["id"],
        name="Updated synthetic guardian",
        relationship="Guardian",
        approved=True,
        approvalNote="Updated synthetic authorisation",
    )
    assert (
        next(r for r in snapshot(client, w["admin"])["releases"] if r["id"] == release["id"])[
            "guardianName"
        ]
        == guardian["name"]
    )
    row = next(
        r
        for r in snapshot(client, w["admin"])["rollCall"]
        if r["incidentId"] == incident["id"] and r["personId"] == student["id"]
    )
    assert row["status"] == "released" and row["lastConfirmedAt"]
    emergency(
        client,
        w["officerSession"],
        "rollcall.confirm",
        409,
        id=row["id"],
        expectedRevision=row["revision"],
        status="unaccounted",
        note="Cannot undo a handover",
    )
    approved = emergency(
        client,
        w["supervisorSession"],
        "recovery.approve",
        id=recovery["id"],
        expectedRevision=recovery["revision"],
        authorised=True,
        note="Competent authority reopening decision recorded",
    )
    assert approved["approvedAt"]
    extra = person(client, w, "visitor")
    current = next(r for r in snapshot(client, w["admin"])["recovery"] if r["id"] == recovery["id"])
    assert current["approvedAt"] is None
    assert any(
        r["personId"] == extra["id"] and r["status"] == "unaccounted"
        for r in snapshot(client, w["admin"])["rollCall"]
    )


def test_reopening_blocked_by_unresolved_high_finding(client, workspace):
    w = workspace
    person(client, w, "staff")
    assembly = point(client, w)
    incident = report(client, w)
    confirm_all(client, w, incident, assembly)
    incident = emergency(
        client,
        w["admin"],
        "incident.phase",
        id=incident["id"],
        expectedRevision=incident["revision"],
        status="recovery",
        note="Synthetic recovery phase",
    )
    recovery = emergency(
        client,
        w["admin"],
        "recovery.save",
        id=str(uuid4()),
        incidentId=incident["id"],
        accommodation="Arranged",
        welfare="Arranged",
        learningContinuity="Arranged",
    )
    descriptor, _ = photo()
    post(
        client,
        w["officerSession"],
        "/api/v1/findings",
        command(
            id=str(uuid4()),
            locationId=w["location"]["id"],
            description="Synthetic unsafe exit",
            severity="high",
            reportedAt=datetime.now(UTC).isoformat(),
            evidence=[descriptor],
        ),
    )
    blocked = emergency(
        client,
        w["admin"],
        "recovery.approve",
        409,
        id=recovery["id"],
        expectedRevision=recovery["revision"],
        authorised=True,
        note="Cannot reopen",
    )
    assert blocked["code"] == "UNRESOLVED_LIFE_SAFETY_FINDINGS"


def test_overnight_programmes_and_shift_handover(client, workspace, schools):
    w = workspace
    body = command(
        id=str(uuid4()),
        name="Synthetic approved overnight check",
        recurrence="daily",
        startTime="22:00",
        endTime="06:00",
        graceMinutes=0,
        lifeSafety=True,
        approved=True,
        approvalNote="Approved synthetic school checklist",
        locationIds=[w["location"]["id"]],
        items=[{"key": "route", "type": "yes_no", "text": "Approved synthetic condition"}],
    )
    programme = post(client, w["admin"], "/api/v1/programmes", body)
    local = datetime.now(ZoneInfo("Africa/Nairobi"))
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0)
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        schedule_school(conn, schools["ALPHA"][0], tomorrow)
        row = conn.execute(
            "SELECT * FROM bms.inspection_occurrences WHERE tenant_id=%s AND programme_id=%s "
            "AND period_key=%s",
            (schools["ALPHA"][0], programme["id"], local.date().isoformat()),
        ).fetchone()
        assert row["timing_state"] == "due"
        assert row["due_start"].astimezone(ZoneInfo("Africa/Nairobi")).hour == 22
        assert row["due_end"].astimezone(ZoneInfo("Africa/Nairobi")).date() == tomorrow.date()
    invalid = {
        **body,
        "clientOperationId": str(uuid4()),
        "id": str(uuid4()),
        "assignmentMode": "supervisor_random",
    }
    post(client, w["admin"], "/api/v1/programmes", invalid, 400)
    now = datetime.now(UTC)
    shift_values = dict(
        buildingId=w["building"]["id"],
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        leadUserId=w["admin"]["user"]["id"],
        approvalNote="Synthetic duty plan",
    )
    first = emergency(
        client,
        w["admin"],
        "emergency.shift",
        id=str(uuid4()),
        startsAt=now.isoformat(),
        endsAt=(now + timedelta(hours=8)).isoformat(),
        **shift_values,
    )
    emergency(
        client,
        w["admin"],
        "emergency.shift",
        409,
        id=str(uuid4()),
        startsAt=(now + timedelta(hours=1)).isoformat(),
        endsAt=(now + timedelta(hours=9)).isoformat(),
        **shift_values,
    )
    second = emergency(
        client,
        w["admin"],
        "emergency.shift",
        id=str(uuid4()),
        startsAt=(now + timedelta(hours=8)).isoformat(),
        endsAt=(now + timedelta(hours=16)).isoformat(),
        **{
            **shift_values,
            "primaryUserId": w["supervisor"]["id"],
            "backupUserId": w["officer"]["id"],
        },
    )
    handover = emergency(
        client,
        w["officerSession"],
        "emergency.handover",
        id=str(uuid4()),
        outgoingShiftId=first["id"],
        incomingShiftId=second["id"],
        note="Outstanding checks and assistance arrangements reviewed",
    )
    emergency(
        client,
        w["officerSession"],
        "emergency.handover_accept",
        403,
        id=handover["id"],
        expectedRevision=handover["revision"],
        note="Wrong incoming responder",
    )
    accepted = emergency(
        client,
        w["supervisorSession"],
        "emergency.handover_accept",
        id=handover["id"],
        expectedRevision=handover["revision"],
        note="Incoming responder confirms handover",
    )
    assert accepted["acceptedBy"] == w["supervisor"]["id"]


def test_drills_are_distinct_and_track_participation_and_improvements(client, workspace):
    w = workspace
    person(client, w)
    assembly = point(client, w)
    now = datetime.now(UTC)
    drill = emergency(
        client,
        w["supervisorSession"],
        "drill.plan",
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        title="Synthetic night exercise",
        hazard="fire",
        reportedAt=now.isoformat(),
        plannedAt=(now + timedelta(days=1)).isoformat(),
        nighttime=True,
        communicationsFailure=True,
        individualAssistance=True,
        approvalNote="School-approved synthetic exercise",
        safeExercisePlan="Age-appropriate, supervised practice; no hazardous simulation",
    )
    assert drill["isDrill"] and drill["status"] == "planned"
    assert not any(a["incidentId"] == drill["id"] for a in snapshot(client, w["admin"])["alerts"])
    active = emergency(
        client,
        w["supervisorSession"],
        "drill.start",
        id=drill["id"],
        expectedRevision=drill["revision"],
        note="Authorised exercise starts",
    )
    assert active["status"] == "active"
    confirm_all(client, w, active, assembly)
    task = emergency(
        client,
        w["supervisorSession"],
        "incident.task",
        id=str(uuid4()),
        incidentId=drill["id"],
        title="Improve backup communications",
        ownerId=w["officer"]["id"],
        dueAt=(now + timedelta(days=2)).isoformat(),
        observation="Backup communications were not understood by participants",
    )
    recovery = emergency(
        client,
        w["supervisorSession"],
        "incident.phase",
        id=active["id"],
        expectedRevision=active["revision"],
        status="recovery",
        note="Exercise debrief",
    )
    blocked = emergency(
        client,
        w["supervisorSession"],
        "incident.phase",
        409,
        id=recovery["id"],
        expectedRevision=recovery["revision"],
        status="closed",
        note="Exercise reviewed",
    )
    assert blocked["code"] == "EMERGENCY_TASKS_OPEN"
    emergency(
        client,
        w["officerSession"],
        "incident.task_complete",
        id=task["id"],
        expectedRevision=task["revision"],
        note="Backup communication briefing completed",
    )
    closed = emergency(
        client,
        w["supervisorSession"],
        "incident.phase",
        id=recovery["id"],
        expectedRevision=recovery["revision"],
        status="closed",
        note="Debrief complete",
    )
    assert closed["isDrill"] and closed["status"] == "closed"


def test_notification_reassignment_preserves_history_and_rejects_old_round(client, workspace):
    w = workspace
    incident = report(client, w)
    original = [
        a for a in snapshot(client, w["admin"])["alerts"] if a["incidentId"] == incident["id"]
    ]
    assert original and all(a["status"] == "failed" for a in original)
    incident = emergency(
        client,
        w["admin"],
        "incident.responders",
        id=incident["id"],
        expectedRevision=incident["revision"],
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        leadUserId=w["admin"]["user"]["id"],
        note="Assign a named response team",
    )
    assert incident["notificationRound"] == 2
    snapshot(client, w["officerSession"])
    incident = emergency(
        client,
        w["admin"],
        "incident.responders",
        id=incident["id"],
        expectedRevision=incident["revision"],
        primaryUserId=w["supervisor"]["id"],
        backupUserId=w["officer"]["id"],
        leadUserId=w["admin"]["user"]["id"],
        note="Replacement primary responder",
    )
    emergency(
        client,
        w["officerSession"],
        "incident.acknowledge",
        403,
        id=incident["id"],
        expectedRevision=incident["revision"],
        note="Previous primary notification cannot acknowledge",
    )
    alerts = [
        a for a in snapshot(client, w["admin"])["alerts"] if a["incidentId"] == incident["id"]
    ]
    assert {a["id"] for a in original} <= {a["id"] for a in alerts}
    assert all(a["status"] == "failed" for a in alerts if a["notificationRound"] == 1)
    assert all(a["status"] == "cancelled" for a in alerts if a["notificationRound"] == 2)
    current = emergency(
        client,
        w["supervisorSession"],
        "incident.acknowledge",
        id=incident["id"],
        expectedRevision=incident["revision"],
        note="Current primary accepts responsibility",
    )
    assert current["acknowledgedBy"] == w["supervisor"]["id"]
    current = emergency(
        client,
        w["supervisorSession"],
        "incident.accept",
        id=current["id"],
        expectedRevision=current["revision"],
        note="Explicitly accept ongoing responsibility",
    )
    emergency(
        client,
        w["admin"],
        "incident.responders",
        409,
        id=incident["id"],
        expectedRevision=current["revision"],
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        leadUserId=w["admin"]["user"]["id"],
        note="Cannot silently change acknowledged responsibility",
    )


def test_scoped_supervisor_report_does_not_unlock_private_roster(client, workspace):
    w = workspace
    post(
        client,
        w["admin"],
        f"/api/v1/admin/users/{w['supervisor']['id']}/scope",
        command(expectedRevision=w["supervisor"]["revision"], buildingIds=[w["building"]["id"]]),
    )
    outside = post(
        client,
        w["admin"],
        "/api/v1/facilities",
        command(id=str(uuid4()), name="Outside supervisor scope", level="building"),
    )
    occupant = emergency(
        client,
        w["admin"],
        "emergency.person",
        id=str(uuid4()),
        buildingId=outside["id"],
        name="Private synthetic occupant",
        kind="student",
    )
    guardian = emergency(
        client,
        w["admin"],
        "emergency.guardian",
        id=str(uuid4()),
        personId=occupant["id"],
        name="Private synthetic guardian",
        relationship="Guardian",
    )
    incident = emergency(
        client,
        w["supervisorSession"],
        "incident.report",
        id=str(uuid4()),
        buildingId=outside["id"],
        title="Outside-scope rapid report",
        hazard="other",
        reportedAt=datetime.now(UTC).isoformat(),
    )
    data = snapshot(client, w["supervisorSession"])
    assert not any(p["id"] == occupant["id"] for p in data["people"])
    assert not any(g["id"] == guardian["id"] for g in data["guardians"])
    assert not any(r["incidentId"] == incident["id"] for r in data["rollCall"])
    visible = next(i for i in data["incidents"] if i["id"] == incident["id"])
    assert not visible["canLead"] and not visible["canAccount"]
    emergency(
        client,
        w["supervisorSession"],
        "incident.phase",
        403,
        id=incident["id"],
        expectedRevision=incident["revision"],
        status="recovery",
        note="Not an authorised lead",
    )
    emergency(
        client,
        w["admin"],
        "duty.grant",
        id=str(uuid4()),
        userId=w["supervisor"]["id"],
        buildingId=outside["id"],
        capability="incident.command",
        startsAt=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        endsAt=(datetime.now(UTC) + timedelta(hours=8)).isoformat(),
        approvalNote="Explicit incident command delegation for this synthetic building",
    )
    assigned = emergency(
        client,
        w["admin"],
        "incident.responders",
        id=incident["id"],
        expectedRevision=incident["revision"],
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        leadUserId=w["supervisor"]["id"],
        note="Explicit emergency leadership assignment, not inspection scope",
    )
    delegated = snapshot(client, w["supervisorSession"])
    assert any(r["incidentId"] == assigned["id"] for r in delegated["rollCall"])
    assert any(g["id"] == guardian["id"] for g in delegated["guardians"])
    assert next(i for i in delegated["incidents"] if i["id"] == assigned["id"])["canLead"]


def test_scope_approval_and_online_safeguards(client, workspace, admins):
    w = workspace
    building = post(
        client,
        w["admin"],
        "/api/v1/facilities",
        command(id=str(uuid4()), name="Synthetic outside scope", level="building"),
    )
    hidden_person = emergency(
        client,
        w["admin"],
        "emergency.person",
        id=str(uuid4()),
        buildingId=building["id"],
        name="Private synthetic student",
        kind="student",
    )
    outsider = emergency(
        client,
        w["officerSession"],
        "incident.report",
        id=str(uuid4()),
        buildingId=building["id"],
        title="Outside-scope report",
        hazard="other",
        reportedAt=datetime.now(UTC).isoformat(),
    )
    officer = snapshot(client, w["officerSession"])
    assert not any(p["id"] == hidden_person["id"] for p in officer["people"])
    assert not any(r["incidentId"] == outsider["id"] for r in officer["rollCall"])
    roll = next(
        r for r in snapshot(client, w["admin"])["rollCall"] if r["incidentId"] == outsider["id"]
    )
    emergency(
        client,
        w["officerSession"],
        "rollcall.confirm",
        403,
        id=roll["id"],
        expectedRevision=roll["revision"],
        status="confirmed_offsite",
        note="No roster authority",
    )
    emergency(
        client,
        admins["BETA"],
        "incident.phase",
        404,
        id=outsider["id"],
        expectedRevision=outsider["revision"],
        status="recovery",
        note="Wrong school",
    )
    settings = snapshot(client, w["admin"])["settings"]
    emergency(
        client,
        w["officerSession"],
        "emergency.settings",
        403,
        id=settings["id"],
        expectedRevision=settings["revision"],
        acknowledgementMinutes=3,
    )
    approved = emergency(
        client,
        w["admin"],
        "emergency.settings",
        id=settings["id"],
        expectedRevision=settings["revision"],
        acknowledgementMinutes=3,
        approved=True,
        approvalNote="School approval recorded for prototype timing",
    )
    assert approved["approvedAt"]
    reset = emergency(
        client,
        w["admin"],
        "emergency.settings",
        id=settings["id"],
        expectedRevision=approved["revision"],
        acknowledgementMinutes=4,
    )
    assert reset["approvedAt"] is None
    contact = emergency(
        client,
        w["admin"],
        "emergency.contact",
        id=str(uuid4()),
        name="Synthetic emergency contact",
        phone="000",
        purpose="Not a real number",
    )
    assert not any(
        c["id"] == contact["id"] for c in snapshot(client, w["officerSession"])["contacts"]
    )
    emergency(
        client,
        w["admin"],
        "emergency.contact",
        id=contact["id"],
        expectedRevision=contact["revision"],
        name=contact["name"],
        phone="000",
        purpose="Synthetic only",
        approved=True,
        approvalNote="Synthetic approval",
    )
    assert any(c["id"] == contact["id"] for c in snapshot(client, w["officerSession"])["contacts"])
    emergency(
        client,
        w["officerSession"],
        "rollcall.confirm",
        400,
        id=roll["id"],
        expectedRevision=roll["revision"],
        status="confirmed_offsite",
        note="Offline update must not appear confirmed",
        offline=True,
    )
