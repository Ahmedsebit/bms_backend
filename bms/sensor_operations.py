"""Pull the separate journal and derive conservative device availability."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from psycopg.types.json import Jsonb

from bms.config import settings
from bms.daily_operations import transition_sensor
from bms.operation_access import facility_access, public, require, row_by_id
from bms.schemas import Command
from bms.sensor_contract import SensorFeed
from bms.services import ApiError, audit, lock_tenant, remember, replay

ALARM_KEYS = {"smokeAlarm", "coAlarm", "waterLeak", "tamper"}


def service_state(conn, tenant):
    conn.execute(
        "INSERT INTO bms.sensor_service_state(id,tenant_id) VALUES (%s,%s) "
        "ON CONFLICT (tenant_id) DO NOTHING",
        (uuid4(), tenant),
    )
    return conn.execute(
        "SELECT * FROM bms.sensor_service_state WHERE tenant_id=%s", (tenant,)
    ).fetchone()


def create_service_sensor(conn, actor, body):
    require(actor, "setup.manage")
    key = "sensor.service_create"
    old = replay(conn, actor, key, body)
    if old is not None:
        return old
    facility_access(conn, actor, body.location_id, active=True)
    duplicate = conn.execute(
        "SELECT id FROM bms.sensors WHERE tenant_id=%s AND gateway_id=%s AND external_device_id=%s",
        (actor["tenant_id"], body.gateway_id, body.external_device_id),
    ).fetchone()
    if duplicate:
        raise ApiError(409, "SENSOR_DEVICE_ALREADY_MAPPED")
    conn.execute(
        "INSERT INTO bms.sensors(id,tenant_id,location_id,name,source,gateway_id,"
        "external_device_id,stale_after_seconds,availability_reason) "
        "VALUES (%s,%s,%s,%s,'service',%s,%s,%s,'never_received')",
        (
            body.id,
            actor["tenant_id"],
            body.location_id,
            body.name,
            body.gateway_id,
            body.external_device_id,
            body.stale_after_seconds,
        ),
    )
    current = row_by_id(conn, actor, "sensors", body.id)
    transition_sensor(conn, actor, None, current)
    service_state(conn, actor["tenant_id"])
    from bms.scheduling import schedule_school

    schedule_school(conn, actor["tenant_id"])
    result = public(row_by_id(conn, actor, "sensors", body.id))
    audit(conn, actor, key, body.id, body, after=result)
    return remember(conn, actor, key, body, result)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_feed(state):
    config = settings()
    if not config.sensor_service_url or not config.sensor_service_token:
        raise ValueError("not_configured")
    query = {"after": state["cursor"], "limit": 100}
    if state["stream_id"]:
        query["streamId"] = str(state["stream_id"])
    request = Request(
        config.sensor_service_url + "/api/v1/events?" + urlencode(query),
        headers={"Authorization": "Bearer " + config.sensor_service_token.get_secret_value()},
    )
    with build_opener(NoRedirect()).open(request, timeout=3) as response:
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise ValueError("response_too_large")
    return SensorFeed.model_validate_json(payload)


def refresh_sensor_availability(conn, tenant, now=None):
    """Also runs on snapshots, so a stopped worker cannot leave stale green statuses."""
    now = now or datetime.now(UTC)
    state = service_state(conn, tenant)
    actor = {"id": None, "tenant_id": tenant, "role": "system"}
    rows = conn.execute(
        "SELECT * FROM bms.sensors WHERE tenant_id=%s AND source='service'", (tenant,)
    ).fetchall()
    for before in rows:
        if state["last_error"]:
            status, reason = "unknown", "service_unavailable"
        elif not before["last_observed_at"]:
            status, reason = "unknown", "never_received"
        elif before["last_observed_at"] > now + timedelta(seconds=5) or (
            before["last_received_at"] and before["last_received_at"] > now + timedelta(seconds=5)
        ):
            status, reason = "unknown", "clock_skew"
        elif (
            not before["last_received_at"]
            or (now - before["last_observed_at"]).total_seconds() > before["stale_after_seconds"]
            or (now - before["last_received_at"]).total_seconds() > before["stale_after_seconds"]
            or not state["last_success_at"]
            or (now - state["last_success_at"]).total_seconds() > before["stale_after_seconds"]
        ):
            status, reason = "unknown", "stale"
        elif before["device_health"] == "healthy":
            status, reason = "working", "fresh"
        elif before["device_health"] in {"fault", "offline"}:
            status, reason = "faulty", "device_" + before["device_health"]
        else:
            status, reason = "unknown", "device_unknown"
        if (status, reason) == (before["status"], before["availability_reason"]):
            continue
        current = conn.execute(
            "UPDATE bms.sensors SET status=%s,availability_reason=%s,"
            "status_changed_at=CASE WHEN status<>%s THEN %s ELSE status_changed_at END,"
            "revision=revision+1,updated_at=now() WHERE tenant_id=%s AND id=%s RETURNING *",
            (status, reason, status, now, tenant, before["id"]),
        ).fetchone()
        transition_sensor(conn, actor, before, current)
        audit(
            conn,
            actor,
            "sensor.availability",
            before["id"],
            Command(client_operation_id=uuid4()),
            before={"status": before["status"], "reason": before["availability_reason"]},
            after={"status": status, "reason": reason},
        )


def poll_sensor_service(conn, tenant, now=None, fetch=fetch_feed):
    now = now or datetime.now(UTC)
    initial = service_state(conn, tenant)
    try:
        feed = fetch(initial)
        school = conn.execute(
            "SELECT school_code FROM bms.tenants WHERE tenant_id=%s", (tenant,)
        ).fetchone()["school_code"]
        if feed.school_code != school:
            raise ValueError("school_mismatch")
        previous = initial["cursor"] if initial["stream_id"] == feed.stream_id else 0
        for event in feed.events:
            if event.sequence <= previous or event.received_at > now + timedelta(seconds=120):
                raise ValueError("invalid_sequence_or_time")
            previous = event.sequence
        expected_cursor = previous
        if feed.cursor != expected_cursor:
            raise ValueError("invalid_cursor")
        error = None
    except Exception:
        # Never log upstream URLs, credentials, device payloads or untrusted exception text.
        feed, error = None, "feed_unavailable_or_invalid"
    lock_tenant(conn, tenant)
    current = service_state(conn, tenant)
    if (current["stream_id"], current["cursor"]) != (initial["stream_id"], initial["cursor"]):
        return
    actor = {"id": None, "tenant_id": tenant, "role": "system"}
    ignored = 0
    if feed is not None:
        for event in feed.events:
            reading = event.reading
            before = conn.execute(
                "SELECT * FROM bms.sensors WHERE tenant_id=%s AND source='service' "
                "AND gateway_id=%s AND external_device_id=%s",
                (tenant, event.gateway_id, reading.device_id),
            ).fetchone()
            if not before or reading.observed_at > now + timedelta(seconds=120):
                ignored += 1
                continue
            measurements = dict(before["measurements"])
            measurement_times = dict(before["measurement_times"])
            update = reading.measurements.model_dump(by_alias=True, exclude_none=True)
            alarms = set(before["active_alarms"])
            changed = False
            for key, value in update.items():
                previous_time = (
                    datetime.fromisoformat(measurement_times[key])
                    if key in measurement_times
                    else None
                )
                if previous_time and (
                    reading.observed_at < previous_time
                    or (reading.observed_at == previous_time and not (key in ALARM_KEYS and value))
                ):
                    continue
                changed = True
                measurements[key] = value
                measurement_times[key] = reading.observed_at.isoformat()
                if key in ALARM_KEYS:
                    if value:
                        alarms.add(key)
                    else:
                        alarms.discard(key)
            newest = (
                not before["last_observed_at"] or reading.observed_at > before["last_observed_at"]
            )
            if not newest and not changed:
                ignored += 1
                continue
            conn.execute(
                "UPDATE bms.sensors SET last_observed_at=%s,last_received_at=%s,last_sequence=%s,"
                "telemetry_mode=%s,device_health=%s,measurements=%s,measurement_times=%s,active_alarms=%s,"
                "revision=revision+1,updated_at=now() WHERE tenant_id=%s AND id=%s",
                (
                    reading.observed_at if newest else before["last_observed_at"],
                    event.received_at if newest else before["last_received_at"],
                    event.sequence if newest else before["last_sequence"],
                    event.mode if newest else before["telemetry_mode"],
                    reading.health if newest else before["device_health"],
                    Jsonb(measurements),
                    Jsonb(measurement_times),
                    sorted(alarms),
                    tenant,
                    before["id"],
                ),
            )
            audit(
                conn,
                actor,
                "sensor.reading_received",
                before["id"],
                Command(client_operation_id=reading.event_id),
                after={
                    "sequence": event.sequence,
                    "mode": event.mode,
                    "activeAlarms": sorted(alarms),
                },
            )
        conn.execute(
            "UPDATE bms.sensor_service_state SET stream_id=%s,cursor=%s,last_success_at=%s,"
            "last_attempt_at=%s,last_error=NULL,ignored_events=ignored_events+%s,"
            "revision=revision+1,updated_at=now() WHERE tenant_id=%s",
            (feed.stream_id, feed.cursor, now, now, ignored, tenant),
        )
    else:
        conn.execute(
            "UPDATE bms.sensor_service_state SET last_attempt_at=%s,last_error=%s,"
            "revision=revision+1,updated_at=now() WHERE tenant_id=%s",
            (now, error, tenant),
        )
    refresh_sensor_availability(conn, tenant, now)
