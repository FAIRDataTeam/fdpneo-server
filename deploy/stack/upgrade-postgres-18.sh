#!/usr/bin/env bash
# Migrate a bundled stack's PostgreSQL volume from PostgreSQL 16 to 18.
#
# Server 0.19 moves the bundled stacks from postgres:16 to postgres:18. A major
# PostgreSQL version cannot open an older data directory, and the official
# postgres:18 image also moved its volume from /var/lib/postgresql/data to
# /var/lib/postgresql. On an existing volume the new image therefore refuses to
# start ("old database(s) found"). This script performs the upgrade by logical
# dump and restore:
#
#   1. stop the stack;
#   2. dump every database and role with a temporary postgres:16 container;
#   3. copy the old volume to <volume>-pg16-backup (kept for rollback);
#   4. empty the volume, initialise it with postgres:18, restore the dump;
#   5. start the stack again.
#
# Usage (from the stack directory that holds compose.yaml and .env):
#   cd deploy/stack && ./upgrade-postgres-18.sh                    # evaluation stack
#   cd deploy/stack/production && ../upgrade-postgres-18.sh        # production stack
#
# Options via environment: POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
# override the values read from .env (defaults: fdp / fdp / fdp).
# KEEP_STOPPED=1 leaves the stack stopped after the upgrade.
#
# Rollback: restore the old compose files (postgres:16, volume mounted at
# /var/lib/postgresql/data), copy <volume>-pg16-backup back into the volume,
# and start the stack. The SQL dump is kept next to this run as well.
set -euo pipefail

log() { printf '[upgrade-postgres] %s\n' "$*"; }
die() { printf '[upgrade-postgres] ERROR: %s\n' "$*" >&2; exit 1; }

[ -f compose.yaml ] || die "run this from the stack directory that contains compose.yaml"
command -v docker >/dev/null || die "docker is not installed"

env_value() {
  # Value of KEY from .env (last assignment wins), without surrounding quotes.
  [ -f .env ] || return 0
  sed -n "s/^[[:space:]]*$1=//p" .env | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

PG_USER=${POSTGRES_USER:-$(env_value POSTGRES_USER)}; PG_USER=${PG_USER:-fdp}
PG_PASSWORD=${POSTGRES_PASSWORD:-$(env_value POSTGRES_PASSWORD)}; PG_PASSWORD=${PG_PASSWORD:-fdp}
PG_DB=${POSTGRES_DB:-$(env_value POSTGRES_DB)}; PG_DB=${PG_DB:-fdp}

project=$(docker compose config 2>/dev/null | sed -n 's/^name: //p' | head -n 1)
[ -n "$project" ] || die "could not resolve the compose project name (docker compose config failed)"
volume="${project}_postgres-data"
backup_volume="${volume}-pg16-backup"
helper=alpine:3

docker volume inspect "$volume" >/dev/null 2>&1 || die "volume $volume not found; nothing to upgrade"

layout=$(docker run --rm -v "$volume":/v "$helper" sh -c '
  if [ -s /v/18/docker/PG_VERSION ]; then echo "new:$(cat /v/18/docker/PG_VERSION)";
  elif [ -s /v/PG_VERSION ]; then echo "old:$(cat /v/PG_VERSION)";
  elif [ -z "$(ls -A /v)" ]; then echo empty; else echo unknown; fi')
case "$layout" in
  new:18) log "volume $volume already holds a PostgreSQL 18 cluster; nothing to do."; exit 0 ;;
  old:16) log "volume $volume holds a PostgreSQL 16 cluster; upgrading to 18." ;;
  empty) log "volume $volume is empty; postgres:18 will initialise it on start. Nothing to do."; exit 0 ;;
  *) die "unexpected contents in $volume ($layout); expected a PostgreSQL 16 data directory" ;;
esac

docker volume inspect "$backup_volume" >/dev/null 2>&1 \
  && die "backup volume $backup_volume already exists; remove or rename it before re-running"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
dump_dir="postgres-upgrade-$stamp"
mkdir -p "$dump_dir"
dump="$dump_dir/pg16-dumpall.sql"

wait_ready() {
  local name=$1 i
  for i in $(seq 1 60); do
    if docker exec "$name" pg_isready -U "$PG_USER" -d postgres -q 2>/dev/null \
       && docker exec "$name" psql -U "$PG_USER" -d postgres -Atqc 'select 1' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  docker logs "$name" >&2 || true
  die "$name did not become ready"
}

cleanup() { docker rm -f "${project}-pg16-dump" "${project}-pg18-restore" >/dev/null 2>&1 || true; }
trap cleanup EXIT

log "stopping the stack (volumes are kept)"
docker compose stop

log "dumping all databases with postgres:16"
docker run -d --name "${project}-pg16-dump" -v "$volume":/var/lib/postgresql/data postgres:16 >/dev/null
wait_ready "${project}-pg16-dump"
docker exec "${project}-pg16-dump" psql -U "$PG_USER" -d postgres -Atc \
  "select datname from pg_database where not datistemplate order by 1" > "$dump_dir/databases-before.txt"
docker exec "${project}-pg16-dump" pg_dumpall -U "$PG_USER" > "$dump"
docker rm -f "${project}-pg16-dump" >/dev/null
[ -s "$dump" ] || die "the dump is empty; aborting before touching the volume"
log "dump written to $dump ($(wc -c < "$dump" | tr -d ' ') bytes)"

log "copying $volume to $backup_volume (rollback copy)"
docker volume create "$backup_volume" >/dev/null
docker run --rm -v "$volume":/from:ro -v "$backup_volume":/to "$helper" cp -a /from/. /to/

log "emptying $volume and initialising PostgreSQL 18"
docker run --rm -v "$volume":/v "$helper" find /v -mindepth 1 -delete
docker run -d --name "${project}-pg18-restore" -v "$volume":/var/lib/postgresql \
  -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB="$PG_DB" \
  postgres:18 >/dev/null
wait_ready "${project}-pg18-restore"

log "restoring the dump (\"already exists\" notices for the bootstrap role and database are expected)"
docker exec -i "${project}-pg18-restore" psql -U "$PG_USER" -d postgres -q -v ON_ERROR_STOP=0 \
  < "$dump" > "$dump_dir/restore.log" 2>&1 || true
unexpected=$(grep -E 'ERROR' "$dump_dir/restore.log" | grep -vE 'already exists' || true)

docker exec "${project}-pg18-restore" psql -U "$PG_USER" -d postgres -Atc \
  "select datname from pg_database where not datistemplate order by 1" > "$dump_dir/databases-after.txt"
docker rm -f "${project}-pg18-restore" >/dev/null

if ! diff -q "$dump_dir/databases-before.txt" "$dump_dir/databases-after.txt" >/dev/null; then
  die "database list differs after restore; see $dump_dir (old data is intact in $backup_volume)"
fi
if [ -n "$unexpected" ]; then
  printf '%s\n' "$unexpected" >&2
  die "restore reported errors; see $dump_dir/restore.log (old data is intact in $backup_volume)"
fi
log "restored databases: $(tr '\n' ' ' < "$dump_dir/databases-after.txt")"

if [ "${KEEP_STOPPED:-0}" = 1 ]; then
  log "done. The stack is stopped (KEEP_STOPPED=1); start it with: docker compose up -d"
else
  log "starting the stack"
  docker compose up -d
  log "done."
fi
log "rollback copy: volume $backup_volume; dump: $dump. Remove both once you are satisfied."
