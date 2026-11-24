ALTER TABLE bms.users RENAME COLUMN pin_hash TO password_hash;
ALTER TABLE bms.users ALTER COLUMN password_hash DROP NOT NULL;
ALTER TABLE bms.users RENAME COLUMN must_change_pin TO must_change_password;
ALTER TABLE bms.users RENAME COLUMN failed_pin_attempts TO failed_password_attempts;

ALTER TABLE bms.audit_events ALTER COLUMN actor_id DROP NOT NULL;
ALTER TABLE bms.audit_events ADD CONSTRAINT audit_actor_kind CHECK (
    (actor_id IS NULL AND actor_role = 'system') OR
    (actor_id IS NOT NULL AND actor_role IN (
        'officer', 'supervisor', 'administrator', 'leadership'
    ))
);

-- A PIN hash cannot be upgraded into a password: invalidate, never reinterpret it.
INSERT INTO bms.audit_events (
    id, tenant_id, actor_id, actor_role, action, entity_id, after_data, reason, operation_id
)
SELECT gen_random_uuid(), tenant_id, NULL, 'system', 'auth.credentials_invalidated', id,
    '{"mustChangePassword":true,"passwordResetRequired":true}'::jsonb,
    'PIN authentication replaced by passwords; administrator reset required', gen_random_uuid()
FROM bms.users;
UPDATE bms.users SET password_hash = NULL, must_change_password = true,
    failed_password_attempts = 0, locked_until = NULL, updated_at = now(), revision = revision + 1;
UPDATE bms.sessions SET revoked_at = now(), updated_at = now(), revision = revision + 1
WHERE revoked_at IS NULL;
