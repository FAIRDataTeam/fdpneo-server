#!/bin/sh
# Server container entrypoint: bring the database schema up to date, then serve.
#
# Migrations are idempotent (alembic upgrade head); on a fresh database this
# creates the schema, on an existing one it is a no-op. The default deployment
# profile auto-applies inside the app lifespan (FDP_PROFILE_AUTO_APPLY=true) once
# the triple store + Postgres are reachable. The `Server:` banner is stripped
# (--no-server-header, audit R-05); terminate TLS + add HSTS at a reverse proxy.
set -eu

echo "[entrypoint] applying database migrations (fdp db migrate)..."
fdp db migrate

echo "[entrypoint] starting uvicorn on 0.0.0.0:8000..."
exec uvicorn fdpneo_server.main:app --host 0.0.0.0 --port 8000 --no-server-header
