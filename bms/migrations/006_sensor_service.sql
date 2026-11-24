ALTER TABLE bms.sensors DROP CONSTRAINT sensors_source_check;
ALTER TABLE bms.sensors
    ADD CHECK (source IN ('simulator','service')),
    ADD COLUMN gateway_id text,
    ADD COLUMN external_device_id text,
    ADD COLUMN stale_after_seconds integer NOT NULL DEFAULT 300
        CHECK (stale_after_seconds BETWEEN 15 AND 604800),
    ADD COLUMN telemetry_mode text CHECK (telemetry_mode IN ('test','hardware')),
    ADD COLUMN last_observed_at timestamptz,
    ADD COLUMN last_received_at timestamptz,
    ADD COLUMN last_sequence bigint,
    ADD COLUMN device_health text NOT NULL DEFAULT 'unknown'
        CHECK (device_health IN ('unknown','healthy','fault','offline')),
    ADD COLUMN availability_reason text NOT NULL DEFAULT 'unknown',
    ADD COLUMN measurements jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN measurement_times jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN active_alarms text[] NOT NULL DEFAULT '{}',
    ADD CHECK ((source='simulator' AND gateway_id IS NULL AND external_device_id IS NULL)
        OR (source='service' AND gateway_id IS NOT NULL AND external_device_id IS NOT NULL));
CREATE UNIQUE INDEX sensor_service_device ON bms.sensors(tenant_id,gateway_id,external_device_id)
    WHERE source='service';

CREATE TABLE bms.sensor_service_state (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL UNIQUE REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision>0),
    stream_id uuid,
    cursor bigint NOT NULL DEFAULT 0 CHECK (cursor>=0),
    last_attempt_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    ignored_events bigint NOT NULL DEFAULT 0 CHECK (ignored_events>=0)
);
ALTER TABLE bms.sensor_service_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE bms.sensor_service_state FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON bms.sensor_service_state USING
    (tenant_id=nullif(current_setting('bms.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id=nullif(current_setting('bms.tenant_id',true),'')::uuid);
GRANT SELECT,INSERT,UPDATE ON bms.sensor_service_state TO bms_runtime;
