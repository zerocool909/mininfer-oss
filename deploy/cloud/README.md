# CLOUD variant

MinInfer served to other people. Access control on, limits on, state on a
volume. The profile is `config/profiles/cloud.env`.

**Decide what you are offering before exposing this to anyone.** The manifests
here cover the infra layer only; the product-shape choice — hosted gateway vs
BYO-key control plane, the second avoiding the provider-terms problem — and the
remaining gaps are yours to settle.

## Secrets

Never commit these. `config/profiles/cloud.env` holds `do-not-deploy`
placeholders so that shipping it unchanged is a visible mistake, and
`tests/test_profiles.py` fails if that stops being true.

```bash
# Fly
fly secrets set MI_ADMIN_TOKEN="$(openssl rand -hex 32)"
fly secrets set MI_API_KEYS_FILE=/run/secrets/keys.json

# Kubernetes
kubectl create secret generic mininfer-secrets \
  --from-literal=admin-token="$(openssl rand -hex 32)" \
  --from-literal=database-url="postgresql://mininfer:…@db.internal:5432/mininfer" \
  --from-literal=redis-url="redis://redis.internal:6379/0" \
  --from-file=keys.json=./keys.json
```

Issue a tenant key, then store only its digest:

```bash
KEY="sk-$(openssl rand -hex 24)"
echo "give this to the tenant, it is not recoverable: $KEY"
python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$KEY"
# -> put the digest into keys.json as key_sha256
```

`keys.example.json` shows the file format. The digest form is why
`MI_API_KEYS_FILE` is preferred over `MI_API_KEYS`: the file never holds a usable
credential.

## Run it

```bash
# Fly — the config must sit where the build context is (the repo root)
cp deploy/cloud/fly.toml fly.toml && fly deploy
#   or: fly deploy --config deploy/cloud/fly.toml   (from the repo root)

# Kubernetes
kubectl apply -f deploy/cloud/k8s.yaml

# Ephemeral filesystem on a single node (SQLite fallback, not the Postgres
# shape): add continuous replication to object storage
litestream restore -if-replica-exists /data/mininfer.db
litestream replicate -exec "uvicorn mininfer.proxy:app --host 0.0.0.0 --port 8000"
```

## One image, two process types

The API and the registry's batch jobs (`mi ingest`, `mi metrics`, `mi bench`)
share one image, because a fix to the router must land in both — this is the same
argument against a second codebase. What differs is
the **command**, not the artifact:

| Process | Command | Scale |
|:--|:--|:--|
| `web` | `uvicorn mininfer.proxy:app` | with request volume |
| `worker` | `mi ingest` / `mi metrics` on a schedule | with catalogue size — **one**, never with requests |
| `verify` | `mi verify` daily | **one**; the drift check, not a request path |

### The daily drift check (`mi verify`)

`mi ingest` refreshes the registry from the raw cache; a cache cannot *verify*
anything, because it is the thing being questioned. `mi verify` re-reads every
catalogue with the cache bypassed, diffs price and free status against what the
registry currently claims, and prints (and optionally writes) the changes:

```bash
mi verify --report /data/verify.json
# verify: 1711 deployments checked · 8 new · 33 changed · 0 free-status flips
#   FREE -> PAID  openrouter:qwen/qwen3.8-27b:free
#       was free  ->  now $0.3000/$1.2000 per Mtok
```

It exists for one failure: a provider starts charging for a model that was free
when it was ingested. Nothing errors, the router keeps choosing it on price, and
every request quietly costs money. A registry that says "free" when it is not is
worse than an empty one.

Scheduled daily — a Fly `verify` process, or the `mininfer-verify` CronJob
(`0 3 * * *`) on Kubernetes. It shares the worker's env, so it needs the same
provider keys.

The `Dockerfile` has a `worker` stage (`FROM runner`), and `entrypoint.sh` runs a
command passed to the container instead of the web server, so both reuse the same
seeding step:

```bash
# the worker does not need HTTP; give it a command and it batch-runs it
docker run --rm mininfer:worker mi ingest --force
```

On Fly this is a `[processes]` block (already in `fly.toml`); on k8s a **CronJob**
with the same image; on ECS a scheduled task. The batch cadence belongs to the
platform — the image ships the runtime, the scheduler owns the timing.

### The optional understanding layer (GLiNER2.5) — Phase 2.0, deferred

The local understanding layer is **not** part of the Phase 1.0 image. It adds a
PyTorch dependency and a ~2 GB checkpoint, and nothing on the request path needs
it, so Phase 1.0 ships without it. The seam is already
in the tree and inert: `MI_INTENT_BACKEND` / `MI_EXTRACT_BACKEND` do nothing
unless the `mininfer[understanding]` extra is installed, and the proxy and
ingest graphs fall back to their existing behaviour when it is not.

When Phase 2.0 lands it will re-add the opt-in worker build and document the
model here. Do not install the extra into the Phase 1.0 image.


## What this variant changes

| | effect |
|---|---|
| `MI_API_KEYS` / `MI_API_KEYS_FILE` | every `/v1/*` route needs a tenant key |
| `MI_ADMIN_TOKEN` | `/`, `/legacy`, `/v1/stats`, `/v1/plan`, `/v1/providers`, `/v1/local/*`, `/docs` need it |
| `MI_RATE_LIMIT_RPM` | per-tenant requests/minute, `Retry-After` on 429 |
| `MI_MAX_BODY_BYTES` | oversized bodies rejected before the router |
| `HOST=0.0.0.0` | required behind a load balancer |

`/healthz` and `/assets/*` stay public — a load balancer has no credential, and
a login page must be able to fetch its own bundle. Everything else fails closed:
an unrecognised path is treated as admin, so a new endpoint is guarded by
default.

## Validate before exposing

```bash
./scripts/validate-phase0.sh
```

27 checks across both profiles, hermetic (its own ports, its own temp databases,
no upstream calls, kills what it starts). Then the hand-checks the script cannot
make:

- `curl -s https://<host>/openapi.json` → **401**, not the schema
- `curl -s https://<host>/v1/chat/completions -d '{}'` → **401**, not 422 —
  auth runs before validation, so an unauthenticated caller learns nothing
- the dashboard prompts for credentials, and its playground still completes a
  call afterwards (an admin token satisfies tenant endpoints)

## Known limitations

- **The registry is Postgres and the limits are Redis.** `k8s.yaml` ships
  `replicas: 3` with `MI_DB` and `MI_REDIS_URL` from `mininfer-secrets`. On Fly
  those two are secrets as well (`fly secrets set MI_DB=… MI_REDIS_URL=…`); the
  `[env]` `MI_DB=/data/mininfer.db` is the single-replica fallback.
- **`MI_REDIS_URL` also backs the benchmark-norms cache.** Without it every
  replica rescans the benchmark table on its own 120 s schedule; with it they
  share one cached band (`mininfer/cache.py`).
- **`/v1/local/register` writes to the registry** and `/v1/local/probe` fetches a
  caller-supplied URL. Both are admin-only; keep them that way.
