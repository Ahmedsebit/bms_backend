from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import command, headers
from test_mobile_workflows import post
from test_mobile_workflows import workspace as workspace

from bms.config import settings
from bms.db import connection, set_tenant
from bms.sensor_contract import SensorFeed
from bms.sensor_operations import poll_sensor_service, refresh_sensor_availability

READER_A = "ALPHA"
READER_B = "BETA"


@pytest.fixture
def sensor(client, workspace):
    device = f"smoke-{uuid4().hex[:12]}"
    body = command(
        id=str(uuid4()),
        locationId=workspace["location"]["id"],
        name="Synthetic service smoke sensor",
        gatewayId="gateway-01",
        externalDeviceId=device,
        staleAfterSeconds=15,
    )
    record = post(client, workspace["admin"], "/api/v1/admin/sensors/service", body)
    return {
        **workspace,
        "sensor": record,
        "body": body,
        "device": device,
        "stream": str(uuid4()),
        "events": [],
    }


def send(s, time, **changes):
    s["events"].append(
        {
            "sequence": len(s["events"]) + 1,
            "gatewayId": "gateway-01",
            "mode": "test",
            "receivedAt": max(datetime.now(UTC), time).isoformat(),
            "reading": {
                "eventId": str(uuid4()),
                "deviceId": s["device"],
                "observedAt": time.isoformat(),
                "health": "healthy",
                "measurements": {"smokeAlarm": False, "temperatureC": 24.2},
                **changes,
            },
        }
    )


def poll(s, time, token=READER_A, fail=False):
    def fetch(state):
        if fail:
            raise OSError("synthetic outage")
        after = state["cursor"] if str(state["stream_id"]) == s["stream"] else 0
        events = [row for row in s["events"] if row["sequence"] > after]
        return SensorFeed.model_validate(
            {
                "schoolCode": token,
                "streamId": s["stream"],
                "cursor": events[-1]["sequence"] if events else after,
                "events": events,
            }
        )

    with connection() as conn:
        tenant = conn.execute("SELECT bms.school_tenant('ALPHA') AS id").fetchone()["id"]
        set_tenant(conn, tenant)
        poll_sensor_service(conn, tenant, time, fetch)
        return conn.execute(
            "SELECT * FROM bms.sensors WHERE tenant_id=%s AND id=%s", (tenant, s["sensor"]["id"])
        ).fetchone()


def test_unknown_mapping_auth_replay_and_no_simulator_override(client, sensor, admins):
    s = sensor
    assert s["sensor"]["status"] == "unknown" and s["sensor"]["lastObservedAt"] is None
    assert s["sensor"] == post(client, s["admin"], "/api/v1/admin/sensors/service", s["body"])
    post(
        client,
        s["officerSession"],
        "/api/v1/admin/sensors/service",
        {**s["body"], "clientOperationId": str(uuid4()), "id": str(uuid4())},
        403,
    )
    error = post(
        client,
        s["admin"],
        f"/api/v1/admin/sensors/{s['sensor']['id']}/simulate",
        command(expectedRevision=s["sensor"]["revision"], status="working", reason="Not allowed"),
        409,
    )
    assert error["code"] == "SERVICE_SENSOR_CANNOT_BE_SIMULATED"
    post(
        client,
        admins["BETA"],
        f"/api/v1/admin/sensors/{s['sensor']['id']}/simulate",
        command(expectedRevision=1, status="working", reason="Other school"),
        404,
    )
    duplicate = post(
        client,
        s["admin"],
        "/api/v1/admin/sensors/service",
        {**s["body"], "clientOperationId": str(uuid4()), "id": str(uuid4())},
        409,
    )
    assert duplicate["code"] == "SENSOR_DEVICE_ALREADY_MAPPED"


def test_fresh_service_reading_restores_then_stale_and_failure_require_followups(client, sensor):
    s = sensor
    now = datetime.now(UTC)
    send(s, now)
    fresh = poll(s, now)
    assert fresh["status"] == "working" and fresh["telemetry_mode"] == "test"
    assert fresh["measurements"]["temperatureC"] == 24.2
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(s["officerSession"])).json()[
        "data"
    ]
    original = [
        row for row in snap["occurrences"] if row["programmeId"] == s["sensor"]["faultProgrammeId"]
    ]
    assert original and all(row["cancelledAt"] for row in original)
    stale = poll(s, now + timedelta(seconds=16))
    assert stale["status"] == "unknown" and stale["availability_reason"] == "stale"
    assert stale["fault_programme_id"] != fresh["fault_programme_id"]
    send(s, now + timedelta(seconds=20))
    restored = poll(s, now + timedelta(seconds=20))
    assert restored["status"] == "working"
    failed = poll(s, now + timedelta(seconds=21), fail=True)
    assert failed["status"] == "unknown" and failed["availability_reason"] == "service_unavailable"
    assert poll(s, now + timedelta(seconds=22))["status"] == "working"


def test_alarm_survives_heartbeat_outage_and_old_clear_and_is_visible_while_device_healthy(
    client, sensor
):
    s = sensor
    now = datetime.now(UTC)
    send(s, now, measurements={"smokeAlarm": True})
    record = poll(s, now)
    assert record["status"] == "working" and record["active_alarms"] == ["smokeAlarm"]
    snap = client.get("/api/v1/mobile/snapshot", headers=headers(s["supervisorSession"])).json()[
        "data"
    ]
    alert = next(row for row in snap["sensorAlerts"] if row["sensorId"] == s["sensor"]["id"])
    assert alert["activeAlarms"] == ["smokeAlarm"] and alert["source"] == "service"
    send(s, now + timedelta(seconds=1), measurements={})
    assert poll(s, now + timedelta(seconds=1))["active_alarms"] == ["smokeAlarm"]
    assert poll(s, now + timedelta(seconds=2), fail=True)["active_alarms"] == ["smokeAlarm"]
    send(s, now - timedelta(seconds=10), measurements={"smokeAlarm": False})
    assert poll(s, now + timedelta(seconds=3))["active_alarms"] == ["smokeAlarm"]
    send(s, now + timedelta(seconds=4), measurements={"smokeAlarm": False})
    assert poll(s, now + timedelta(seconds=4))["active_alarms"] == []


def test_partial_measurements_use_independent_times_and_same_time_alarm_dominates(sensor):
    s = sensor
    now = datetime.now(UTC)
    send(s, now, measurements={})
    poll(s, now)
    send(s, now - timedelta(seconds=2), measurements={"smokeAlarm": True})
    record = poll(s, now)
    assert record["active_alarms"] == ["smokeAlarm"]
    assert record["last_observed_at"] == now
    assert record["measurement_times"]["smokeAlarm"] == (now - timedelta(seconds=2)).isoformat()
    send(s, now - timedelta(seconds=2), measurements={"smokeAlarm": False})
    assert poll(s, now)["active_alarms"] == ["smokeAlarm"]
    send(s, now + timedelta(seconds=1), measurements={"smokeAlarm": False})
    assert poll(s, now + timedelta(seconds=1))["active_alarms"] == []


def test_replayed_delayed_reading_cannot_restore_device_and_wrong_school_feed_is_rejected(sensor):
    s = sensor
    now = datetime.now(UTC)
    send(s, now - timedelta(seconds=60))
    stale = poll(s, now)
    assert stale["status"] == "unknown"
    assert poll(s, now + timedelta(seconds=1))["status"] == "unknown"
    mismatch = poll(s, now + timedelta(seconds=2), token=READER_B)
    assert mismatch["availability_reason"] == "service_unavailable"
    assert mismatch["last_observed_at"] == stale["last_observed_at"]


def test_snapshot_evaluates_staleness_even_when_worker_stops(sensor):
    s = sensor
    now = datetime.now(UTC)
    send(s, now)
    assert poll(s, now)["status"] == "working"
    with connection() as conn:
        tenant = conn.execute("SELECT bms.school_tenant('ALPHA') AS id").fetchone()["id"]
        set_tenant(conn, tenant)
        refresh_sensor_availability(conn, tenant, now + timedelta(seconds=16))
        row = conn.execute(
            "SELECT status FROM bms.sensors WHERE tenant_id=%s AND id=%s",
            (tenant, s["sensor"]["id"]),
        ).fetchone()
        assert row["status"] == "unknown"


def test_future_device_clock_within_ingestion_tolerance_cannot_mark_device_available(sensor):
    now = datetime.now(UTC)
    send(sensor, now + timedelta(seconds=60))
    record = poll(sensor, now)
    assert record["status"] == "unknown" and record["availability_reason"] == "clock_skew"


def test_replacement_journal_resets_cursor_without_losing_new_observations(sensor):
    s = sensor
    now = datetime.now(UTC)
    send(s, now)
    assert poll(s, now)["last_sequence"] == 1
    s["stream"] = str(uuid4())
    s["events"] = []
    send(s, now + timedelta(seconds=1), measurements={"smokeAlarm": True})
    record = poll(s, now + timedelta(seconds=1))
    assert record["last_sequence"] == 1
    assert record["last_observed_at"] == now + timedelta(seconds=1)
    assert record["active_alarms"] == ["smokeAlarm"]


def test_service_status_and_rosters_remain_scoped(client, sensor, admins):
    s = sensor
    status = client.get("/api/v1/admin/sensors/service-status", headers=headers(s["admin"]))
    assert status.status_code == 200
    assert "token" not in status.text.lower()
    assert (
        client.get(
            "/api/v1/admin/sensors/service-status", headers=headers(s["officerSession"])
        ).status_code
        == 403
    )
    other = client.get("/api/v1/mobile/snapshot", headers=headers(admins["BETA"])).json()["data"]
    assert s["sensor"]["id"] not in {row["id"] for row in other["sensors"]}


def test_sensor_reader_configuration_rejects_unencrypted_remote_urls(monkeypatch, database):
    with pytest.raises(ValueError):
        settings().trusted_sensor_url("http://example.invalid")
    with pytest.raises(ValueError):
        settings().trusted_sensor_url("https://token:secret@example.invalid")
