#!/bin/sh
set -e

# Determine the registry target: a file path, or a `postgresql://` DSN.
TARGET_DB="${MI_DB:-${MI_DB:-/data/mininfer.db}}"

case "$TARGET_DB" in
  postgres://*|postgresql://*)
    # A DSN is the shared registry, not a file. Seeding by copying is meaningless
    # here, and treating the DSN as a path made `mkdir -p $(dirname …)` create a
    # literal `postgresql:/user:pw@host:5432` directory tree inside the image —
    # the entrypoint reported "seed copied successfully" for a file the database
    # never read. Create the schema now instead: idempotent, and it fails the
    # container at boot rather than on the first request if the database is
    # unreachable or the DDL is broken. Populate the catalog with `mi ingest`.
    echo "==> Registry is Postgres; initialising the schema (idempotent)…"
    MI_DB="$TARGET_DB" python -c 'import os; from mininfer.store import Store; Store(os.environ["MI_DB"]).close()'
    ;;
  *)
    DATA_DIR=$(dirname "$TARGET_DB")
    mkdir -p "$DATA_DIR"

    # Seed initial deployment catalog if no database exists yet on the volume
    if [ ! -f "$TARGET_DB" ] || [ ! -s "$TARGET_DB" ]; then
        echo "==> No database found at $TARGET_DB. Seeding catalog..."
        if [ -f "/app/seed.db" ]; then
            cp /app/seed.db "$TARGET_DB"
            echo "==> Seed database copied successfully ($(stat -c%s "$TARGET_DB" 2>/dev/null || wc -c < "$TARGET_DB") bytes)."
        else
            echo "==> Notice: No seed database found; an empty registry will be initialized."
        fi
    fi
    ;;
esac

BIND_HOST="${HOST:-0.0.0.0}"
BIND_PORT="${PORT:-8000}"

# A command passed to the container (`docker run … mi ingest`, a Fly `[processes]`
# entry, a k8s CronJob) makes this a *worker*: the registry is already prepared
# above, so run that command instead of the web server. No argument still means
# the web server, so every existing invocation is unchanged.
if [ "$#" -gt 0 ]; then
    echo "==> Executing: $*"
    exec "$@"
fi

# --- first-run catalogue ---------------------------------------------------
# The image ships NO registry: it is derived from provider APIs, so a fresh
# volume starts empty and every dashboard panel reads zero. Populate it once here
# so `docker compose up` shows a working dashboard instead of a blank one.
#
# Skippable with MI_BOOTSTRAP=0 — for a deployment whose catalogue arrives by
# another route (a restored volume, a scheduled job, a seeded Postgres), the
# ingest is pointless work on every boot.
if [ "${MI_BOOTSTRAP:-1}" != "0" ]; then
    count=$(MI_DB="$TARGET_DB" python -c \
        "import os; from mininfer.store import Store; s=Store(os.environ['MI_DB']); print(len(s.deployments())); s.close()" \
        2>/dev/null || echo unknown)
    if [ "$count" = "0" ]; then
        echo "==> Registry is empty — ingesting provider catalogues (one time)…"
        if mi ingest; then
            echo "==> Catalogue populated."
            mi quota seed >/dev/null 2>&1 || true
        else
            echo "==> Warning: ingest failed (no network, or a missing CA bundle)."
            echo "    The server will start empty; run 'mi refresh' when ready."
        fi
    fi
fi

echo "==> Launching MinInfer Proxy on http://${BIND_HOST}:${BIND_PORT}"
echo "    Database: $TARGET_DB"
echo "    Policy:   ${MI_POLICY:-config/policy.yaml}"

exec uvicorn mininfer.proxy:app --host "$BIND_HOST" --port "$BIND_PORT" --log-level "${LOG_LEVEL:-info}"
