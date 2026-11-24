CREATE TABLE bms.facilities (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    parent_id uuid,
    building_id uuid NOT NULL,
    level text NOT NULL CHECK (level IN ('building','zone','location')),
    name text NOT NULL,
    location_type text CHECK (location_type IN (
        'door','emergency_exit','extinguisher_point','room','corridor'
    )),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    UNIQUE (tenant_id,id),
    FOREIGN KEY (tenant_id,parent_id) REFERENCES bms.facilities(tenant_id,id),
    FOREIGN KEY (tenant_id,building_id) REFERENCES bms.facilities(tenant_id,id),
    CHECK ((level='building' AND parent_id IS NULL AND building_id=id) OR
           (level<>'building' AND parent_id IS NOT NULL)),
    CHECK ((level='location' AND location_type IS NOT NULL) OR
           (level<>'location' AND location_type IS NULL))
);
CREATE TABLE bms.user_scopes (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    user_id uuid NOT NULL,
    building_id uuid NOT NULL,
    active boolean NOT NULL DEFAULT true,
    FOREIGN KEY (tenant_id,user_id) REFERENCES bms.users(tenant_id,id),
    FOREIGN KEY (tenant_id,building_id) REFERENCES bms.facilities(tenant_id,id),
    UNIQUE (tenant_id,user_id,building_id)
);
CREATE TABLE bms.responsibility_assignments (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    zone_id uuid NOT NULL,
    primary_user_id uuid NOT NULL,
    backup_user_id uuid,
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    UNIQUE (tenant_id,id),
    FOREIGN KEY (tenant_id,zone_id) REFERENCES bms.facilities(tenant_id,id),
    FOREIGN KEY (tenant_id,primary_user_id) REFERENCES bms.users(tenant_id,id),
    FOREIGN KEY (tenant_id,backup_user_id) REFERENCES bms.users(tenant_id,id),
    CHECK (backup_user_id IS NULL OR backup_user_id<>primary_user_id)
);
CREATE UNIQUE INDEX one_active_zone_assignment
ON bms.responsibility_assignments(tenant_id,zone_id) WHERE valid_to IS NULL;
CREATE TABLE bms.qr_tokens (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    location_id uuid NOT NULL,
    token text NOT NULL UNIQUE,
    active boolean NOT NULL DEFAULT true,
    FOREIGN KEY (tenant_id,location_id) REFERENCES bms.facilities(tenant_id,id)
);
CREATE UNIQUE INDEX one_active_qr_token
ON bms.qr_tokens(tenant_id,location_id) WHERE active;
CREATE TABLE bms.programmes (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    name text NOT NULL,
    recurrence text NOT NULL CHECK (recurrence IN ('daily','weekly')),
    start_time time NOT NULL,
    end_time time NOT NULL,
    grace_minutes integer NOT NULL CHECK (grace_minutes>=0),
    life_safety boolean NOT NULL DEFAULT false,
    enabled boolean NOT NULL DEFAULT true,
    current_version integer NOT NULL DEFAULT 1,
    UNIQUE (tenant_id,id),
    CHECK (start_time<end_time)
);
CREATE TABLE bms.programme_versions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    programme_id uuid NOT NULL,
    version_number integer NOT NULL,
    items jsonb NOT NULL,
    UNIQUE (tenant_id,id),
    UNIQUE (tenant_id,programme_id,version_number),
    FOREIGN KEY (tenant_id,programme_id) REFERENCES bms.programmes(tenant_id,id)
);
CREATE TABLE bms.programme_scopes (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    programme_id uuid NOT NULL,
    location_id uuid NOT NULL,
    active boolean NOT NULL DEFAULT true,
    UNIQUE (tenant_id,programme_id,location_id),
    FOREIGN KEY (tenant_id,programme_id) REFERENCES bms.programmes(tenant_id,id),
    FOREIGN KEY (tenant_id,location_id) REFERENCES bms.facilities(tenant_id,id)
);
CREATE TABLE bms.inspection_occurrences (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    programme_id uuid NOT NULL,
    programme_version_id uuid NOT NULL,
    location_id uuid NOT NULL,
    primary_user_id uuid NOT NULL,
    backup_user_id uuid,
    period_key text NOT NULL,
    due_start timestamptz NOT NULL,
    due_end timestamptz NOT NULL,
    grace_end timestamptz NOT NULL,
    timing_state text NOT NULL DEFAULT 'not_yet_due' CHECK (timing_state IN (
        'not_yet_due','due','in_grace','missed','closed_on_time','closed_late'
    )),
    missed_at timestamptz,
    canonical_inspection_id uuid,
    UNIQUE (tenant_id,id),
    UNIQUE (tenant_id,programme_id,location_id,period_key),
    FOREIGN KEY (tenant_id,programme_id) REFERENCES bms.programmes(tenant_id,id),
    FOREIGN KEY (tenant_id,programme_version_id) REFERENCES bms.programme_versions(tenant_id,id),
    FOREIGN KEY (tenant_id,location_id) REFERENCES bms.facilities(tenant_id,id),
    FOREIGN KEY (tenant_id,primary_user_id) REFERENCES bms.users(tenant_id,id),
    FOREIGN KEY (tenant_id,backup_user_id) REFERENCES bms.users(tenant_id,id)
);
CREATE TABLE bms.inspections (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    occurrence_id uuid NOT NULL,
    performer_id uuid NOT NULL,
    lifecycle text NOT NULL CHECK (lifecycle IN ('in_progress','completed','abandoned')),
    location_method text NOT NULL CHECK (location_method IN ('qr','manual')),
    scanned_at timestamptz,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    answers jsonb NOT NULL DEFAULT '[]',
    outcome text,
    device_id uuid,
    clock_offset_seconds integer NOT NULL DEFAULT 0,
    clock_flag boolean NOT NULL DEFAULT false,
    duplicate boolean NOT NULL DEFAULT false,
    catch_up boolean NOT NULL DEFAULT false,
    UNIQUE (tenant_id,id),
    FOREIGN KEY (tenant_id,occurrence_id) REFERENCES bms.inspection_occurrences(tenant_id,id),
    FOREIGN KEY (tenant_id,performer_id) REFERENCES bms.users(tenant_id,id)
);
ALTER TABLE bms.inspection_occurrences ADD CONSTRAINT occurrence_canonical_inspection
FOREIGN KEY (tenant_id,canonical_inspection_id) REFERENCES bms.inspections(tenant_id,id);
CREATE TABLE bms.findings (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    location_id uuid NOT NULL,
    source text NOT NULL CHECK (source IN ('manual','inspection')),
    inspection_id uuid,
    item_key text,
    description text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('critical','high','medium','low')),
    status text NOT NULL DEFAULT 'open' CHECK (status IN (
        'open','action_required','awaiting_verification','verified_closed'
    )),
    reporter_id uuid NOT NULL,
    owner_id uuid,
    reported_at timestamptz NOT NULL,
    verified_closed_at timestamptz,
    UNIQUE (tenant_id,id),
    FOREIGN KEY (tenant_id,location_id) REFERENCES bms.facilities(tenant_id,id),
    FOREIGN KEY (tenant_id,inspection_id) REFERENCES bms.inspections(tenant_id,id),
    FOREIGN KEY (tenant_id,reporter_id) REFERENCES bms.users(tenant_id,id),
    FOREIGN KEY (tenant_id,owner_id) REFERENCES bms.users(tenant_id,id)
);
CREATE TABLE bms.corrective_actions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    finding_id uuid NOT NULL,
    title text NOT NULL,
    owner_id uuid NOT NULL,
    due_date date NOT NULL,
    status text NOT NULL DEFAULT 'assigned' CHECK (status IN ('assigned','completed','verified')),
    completion_note text,
    completed_at timestamptz,
    completed_by uuid,
    verification_note text,
    verified_at timestamptz,
    verified_by uuid,
    rejection_note text,
    UNIQUE (tenant_id,id),
    UNIQUE (tenant_id,finding_id),
    FOREIGN KEY (tenant_id,finding_id) REFERENCES bms.findings(tenant_id,id),
    FOREIGN KEY (tenant_id,owner_id) REFERENCES bms.users(tenant_id,id),
    FOREIGN KEY (tenant_id,completed_by) REFERENCES bms.users(tenant_id,id),
    FOREIGN KEY (tenant_id,verified_by) REFERENCES bms.users(tenant_id,id)
);
CREATE TABLE bms.evidence (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    creator_id uuid NOT NULL,
    owner_type text NOT NULL CHECK (owner_type IN ('inspection','finding','action','review')),
    owner_id uuid NOT NULL,
    item_key text,
    captured_at timestamptz NOT NULL,
    sha256 text NOT NULL CHECK (length(sha256)=64),
    byte_size integer NOT NULL CHECK (byte_size>0),
    storage_key text NOT NULL UNIQUE,
    upload_state text NOT NULL DEFAULT 'pending' CHECK (upload_state IN ('pending','uploaded')),
    received_at timestamptz,
    FOREIGN KEY (tenant_id,creator_id) REFERENCES bms.users(tenant_id,id)
);
CREATE TABLE bms.sync_submissions (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1,
    creator_id uuid NOT NULL,
    request_hash text NOT NULL,
    payload jsonb NOT NULL,
    outcome jsonb NOT NULL,
    state text NOT NULL,
    PRIMARY KEY (tenant_id,id),
    FOREIGN KEY (tenant_id,creator_id) REFERENCES bms.users(tenant_id,id)
);
CREATE INDEX ON bms.inspection_occurrences(tenant_id,location_id,due_end);
CREATE INDEX ON bms.findings(tenant_id,location_id,status,reported_at);
CREATE INDEX ON bms.evidence(tenant_id,owner_type,owner_id);

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'facilities','user_scopes','responsibility_assignments','qr_tokens','programmes',
        'programme_versions','programme_scopes','inspection_occurrences','inspections',
        'findings','corrective_actions','evidence','sync_submissions'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE bms.%I ADD CONSTRAINT positive_revision CHECK (revision>0)',
            table_name
        );
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
REVOKE UPDATE ON bms.programme_versions,bms.sync_submissions FROM bms_runtime;
