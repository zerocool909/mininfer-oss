# =============================================================================
# Stage 1: Build Web SPA Frontend
# =============================================================================
FROM node:20-slim AS frontend-builder
WORKDIR /app/web

# Install build dependencies
COPY web/package.json web/package-lock.json* ./
RUN npm ci || npm install

# Build static bundle to web/dist
COPY web/ ./
RUN npm run build

# =============================================================================
# Stage 2: Production Python Runtime
# =============================================================================
FROM python:3.11-slim AS runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    MI_DB=/data/mininfer.db \
    MI_POLICY=/app/config/policy.yaml

WORKDIR /app

# Install system dependencies (curl for health check, ca-certificates for outbound provider TLS)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the pinned lock, then the package itself with
# --no-deps, so the lock — not pip's resolver — decides the versions. The image
# is a deployment artifact; it should not change because an upstream minor
# release landed between two builds.
COPY pyproject.toml requirements.lock ./
COPY mininfer/ mininfer/
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps .

# Copy application configuration and seed catalog
COPY config/ /app/config/
# The Postgres schema: `mi sync` mirrors through it, and Store uses it as
# the DDL when MI_DB is a postgresql:// URL.
COPY supabase/ /app/supabase/
COPY mininfer.db /app/seed.db

# Copy compiled frontend from Stage 1
COPY --from=frontend-builder /app/web/dist /app/web/dist

# Copy entrypoint script
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create persistent storage mount and run as non-root user
RUN mkdir -p /data && \
    addgroup --system --gid 1001 mininfer && \
    adduser --system --uid 1001 --gid 1001 mininfer && \
    chown -R mininfer:mininfer /app /data

USER mininfer

# Expose HTTP proxy port
EXPOSE 8000

# Container liveness & readiness health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:${PORT:-8000}/healthz || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]

# =============================================================================
# Stage 3: Worker — the same runtime, driven by the batch entrypoint.
#
# One image, two process types (PRODUCTIZATION.md §5 layer ⑤): `web` runs
# `uvicorn mininfer.proxy:app`, the `worker` runs `mi ingest` / `mi metrics` /
# `mi bench` on a schedule. They share the image on purpose — a fix to the router
# must land in both, which is the argument PRODUCTIZATION.md §3a makes against a
# second codebase.
#
# The local understanding layer (GLiNER2.5) is deliberately NOT built here. It
# is Phase 2.0 (CLOUD_ACTIVITY.md §2.0): it pulls in PyTorch, which turns the
# ~60 MB web image into a multi-GB one, and nothing on the Phase 1.0 request
# path needs it. The optional `[understanding]` extra stays declared in
# pyproject.toml so the seam can be opted into without a code change.
#
# No ENTRYPOINT override: the batch cadence belongs to the platform (Fly
# `[processes]`, a k8s CronJob, an ECS scheduled task), and `entrypoint.sh` now
# runs a passed command instead of the web server, so `worker = "mi ingest"`
# works with the same image.
# =============================================================================
FROM runner AS worker
ENV MI_ROLE=worker
