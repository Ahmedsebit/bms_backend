from datetime import UTC, datetime, timedelta
from uuid import uuid4

from conftest import command, headers
from test_emergency_operations import confirm_all, emergency, person, point, report, snapshot
from test_mobile_workflows import post
from test_mobile_workflows import workspace as workspace

from bms.db import connection, set_tenant
from bms.emergency_operations import process_emergency_escalations


def grant(client, w, user, capability, building=None, **extra):
    now = datetime.now(UTC)
    return emergency(
        client,
        w["admin"],
        "duty.grant",
        id=str(uuid4()),
        userId=user,
        capability=capability,
        buildingId=building,
        startsAt=(now - timedelta(minutes=1)).isoformat(),
        endsAt=(now + timedelta(hours=8)).isoformat(),
        approvalNote="Synthetic approved duty",
        **extra,
    )


def incident_row(client, session, identifier):
    return next(i for i in snapshot(client, session)["incidents"] if i["id"] == identifier)


def test_explicit_roster_grants_scope_revocation_and_technical_admin(client, workspace, new_user):
    w = workspace
    technical, session, _ = new_user("administrator")
    emergency(
        client,
        session,
        "emergency.person",
        403,
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        name="Cannot maintain",
        kind="visitor",
    )
    g = grant(client, w, technical["id"], "roster.manage", w["building"]["id"])
    p = emergency(
        client,
        session,
        "emergency.person",
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        name="Scoped synthetic visitor",
        kind="visitor",
    )
    assert p["maintainedBy"] == technical["id"]
    outside = post(
        client,
        w["admin"],
        "/api/v1/facilities",
        command(id=str(uuid4()), name="Another synthetic building", level="building"),
    )
    emergency(
        client,
        session,
        "emergency.person",
        403,
        id=p["id"],
        expectedRevision=p["revision"],
        buildingId=outside["id"],
        name=p["name"],
        kind=p["kind"],
    )
    emergency(
        client,
        w["admin"],
        "duty.grant",
        id=g["id"],
        expectedRevision=g["revision"],
        userId=technical["id"],
        capability="roster.manage",
        buildingId=w["building"]["id"],
        startsAt=g["startsAt"],
        endsAt=g["endsAt"],
        active=False,
        approvalNote="Duty revoked",
    )
    emergency(
        client,
        session,
        "emergency.person",
        403,
        id=p["id"],
        expectedRevision=p["revision"],
        buildingId=w["building"]["id"],
        name=p["name"],
        kind=p["kind"],
    )
    assert client.get("/api/v1/leadership", headers=headers(session)).status_code == 200


def test_receipt_is_not_acceptance_and_transfer_preserves_owner_until_accepted(client, workspace):
    w = workspace
    incident = report(client, w)
    incident = emergency(
        client,
        w["admin"],
        "incident.responders",
        id=incident["id"],
        expectedRevision=incident["revision"],
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        leadUserId=w["admin"]["user"]["id"],
        note="Assign response team",
    )
    receipt = emergency(
        client,
        w["officerSession"],
        "incident.acknowledge",
        id=incident["id"],
        expectedRevision=incident["revision"],
        note="Received, not yet accepted",
    )
    assert receipt["responseOwnerId"] is None
    owner = emergency(
        client,
        w["officerSession"],
        "incident.accept",
        id=incident["id"],
        expectedRevision=receipt["revision"],
        note="I accept ongoing responsibility",
    )
    emergency(
        client,
        w["supervisorSession"],
        "incident.progress",
        403,
        id=owner["id"],
        expectedRevision=owner["revision"],
        note="Not the current owner",
    )
    owner = emergency(
        client,
        w["officerSession"],
        "incident.progress",
        id=owner["id"],
        expectedRevision=owner["revision"],
        note="Approved response progress reported",
    )
    owner = emergency(
        client,
        w["admin"],
        "incident.responders",
        id=owner["id"],
        expectedRevision=owner["revision"],
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        leadUserId=w["supervisor"]["id"],
        note="Change lead without dropping accepted ownership",
    )
    assert owner["responseOwnerId"] == w["officer"]["id"]
    assert owner["progressDeadline"] is not None
    transfer = emergency(
        client,
        w["officerSession"],
        "incident.transfer",
        id=owner["id"],
        expectedRevision=owner["revision"],
        incomingUserId=w["supervisor"]["id"],
        note="Need an accepted replacement",
    )
    assert incident_row(client, w["admin"], owner["id"])["responseOwnerId"] == w["officer"]["id"]
    emergency(
        client,
        w["officerSession"],
        "incident.transfer_accept",
        403,
        id=transfer["id"],
        expectedRevision=transfer["revision"],
        note="Wrong recipient",
    )
    emergency(
        client,
        w["supervisorSession"],
        "incident.transfer_accept",
        id=transfer["id"],
        expectedRevision=transfer["revision"],
        note="Incoming responder accepts",
    )
    current = incident_row(client, w["admin"], owner["id"])
    assert current["responseOwnerId"] == w["supervisor"]["id"]
    assert (
        len([p for p in snapshot(client, w["admin"])["progress"] if p["incidentId"] == owner["id"]])
        == 3
    )


def test_pending_transfer_blocks_closure_until_audited_cancellation(client, workspace):
    w = workspace
    incident = report(client, w)
    incident = emergency(
        client,
        w["admin"],
        "incident.accept",
        id=incident["id"],
        expectedRevision=incident["revision"],
        note="Approved commander accepts responsibility",
    )
    transfer = emergency(
        client,
        w["admin"],
        "incident.transfer",
        id=incident["id"],
        expectedRevision=incident["revision"],
        incomingUserId=w["supervisor"]["id"],
        note="Pending incoming response",
    )
    incident = emergency(
        client,
        w["admin"],
        "incident.phase",
        id=incident["id"],
        expectedRevision=incident["revision"],
        status="recovery",
        note="Keep accepted ownership during recovery",
    )
    blocked = emergency(
        client,
        w["admin"],
        "incident.phase",
        409,
        id=incident["id"],
        expectedRevision=incident["revision"],
        status="closed",
        note="Do not abandon the pending responsibility transfer",
    )
    assert blocked["code"] == "TRANSFER_PENDING"
    cancelled = emergency(
        client,
        w["admin"],
        "incident.transfer_cancel",
        id=transfer["id"],
        expectedRevision=transfer["revision"],
        note="Current owner will continue responding",
    )
    assert cancelled["status"] == "cancelled"
    assert (
        incident_row(client, w["admin"], incident["id"])["responseOwnerId"]
        == incident["responseOwnerId"]
    )


def test_stalled_response_escalates_to_backup_then_named_lead(client, workspace, schools):
    w = workspace
    incident = report(client, w)
    incident = emergency(
        client,
        w["admin"],
        "incident.responders",
        id=incident["id"],
        expectedRevision=incident["revision"],
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        leadUserId=w["admin"]["user"]["id"],
        note="Assign responders",
    )
    owner = emergency(
        client,
        w["officerSession"],
        "incident.accept",
        id=incident["id"],
        expectedRevision=incident["revision"],
        note="Accept responsibility",
    )
    deadline = datetime.fromisoformat(owner["progressDeadline"])
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        process_emergency_escalations(conn, schools["ALPHA"][0], deadline + timedelta(seconds=1))
    alerts = [
        a
        for a in snapshot(client, w["admin"])["alerts"]
        if a["incidentId"] == incident["id"]
        and a["notificationRound"] == owner["notificationRound"]
    ]
    backup = next(a for a in alerts if a["stage"] == "backup")
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        process_emergency_escalations(
            conn,
            schools["ALPHA"][0],
            datetime.fromisoformat(backup["deadline"]) + timedelta(seconds=1),
        )
    alerts = [
        a for a in snapshot(client, w["admin"])["alerts"] if a["incidentId"] == incident["id"]
    ]
    assert any(
        a["stage"] == "lead" and a["recipientId"] == w["admin"]["user"]["id"] for a in alerts
    )
    emergency(
        client,
        w["admin"],
        "incident.contact",
        id=str(uuid4()),
        incidentId=incident["id"],
        recipientUserId=w["supervisor"]["id"],
        channel="radio",
        outcome="reached",
        occurredAt=datetime.now(UTC).isoformat(),
        note="Manual contact log, not delivery proof",
    )
    assert incident_row(client, w["admin"], owner["id"])["responseOwnerId"] == w["officer"]["id"]


def test_staffing_double_booking_gap_and_availability_confirmation(client, workspace):
    w = workspace
    now = datetime.now(UTC)
    values = dict(
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        leadUserId=w["admin"]["user"]["id"],
        approvalNote="Synthetic duty",
        startsAt=now.isoformat(),
        endsAt=(now + timedelta(hours=8)).isoformat(),
    )
    shift = emergency(
        client,
        w["admin"],
        "emergency.shift",
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        **values,
    )
    outside = post(
        client,
        w["admin"],
        "/api/v1/facilities",
        command(id=str(uuid4()), name="Other duty building", level="building"),
    )
    conflict = emergency(
        client,
        w["admin"],
        "emergency.shift",
        409,
        id=str(uuid4()),
        buildingId=outside["id"],
        **values,
    )
    assert conflict["code"] == "RESPONDER_DOUBLE_BOOKED"
    second = emergency(
        client,
        w["admin"],
        "emergency.shift",
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        **(
            values
            | {
                "startsAt": (now + timedelta(hours=9)).isoformat(),
                "endsAt": (now + timedelta(hours=16)).isoformat(),
            }
        ),
    )
    emergency(
        client,
        w["officerSession"],
        "emergency.handover",
        400,
        id=str(uuid4()),
        outgoingShiftId=shift["id"],
        incomingShiftId=second["id"],
        note="Uncovered hour",
    )
    shift = emergency(
        client,
        w["officerSession"],
        "emergency.shift_accept",
        id=shift["id"],
        expectedRevision=shift["revision"],
        note="Primary confirms availability",
    )
    emergency(
        client,
        w["supervisorSession"],
        "emergency.shift_accept",
        id=shift["id"],
        expectedRevision=shift["revision"],
        note="Backup confirms availability",
    )
    gaps = snapshot(client, w["admin"])["coverage"]
    assert any(g["buildingId"] == w["building"]["id"] and g["kind"] == "coverage_gap" for g in gaps)
    assert not any(g.get("shiftId") == shift["id"] and "not_confirmed" in g["kind"] for g in gaps)


def test_coverage_warns_shared_lead_and_authority_expiring_mid_shift(
    client, workspace, schools, new_user
):
    w = workspace
    now = datetime.now(UTC)
    values = dict(
        leadUserId=w["admin"]["user"]["id"],
        approvalNote="Synthetic coverage",
        startsAt=now.isoformat(),
        endsAt=(now + timedelta(hours=4)).isoformat(),
    )
    emergency(
        client,
        w["admin"],
        "emergency.shift",
        id=str(uuid4()),
        buildingId=w["building"]["id"],
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        **values,
    )
    outside = post(
        client,
        w["admin"],
        "/api/v1/facilities",
        command(id=str(uuid4()), name="Other staffed building", level="building"),
    )
    other_primary, _, _ = new_user("officer")
    other_backup, _, _ = new_user("supervisor")
    emergency(
        client,
        w["admin"],
        "emergency.shift",
        id=str(uuid4()),
        buildingId=outside["id"],
        primaryUserId=other_primary["id"],
        backupUserId=other_backup["id"],
        **values,
    )
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        conn.execute(
            "UPDATE bms.duty_grants SET ends_at=%s WHERE tenant_id=%s AND user_id=%s "
            "AND capability='incident.command'",
            (now + timedelta(hours=1), schools["ALPHA"][0], w["admin"]["user"]["id"]),
        )
    warnings = snapshot(client, w["admin"])["coverage"]
    for building in (w["building"]["id"], outside["id"]):
        kinds = {g["kind"] for g in warnings if g["buildingId"] == building}
        assert {"lead_capacity_shared", "lead_authority_unavailable"} <= kinds


def test_expired_commander_escalates_before_future_progress_deadline(client, workspace, schools):
    w = workspace
    incident = report(client, w)
    incident = emergency(
        client,
        w["admin"],
        "incident.responders",
        id=incident["id"],
        expectedRevision=incident["revision"],
        primaryUserId=w["officer"]["id"],
        backupUserId=w["supervisor"]["id"],
        leadUserId=w["admin"]["user"]["id"],
        note="Synthetic response team",
    )
    incident = emergency(
        client,
        w["admin"],
        "incident.accept",
        id=incident["id"],
        expectedRevision=incident["revision"],
        note="Commander accepts ongoing response",
    )
    now = datetime.now(UTC)
    assert datetime.fromisoformat(incident["progressDeadline"]) > now
    with connection() as conn:
        set_tenant(conn, schools["ALPHA"][0])
        conn.execute(
            "UPDATE bms.duty_grants SET active=false WHERE tenant_id=%s AND user_id=%s "
            "AND capability='incident.command'",
            (schools["ALPHA"][0], w["admin"]["user"]["id"]),
        )
        process_emergency_escalations(conn, schools["ALPHA"][0], now)
    alerts = snapshot(client, w["admin"])["alerts"]
    assert any(a["incidentId"] == incident["id"] and a["stage"] == "backup" for a in alerts)


def test_critical_tasks_independent_verification_and_recovery_approval(client, workspace):
    w = workspace
    person(client, w, "staff")
    assembly = point(client, w)
    incident = report(client, w)
    confirm_all(client, w, incident, assembly)
    task = emergency(
        client,
        w["admin"],
        "incident.task",
        id=str(uuid4()),
        incidentId=incident["id"],
        title="Safety-critical improvement",
        ownerId=w["supervisor"]["id"],
        dueAt=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        observation="Synthetic observation",
        safetyCritical=True,
    )
    task = emergency(
        client,
        w["supervisorSession"],
        "incident.task_complete",
        id=task["id"],
        expectedRevision=task["revision"],
        note="Work completed",
    )
    emergency(
        client,
        w["supervisorSession"],
        "incident.task_verify",
        409,
        id=task["id"],
        expectedRevision=task["revision"],
        note="Self-verification denied",
    )
    emergency(
        client,
        w["admin"],
        "incident.task_verify",
        id=task["id"],
        expectedRevision=task["revision"],
        note="Independent synthetic assessment",
    )
    recovery = emergency(
        client,
        w["admin"],
        "recovery.save",
        id=str(uuid4()),
        incidentId=incident["id"],
        accommodation="Synthetic alternative",
        welfare="Synthetic welfare",
        learningContinuity="Synthetic lessons",
    )
    incident = incident_row(client, w["admin"], incident["id"])
    emergency(
        client,
        w["admin"],
        "incident.phase",
        id=incident["id"],
        expectedRevision=incident["revision"],
        status="recovery",
        note="Recovery phase",
    )
    emergency(
        client,
        w["admin"],
        "recovery.approve",
        409,
        id=recovery["id"],
        expectedRevision=recovery["revision"],
        authorised=True,
        note="Self-approval denied",
    )
    approved = emergency(
        client,
        w["supervisorSession"],
        "recovery.approve",
        id=recovery["id"],
        expectedRevision=recovery["revision"],
        authorised=True,
        note="Independent competent-authority decision in synthetic exercise",
    )
    assert approved["approvedBy"] != approved["authoredBy"]
    p = next(
        p for p in snapshot(client, w["admin"])["people"] if p["buildingId"] == w["building"]["id"]
    )
    emergency(
        client,
        w["admin"],
        "emergency.person",
        id=p["id"],
        expectedRevision=p["revision"],
        buildingId=p["buildingId"],
        name=p["name"],
        kind=p["kind"],
        presence="temporarily_away",
        awayUntil=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    assert (
        next(r for r in snapshot(client, w["admin"])["recovery"] if r["id"] == recovery["id"])[
            "approvedAt"
        ]
        is None
    )
    assert not next(
        r
        for r in snapshot(client, w["admin"])["rosterReviews"]
        if r["buildingId"] == w["building"]["id"]
    )["current"]


def test_leadership_reads_live_summaries_without_private_roster_or_implicit_command(
    client, workspace, new_user
):
    w = workspace
    _, leadership, _ = new_user("leadership")
    p = person(client, w)
    incident = report(client, w)
    data = client.get("/api/v1/leadership", headers=headers(leadership)).json()["data"]
    current = next(i for i in data["emergency"]["incidents"] if i["id"] == incident["id"])
    assert current["accountability"]["unaccounted"] == 1
    assert not current["canLead"] and not current["canApprove"]
    assert not any(r["personId"] == p["id"] for r in data["emergency"]["rollCall"])
    emergency(
        client,
        leadership,
        "incident.phase",
        403,
        id=incident["id"],
        expectedRevision=current["revision"],
        status="recovery",
        note="Not delegated",
    )
    assert client.get("/api/v1/mobile/snapshot", headers=headers(leadership)).status_code == 403


def test_sync_resolution_preserves_submission_and_returns_creator_feedback(
    client, workspace, admins
):
    w = workspace
    token = w["officerSession"]["syncCredential"]["token"]
    op = command(
        id=str(uuid4()), occurrenceId=str(uuid4()), startedAt=datetime.now(UTC).isoformat()
    )
    submission = {
        "clientOperationId": op["clientOperationId"],
        "command": "inspection.start",
        "payload": op,
    }
    response = client.post(
        "/api/v1/sync/operations",
        headers={"Authorization": f"Bearer {token}"},
        json={"operations": [submission]},
    )
    original = response.json()["data"]["outcomes"][0]
    assert original["state"] == "rejected"
    body = command(
        id=op["clientOperationId"],
        ownerId=w["admin"]["user"]["id"],
        status="reviewing",
        decision="retain",
        note="Assigned for investigation",
    )
    result = post(client, w["admin"], "/api/v1/admin/sync-resolutions", body)
    resolved = post(
        client,
        w["admin"],
        "/api/v1/admin/sync-resolutions",
        command(
            **{k: v for k, v in body.items() if k != "clientOperationId"},
            expectedRevision=result["revision"],
        )
        | {
            "status": "resolved",
            "decision": "dismiss",
            "note": "Invalid location; creator informed",
        },
    )
    assert resolved["status"] == "resolved"
    feedback = client.get("/api/v1/mobile/snapshot", headers=headers(w["officerSession"])).json()[
        "data"
    ]
    assert any(r["submissionId"] == op["clientOperationId"] for r in feedback["syncResolutions"])
    replay = client.post(
        "/api/v1/sync/operations",
        headers={"Authorization": f"Bearer {token}"},
        json={"operations": [submission]},
    ).json()["data"]["outcomes"][0]
    assert replay == original
    post(client, admins["BETA"], "/api/v1/admin/sync-resolutions", body, 404)
