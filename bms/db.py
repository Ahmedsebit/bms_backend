from contextlib import contextmanager
from importlib.resources import files
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from bms.config import settings


@contextmanager
def connection():
    with psycopg.connect(settings().database_url.get_secret_value(), row_factory=dict_row) as conn:
        conn.execute("SET LOCAL ROLE bms_runtime")
        yield conn


def set_tenant(conn, tenant_id: UUID):
    conn.execute("SELECT set_config('bms.tenant_id', %s, true)", (str(tenant_id),))


def migrate(url: str):
    with psycopg.connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(682031467)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.bms_schema_versions "
            "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for path in sorted(files("bms").joinpath("migrations").iterdir(), key=lambda p: p.name):
            exists = conn.execute(
                "SELECT 1 FROM public.bms_schema_versions WHERE version = %s", (path.name,)
            ).fetchone()
            if not exists:
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO public.bms_schema_versions(version) VALUES (%s)", (path.name,)
                )
