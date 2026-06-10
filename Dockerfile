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

# Project sources + the bits the runtime needs (migrations, alembic, profiles).
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY profiles ./profiles
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

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
    FDP_PROFILE_PATH=/app/profiles/default

USER fdp
EXPOSE 8000

# Liveness via the relocated health probe (no curl in slim — use stdlib urllib).
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=12 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/fdp-api/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
