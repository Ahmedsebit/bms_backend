CREATE TABLE bms.school_operations_settings (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL UNIQUE REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision>0),
    supervisor_daily_percentage integer NOT NULL DEFAULT 30
        CHECK (supervisor_daily_percentage BETWEEN 1 AND 100)
);
CREATE TABLE bms.sensors (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision>0),
    location_id uuid NOT NULL,
    name text NOT NULL,
    source text NOT NULL DEFAULT 'simulator' CHECK (source='simulator'),
    status text NOT NULL DEFAULT 'unknown' CHECK (status IN ('unknown','working','faulty')),
    fault_programme_id uuid,
    status_changed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,id),
    FOREIGN KEY (tenant_id,location_id) REFERENCES bms.facilities(tenant_id,id),
    FOREIGN KEY (tenant_id,fault_programme_id) REFERENCES bms.programmes(tenant_id,id)
);
ALTER TABLE bms.programmes
    ADD COLUMN assignment_mode text NOT NULL DEFAULT 'zone'
        CHECK (assignment_mode IN ('zone','supervisor_random')),
    ADD COLUMN sensor_id uuid,
    ADD FOREIGN KEY (tenant_id,sensor_id) REFERENCES bms.sensors(tenant_id,id),
    ADD CHECK (assignment_mode<>'supervisor_random' OR recurrence='daily');
ALTER TABLE bms.inspection_occurrences
    ADD COLUMN source_kind text NOT NULL DEFAULT 'routine'
        CHECK (source_kind IN ('routine','sensor_fault','supervisor_spot')),
    ADD COLUMN cancelled_at timestamptz,
    ADD COLUMN cancellation_reason text;
CREATE TABLE bms.supervisor_daily_samples (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision>0),
    supervisor_user_id uuid NOT NULL,
    local_date date NOT NULL,
    percentage integer NOT NULL CHECK (percentage BETWEEN 1 AND 100),
    total_dormitories integer NOT NULL CHECK (total_dormitories>=0),
    UNIQUE (tenant_id,id),
    UNIQUE (tenant_id,supervisor_user_id,local_date),
    FOREIGN KEY (tenant_id,supervisor_user_id) REFERENCES bms.users(tenant_id,id)
);
CREATE TABLE bms.supervisor_dorm_assignments (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision>0),
    sample_id uuid NOT NULL,
    building_id uuid NOT NULL,
    UNIQUE (tenant_id,sample_id,building_id),
    FOREIGN KEY (tenant_id,sample_id) REFERENCES bms.supervisor_daily_samples(tenant_id,id),
    FOREIGN KEY (tenant_id,building_id) REFERENCES bms.facilities(tenant_id,id)
);
CREATE INDEX ON bms.sensors(tenant_id,status);
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'school_operations_settings','sensors','supervisor_daily_samples',
        'supervisor_dorm_assignments'
    ] LOOP
        EXECUTE format('ALTER TABLE bms.%I ENABLE ROW LEVEL SECURITY',table_name);
        EXECUTE format('ALTER TABLE bms.%I FORCE ROW LEVEL SECURITY',table_name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON bms.%I USING
             (tenant_id=nullif(current_setting(''bms.tenant_id'',true),'''')::uuid)
             WITH CHECK (tenant_id=nullif(current_setting(''bms.tenant_id'',true),'''')::uuid)',
            table_name
        );
        EXECUTE format('GRANT SELECT,INSERT,UPDATE ON bms.%I TO bms_runtime',table_name);
    END LOOP;
END $$;
REVOKE UPDATE ON bms.supervisor_daily_samples,bms.supervisor_dorm_assignments FROM bms_runtime;
