ALTER TABLE bms.programmes DROP CONSTRAINT programmes_check;
ALTER TABLE bms.programmes ADD CHECK (
    start_time<>end_time AND (recurrence='daily' OR start_time<end_time)
);
ALTER TABLE bms.programme_versions
    ADD COLUMN approved_by uuid,
    ADD COLUMN approved_at timestamptz,
    ADD COLUMN approval_note text NOT NULL DEFAULT '',
    ADD FOREIGN KEY (tenant_id,approved_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.evidence DROP CONSTRAINT evidence_owner_type_check;
ALTER TABLE bms.evidence ADD CHECK (
    owner_type IN ('inspection','finding','action','review','incident')
);

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'emergency_settings','emergency_contacts','emergency_points','emergency_people',
        'emergency_guardians','emergency_shifts','emergency_handovers','emergency_incidents',
        'emergency_alerts','emergency_rollcall','emergency_tasks','emergency_recovery',
        'emergency_releases'
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

ALTER TABLE bms.emergency_settings
    ADD UNIQUE (tenant_id),
    ADD COLUMN acknowledgement_minutes integer NOT NULL DEFAULT 5
        CHECK (acknowledgement_minutes BETWEEN 1 AND 60),
    ADD COLUMN approved_by uuid,
    ADD COLUMN approved_at timestamptz,
    ADD COLUMN approval_note text NOT NULL DEFAULT '',
    ADD FOREIGN KEY (tenant_id,approved_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_contacts
    ADD COLUMN name text NOT NULL,
    ADD COLUMN phone text NOT NULL,
    ADD COLUMN purpose text NOT NULL,
    ADD COLUMN active boolean NOT NULL DEFAULT true,
    ADD COLUMN approved_by uuid,
    ADD COLUMN approved_at timestamptz,
    ADD COLUMN approval_note text NOT NULL DEFAULT '',
    ADD FOREIGN KEY (tenant_id,approved_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_points
    ADD COLUMN building_id uuid NOT NULL,
    ADD COLUMN name text NOT NULL,
    ADD COLUMN instructions text NOT NULL DEFAULT '',
    ADD COLUMN active boolean NOT NULL DEFAULT true,
    ADD FOREIGN KEY (tenant_id,building_id) REFERENCES bms.facilities(tenant_id,id);
ALTER TABLE bms.emergency_people
    ADD COLUMN building_id uuid NOT NULL,
    ADD COLUMN name text NOT NULL,
    ADD COLUMN kind text NOT NULL CHECK (kind IN ('student','staff','visitor')),
    ADD COLUMN presence text NOT NULL CHECK (presence IN ('on_campus','temporarily_away')),
    ADD COLUMN away_until timestamptz,
    ADD COLUMN assistance text NOT NULL DEFAULT '',
    ADD COLUMN active boolean NOT NULL DEFAULT true,
    ADD FOREIGN KEY (tenant_id,building_id) REFERENCES bms.facilities(tenant_id,id);
ALTER TABLE bms.emergency_guardians
    ADD COLUMN person_id uuid NOT NULL,
    ADD COLUMN name text NOT NULL,
    ADD COLUMN relationship text NOT NULL,
    ADD COLUMN active boolean NOT NULL DEFAULT true,
    ADD COLUMN approved_by uuid,
    ADD COLUMN approved_at timestamptz,
    ADD COLUMN approval_note text NOT NULL DEFAULT '',
    ADD FOREIGN KEY (tenant_id,person_id) REFERENCES bms.emergency_people(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,approved_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_shifts
    ADD COLUMN building_id uuid NOT NULL,
    ADD COLUMN primary_user_id uuid NOT NULL,
    ADD COLUMN backup_user_id uuid NOT NULL,
    ADD COLUMN lead_user_id uuid NOT NULL,
    ADD COLUMN starts_at timestamptz NOT NULL,
    ADD COLUMN ends_at timestamptz NOT NULL,
    ADD COLUMN approval_note text NOT NULL DEFAULT '',
    ADD CHECK (starts_at<ends_at),
    ADD CHECK (primary_user_id<>backup_user_id),
    ADD FOREIGN KEY (tenant_id,building_id) REFERENCES bms.facilities(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,primary_user_id) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,backup_user_id) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,lead_user_id) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_handovers
    ADD COLUMN outgoing_shift_id uuid NOT NULL,
    ADD COLUMN incoming_shift_id uuid NOT NULL,
    ADD COLUMN note text NOT NULL,
    ADD COLUMN created_by uuid NOT NULL,
    ADD COLUMN accepted_by uuid,
    ADD COLUMN accepted_at timestamptz,
    ADD CHECK (outgoing_shift_id<>incoming_shift_id),
    ADD UNIQUE (tenant_id,outgoing_shift_id,incoming_shift_id),
    ADD FOREIGN KEY (tenant_id,outgoing_shift_id) REFERENCES bms.emergency_shifts(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,incoming_shift_id) REFERENCES bms.emergency_shifts(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,created_by) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,accepted_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_incidents
    ADD COLUMN building_id uuid NOT NULL,
    ADD COLUMN title text NOT NULL,
    ADD COLUMN hazard text NOT NULL,
    ADD COLUMN location_note text NOT NULL DEFAULT '',
    ADD COLUMN is_drill boolean NOT NULL DEFAULT false,
    ADD COLUMN status text NOT NULL CHECK (status IN ('planned','active','recovery','closed')),
    ADD COLUMN reported_by uuid NOT NULL,
    ADD COLUMN reported_at timestamptz NOT NULL,
    ADD COLUMN started_at timestamptz,
    ADD COLUMN notification_round integer NOT NULL DEFAULT 1 CHECK (notification_round>0),
    ADD COLUMN planned_at timestamptz,
    ADD COLUMN drill_plan jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN lead_user_id uuid,
    ADD COLUMN primary_user_id uuid,
    ADD COLUMN backup_user_id uuid,
    ADD COLUMN timing_approved boolean NOT NULL DEFAULT false,
    ADD COLUMN acknowledgement_minutes integer NOT NULL,
    ADD COLUMN acknowledged_by uuid,
    ADD COLUMN acknowledged_at timestamptz,
    ADD COLUMN phase_note text NOT NULL DEFAULT '',
    ADD FOREIGN KEY (tenant_id,building_id) REFERENCES bms.facilities(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,reported_by) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,lead_user_id) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,primary_user_id) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,backup_user_id) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,acknowledged_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_alerts
    ADD COLUMN incident_id uuid NOT NULL,
    ADD COLUMN recipient_id uuid,
    ADD COLUMN stage text NOT NULL CHECK (stage IN ('primary','backup')),
    ADD COLUMN notification_round integer NOT NULL DEFAULT 1 CHECK (notification_round>0),
    ADD COLUMN status text NOT NULL CHECK (status IN (
        'queued','available_in_app','acknowledged','escalated','failed','cancelled'
    )),
    ADD COLUMN deadline timestamptz NOT NULL,
    ADD COLUMN available_at timestamptz,
    ADD COLUMN acknowledged_at timestamptz,
    ADD COLUMN failure_reason text NOT NULL DEFAULT '',
    ADD UNIQUE (tenant_id,incident_id,stage,notification_round),
    ADD FOREIGN KEY (tenant_id,incident_id) REFERENCES bms.emergency_incidents(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,recipient_id) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_rollcall
    ADD COLUMN incident_id uuid NOT NULL,
    ADD COLUMN person_id uuid NOT NULL,
    ADD COLUMN person_name text NOT NULL,
    ADD COLUMN person_kind text NOT NULL,
    ADD COLUMN expected_presence text NOT NULL,
    ADD COLUMN away_until timestamptz,
    ADD COLUMN assistance text NOT NULL DEFAULT '',
    ADD COLUMN roster_updated_at timestamptz NOT NULL,
    ADD COLUMN status text NOT NULL DEFAULT 'unaccounted' CHECK (status IN (
        'unaccounted','at_assembly','needs_assistance','confirmed_offsite','released'
    )),
    ADD COLUMN point_id uuid,
    ADD COLUMN note text NOT NULL DEFAULT '',
    ADD COLUMN last_confirmed_at timestamptz,
    ADD COLUMN confirmed_by uuid,
    ADD UNIQUE (tenant_id,incident_id,person_id),
    ADD FOREIGN KEY (tenant_id,incident_id) REFERENCES bms.emergency_incidents(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,person_id) REFERENCES bms.emergency_people(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,point_id) REFERENCES bms.emergency_points(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,confirmed_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_tasks
    ADD COLUMN incident_id uuid NOT NULL,
    ADD COLUMN title text NOT NULL,
    ADD COLUMN owner_id uuid NOT NULL,
    ADD COLUMN due_at timestamptz NOT NULL,
    ADD COLUMN status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','completed')),
    ADD COLUMN observation text NOT NULL,
    ADD COLUMN completion_note text NOT NULL DEFAULT '',
    ADD COLUMN completed_by uuid,
    ADD COLUMN completed_at timestamptz,
    ADD FOREIGN KEY (tenant_id,incident_id) REFERENCES bms.emergency_incidents(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,owner_id) REFERENCES bms.users(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,completed_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_recovery
    ADD COLUMN incident_id uuid NOT NULL,
    ADD COLUMN accommodation text NOT NULL,
    ADD COLUMN welfare text NOT NULL,
    ADD COLUMN learning_continuity text NOT NULL,
    ADD COLUMN reopening_note text NOT NULL DEFAULT '',
    ADD COLUMN approved_by uuid,
    ADD COLUMN approved_at timestamptz,
    ADD UNIQUE (tenant_id,incident_id),
    ADD FOREIGN KEY (tenant_id,incident_id) REFERENCES bms.emergency_incidents(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,approved_by) REFERENCES bms.users(tenant_id,id);
ALTER TABLE bms.emergency_releases
    ADD COLUMN incident_id uuid NOT NULL,
    ADD COLUMN person_id uuid NOT NULL,
    ADD COLUMN guardian_id uuid NOT NULL,
    ADD COLUMN guardian_name text NOT NULL,
    ADD COLUMN person_name text NOT NULL,
    ADD COLUMN verification_note text NOT NULL,
    ADD COLUMN released_by uuid NOT NULL,
    ADD COLUMN released_at timestamptz NOT NULL DEFAULT now(),
    ADD UNIQUE (tenant_id,incident_id,person_id),
    ADD FOREIGN KEY (tenant_id,incident_id) REFERENCES bms.emergency_incidents(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,person_id) REFERENCES bms.emergency_people(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,guardian_id) REFERENCES bms.emergency_guardians(tenant_id,id),
    ADD FOREIGN KEY (tenant_id,released_by) REFERENCES bms.users(tenant_id,id);
REVOKE UPDATE ON bms.emergency_releases FROM bms_runtime;
CREATE INDEX ON bms.emergency_alerts(tenant_id,status,deadline);
CREATE INDEX ON bms.emergency_shifts(tenant_id,building_id,starts_at,ends_at);
CREATE INDEX ON bms.emergency_rollcall(tenant_id,incident_id,status);
