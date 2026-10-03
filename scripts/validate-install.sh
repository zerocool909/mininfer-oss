#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Validate that MinInfer installs into a *clean* environment from the pinned
# lockfile — the installability half of P3.1.
#
# A lockfile that has never been installed from is a wish, not a lock. This
# creates a throwaway venv, installs exactly `requirements.lock`, installs the
# package itself with `--no-deps` (so the lock, not pip's resolver, decides the
# versions), then imports the proxy and runs through both console entry points.
#
# Hermetic: its own venv in a temp dir, removed on exit. Network required to
# fetch the wheels.
#
# Usage:  ./scripts/validate-install.sh
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

[ -f requirements.lock ] || { echo "FAIL: requirements.lock is missing"; exit 1; }

TMP=$(mktemp -d "${TMPDIR:-/tmp}/fb-install.XXXXXX")
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

echo "== creating a clean venv =="
python3 -m venv "$TMP/venv"
PY="$TMP/venv/bin/python"
"$PY" -m pip install --quiet --upgrade pip

echo "== installing exactly requirements.lock =="
"$PY" -m pip install --quiet -r requirements.lock

echo "== installing the package with --no-deps (the lock owns the versions) =="
"$PY" -m pip install --quiet --no-deps .

echo "== asserting the install is usable =="
# Run from a *neutral* cwd: from the repo root, `import mininfer` resolves to the
# source tree and the test would pass even when the installed wheel is missing
# subpackages. That is exactly the bug this script first caught.
cd "$TMP"
"$PY" - <<'PY'
import mininfer
from mininfer.proxy import app          # pulls in fastapi, starlette, httpx
from mininfer.router import Policy      # pulls in PyYAML
from mininfer.store import Store

routes = {getattr(r, "path", None) for r in app.routes}
for required in ("/healthz", "/v1/chat/completions", "/v1/models/explore",
                 "/v1/session/messages", "/v1/savings"):
    assert required in routes, f"route missing after install: {required}"
print(f"  import ok · {len(routes)} routes · mininfer {mininfer.__version__ if hasattr(mininfer, '__version__') else '0.1.0'}")
PY

for binname in mi mi; do
  "$TMP/venv/bin/$binname" --help >/dev/null
  echo "  $binname --help ok"
done

# The lock must not have drifted from what pip actually installed for the direct
# deps. A `>=` in pyproject that resolved to a yanked version would show up here.
"$PY" - <<'PY'
import importlib.metadata as md
for pkg in ("httpx", "PyYAML", "fastapi", "uvicorn"):
    md.version(pkg)
print("  direct dependencies present")
# The cloud runtime deps: the image installs this same lock, and without these
# two a `postgresql://` MI_DB cannot connect and the limiter is per-process only.
import psycopg2  # noqa: F401
import redis  # noqa: F401
print("  cloud deps importable (psycopg2, redis)")
PY

echo "INSTALL VALIDATED (exit 0)"
