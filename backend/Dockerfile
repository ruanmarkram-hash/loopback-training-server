# Build context is the REPO ROOT (see docker-compose.yml) so this image can
# bake in the React dashboard from ../frontend alongside the backend.

# --platform=$BUILDPLATFORM: the frontend output is static files, so this stage
# always runs on the build host's native arch (no QEMU emulation of npm when
# cross-building the multi-arch image in CI).
FROM --platform=$BUILDPLATFORM node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential libpq-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /uvx /bin/

WORKDIR /app

ENV VIRTUAL_ENV=/opt/venv
RUN uv venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY backend/pyproject.toml backend/uv.lock ./
# Exactly what uv.lock pins, installed into /opt/venv. Resolving the pyproject
# ranges instead is how an upstream release (fastmcp 4.0) broke CI with no
# change here. --locked fails the build if the lock has drifted from
# pyproject. The app isn't installed as a package (and isn't copied yet),
# hence --no-install-project.
ENV UV_PROJECT_ENVIRONMENT=$VIRTUAL_ENV
RUN uv sync --locked --no-install-project --extra test

COPY backend/ .
# Docker COPY can reuse directory modes from a cached source layer. Keep the
# application source searchable by the nonroot runtime user.
RUN find /app -type d -exec chmod 0755 {} +

FROM postgres:16@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d AS pgtools

FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c

# Install only pg_dump runtime libraries, then copy the pinned server-matching pg_dump16.
# A newer client may emit restore settings unsupported by the Postgres16 server.
# Pin the verified Debian DSA-6549-1 liblzma security fix in the runtime.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 liblz4-1 libzstd1 liblzma5=5.8.1-1+deb13u2 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=pgtools /usr/lib/postgresql/16/bin/pg_dump /usr/bin/pg_dump
ARG SOURCE_REVISION=unknown
ARG SOURCE_TREE_HASH=unknown
ENV SOURCE_REVISION=$SOURCE_REVISION SOURCE_TREE_HASH=$SOURCE_TREE_HASH

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
COPY --from=frontend /fe/dist /app/static
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8001

CMD ["bash", "scripts/start.sh"]
