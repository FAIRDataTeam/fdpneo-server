#!/usr/bin/env bash
# Take a full, encrypted backup of a bundled FDPneo stack (ADR-0029 §4).
#
# The bundle holds everything needed to recreate the same deployment on another
# host: the application archive (`fdp backup dump --full`: triple store + Postgres
# state), the Keycloak realm *with users* (Keycloak's own export, which keeps
# user IDs, so token subjects and API-key ownership stay valid), and the
# deployment configuration (.env and any compose override). It is sealed with a
# passphrase (FDPNEO-ENC1), because it contains credentials.
#
# Usage, from the stack directory that holds compose.yaml:
#   cd deploy/stack && ./backup.sh [OUTPUT.fdpbk]                 # evaluation stack
#   cd deploy/stack/production && ../backup.sh [OUTPUT.fdpbk]     # production stack
#
# The server and Keycloak are stopped while the dump and export run (typically a
# few minutes) and started again afterwards. Restore with restore.sh.
#
# Environment:
#   BACKUP_PASSPHRASE_FILE  read the passphrase from this file instead of prompting
#   REALM                   Keycloak realm (default: fdp-dev, or fdp for production)
set -euo pipefail
umask 077

SCRIPT_NAME=backup
# shellcheck source=backup-common.sh
. "$(cd "$(dirname "$0")" && pwd)/backup-common.sh"
require_stack_dir
require_compose_config

stamp=$(date -u +%Y%m%dT%H%M%SZ)
OUTPUT=${1:-fdpneo-backup-$stamp.fdpbk}
case "$OUTPUT" in /*) ;; *) OUTPUT="$PWD/$OUTPUT" ;; esac
[ -e "$OUTPUT" ] && die "$OUTPUT already exists"

read_passphrase confirm

WORK=$(mktemp -d "${TMPDIR:-/tmp}/fdpneo-backup.XXXXXX")
STAGING="$WORK/fdpneo-backup"
EXPORT_VOLUME="${PROJECT}_kc-export-$$"
stopped=0
cleanup() {
  if [ "$stopped" = 1 ]; then docker compose start keycloak server >/dev/null 2>&1 || true; fi
  docker volume rm -f "$EXPORT_VOLUME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$STAGING/keycloak" "$STAGING/config"

images=$(docker compose ps --format '{{.Service}}={{.Image}}' | sort)
[ -n "$images" ] || die "the stack is not running; start it first (docker compose up -d)"

log "stopping server and keycloak so nothing writes during the backup"
docker compose stop server keycloak
stopped=1

log "dumping the triple store and Postgres state (fdp backup dump --full)"
run_fdp backup dump --full /work/fdpneo-backup/app.zip --no-encrypt

log "exporting Keycloak realm '$REALM' with users"
docker volume create "$EXPORT_VOLUME" >/dev/null
docker run --rm -v "$EXPORT_VOLUME:/x" "$HELPER_IMAGE" chown 1000:0 /x
docker compose run --rm --no-deps -T -v "$EXPORT_VOLUME:/export" keycloak \
  export --dir /export --realm "$REALM" --users realm_file >"$WORK/kc-export.log" 2>&1 \
  || { cat "$WORK/kc-export.log" >&2; die "Keycloak export failed"; }
volume_to_dir "$EXPORT_VOLUME" "$STAGING/keycloak"
ls "$STAGING"/keycloak/*.json >/dev/null 2>&1 || die "the Keycloak export produced no files"

log "starting server and keycloak again"
docker compose start keycloak server
stopped=0

for f in .env compose.override.yaml compose.override.yml; do
  if [ -f "$f" ]; then cp "$f" "$STAGING/config/"; fi
done

{
  printf '{\n  "bundle_format": 1,\n  "created_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '  "compose_project": "%s",\n  "keycloak_realm": "%s",\n  "images": {\n' "$PROJECT" "$REALM"
  printf '%s\n' "$images" | awk -F= 'NR>1{printf ",\n"} {printf "    \"%s\": \"%s\"", $1, $2} END{printf "\n"}'
  printf '  }\n}\n'
} >"$STAGING/bundle.json"

log "sealing the bundle"
tar -C "$WORK" -cf "$WORK/bundle.tar" fdpneo-backup
rm -rf "$STAGING"
printf '%s\n' "$PASSPHRASE" | run_fdp backup seal /work/bundle.tar /work/bundle.fdpbk --passphrase-file - >/dev/null
rm -f "$WORK/bundle.tar"
printf '%s\n' "$PASSPHRASE" | run_fdp backup verify /work/bundle.fdpbk --passphrase-file -

mv "$WORK/bundle.fdpbk" "$OUTPUT"
chmod 600 "$OUTPUT"
log "done: $OUTPUT"
log "keep the passphrase safe: without it the backup cannot be restored."
