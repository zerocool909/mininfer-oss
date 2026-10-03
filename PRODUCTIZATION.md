# MinInfer as an API — Productization Plan

`cloud_deploy.md` answers *"run this container for myself."* This answers the
next question: **what does it take to offer MinInfer as an API to other people?**

The two are not the same problem. Self-hosting is one tenant, one set of provider
keys, one operator who trusts themselves. A hosted API needs identity, quotas,
metering, a shared datastore, and a story for the provider terms of service.

---

## 1. Decide the product shape first

This is the only decision that materially changes the infra.

| Shape | Who holds provider keys | Infra weight | Main risk |
|---|---|---|---|
| **A. Hosted gateway** | MinInfer | Full multi-tenant SaaS | **Provider ToS.** `:free` variants and startup credits are end-user subsidies; reselling them is commonly prohibited. You also pay for every fallback. |
| **B. BYO-key control plane** | Tenant | Moderate | Low. MinInfer sells routing, policy and telemetry. |
| **C. Self-host / OEM** | Tenant | Lowest | None — this is `cloud_deploy.md` today. |

**Recommendation: B, with A as a later paid tier.**

B is the shape the code already fits. `quota_buckets` is keyed
`(deploy_id, window, api_key_alias)` and `api_key_alias` defaults to `'default'`
— that column exists so one deployment can be tracked per *upstream key*. Under B
each tenant gets their own alias, their own free-tier headroom, and the router's
whole value proposition (cost-per-success, free-first, diversity, quota headroom)
becomes per-tenant and honest.

---

## 2. What already exists — do not rebuild

- **OpenAI-compatible surface**: `/v1/chat/completions`, `/v1/models`,
  `/v1/models/explore`, `/v1/route`, `/v1/plan`, `/v1/search`, `/v1/approve`,
  `/v1/route-verdict`, `/v1/session`, `/v1/session/messages`, `/v1/savings`,
  `/v1/usage`, `/v1/stats`, `/healthz`
- **Server-enforced session budgets** — token *and* USD caps, checked *before*
  routing so a refused session cannot spend on a fallback chain
  (`proxy.py:_session_block`)
- **Per-call cost accounting** — `observations.cost_usd` → `routing_stats`
- **A complete decision log** — every request recorded, including refusals
- **The Postgres schema already written** — `supabase/schema.sql`, shape-identical,
  with the FK relaxation documented; `tests/test_sync.py` pins the table set
- **Provider-key indirection** — `execute.PROVIDERS` maps provider → `(base_url, key_env)`,
  so tenant key lookup slots into an existing seam
- **Container**: multi-stage (node + python), non-root uid 1001, `/healthz`, `/data` volume

---

## 3. Phase 0 — shipped

The gating item was `BACKLOG.md` **P3.2**: *"it is a local dev tool today; the
moment it is exposed, `/v1/search` spends money and nothing authenticates it."*
**Access control is now opt-in and inert by default.** With nothing configured the
proxy is byte-for-byte the local tool it was: no auth, no rate limiting, open
dashboard, and every existing test unchanged.

| Variable | Effect |
|---|---|
| `MI_API_KEYS` | `key:tenant,key2:tenant2` — quick form, key in the environment |
| `MI_API_KEYS_FILE` | JSON `[{"tenant", "key_sha256", "rpm"?}]` — keys compared as SHA-256 digests, so the file never holds a usable credential |
| `MI_ADMIN_TOKEN` | Unlocks the operational surface |
| `MI_RATE_LIMIT_RPM` | Requests/minute per tenant (`0` = off) |
| `MI_MAX_BODY_BYTES` | Reject oversized bodies (`0` = off) |
| `MI_CORS_ORIGINS` | Cross-origin browser clients (unset = same-origin only) |

**Three surfaces, classified in `auth.classify()`:**

| Surface | Paths | Credential |
|---|---|---|
| public | `/healthz`, `/assets/*`, favicon | none — a load balancer and a login page need them |
| tenant | `/v1/chat/completions`, `/v1/route`, `/v1/search`, `/v1/approve`, `/v1/route-verdict`, `/v1/models`, `/v1/session` | tenant key, rate limited |
| admin | `/`, `/legacy`, `/v1/stats`, `/v1/plan`, `/v1/providers`, `/v1/local/*` | admin token |

Design notes worth keeping:

- **Opt-in, not opt-out.** The default-open path is one early return in a
  middleware, not a `Depends` threaded through 15 handlers. That keeps the
  zero-config local workflow — and its tests — untouched.
- **`Authorization: Bearer` and `Basic` both work.** A browser navigation cannot
  set a header, so the admin surface also accepts Basic (token as password) and
  challenges with `realm="mininfer"`. After one login the SPA's own `fetch`
  calls carry the same credentials.
- **A client-supplied session is a label, not a boundary.** With auth on, the
  session ledger is keyed `tenant/session`, and an unnamed request gets the
  tenant itself — so a per-tenant cap applies even when the caller names nothing.
  Two tenants naming `shared` no longer share a budget.
- **401/429 use the OpenAI error shape** so existing SDKs surface them without
  special-casing.

**Known limitation at the time, since resolved:** the rate limiter was
*in-process* — correct for one replica, wrong for several. Phase 1 (§4a) added
the Redis backend behind `MI_REDIS_URL`; without it set, this is still the
behaviour.

Covered by `tests/test_auth.py` (30 tests), including the property that matters
most: `test_auth_is_off_by_default`.

---

## 3a. Two variants, one codebase

MinInfer ships as **one codebase serving two profiles**, not two forks.

| | LOCAL | CLOUD |
|---|---|---|
| Profile | `config/profiles/local.env` | `config/profiles/cloud.env` |
| Access control | off — dashboard and API open | on — fail closed |
| Bind | `127.0.0.1:8765` | `0.0.0.0:8000` |
| State | `mininfer.db` beside the code | mounted volume (`/data`) |
| Limits | none | `MI_RATE_LIMIT_RPM`, `MI_MAX_BODY_BYTES` |
| Runner | `mi proxy` · `docker compose up` | `deploy/cloud/` manifests (Fly, k8s) |

**Why profiles and not two folders.** A second copy of the code drifts. This
repository already has that scar: a duplicated package was once kept "for
compatibility", and by the time it was removed it was carrying a stale user
agent, out-of-date environment variables, a broken `sys.modules` alias that
produced *duplicate* adapter registries, and a failing test. None of that was
written on purpose — it accumulated because two copies drifted apart. A second
folder for the cloud variant would reintroduce exactly that failure mode.

The variants differ in **configuration and infrastructure**, not in logic. So
they are data, and data can be tested:

- `tests/test_profiles.py` parses both profiles, feeds each through the real
  `auth.AuthConfig`, and asserts the invariant each variant promises — local must
  be open, cloud must be locked down and must hold no usable credential.
- `scripts/validate-phase0.sh` **sources** those same files, so the profiles and
  the validation cannot drift: change `cloud.env` to drop auth and both the test
  and the script fail.
- Both variants run the same `pytest`, so a fix to the router lands in local and
  cloud at once. There is no second code path to remember.

What legitimately differs per variant — manifests, secret injection, replica
count — lives in `deploy/local/` and `deploy/cloud/`, and holds **no Python**.
The shared image stays at the repo root as `Dockerfile`; the root
`docker-compose.yml` is a one-line `include:` of `deploy/local/docker-compose.yml`,
so `docker compose up` and `docker build .` keep working unchanged. See
`deploy/README.md`.

---

## 4. What is still missing — the app layer

| # | Gap | Evidence | Needed |
|---|---|---|---|
| 1 | ~~No authentication~~ | ✅ Phase 0 | — |
| 2 | ~~Tenant identity is client-supplied~~ | ✅ Phase 0 | — |
| 3 | ~~SQLite is single-writer~~ | ✅ Phase 1 — `MI_DB=postgresql://…` | — |
| 4 | ~~Rate limit is per-process~~ | ✅ Phase 1 — `MI_REDIS_URL` | — |
| 5 | ~~Usage tables have no `tenant_id`~~ | ✅ Phase 1b — `observations`, `decisions`, `sessions` | Decision: **shared learning, tenant-attributed usage.** The router still ranks on every tenant's observations — quality and price are properties of the deployment, not the caller — while spend, headroom and transcripts are scoped per tenant. `quota_buckets` stays keyed by `api_key_alias`, a separate axis (which *upstream* key), not conflated with `tenant_id` |
| 6 | ~~Sessions are not a transcript~~ | ✅ `messages` table, bounded and opt-in | Kept the split: `sessions` is spend, `messages` is content. Not mirrored by `mi sync` unless Postgres is primary |
| 7 | **No metering or billing** | ✅ savings view + `/v1/usage`; no Stripe | Usage API exists per tenant; billing/Stripe still to come |
| 8 | **No idempotency keys / request cancellation** | — | Retry safety for paying callers |
| 9 | **No versioning contract** | — | `/v1` pinning + deprecation policy |
| 10 | **`/v1/local/probe` fetches an arbitrary URL** | `proxy.py` | SSRF surface — admin-only today (correct), keep it that way |

---

## 4a. Phase 1 — shipped: the registry runs on Postgres

`MI_DB` now takes either a path or a DSN. A `postgresql://` URL selects Postgres;
anything else is still `mininfer.db` exactly as before. Nothing else in the
codebase had to learn about it, because `Store` is the only module allowed to
write SQL — `tests/test_encapsulation.py` enforces that — so the dialect boundary
is one file wide: `mininfer/db.py`.

**It is verified, not asserted.** The same suite runs on both engines:

```bash
./scripts/validate-postgres.sh      # initdb's a throwaway cluster and runs both
MI_TEST_PG_DSN="postgresql://…" pytest -q   # or against your own server
```

Current: **381 passed** on SQLite, **379 passed / 3 skipped** on Postgres (the
three skip because they exercise SQLite-only behaviour — two read the file with
`sqlite3` directly, one tests the SQLite column migration).

Single-writer was the reason `replicas: 1` was load-bearing, so that constraint
is lifted. `deploy/local/docker-compose.postgres.yml` runs the cloud shape on a
laptop — registry in Postgres, limits in Redis — so the path is exercisable
before it is deployed.

**What the port actually cost**, since it is the argument for the seam:

| Bug | How it presented |
|---|---|
| `MAX(a, b)` | SQLite's scalar two-argument form; Postgres only has the aggregate. Translated to `GREATEST`/`LEAST`. |
| `window` | A reserved word in Postgres. `schema.sql` already quoted it; the queries did not. Quoted at the source, since both engines accept it. |
| `calls = calls + excluded.calls` | Ambiguous in Postgres between the target row and `excluded`. Qualified with the table name. |
| `%` in a `LIKE` | psycopg2 treats `%` as its own format character and does not parse the SQL first, so a wildcard raises. Escaped — inside literals too. |
| `INSERT OR REPLACE` / `OR IGNORE` | No Postgres equivalent. Rewritten as `ON CONFLICT (pk) DO UPDATE` / `DO NOTHING`, which both engines have had since 2018. |
| `Store(pathlib.Path(dsn))` | `Path` collapses `postgresql://` to `postgresql:/`, so the DSN silently became a *SQLite filename*. This was the dangerous one: the wrong engine, no error. `is_postgres` now matches either form so it fails loudly instead. |

Redis is the other half: `MI_REDIS_URL` moves the rate-limit counters out of the
process, because "120 rpm" per replica is not a limit once you scale out. The
`redis` package stays optional — the limiter takes an injected client, so the
policy is tested without a server, and a missing package falls back to
in-process rather than refusing to serve.

```
① Edge            DNS · TLS · WAF · DDoS          SSE: disable proxy buffering
② API gateway     key auth · rate limit           or in-app (Phase 0 does this)
③ App runtime     stateless, 2+ replicas          forces ④
④ Data            Postgres (primary) + Redis + object storage
⑤ Batch           scheduler + queue               ingest / metrics / bench
⑥ Observability   metrics · logs · traces         + product analytics
⑦ Secrets         KMS / Secret Manager            rotation
⑧ CI/CD           image · migrations · canary
⑨ Billing         metering → Stripe
⑩ Trust           DPA · retention · residency · audit log
```

Two are load-bearing; the rest is standard.

**④ Data.** Every request writes — 16 write-sites in `proxy.py`, ~2 rows per call.
SQLite is single-writer, so this is now on the hot path:

- **Postgres as primary**, not a mirror. The schema exists; `mi sync` becomes the
  migration bridge rather than the end state.
- **Redis** for rate-limit counters, session counters, and the 120 s
  benchmark-norms cache. The cache was a *per-process* global (`router.py`), cold
  on every replica; it now goes through `mininfer/cache.py`, which is
  in-process when `MI_REDIS_URL` is unset and shared when it is set.
- **Object storage** for the evidence lake. `fetch.py` already calls `raw/` "a
  local stand-in for the S3/R2 bucket" and records `storage_uri`.

**⑤ Batch.** `ingest` / `metrics` / `bench` are manual today; a hosted service
needs a scheduler so the registry stays current without an operator.

---

## 6. Risks

- **Provider ToS is the biggest one.** Hosting and reselling subsidised free tiers
  is commonly prohibited. Shape B sidesteps it entirely.
- **Free-first economics under tenancy.** In shape A, tenants drain the shared
  free tier and *you* pay for the fallback. Margin = price − paid-fallback cost,
  so per-tenant metering must exist before the first customer.
- **The pool is small.** After removing Featherless: 1,690 deployments, 1,040
  weights, and for `general_chat` only **221 eligible / 53 free across 37
  upstreams**. Free-tier concurrency caps QPS long before the infra does.
- **Per-request routing cost.** `build_candidates` scans every deployment per
  request (norms cached 120 s). At real QPS this wants a materialised candidate
  cache.

---

## 7. Sequencing

| Phase | Scope | Unlocks |
|---|---|---|
| **0** | ✅ API-key auth, per-key rate limiting, surface split, body cap, CORS | Safe to expose privately |
| **1** | ✅ Postgres primary (`MI_DB` DSN), Redis limiter and norms cache (`MI_REDIS_URL`), multi-replica cloud manifests, suite green on both engines | Horizontal scale |
| **1b** | ✅ `tenant_id` on `observations`/`decisions`/`sessions`, `GET /v1/usage` | Per-tenant reporting |
| **1.0** | ✅ the shipping boundary: local + cloud deployable, **no ML runtime** in the image. Tracked in `CLOUD_ACTIVITY.md` | First deployable release |
| **2** | Control plane (tenants/keys/usage), Stripe, BYO-key encryption at rest, **and the deferred GLiNER2.5 understanding layer** (`CLOUD_ACTIVITY.md` §2.0) | Real customers |
| **3** | Autoscaling, multi-region, abuse tooling, audit/compliance | Public launch |

---

## 8. Day-2 notes

**Validating the access-control layer (`scripts/validate-phase0.sh`)**

The script is the acceptance test for Phase 0. It is hermetic — its own ports,
its own temp databases, no upstream calls, and it kills what it starts — so it is
safe to run against a machine that is already serving traffic. It asserts, in
order:

1. the pinned properties (`tests/test_auth.py`, `tests/test_profiles.py`);
2. the **LOCAL** profile is still completely open;
3. the **CLOUD** profile authenticates every surface correctly, including that a
   tenant key cannot read the admin surface and that the operator console still
   works through HTTP Basic;
4. rate limiting is per tenant and returns `Retry-After`;
5. an oversized body is rejected before it reaches the router.

```bash
./scripts/validate-phase0.sh      # exit 0 = validated
```

**Hand-checks the script cannot make** — do these once, against a real
deployment, before exposing it:

- `curl -s https://<host>/openapi.json` returns **401**, not the schema.
- `curl -s https://<host>/v1/chat/completions -d '{}'` returns **401**, not 422 —
  auth runs before validation, so an unauthenticated caller learns nothing.
- The dashboard at `/` prompts for credentials and, once in, its playground can
  still complete a call (the admin token satisfies tenant endpoints).
- `MI_ADMIN_TOKEN` and every tenant key live in the platform secret store, not
  in `config/profiles/cloud.env` — that file's values contain `do-not-deploy`,
  and `tests/test_profiles.py` enforces that they keep doing so.

**Other**

- Keys: rotate via `MI_API_KEYS_FILE` — the config cache is keyed on the file's
  mtime, so a rewrite is picked up without a restart. Never commit a live key.
- The admin surface exposes provider pricing, routing decisions and caller IPs.
  Keep it off the public internet, or behind Cloudflare Access.
- `Dockerfile` bakes `mininfer.db` in as `/app/seed.db`. Fine for self-host; a
  hosted service should build the registry with a scheduled job instead of
  shipping it in the image.
