#!/usr/bin/env bash
# Restore a bundle made by backup.sh onto a fresh stack, e.g. on a new host (ADR-0029 §4).
#
# Usage, from the stack directory that holds compose.yaml (same compose files
# as the source, typically the same FDPneo release):
#   cd deploy/stack && ./restore.sh BUNDLE.fdpbk                  # evaluation stack
#   cd deploy/stack/production && ../restore.sh BUNDLE.fdpbk      # production stack
#
# Steps: unseal and check the bundle, compare image versions, restore .env (you
# may change PUBLIC_HOST), restore the application archive into empty stores,
# import the Keycloak realm with its users, then start the stack. When the
# deployment's identifier base changes (new PUBLIC_HOST or port), records are
# rebased, the realm's URLs follow the new host, and metrics are re-keyed.
#
# The target must be fresh: no existing postgres/graphdb/keycloak volumes for
# this compose project. FORCE=1 replaces an existing deployment's data instead.
#
# Environment:
#   BACKUP_PASSPHRASE_FILE  read the passphrase from this file instead of prompting
#   NEW_PUBLIC_HOST         set PUBLIC_HOST without prompting
#   ASSUME_YES=1            do not ask for confirmation (image differences, PUBLIC_HOST)
#   FORCE=1                 restore over existing data (--overwrite)
#   SERVER_IMAGE            server image used to unseal before .env exists
#                           (default: from the compose files, else :${IMAGE_TAG:-main})
set -euo pipefail
umask 077

SCRIPT_NAME=restore
# shellcheck source=backup-common.sh
. "$(cd "$(dirname "$0")" && pwd)/backup-common.sh"
require_stack_dir

[ $# -ge 1 ] || die "usage: restore.sh BUNDLE.fdpbk"
BUNDLE=$1
[ -f "$BUNDLE" ] || die "$BUNDLE not found"

confirm() {
  [ "${ASSUME_YES:-0}" = 1 ] && return 0
  [ -t 0 ] || die "$1 (set ASSUME_YES=1 to proceed non-interactively)"
  printf '%s [y/N] ' "$1" >&2
  read -r answer
  case "$answer" in y|Y|yes) return 0 ;; *) die "aborted" ;; esac
}

overwrite=()
existing=$(docker volume ls -q --filter "name=^${PROJECT}_(postgres|graphdb|keycloak)-data$")
if [ -n "$existing" ]; then
  [ "${FORCE:-0}" = 1 ] || die "this stack already has data volumes ($(printf '%s' "$existing" | tr '\n' ' ')). Restore needs a fresh stack (docker compose down -v), or FORCE=1 to replace its data."
  overwrite=(--overwrite)
  log "FORCE=1: existing data will be replaced"
  docker compose stop server keycloak >/dev/null 2>&1 || true
fi

read_passphrase once

WORK=$(mktemp -d "${TMPDIR:-/tmp}/fdpneo-restore.XXXXXX")
IMPORT_VOLUME="${PROJECT}_kc-import-$$"
cleanup() {
  docker volume rm -f "$IMPORT_VOLUME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# The compose files may not render yet (production needs the .env that is still
# inside the bundle), so unseal with a plain `docker run` of the server image.
server_image=${SERVER_IMAGE:-$(docker compose config --images 2>/dev/null | grep 'fdpneo-server' | head -n 1 || true)}
server_image=${server_image:-ghcr.io/fairdatateam/fdpneo-server:${IMAGE_TAG:-main}}
log "unsealing the bundle (with $server_image)"
cp "$BUNDLE" "$WORK/bundle.fdpbk"
printf '%s\n' "$PASSPHRASE" | docker run --rm -i --user "$(id -u):$(id -g)" -v "$WORK:/work" \
  --entrypoint fdp "$server_image" backup unseal /work/bundle.fdpbk /work/bundle.tar \
  --passphrase-file - >/dev/null
tar -C "$WORK" -xf "$WORK/bundle.tar"
rm -f "$WORK/bundle.tar" "$WORK/bundle.fdpbk"
STAGING="$WORK/fdpneo-backup"
[ -f "$STAGING/bundle.json" ] && [ -f "$STAGING/app.zip" ] || die "not a backup.sh bundle"
log "bundle:"
sed 's/^/    /' "$STAGING/bundle.json"

old_host=""
if [ -f "$STAGING/config/.env" ]; then
  if [ -f .env ] && ! cmp -s .env "$STAGING/config/.env"; then
    mv .env ".env.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
    log "kept the previous .env as .env.before-restore-*"
  fi
  cp "$STAGING/config/.env" .env
  old_host=$(sed -n 's/^PUBLIC_HOST=//p' .env | tail -n 1)
fi
for f in compose.override.yaml compose.override.yml; do
  if [ -f "$STAGING/config/$f" ] && [ ! -f "$f" ]; then cp "$STAGING/config/$f" .; fi
done
new_host=${NEW_PUBLIC_HOST:-}
if [ -z "$new_host" ] && [ -n "$old_host" ] && [ "${ASSUME_YES:-0}" != 1 ] && [ -t 0 ]; then
  printf 'PUBLIC_HOST [%s]: ' "$old_host" >&2
  read -r new_host
fi
new_host=${new_host:-$old_host}
if [ -n "$new_host" ] && [ "$new_host" != "$old_host" ]; then
  if grep -q '^PUBLIC_HOST=' .env 2>/dev/null; then
    sed -i.bak "s#^PUBLIC_HOST=.*#PUBLIC_HOST=$new_host#" .env && rm -f .env.bak
  else
    printf 'PUBLIC_HOST=%s\n' "$new_host" >>.env
  fi
  log "PUBLIC_HOST changed: $old_host -> $new_host"
fi
require_compose_config

current_images=$(docker compose config --images | sort -u)
missing=$(sed -n '/"images": {/,/}/p' "$STAGING/bundle.json" | grep -oE '": "[^"]+"' \
  | sed 's/^": "//; s/"$//' | sort -u | while read -r img; do
      printf '%s\n' "$current_images" | grep -qxF "$img" || printf '%s\n' "$img"
    done)
if [ -n "$missing" ]; then
  log "these images of the backed-up stack differ from this stack's compose files:"
  printf '%s\n' "$missing" | sed 's/^/    /'
  confirm "Restore anyway? Restoring into a newer server is supported; into an older one is not."
fi


log "starting the stores"
docker compose up -d --wait postgres graphdb
docker compose run --rm --no-deps graphdb-init

new_base=$(docker compose run --rm --no-deps -T --entrypoint python server -c \
  'from fdpneo_server.config import get_settings; print(get_settings().resolved_identifier_base)' | tail -n 1)
old_base=$(docker run --rm -i "$HELPER_IMAGE" sh -c 'cat >/a.zip && unzip -p /a.zip manifest.json' \
  <"$STAGING/app.zip" | sed -n 's/.*"identifier_base": "\([^"]*\)".*/\1/p' | head -n 1)
[ -n "$old_base" ] || die "could not read the identifier base from the application archive"
[ -n "$new_base" ] || die "could not resolve this deployment's identifier base"

if [ "$old_base" = "$new_base" ]; then
  log "restoring the application archive (identifier base $new_base)"
  run_fdp backup restore /work/fdpneo-backup/app.zip ${overwrite[@]+"${overwrite[@]}"}
else
  log "restoring the application archive, rebased $old_base -> $new_base"
  run_fdp backup import --rebase /work/fdpneo-backup/app.zip ${overwrite[@]+"${overwrite[@]}"}
fi

if [ -n "$old_host" ] && [ "$new_host" != "$old_host" ]; then
  escaped=$(printf '%s' "$old_host" | sed 's/[.[\*^$/]/\\&/g')
  for f in "$STAGING"/keycloak/*.json; do
    sed -i.bak "s#//$escaped#//$new_host#g" "$f" && rm -f "$f.bak"
  done
fi
# The postgres healthcheck answers on the unix socket while first-boot init
# scripts (e.g. Keycloak's database) still run; Keycloak needs TCP.
for _ in $(seq 1 60); do
  docker compose exec -T postgres sh -c 'pg_isready -q -h 127.0.0.1 -U "$POSTGRES_USER"' && break
  sleep 2
done
log "importing the Keycloak realm with its users"
docker volume create "$IMPORT_VOLUME" >/dev/null
dir_to_volume "$STAGING/keycloak" "$IMPORT_VOLUME"
docker compose run --rm --no-deps -T -v "$IMPORT_VOLUME:/import" keycloak \
  import --dir /import --override true >"$WORK/kc-import.log" 2>&1 \
  || { cat "$WORK/kc-import.log" >&2; die "Keycloak import failed"; }

log "starting the stack"
docker compose up -d
for _ in $(seq 1 60); do
  [ "$(docker compose ps server --format '{{.Health}}')" = healthy ] && break
  sleep 5
done
[ "$(docker compose ps server --format '{{.Health}}')" = healthy ] || die "the server did not become healthy; check docker compose logs server"

if [ "$old_base" != "$new_base" ]; then
  log "re-keying metrics from $old_base to $new_base"
  docker compose exec -T server fdp metrics rebase-resources --from "$old_base" --to "$new_base"
fi
log "done. Sign in and check the deployment; then delete the bundle copy you no longer need."
