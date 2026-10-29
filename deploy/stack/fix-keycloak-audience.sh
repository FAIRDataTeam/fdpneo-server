#!/usr/bin/env bash
# Repair the `fdp` audience mapper of an existing Keycloak realm for Keycloak 26.
#
# The bundled realms used to declare the access-token audience with
# `included.client.audience: "fdp"`. That option names a *client*, and the
# realm has no client called `fdp`: Keycloak 25 emitted the audience anyway,
# Keycloak 26 does not. Tokens then lack `aud: fdp` and the FDP server rejects
# every login ("invalid bearer token").
#
# The realm files now use `included.custom.audience`, but `--import-realm`
# never touches a realm that already exists. Run this once after upgrading an
# existing stack to Keycloak 26 (server 0.19):
#
#   cd deploy/stack && ./fix-keycloak-audience.sh                  # evaluation stack
#   cd deploy/stack/production && ../fix-keycloak-audience.sh      # production stack
#
# It is idempotent. REALM defaults to fdp-dev (deploy/stack) or fdp
# (deploy/stack/production); CLIENT_ID defaults to fdp-client.
set -euo pipefail

log() { printf '[fix-keycloak-audience] %s\n' "$*"; }
die() { printf '[fix-keycloak-audience] ERROR: %s\n' "$*" >&2; exit 1; }

[ -f compose.yaml ] || die "run this from the stack directory that contains compose.yaml"
if [ -z "${REALM:-}" ]; then
  if grep -q 'realm-fdp-prod.json' compose.yaml; then REALM=fdp; else REALM=fdp-dev; fi
fi
CLIENT_ID=${CLIENT_ID:-fdp-client}

docker compose exec -T -e REALM="$REALM" -e CLIENT_ID="$CLIENT_ID" keycloak bash -s <<'IN_CONTAINER'
set -euo pipefail
kc=/opt/keycloak/bin/kcadm.sh
base="http://localhost:8080${KC_HTTP_RELATIVE_PATH:-}"
user=${KC_BOOTSTRAP_ADMIN_USERNAME:-${KEYCLOAK_ADMIN:-admin}}
pass=${KC_BOOTSTRAP_ADMIN_PASSWORD:-${KEYCLOAK_ADMIN_PASSWORD:-}}
cfg=$(mktemp)
$kc config credentials --config "$cfg" --server "$base" --realm master --user "$user" --password "$pass" >/dev/null
cid=$($kc get clients --config "$cfg" -r "$REALM" -q clientId="$CLIENT_ID" --fields id --format csv --noquotes)
[ -n "$cid" ] || { echo "client $CLIENT_ID not found in realm $REALM" >&2; exit 1; }
models=$($kc get "clients/$cid/protocol-mappers/models" --config "$cfg" -r "$REALM")
mid=$(printf '%s' "$models" | tr -d '\n' | grep -o '"id" *: *"[^"]*" *, *"name" *: *"fdp-audience"' | sed 's/.*"id" *: *"\([^"]*\)".*/\1/' || true)
if [ -z "$mid" ]; then
  echo "no fdp-audience mapper on $CLIENT_ID; creating it"
  $kc create "clients/$cid/protocol-mappers/models" --config "$cfg" -r "$REALM" \
    -s name=fdp-audience -s protocol=openid-connect -s protocolMapper=oidc-audience-mapper \
    -s 'config."included.custom.audience"=fdp' -s 'config."access.token.claim"=true' \
    -s 'config."id.token.claim"=false' -s 'config."introspection.token.claim"=true'
else
  $kc update "clients/$cid/protocol-mappers/models/$mid" --config "$cfg" -r "$REALM" \
    -s 'config."included.custom.audience"=fdp' -d 'config."included.client.audience"'
  echo "updated mapper fdp-audience ($mid) on $CLIENT_ID in realm $REALM"
fi
rm -f "$cfg"
IN_CONTAINER
log "done: access tokens issued by realm $REALM for $CLIENT_ID now carry aud=fdp."
