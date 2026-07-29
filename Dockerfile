# syntax=docker/dockerfile:1
#
# FDP server image — multi-stage uv build into a self-contained venv, copied
# into a slim, non-root runtime. The entrypoint runs DB migrations then serves
# uvicorn; the default profile auto-applies on first boot (FDP_PROFILE_AUTO_APPLY).

# --- builder: resolve + install deps and the project into /app/.venv ---------
FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependency layer first so it caches unless pyproject/uv.lock change. README.md
# is referenced by pyproject (project.readme) and is needed to build the project.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Project sources (migrations + alembic.ini ship inside the package) and the
# default deployment profile the runtime auto-applies.
COPY src ./src
COPY profiles ./profiles
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Bundle the DB-IP IP-to-City Lite database (CC BY 4.0 — redistributable, unlike
# MaxMind GeoLite2). Binary-compatible with the MaxMind DB format, so the geo
# lookup reads it unchanged. Bump DBIP_CITY_VERSION monthly (CI passes --build-arg);
# the layer re-downloads only when the arg changes. Attribution lives in NOTICE.
# `gunzip` is in the debian-slim base; this all happens in the discarded builder.
ARG DBIP_CITY_VERSION=2026-06
ADD https://download.db-ip.com/free/dbip-city-lite-${DBIP_CITY_VERSION}.mmdb.gz /tmp/geo.mmdb.gz
RUN mkdir -p /app/geo \
    && gunzip -c /tmp/geo.mmdb.gz > /app/geo/GeoLite2-City.mmdb \
    && rm /tmp/geo.mmdb.gz

# --- runtime: slim image with just the venv + app, run as non-root -----------
FROM python:3.12-slim AS runtime

RUN groupadd --system fdp \
    && useradd --system --gid fdp --home-dir /app --no-create-home fdp

WORKDIR /app
COPY --from=builder --chown=fdp:fdp /app /app
COPY --chown=fdp:fdp deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    FDP_PROFILE_AUTO_APPLY=true \
    FDP_PROFILE_PATH=/app/profiles/default \
    FDP_METRICS_GEOIP_DATABASE_PATH=/app/geo/GeoLite2-City.mmdb

USER fdp
EXPOSE 8000

# Liveness via the relocated health probe (no curl in slim — use stdlib urllib).
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=12 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/fdp-api/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
