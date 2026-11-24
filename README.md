# Wallinde backend

Standalone Python 3.13+ / FastAPI / PostgreSQL 18 project. Owns authentication,
tenant isolation, inspections, evidence, findings, duties, emergencies, migrations
and the scheduler/worker that reads sensor events over HTTP.
It does not import or require the sensor-service project.

## Install and run

Run these commands from this folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
Copy-Item .env.example .env
```

Set the runtime database URL and a generated secret in `.env`, not in source.
Migrations require a separate privileged connection:

```powershell
$env:BMS_MIGRATION_DATABASE_URL = 'postgresql://MIGRATION_USER:PASSWORD@localhost:5432/bms'
.\.venv\Scripts\bms.exe migrate
.\.venv\Scripts\bms.exe bootstrap --school-code YOUR-SCHOOL --school-name "Synthetic School" --timezone Africa/Nairobi --user-id ADMIN --name "Administrator"
.\.venv\Scripts\python.exe -m uvicorn bms.app:app --host 127.0.0.1 --port 8000
```

Bootstrap prompts for a temporary password. In another terminal, with the same
runtime configuration, run `.\.venv\Scripts\bms-scheduler.exe --school-code YOUR-SCHOOL`.
API documentation is served at `/docs`; health is `/health`.

For sensor polling set `BMS_SENSOR_SERVICE_URL` and `BMS_SENSOR_SERVICE_TOKEN`
to this school's reader credential. The sensor service is optional; inspection
and emergency workflows do not require it. Mobile and frontend consume `/api/v1`.

## Checks

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
$env:BMS_TEST_ADMIN_DATABASE_URL = 'postgresql://TEST_ADMIN:PASSWORD@localhost:5432/postgres'
.\.venv\Scripts\python.exe -m pytest -q
```

Tests create/drop a uniquely named database and require database-creation rights.
Never use a production database. Sensor-consumer tests use v1 wire fixtures;
the workspace's sensor E2E runner separately tests the actual service boundary.

## Data and deployment

The working directory determines `.env` and the default evidence directory.
The existing workspace demo continues to use its original root `.local` paths.
Do not point a standalone deployment at a new relative evidence directory when
reusing an existing database: configure `BMS_EVIDENCE_DIRECTORY` to the existing
absolute path. Keep the existing secret stable to preserve credentials.
Do not copy private demo settings into source or reset existing accounts.

Production hosting, backups, evidence retention, approved procedures and real
device/communication testing remain separate deployment gates. Notifications
are in-app only, not a replacement for established emergency arrangements.
