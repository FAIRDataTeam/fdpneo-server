# Shared helpers for backup.sh and restore.sh (ADR-0029). Sourced, not executed.
# shellcheck shell=bash

# macOS tar would otherwise add AppleDouble "._*" entries (extended attributes),
# which Keycloak's import would try to read as realm files.
export COPYFILE_DISABLE=1

log() { printf '[%s] %s\n' "$SCRIPT_NAME" "$*"; }
die() { printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2; exit 1; }

require_stack_dir() {
  [ -f compose.yaml ] || die "run this from the stack directory that contains compose.yaml"
  command -v docker >/dev/null || die "docker is not installed"
  # Static resolution, so it works before .env exists (a fresh host during restore).
  PROJECT=${COMPOSE_PROJECT_NAME:-$(sed -n 's/^name: *//p' compose.yaml | head -n 1)}
  [ -n "$PROJECT" ] || die "could not resolve the compose project name"
  if [ -z "${REALM:-}" ]; then
    if grep -q 'realm-fdp-prod.json' compose.yaml; then REALM=fdp; else REALM=fdp-dev; fi
  fi
  HELPER_IMAGE=${HELPER_IMAGE:-alpine:3}
}

require_compose_config() {
  docker compose config -q >/dev/null 2>&1 \
    || die "docker compose config failed; check .env (docker compose config shows the error)"
}

# read_passphrase confirm|once -> sets PASSPHRASE. BACKUP_PASSPHRASE_FILE avoids the prompt.
read_passphrase() {
  if [ -n "${BACKUP_PASSPHRASE_FILE:-}" ]; then
    PASSPHRASE=$(head -n 1 "$BACKUP_PASSPHRASE_FILE")
  else
    [ -t 0 ] || die "no terminal for the passphrase prompt; set BACKUP_PASSPHRASE_FILE"
    printf 'Passphrase: ' >&2; IFS= read -rs PASSPHRASE; printf '\n' >&2
    if [ "$1" = confirm ]; then
      printf 'Repeat passphrase: ' >&2; IFS= read -rs again; printf '\n' >&2
      [ "$PASSPHRASE" = "$again" ] || die "the passphrases do not match"
    fi
  fi
  [ "${#PASSPHRASE}" -ge 12 ] || die "the passphrase must be at least 12 characters long"
}

# Run the server image's `fdp` CLI in a one-off container with $WORK mounted at /work.
# Runs as the calling user so files in $WORK stay readable and removable.
run_fdp() {
  docker compose run --rm --no-deps -T --user "$(id -u):$(id -g)" \
    -v "$WORK:/work" --entrypoint fdp server "$@"
}

# Copy a host directory into a Docker volume (owned by Keycloak's uid), and back.
dir_to_volume() {
  tar -C "$1" -cf - . | docker run --rm -i -v "$2:/x" "$HELPER_IMAGE" \
    sh -c 'tar -C /x -xf - && chown -R 1000:0 /x'
}
volume_to_dir() {
  docker run --rm -v "$1:/x:ro" "$HELPER_IMAGE" tar -C /x -cf - . | tar -C "$2" -xf -
}
