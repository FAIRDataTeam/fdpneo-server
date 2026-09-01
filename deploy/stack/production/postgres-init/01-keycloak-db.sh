#!/bin/sh
# Creates Keycloak's own database + role inside the stack's Postgres.
# Runs ONCE, on the first boot of an empty postgres-data volume (the standard
# /docker-entrypoint-initdb.d contract) — never against existing data.
set -eu

: "${KC_DB_PASSWORD:?KC_DB_PASSWORD must be set (see .env.example)}"

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE USER keycloak WITH PASSWORD '${KC_DB_PASSWORD}';
CREATE DATABASE keycloak OWNER keycloak;
SQL

echo "[postgres-init] keycloak database + role created."
