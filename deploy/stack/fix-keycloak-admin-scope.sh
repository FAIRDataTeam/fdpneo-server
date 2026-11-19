#!/usr/bin/env bash
# Give the `fdp-server` service account the `kc-admin-api` client scope.
#
# From Keycloak 26.8.2 the Admin REST API only accepts access tokens that
# carry the `kc-admin-api` scope. Keycloak's own admin clients have it; the
# `fdp-server` client of the bundled realms did not. Without it the /users
# facade (ADR-0013) fails: the token is still issued, but every Admin API call
# answers 401 and the server reports "IdP admin ... returned HTTP 401".
#
# The realm files now assign the scope, but `--import-realm` never touches a
# realm that already exists. Run this once after Keycloak moves to 26.8.2 or
# later (the `26.8` image tag picks it up on the next pull):
#
#   cd deploy/stack && ./fix-keycloak-admin-scope.sh                  # evaluation stack
#   cd deploy/stack/production && ../fix-keycloak-admin-scope.sh      # production stack
#
# It is idempotent. On an older Keycloak, which has no such scope, it changes
# nothing; run it again after the upgrade. REALM defaults to fdp-dev
# (deploy/stack) or fdp (deploy/stack/production); CLIENT_ID defaults to
# fdp-server.
set -euo pipefail

log() { printf '[fix-keycloak-admin-scope] %s\n' "$*"; }
die() { printf '[fix-keycloak-admin-scope] ERROR: %s\n' "$*" >&2; exit 1; }

[ -f compose.yaml ] || die "run this from the stack directory that contains compose.yaml"
if [ -z "${REALM:-}" ]; then
  if grep -q 'realm-fdp-prod.json' compose.yaml; then REALM=fdp; else REALM=fdp-dev; fi
fi
CLIENT_ID=${CLIENT_ID:-fdp-server}

docker compose exec -T -e REALM="$REALM" -e CLIENT_ID="$CLIENT_ID" keycloak bash -s <<'IN_CONTAINER'
set -euo pipefail
kc=/opt/keycloak/bin/kcadm.sh
base="http://localhost:8080${KC_HTTP_RELATIVE_PATH:-}"
user=${KC_BOOTSTRAP_ADMIN_USERNAME:-${KEYCLOAK_ADMIN:-admin}}
pass=${KC_BOOTSTRAP_ADMIN_PASSWORD:-${KEYCLOAK_ADMIN_PASSWORD:-}}
cfg=$(mktemp)
trap 'rm -f "$cfg"' EXIT
$kc config credentials --config "$cfg" --server "$base" --realm master --user "$user" --password "$pass" >/dev/null
cid=$($kc get clients --config "$cfg" -r "$REALM" -q clientId="$CLIENT_ID" --fields id --format csv --noquotes)
[ -n "$cid" ] || { echo "client $CLIENT_ID not found in realm $REALM" >&2; exit 1; }
sid=$($kc get client-scopes --config "$cfg" -r "$REALM" --fields id,name --format csv --noquotes \
  | sed -n 's/^\([^,]*\),kc-admin-api$/\1/p')
if [ -z "$sid" ]; then
  echo "realm $REALM has no kc-admin-api scope (Keycloak before 26.8.2): nothing to do"
  exit 0
fi
if $kc get "clients/$cid/default-client-scopes" --config "$cfg" -r "$REALM" --fields name --format csv --noquotes \
  | grep -qx 'kc-admin-api'; then
  echo "$CLIENT_ID in realm $REALM already has kc-admin-api"
else
  $kc update "clients/$cid/default-client-scopes/$sid" --config "$cfg" -r "$REALM"
  echo "added kc-admin-api to the default scopes of $CLIENT_ID in realm $REALM"
fi
IN_CONTAINER
log "done: new tokens for $CLIENT_ID in realm $REALM reach the Admin REST API."
