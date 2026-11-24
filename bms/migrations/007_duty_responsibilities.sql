DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'duty_grants','incident_transfers','incident_progress','incident_contacts',
        'roster_reviews','sync_resolutions'
    ] LOOP
        EXECUTE format('CREATE TABLE bms.%I (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            revision integer NOT NULL DEFAULT 1 CHECK (revision>0),
            UNIQUE (tenant_id,id)
        )',table_name);
        EXECUTE format('ALTER TABLE bms.%I ENABLE ROW LEVEL SECURITY',table_name);
        EXECUTE format('ALTER TABLE bms.%I FORCE ROW LEVEL SECURITY',table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON bms.%I USING
            (tenant_id=nullif(current_setting(''bms.tenant_id'',true),'''')::uuid)
            WITH CHECK (tenant_id=nullif(current_setting(''bms.tenant_id'',true),'''')::uuid)',
            table_name);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE ON bms.%I TO bms_runtime',table_name);
    END LOOP;
END $$;
ALTER TABLE bms.duty_grants
    ADD COLUMN user_id uuid NOT NULL,
    ADD COLUMN building_id uuid,
    ADD COLUMN capability text NOT NULL CHECK (capability IN (
        'incident.command','roster.manage','safety.approve','safety.verify'
    )),
    ADD COLUMN starts_at timestamptz NOT NULL,
    ADD COLUMN ends_at timestamptz NOT NULL,
    ADD COLUMN active boolean NOT NULL DEFAULT true,
    ADD COLUMN approval_note text NOT NULL,
    ADD COLUMN approved_by uuid NOT NULL,
    ADD CHECK (starts_at<ends_at),
    ADD FOREIGN KEY (tenant_id,user_id) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,building_id) REFERENCES bms.facilities(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,approved_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_settings ADD COLUMN progress_minutes integer NOT NULL DEFAULT 5
    CHECK (progress_minutes BETWEEN 1 AND 60);
ALTER TABLE bms.emergency_incidents
    ADD COLUMN response_owner_id uuid,
    ADD COLUMN responsibility_accepted_at timestamptz,
    ADD COLUMN progress_at timestamptz,
    ADD COLUMN progress_deadline timestamptz,
    ADD COLUMN progress_minutes integer NOT NULL DEFAULT 5,
    ADD FOREIGN KEY (tenant_id,response_owner_id) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_alerts DROP CONSTRAINT emergency_alerts_stage_check;
ALTER TABLE bms.emergency_alerts ADD CHECK (stage IN ('primary','backup','lead'));
ALTER TABLE bms.incident_transfers
    ADD COLUMN incident_id uuid NOT NULL,
    ADD COLUMN outgoing_user_id uuid,
    ADD COLUMN incoming_user_id uuid NOT NULL,
    ADD COLUMN requested_by uuid NOT NULL,
    ADD COLUMN note text NOT NULL,
    ADD COLUMN status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','accepted','declined','cancelled')),
    ADD COLUMN decided_at timestamptz,
    ADD COLUMN decision_note text NOT NULL DEFAULT '',
    ADD FOREIGN KEY (tenant_id,incident_id) REFERENCES bms.emergency_incidents(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,outgoing_user_id) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,incoming_user_id) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,requested_by) REFERENCES bms.users(tenant_id,id);
CREATE UNIQUE INDEX ON bms.incident_transfers(tenant_id,incident_id) WHERE status='pending';
ALTER TABLE bms.incident_progress
    ADD COLUMN incident_id uuid NOT NULL,
    ADD COLUMN recorded_by uuid NOT NULL,
    ADD COLUMN kind text NOT NULL CHECK (kind IN ('accepted','progress','transfer')),
    ADD COLUMN note text NOT NULL,
    ADD FOREIGN KEY (tenant_id,incident_id) REFERENCES bms.emergency_incidents(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,recorded_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.incident_contacts
    ADD COLUMN incident_id uuid NOT NULL,
    ADD COLUMN recorded_by uuid NOT NULL,
    ADD COLUMN recipient_user_id uuid NOT NULL,
    ADD COLUMN channel text NOT NULL CHECK (channel IN ('phone','radio','in_person')),
    ADD COLUMN outcome text NOT NULL CHECK (outcome IN (
        'attempted','reached','failed','no_response'
    )),
    ADD COLUMN occurred_at timestamptz NOT NULL,
    ADD COLUMN note text NOT NULL,
    ADD FOREIGN KEY (tenant_id,incident_id) REFERENCES bms.emergency_incidents(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,recorded_by) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,recipient_user_id) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.roster_reviews
    ADD COLUMN building_id uuid NOT NULL,
    ADD COLUMN reviewed_by uuid NOT NULL,
    ADD COLUMN expected_count integer NOT NULL CHECK (expected_count>=0),
    ADD COLUMN note text NOT NULL,
    ADD FOREIGN KEY (tenant_id,building_id) REFERENCES bms.facilities(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,reviewed_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.sync_resolutions
    ADD COLUMN submission_id uuid NOT NULL,
    ADD COLUMN owner_id uuid NOT NULL,
    ADD COLUMN status text NOT NULL CHECK (status IN ('reviewing','resolved')),
    ADD COLUMN decision text NOT NULL CHECK (decision IN ('retain','dismiss','replace')),
    ADD COLUMN note text NOT NULL,
    ADD COLUMN replacement_operation_id uuid,
    ADD COLUMN decided_by uuid NOT NULL,
    ADD UNIQUE (tenant_id,submission_id),
    ADD FOREIGN KEY (tenant_id,submission_id) REFERENCES bms.sync_submissions(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,replacement_operation_id) REFERENCES bms.sync_submissions(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,owner_id) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,decided_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_shifts
    ADD COLUMN primary_accepted_at timestamptz,
    ADD COLUMN backup_accepted_at timestamptz;
ALTER TABLE bms.emergency_people
    ADD COLUMN maintained_by uuid,
    ADD FOREIGN KEY (tenant_id,maintained_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_tasks
    ADD COLUMN safety_critical boolean NOT NULL DEFAULT false,
    ADD COLUMN verified_by uuid,
    ADD COLUMN verified_at timestamptz,
    ADD COLUMN verification_note text NOT NULL DEFAULT '',
    ADD FOREIGN KEY (tenant_id,verified_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_recovery
    ADD COLUMN authored_by uuid,
    ADD FOREIGN KEY (tenant_id,authored_by) REFERENCES bms.users(tenant_id,id);
-- Legacy plans have no reliably attributable author; a new authored revision is required.
UPDATE bms.emergency_recovery SET approved_by=NULL,approved_at=NULL;
REVOKE UPDATE ON bms.incident_progress,bms.incident_contacts,bms.roster_reviews FROM bms_runtime;
CREATE INDEX ON bms.duty_grants(tenant_id,user_id,capability,ends_at) WHERE active;
