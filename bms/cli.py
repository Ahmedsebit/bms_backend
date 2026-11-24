import argparse
import getpass
import os
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from bms.db import migrate, set_tenant
from bms.schemas import Command, CreateUser
from bms.security import hash_password, validate_new_password
from bms.services import audit, lock_tenant


def bootstrap(url, school_code, school_name, timezone, user_id, name, password):
    ZoneInfo(timezone)
    if not school_code or not all(c.isascii() and (c.isalnum() or c == "-") for c in school_code):
        raise ValueError("School code must contain only ASCII letters, digits and hyphens")
    tenant_id, user_uuid = uuid4(), uuid4()
    body = CreateUser(
        id=user_uuid,
        client_operation_id=uuid4(),
        name=name,
        user_id=user_id,
        role="administrator",
        password=password,
    )
    with psycopg.connect(url, row_factory=dict_row) as conn:
        set_tenant(conn, tenant_id)
        conn.execute(
            "INSERT INTO bms.tenants(id,tenant_id,school_code,name,timezone) "
            "VALUES (%s,%s,%s,%s,%s)",
            (tenant_id, tenant_id, school_code.upper(), school_name, timezone),
        )
        conn.execute(
            "INSERT INTO bms.users(id,tenant_id,name,login_id,password_hash,role) "
            "VALUES (%s,%s,%s,%s,%s,'administrator')",
            (
                user_uuid,
                tenant_id,
                body.name,
                body.user_id,
                hash_password(body.password.get_secret_value()),
            ),
        )
        audit(
            conn,
            {"id": user_uuid, "tenant_id": tenant_id, "role": "administrator"},
            "tenant.bootstrap",
            tenant_id,
            Command(client_operation_id=uuid4()),
            after={"schoolCode": school_code.upper()},
        )
    return tenant_id, user_uuid


def reset_account_password(url, school_code, user_id, password, reason):
    password = validate_new_password(password)
    if not reason.strip():
        raise ValueError("A reset reason is required")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        tenant = conn.execute(
            "SELECT id FROM bms.tenants WHERE school_code=%s", (school_code.upper(),)
        ).fetchone()
        if not tenant:
            raise ValueError("Account not found")
        set_tenant(conn, tenant["id"])
        lock_tenant(conn, tenant["id"])
        user = conn.execute(
            "SELECT id FROM bms.users WHERE tenant_id=%s AND login_id=%s",
            (tenant["id"], user_id),
        ).fetchone()
        if not user:
            raise ValueError("Account not found")
        conn.execute(
            "UPDATE bms.users SET password_hash=%s, must_change_password=true, "
            "failed_password_attempts=0, locked_until=NULL, updated_at=now(), revision=revision+1 "
            "WHERE tenant_id=%s AND id=%s",
            (hash_password(password), tenant["id"], user["id"]),
        )
        conn.execute(
            "UPDATE bms.sessions SET revoked_at=now(), updated_at=now(), revision=revision+1 "
            "WHERE tenant_id=%s AND user_id=%s AND revoked_at IS NULL",
            (tenant["id"], user["id"]),
        )
        audit(
            conn,
            {"id": None, "tenant_id": tenant["id"], "role": "system"},
            "users.password_recovery",
            user["id"],
            Command(client_operation_id=uuid4()),
            after={"mustChangePassword": True},
            reason=reason,
        )


def main():
    parser = argparse.ArgumentParser(description="BMS database administration")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    create = sub.add_parser("bootstrap")
    for arg in ("school-code", "school-name", "timezone", "user-id", "name"):
        create.add_argument(f"--{arg}", required=True)
    reset = sub.add_parser("reset-password")
    for arg in ("school-code", "user-id", "reason"):
        reset.add_argument(f"--{arg}", required=True)
    args = parser.parse_args()
    url = os.environ.get("BMS_MIGRATION_DATABASE_URL")
    if not url:
        parser.error("Set BMS_MIGRATION_DATABASE_URL to the privileged migration connection")
    if args.command == "migrate":
        migrate(url)
        print("Database migrations applied.")
    else:
        password = getpass.getpass("Temporary password: ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match")
        if args.command == "bootstrap":
            bootstrap(
                url,
                args.school_code,
                args.school_name,
                args.timezone,
                args.user_id,
                args.name,
                password,
            )
            print("School and administrator created; password change required at first login.")
        else:
            reset_account_password(url, args.school_code, args.user_id, password, args.reason)
            print("Password reset; password change required at next login.")


if __name__ == "__main__":
    main()
