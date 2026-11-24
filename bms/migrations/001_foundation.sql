CREATE SCHEMA IF NOT EXISTS bms;
REVOKE ALL ON SCHEMA bms FROM PUBLIC;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bms_runtime') THEN
        CREATE ROLE bms_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname = 'bms_runtime' AND (rolsuper OR rolbypassrls OR rolcanlogin)
    ) THEN
        RAISE EXCEPTION 'bms_runtime must be NOLOGIN NOSUPERUSER NOBYPASSRLS';
    END IF;
END $$;
GRANT USAGE ON SCHEMA bms TO bms_runtime;

CREATE TABLE bms.tenants (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL CHECK (tenant_id = id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    school_code text NOT NULL UNIQUE,
    name text NOT NULL,
    timezone text NOT NULL
);
CREATE TABLE bms.users (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    name text NOT NULL,
    login_id text NOT NULL,
    pin_hash text NOT NULL,
    role text NOT NULL CHECK (role IN ('officer', 'supervisor', 'administrator', 'leadership')),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    must_change_pin boolean NOT NULL DEFAULT true,
    failed_pin_attempts integer NOT NULL DEFAULT 0,
    locked_until timestamptz,
    UNIQUE (tenant_id, login_id),
    UNIQUE (tenant_id, id)
);
CREATE TABLE bms.sessions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    user_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('interactive', 'sync')),
    token_hash text NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    FOREIGN KEY (tenant_id, user_id) REFERENCES bms.users(tenant_id, id)
);
CREATE TABLE bms.client_operations (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    actor_id uuid NOT NULL,
    command text NOT NULL,
    request_hash text NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, actor_id) REFERENCES bms.users(tenant_id, id)
);
CREATE TABLE bms.audit_events (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES bms.tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    actor_id uuid NOT NULL,
    actor_role text NOT NULL,
    action text NOT NULL,
    entity_id uuid NOT NULL,
    before_data jsonb,
    after_data jsonb,
    reason text,
    device_occurred_at timestamptz,
    received_at timestamptz NOT NULL DEFAULT now(),
    device_id uuid,
    offline boolean NOT NULL DEFAULT false,
    operation_id uuid NOT NULL,
    FOREIGN KEY (tenant_id, actor_id) REFERENCES bms.users(tenant_id, id)
);
CREATE INDEX ON bms.audit_events (tenant_id, received_at, id);

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'tenants', 'users', 'sessions', 'client_operations', 'audit_events'
    ] LOOP
        EXECUTE format('ALTER TABLE bms.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE bms.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON bms.%I USING
             (tenant_id = nullif(current_setting(''bms.tenant_id'', true), '''')::uuid)
             WITH CHECK
             (tenant_id = nullif(current_setting(''bms.tenant_id'', true), '''')::uuid)',
            table_name
        );
    END LOOP;
END $$;
GRANT SELECT, INSERT, UPDATE ON bms.tenants, bms.users, bms.sessions,
    bms.client_operations TO bms_runtime;
GRANT SELECT, INSERT ON bms.audit_events TO bms_runtime;

-- These narrowly scoped bootstrap lookups run before a tenant session exists.
CREATE FUNCTION bms.school_tenant(code text) RETURNS uuid
LANGUAGE sql SECURITY DEFINER STABLE SET search_path = pg_catalog, bms AS $$
    SELECT id FROM bms.tenants WHERE school_code = code
$$;
CREATE FUNCTION bms.session_tenant(digest text) RETURNS uuid
LANGUAGE sql SECURITY DEFINER STABLE SET search_path = pg_catalog, bms AS $$
    SELECT tenant_id FROM bms.sessions
    WHERE token_hash = digest
$$;
REVOKE ALL ON FUNCTION bms.school_tenant(text), bms.session_tenant(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bms.school_tenant(text), bms.session_tenant(text) TO bms_runtime;

