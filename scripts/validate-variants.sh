#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Validate BOTH routes — the OSS/local variant and the cloud variant.
#
# MinInfer is one codebase serving two profiles:
#
#   config/profiles/local.env   deploy/local/     open, SQLite, one process
#   config/profiles/cloud.env   deploy/cloud/     fail closed, Postgres+Redis
#
# This is the single command that answers "can we test and validate both
# routes?". It layers three kinds of evidence, cheapest first:
#
#   1. pytest   — the two routes booted in-process from the real profile files
#                 (tests/test_variants.py), plus the pinned invariants.
#   2. live     — both profiles on a real socket (scripts/validate-phase0.sh).
#   3. container— the shared image builds and both process types run, and the
#                 cloud route still fails closed inside the image. Skipped with
#                 a clear notice if the Docker daemon is unavailable.
#
# Usage:  ./scripts/validate-variants.sh
# Exit:   0 = every layer passed (or was skipped), 1 = a failure.
# ---------------------------------------------------------------------------
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PASS=0
FAIL=0
WEB_IMAGE=mininfer:validate-web
WORKER_IMAGE=mininfer:validate-worker
WEB_CONTAINER=mininfer_validate_web
CLOUD_CONTAINER=mininfer_validate_cloud
WEB_PORT=8766
CLOUD_PORT=8767
WEB_VOLUME=mininfer_validate_web_data
CLOUD_VOLUME=mininfer_validate_cloud_data

red()   { printf '\033[31m%s\033[0m' "$1"; }
green() { printf '\033[32m%s\033[0m' "$1"; }

cleanup() {
  docker rm -f "$WEB_CONTAINER" "$CLOUD_CONTAINER" >/dev/null 2>&1
  docker volume rm "$WEB_VOLUME" "$CLOUD_VOLUME" >/dev/null 2>&1
}
trap cleanup EXIT

ok()   { printf '  %s %s\n' "$(green PASS)" "$1"; PASS=$((PASS+1)); }
bad()  { printf '  %s %s\n' "$(red FAIL)" "$1"; FAIL=$((FAIL+1)); }

code() { curl -s -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || echo 000; }

expect() { # expect <label> <want> [curl args...]
  local label="$1" want="$2"; shift 2
  local got; got=$(code "$@")
  [ "$got" = "$want" ] && ok "$(printf '%-46s %s' "$label" "$got")" \
                       || bad "$(printf '%-46s got %s, want %s' "$label" "$got" "$want")"
}

wait_up() { # wait_up <port>
  for _ in $(seq 1 40); do
    [ "$(code "http://127.0.0.1:$1/healthz")" = "200" ] && return 0
    sleep 0.25
  done
  return 1
}

# ---------------------------------------------------------------------------
printf '\n== 1. pytest — both routes booted from config/profiles/*.env ==\n'
if python3 -m pytest tests/test_variants.py tests/test_profiles.py \
     tests/test_auth.py tests/test_deploy_manifests.py -q \
     >/tmp/validate-variants-pytest.log 2>&1; then
  ok "$(tail -1 /tmp/validate-variants-pytest.log)"
else
  bad "see /tmp/validate-variants-pytest.log"
  tail -25 /tmp/validate-variants-pytest.log
fi

# ---------------------------------------------------------------------------
printf '\n== 2. live — both profiles on a real socket ==\n'
if bash scripts/validate-phase0.sh >/tmp/validate-variants-live.log 2>&1; then
  ok "$(grep -E 'passed' /tmp/validate-variants-live.log | tail -1)"
else
  bad "see /tmp/validate-variants-live.log"
  tail -25 /tmp/validate-variants-live.log
fi

# ---------------------------------------------------------------------------
printf '\n== 3. container — one image, two process types ==\n'
if ! docker info >/dev/null 2>&1; then
  printf '  %s Docker daemon unavailable — container layer skipped\n' "$(red SKIP)"
  printf '\n== %s ==\n' "$( [ "$FAIL" -eq 0 ] && green 'ROUTES VALIDATED' || red 'ROUTES FAILED' )"
  printf '   %d passed, %d failed\n' "$PASS" "$FAIL"
  exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
fi

if docker build --target runner -t "$WEB_IMAGE" . >/tmp/validate-variants-build-web.log 2>&1; then
  ok "image target 'runner' (web) builds"
else
  bad "runner build failed — see /tmp/validate-variants-build-web.log"
  tail -15 /tmp/validate-variants-build-web.log
fi
if docker build --target worker -t "$WORKER_IMAGE" . >/tmp/validate-variants-build-worker.log 2>&1; then
  ok "image target 'worker' builds"
else
  bad "worker build failed — see /tmp/validate-variants-build-worker.log"
  tail -15 /tmp/validate-variants-build-worker.log
fi

printf '  -- worker process type: a passed command runs batch instead of the server\n'
if docker run --rm -v "$WEB_VOLUME":/data "$WORKER_IMAGE" mi --help \
     >/tmp/validate-variants-worker.log 2>&1; then
  ok "worker dispatches 'mi --help'"
else
  bad "worker dispatch failed — see /tmp/validate-variants-worker.log"
  tail -10 /tmp/validate-variants-worker.log
fi

printf '  -- web process type: LOCAL route (open, as config/profiles/local.env)\n'
cleanup
docker run -d --name "$WEB_CONTAINER" -p "$WEB_PORT":8000 \
  -v "$WEB_VOLUME":/data "$WEB_IMAGE" >/dev/null 2>&1
if wait_up "$WEB_PORT"; then
  B="http://127.0.0.1:$WEB_PORT"
  expect "healthz"                        200 "$B/healthz"
  expect "/v1/models (no credential)"     200 "$B/v1/models"
  expect "dashboard / (no credential)"    200 "$B/"
else
  bad "local web container never came up"
fi

printf '  -- web process type: CLOUD route (fail closed, profile values)\n'
docker rm -f "$CLOUD_CONTAINER" >/dev/null 2>&1
docker run -d --name "$CLOUD_CONTAINER" -p "$CLOUD_PORT":8000 \
  -v "$CLOUD_VOLUME":/data \
  -e MI_DB=/data/mininfer.db \
  -e MI_API_KEYS='sk-validation-only-do-not-deploy:validation' \
  -e MI_ADMIN_TOKEN='validation-admin-do-not-deploy' \
  -e MI_RATE_LIMIT_RPM=120 -e MI_MAX_BODY_BYTES=1048576 \
  "$WEB_IMAGE" >/dev/null 2>&1
if wait_up "$CLOUD_PORT"; then
  C="http://127.0.0.1:$CLOUD_PORT"
  K=(-H "Authorization: Bearer sk-validation-only-do-not-deploy")
  A=(-H "Authorization: Bearer validation-admin-do-not-deploy")
  expect "healthz (public)"               200 "$C/healthz"
  expect "/v1/models (no credential)"     401 "$C/v1/models"
  expect "/v1/models (tenant key)"        200 "$C/v1/models" "${K[@]}"
  expect "/v1/stats (tenant key)"         401 "$C/v1/stats" "${K[@]}"
  expect "/v1/stats (admin token)"        200 "$C/v1/stats" "${A[@]}"
  expect "unknown path (no credential)"   401 "$C/some/unknown/path"
else
  bad "cloud web container never came up"
fi

# ---------------------------------------------------------------------------
printf '\n== %s ==\n' "$( [ "$FAIL" -eq 0 ] && green 'ROUTES VALIDATED' || red 'ROUTES FAILED' )"
printf '   %d passed, %d failed\n' "$PASS" "$FAIL"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
