#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Validate the Postgres backend by running the WHOLE test suite against it.
#
# That is the point of the dialect seam in `mininfer/db.py`: the same ~47
# statements run on both engines, so the same tests can prove it. A store that
# works on SQLite and silently misbehaves on Postgres is the failure this
# guards against, and it is not detectable by reading the SQL.
#
# Hermetic: initdb's a throwaway cluster in a temp directory on a free port,
# never touches an existing server, and removes the cluster on exit. Skips
# cleanly (exit 0) when no Postgres binaries are installed.
#
# Usage:  ./scripts/validate-postgres.sh
# ---------------------------------------------------------------------------
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

for b in initdb pg_ctl psql createdb; do
  if ! command -v "$b" >/dev/null 2>&1; then
    # Homebrew keeps them out of PATH on some installs.
    for d in /opt/homebrew/opt/postgresql@*/bin /usr/local/opt/postgresql@*/bin; do
      [ -x "$d/$b" ] && PATH="$d:$PATH"
    done
  fi
done

for b in initdb pg_ctl psql; do
  command -v "$b" >/dev/null 2>&1 || {
    echo "SKIP: $b not found — install Postgres to validate this backend."
    exit 0
  }
done

# A stale Kerberos realm makes libpq attempt GSSAPI and fail before connecting.
export PGGSSENCMODE=disable

PGDIR=$(mktemp -d "${TMPDIR:-/tmp}/fb-pgvalidate.XXXXXX")
PORT=""
cleanup() {
  [ -n "$PORT" ] && pg_ctl -D "$PGDIR/data" stop -m immediate >/dev/null 2>&1
  rm -rf "$PGDIR"
}
trap cleanup EXIT

# Pick a port nothing is listening on. 55432/55433 are commonly taken by a
# developer's own cluster, so start higher and probe.
for p in 55440 55441 55442 55443 55444; do
  lsof -nP -iTCP:"$p" -sTCP:LISTEN -t >/dev/null 2>&1 || { PORT="$p"; break; }
done
[ -n "$PORT" ] || { echo "SKIP: no free port for the throwaway cluster."; exit 0; }

echo "== provisioning a throwaway Postgres on :$PORT =="
initdb -D "$PGDIR/data" -U mininfer --auth=trust >"$PGDIR/initdb.log" 2>&1 || {
  echo "FAIL: initdb"; tail -5 "$PGDIR/initdb.log"; exit 1; }
pg_ctl -D "$PGDIR/data" \
  -o "-p $PORT -k $PGDIR -c listen_addresses=127.0.0.1" \
  -l "$PGDIR/pg.log" start >/dev/null 2>&1
for _ in $(seq 1 40); do
  psql -h 127.0.0.1 -p "$PORT" -U mininfer -d postgres -tAc "select 1" >/dev/null 2>&1 && break
  sleep 0.25
done
psql -h 127.0.0.1 -p "$PORT" -U mininfer -d postgres -tAc "select 1" >/dev/null 2>&1 || {
  echo "FAIL: cluster did not come up"; tail -10 "$PGDIR/pg.log"; exit 1; }
psql -h 127.0.0.1 -p "$PORT" -U mininfer -d postgres -q -c "create database mininfer;" \
  >/dev/null 2>&1 || { echo "FAIL: createdb"; exit 1; }

echo
echo "== Store -> SQLite (the default; must be untouched) =="
python3 -m pytest -q 2>&1 | tail -2

echo
echo "== Store -> Postgres (the same suite, one schema per test) =="
PGLOG="$PGDIR/pytest.log"
# Not `| tail -2`: that hid 120 Postgres failures behind a passing line for an
# unknown number of runs. On failure the FAILED lines are printed.
if MI_TEST_PG_DSN="postgresql://mininfer@127.0.0.1:$PORT/mininfer" \
     python3 -m pytest -q >"$PGLOG" 2>&1; then
  status=0
  tail -1 "$PGLOG"
else
  status=1
  tail -1 "$PGLOG"
  echo
  echo "  failures (full log: $PGLOG):"
  grep -E '^FAILED|^ERROR' "$PGLOG" | head -40
fi

echo
if [ "$status" -eq 0 ]; then
  echo "POSTGRES BACKEND VALIDATED"
else
  echo "POSTGRES BACKEND FAILED (exit $status)"
fi
exit "$status"
