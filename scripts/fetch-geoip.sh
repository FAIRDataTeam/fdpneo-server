#!/usr/bin/env bash
#
# Fetch the DB-IP IP-to-City Lite database in MaxMind MMDB format.
#
# DB-IP publishes a fresh "lite" database every month under CC BY 4.0, which
# (unlike MaxMind's GeoLite2 EULA) permits redistribution. It is binary-
# compatible with the MaxMind DB format, so `geoip2.database.Reader` — and thus
# `fdpneo_server.metrics.geo.MaxMindGeoLookup` — reads it unchanged.
#
#   Attribution (CC BY 4.0): IP geolocation by DB-IP — https://db-ip.com
#   See the project NOTICE file. This obligation travels with the data.
#
# Source of truth for both local dev and the Docker build. Monthly URL:
#   https://download.db-ip.com/free/dbip-city-lite-YYYY-MM.mmdb.gz
#
# Usage:
#   scripts/fetch-geoip.sh [OUTPUT_PATH]
#
# OUTPUT_PATH defaults to $FDP_METRICS_GEOIP_DATABASE_PATH, then to
# data/geoip/GeoLite2-City.mmdb (relative to the repo root). Override the month
# with DBIP_CITY_VERSION=YYYY-MM (defaults to the current month, falling back to
# the previous month when the current one isn't published yet).
set -euo pipefail

OUT="${1:-${FDP_METRICS_GEOIP_DATABASE_PATH:-data/geoip/GeoLite2-City.mmdb}}"
VERSION="${DBIP_CITY_VERSION:-$(date +%Y-%m)}"
BASE="https://download.db-ip.com/free"

mkdir -p "$(dirname "$OUT")"

fetch() {
  local ver="$1"
  local url="${BASE}/dbip-city-lite-${ver}.mmdb.gz"
  echo "→ fetching ${url}"
  curl -fsSL "$url" -o "${OUT}.gz"
}

# The current month's file may not exist on the 1st of the month yet; fall back
# to the previous month. `date -v` is BSD/macOS, `date -d` is GNU/Linux.
if ! fetch "$VERSION"; then
  prev="$(date -v-1m +%Y-%m 2>/dev/null || date -d 'last month' +%Y-%m)"
  echo "  ${VERSION} unavailable; retrying ${prev}"
  fetch "$prev"
fi

gunzip -f "${OUT}.gz"
echo "✓ wrote ${OUT}"
echo "  Attribution required (CC BY 4.0): IP geolocation by DB-IP — https://db-ip.com"
