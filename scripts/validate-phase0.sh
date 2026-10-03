#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Validate Phase 0 access control, against the two deployment profiles.
#
# Hermetic by design:
#   * it starts its own instances on throwaway ports and kills them on exit, so
#     the proxy you are actually running is never touched;
#   * it overrides MI_DB to a temp file, so it never writes to your registry;
#   * it never calls an upstream provider — every assertion is answered by the
#     auth layer or by /v1/models, which reads the registry, not the network;
#   * it asserts the LOCAL profile too, because "local development still works
#     exactly as before" is the property Phase 0 could most easily have broken.
#
# The profiles are the single declaration of each variant
# (config/profiles/{local,cloud}.env); this script sources them rather than
# restating their values, so the two cannot drift.
#
# Usage:  ./scripts/validate-phase0.sh
# Exit:   0 = all checks passed, 1 = at least one failed.
# ---------------------------------------------------------------------------
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PASS=0
FAIL=0
SERVER_PIDS=()

red()   { printf '\033[31m%s\033[0m' "$1"; }
green() { printf '\033[32m%s\033[0m' "$1"; }

cleanup() {
  for pid in "${SERVER_PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
}
trap cleanup EXIT

if command -v mi >/dev/null 2>&1; then
  LAUNCH=(mi proxy)
else
  LAUNCH=(python3 -m mininfer proxy)   # must run from the repo root
fi

profile_val() { # profile_val <profile> <key>
  python3 - "$1" "$2" <<'PY'
import sys, pathlib
path, key = sys.argv[1], sys.argv[2]
for raw in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    if k.strip() == key:
        print(v.strip().strip('"').strip("'")); break
PY
}

free_port() {
  for p in "$@"; do
    lsof -nP -iTCP:"$p" -sTCP:LISTEN -t >/dev/null 2>&1 || { echo "$p"; return; }
  done
  echo ""
}

wait_up() {
  for _ in $(seq 1 40); do
    curl -sS -g -m 2 -o /dev/null "http://127.0.0.1:$1/healthz" 2>/dev/null && return 0
    sleep 0.25
  done
  return 1
}

start_profile() { # start_profile <port> <label> <profile>
  local port="$1" label="$2" profile="$3"
  (
    set -a
    # shellcheck disable=SC1090
    . "$profile"
    set +a
    # The profiles describe *semantics*; these two paths are a deployment
    # concern. `cloud.env` names the container's `/app` and `/data`, which do not
    # exist on a laptop, so the harness points them at this checkout. Validation
    # checks the access-control behaviour, never the mount layout.
    export MI_DB="/tmp/validate-phase0-$port.db"
    [ -f "${MI_POLICY:-}" ] || export MI_POLICY="$PWD/config/policy.yaml"
    exec "${LAUNCH[@]}" --port "$port"
  ) >"/tmp/validate-phase0-$port.log" 2>&1 &
  SERVER_PIDS+=("$!")
  if wait_up "$port"; then
    printf '\n%s  —  %s  (: %s)\n' "$label" "$profile" "$port"
    return 0
  fi
  red "FAIL"; printf ' server on :%s never came up\n' "$port"
  sed -n '1,20p' "/tmp/validate-phase0-$port.log"
  FAIL=$((FAIL+1))
  return 1
}

start_env() { # start_env <port> <label> [ENV=VAL ...]  (inline, for mechanics)
  local port="$1" label="$2"; shift 2
  (
    set -a
    for kv in "$@"; do export "$kv"; done
    set +a
    export MI_DB="/tmp/validate-phase0-$port.db"
    [ -f "${MI_POLICY:-}" ] || export MI_POLICY="$PWD/config/policy.yaml"
    exec "${LAUNCH[@]}" --port "$port"
  ) >"/tmp/validate-phase0-$port.log" 2>&1 &
  SERVER_PIDS+=("$!")
  if wait_up "$port"; then
    printf '\n%s  (: %s)\n' "$label" "$port"
    return 0
  fi
  red "FAIL"; printf ' server on :%s never came up\n' "$port"
  FAIL=$((FAIL+1))
  return 1
}

expect() { # expect <label> <want> [curl args...]
  local label="$1" want="$2"; shift 2
  local got
  got=$(curl -sS -g -m 15 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || echo 000)
  if [ "$got" = "$want" ]; then
    printf '  %s %-48s %s\n' "$(green PASS)" "$label" "$got"; PASS=$((PASS+1))
  else
    printf '  %s %-48s got %s, want %s\n' "$(red FAIL)" "$label" "$got" "$want"; FAIL=$((FAIL+1))
  fi
}

# ---------------------------------------------------------------------------
printf '\n== 0. the properties, pinned in code ==\n'
if python3 -m pytest tests/test_auth.py tests/test_profiles.py -q \
      >/tmp/validate-phase0-pytest.log 2>&1; then
  printf '  %s %s\n' "$(green PASS)" "$(tail -1 /tmp/validate-phase0-pytest.log)"
  PASS=$((PASS+1))
else
  printf '  %s see /tmp/validate-phase0-pytest.log\n' "$(red FAIL)"
  tail -20 /tmp/validate-phase0-pytest.log; FAIL=$((FAIL+1))
fi

# ---------------------------------------------------------------------------
printf '\n== 1. LOCAL variant — config/profiles/local.env ==\n'
PORT=$(free_port 8899 8900 8901)
if start_profile "$PORT" "local: nothing configured" config/profiles/local.env; then
  B="http://127.0.0.1:$PORT"
  expect "/healthz open"                     200 "$B/healthz"
  expect "/v1/models open"                   200 "$B/v1/models"
  expect "/v1/stats open (dashboard)"        200 "$B/v1/stats"
  expect "/ open (dashboard)"                200 "$B/"
  expect "/docs open (operator docs)"        200 "$B/docs"
fi

# ---------------------------------------------------------------------------
printf '\n== 2. CLOUD variant — config/profiles/cloud.env ==\n'
PORT=$(free_port 8902 8903 8904)
CLOUD=config/profiles/cloud.env
if start_profile "$PORT" "cloud: auth enabled" "$CLOUD"; then
  B="http://127.0.0.1:$PORT"
  KEY=$(profile_val "$CLOUD" MI_API_KEYS | cut -d: -f1)
  ADMIN=$(profile_val "$CLOUD" MI_ADMIN_TOKEN)
  TENANT=(-H "Authorization: Bearer $KEY")
  ADMINH=(-H "Authorization: Bearer $ADMIN")

  printf '  -- public surface stays public\n'
  expect "GET /healthz (no credential)"          200 "$B/healthz"

  printf '  -- tenant surface\n'
  expect "GET /v1/models (no credential)"        401 "$B/v1/models"
  expect "GET /v1/models (wrong key)"            401 "$B/v1/models" -H "Authorization: Bearer wrong"
  expect "GET /v1/models (X-API-Key header)"     200 "$B/v1/models" -H "X-API-Key: $KEY"
  expect "GET /v1/models (valid key)"            200 "$B/v1/models" "${TENANT[@]}"

  printf '  -- admin surface rejects a tenant key\n'
  expect "GET /v1/stats (tenant key)"            401 "$B/v1/stats" "${TENANT[@]}"
  expect "GET /v1/stats (admin token)"           200 "$B/v1/stats" "${ADMINH[@]}"
  expect "GET /v1/plan (tenant key)"             401 "$B/v1/plan?task=general_chat" "${TENANT[@]}"
  expect "GET / (tenant key)"                    401 "$B/" "${TENANT[@]}"

  printf '  -- operator console works through a browser\n'
  expect "GET / (HTTP Basic, token as password)" 200 "$B/" -u "me:$ADMIN"
  expect "GET /v1/models (admin superset)"       200 "$B/v1/models" "${ADMINH[@]}"

  printf '  -- fail closed: docs and unknown paths are admin\n'
  expect "GET /docs (no credential)"             401 "$B/docs"
  expect "GET /openapi.json (no credential)"     401 "$B/openapi.json"
  expect "GET /some/unknown/path"                401 "$B/some/unknown/path"

  printf '  -- error contract (OpenAI shape)\n'
  code=$(curl -sS -g -m 10 "$B/v1/models" 2>/dev/null |
         python3 -c 'import json,sys; print(json.load(sys.stdin)["error"]["code"])' 2>/dev/null)
  if [ "$code" = "invalid_api_key" ]; then
    printf '  %s %-48s %s\n' "$(green PASS)" "401 body carries error.code" "$code"; PASS=$((PASS+1))
  else
    printf '  %s %-48s got %s\n' "$(red FAIL)" "401 body carries error.code" "${code:-none}"; FAIL=$((FAIL+1))
  fi
fi

# ---------------------------------------------------------------------------
printf '\n== 3. rate limiting — per tenant, with Retry-After ==\n'
PORT=$(free_port 8905 8906 8907)
if start_env "$PORT" "RPM=2" MI_API_KEYS="sk-a:acme,sk-b:beta" MI_RATE_LIMIT_RPM=2; then
  B="http://127.0.0.1:$PORT"
  A=(-H "Authorization: Bearer sk-a")
  BB=(-H "Authorization: Bearer sk-b")
  expect "tenant acme request 1"                 200 "$B/v1/models" "${A[@]}"
  expect "tenant acme request 2"                 200 "$B/v1/models" "${A[@]}"
  expect "tenant acme request 3 (over budget)"   429 "$B/v1/models" "${A[@]}"
  expect "tenant beta has its own window"        200 "$B/v1/models" "${BB[@]}"
  ra=$(curl -sS -g -m 10 -D - -o /dev/null "$B/v1/models" "${A[@]}" 2>/dev/null |
       tr -d '\r' | awk -F': ' 'tolower($1)=="retry-after"{print $2}')
  if [ -n "$ra" ]; then
    printf '  %s %-48s %s\n' "$(green PASS)" "429 carries Retry-After" "$ra"; PASS=$((PASS+1))
  else
    printf '  %s %-48s missing\n' "$(red FAIL)" "429 carries Retry-After"; FAIL=$((FAIL+1))
  fi
fi

# ---------------------------------------------------------------------------
printf '\n== 4. body cap ==\n'
PORT=$(free_port 8908 8909 8910)
if start_env "$PORT" "MI_MAX_BODY_BYTES=200" \
     MI_API_KEYS="sk-tenant-abc:acme" MI_MAX_BODY_BYTES=200; then
  B="http://127.0.0.1:$PORT"
  big=$(python3 -c 'print("x"*500)')
  expect "oversized body rejected"               413 \
    -X POST "$B/v1/chat/completions" \
    -H "Authorization: Bearer sk-tenant-abc" -H 'Content-Type: application/json' \
    -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"$big\"}]}"
fi

# ---------------------------------------------------------------------------
printf '\n== %s ==\n' "$( [ "$FAIL" -eq 0 ] && green 'PHASE 0 VALIDATED' || red 'PHASE 0 FAILED' )"
printf '   %d passed, %d failed\n' "$PASS" "$FAIL"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
