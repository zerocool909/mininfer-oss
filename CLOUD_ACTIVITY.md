# CLOUD ACTIVITY — topology, shared state, understanding layer

Tracks the work described in `PRODUCTIZATION.md` §5 (infra layers), the
"local + cloud deployable" goal, and the GLiNER2.5 "local understanding layer"
decision. Same convention as `IMPLEMENTATION_CHECKLIST.md`: a line is only marked
✅ when its validation has been **run and passed on this tree**.

`[ ]` todo · `[~]` in progress · `[x]` done+validated · `[!]` blocked
· `[–]` **deferred to a later phase**

The work is split into two phases so the first can ship without the second:

| Phase | Scope | Status |
|:--|:--|:--|
| **1.0** | **Local + cloud deployable.** One image, two process types; registry on Postgres; limits *and* caches on Redis; multi-replica manifests; CA bundle mounted. No ML runtime. | this document, §1.0 |
| **2.0** | **The local understanding layer (GLiNER2.5).** Optional, opt-in, deferred — the seam is in place but the checkpoint is not wired into the default image. | §2.0 |

Phase 2.0 is deliberately *out of* Phase 1.0's release boundary: it adds a
PyTorch dependency and a ~2 GB checkpoint, and none of it is needed to serve the
first request. The seam was built early so it could be deferred without leaving a
half-wired request path behind.

Validations used here:

| Name | Command | Proves |
|:--|:--|:--|
| **py** | `pytest -q` | the SQLite suite still passes |
| **pg** | `./scripts/validate-postgres.sh` | the same suite on Postgres (see `IMPLEMENTATION_CHECKLIST.md` §Wave 6) |
| **cache** | `pytest -q tests/test_cache.py` | the norms cache is shared, and degrades to in-process |
| **manifests** | `pytest -q tests/test_deploy_manifests.py` | the cloud manifests keep the Postgres/Redis/replica/CA shape |
| **variants** | `pytest -q tests/test_variants.py` · `./scripts/validate-variants.sh` | both routes (local/OSS + cloud) boot from `config/profiles/*.env` and behave as pinned, in-process and in the image |
| **under** | `pytest -q tests/test_understanding.py` | the (phase 2.0) understanding seam degrades gracefully and wires into intent/ingest |
| **cli** | `python3 -m mininfer classify "free gemini with 32k context"` | the command runs and reports the backend it used |
| **dockercfg** | `docker build --target worker -t mininfer:worker .` | the worker image builds from the same context |
| **toml** | `python3 -c "import tomllib,pathlib; tomllib.loads(pathlib.Path('deploy/cloud/fly.toml').read_text())"` | the Fly manifest parses |
| **k8s** | `python3 -c "import yaml; list(yaml.safe_load_all(open('deploy/cloud/k8s.yaml')))"` | the k8s manifest parses (covered by **manifests**) |
| **compose** | `docker compose -f deploy/local/docker-compose.yml config -q` | the local manifest still resolves |

---

## Phase 1.0 — local + cloud deployable (no ML runtime)

### 1.0a. One image, two process types (`web` + `worker`)

| # | Activity | Validation | State |
|:--|:--|:--|:--|
| 1.0a.1 | `worker` stage in `Dockerfile` — same runtime as `runner`, `MI_ROLE=worker` | **dockercfg** | [x] image builds and both process types run (see evidence log) |
| 1.0a.2 | `fly.toml` `[processes]` split: `web` = uvicorn, `worker` = batch loop; `[http_service]` scoped to `web` | **toml** | [x] |
| 1.0a.3 | `deploy/cloud/README.md` documents the two process types and the scheduler | review | [x] |

The `worker` stage no longer carries a GLiNER build arg — that belongs to Phase
2.0 and has moved there. Phase 1.0 ships one runtime with no torch.

### 1.0b. Shared state, required before replicas > 1

| # | Activity | Validation | State |
|:--|:--|:--|:--|
| 1.0b.1 | `MI_DB` as a Postgres DSN (registry off SQLite) — done in `PRODUCTIZATION.md` §4a | **py** + **pg** | [x] |
| 1.0b.2 | `MI_REDIS_URL` moves the rate-limit counters out of the process | **py** (`test_auth.py`) | [x] |
| 1.0b.3 | `mininfer/cache.py`: shared TTL cache (Redis when `MI_REDIS_URL` is set, in-process otherwise), same degrade-don't-fail contract as `auth.make_limiter` | **cache** | [x] |
| 1.0b.4 | `router.benchmark_norms` reads/writes that cache, so N replicas do **one** whole-table scan per 120 s window instead of N | **cache** `test_norms_are_shared_across_replicas_through_redis`; **py** | [x] |
| 1.0b.5 | k8s manifest to the Postgres/Redis shape: `MI_DB` + `MI_REDIS_URL` from secrets, `replicas: 3`, `RollingUpdate`, stateless web pods | **manifests**; **k8s** | [x] |
| 1.0b.6 | k8s `CronJob` for the batch jobs (`mi ingest` / `mi metrics`), `concurrencyPolicy: Forbid`, evidence-lake PVC | **manifests** `test_k8s_worker_is_a_batch_job_not_a_service` | [x] |

### 1.0c. Outbound TLS / CA bundle (checklist 7.4)

| # | Activity | Validation | State |
|:--|:--|:--|:--|
| 1.0c.1 | k8s: optional `mininfer-ca` ConfigMap mounted at `/etc/mininfer/ca`, `MI_CA_BUNDLE` set on web **and** worker | **manifests** `test_k8s_mounts_the_optional_ca_bundle` | [x] |
| 1.0c.2 | Fly: `MI_CA_BUNDLE=/data/ca-bundle.pem` on the volume (`fly ssh sftp` places the file); absence is handled by `fetch._verify` | **toml**; **manifests** | [x] |
| 1.0c.3 | Local compose still passes `MI_CA_BUNDLE` through (host path) | **compose** | [x] |

### 1.0d. Carried over (unchanged from `IMPLEMENTATION_CHECKLIST.md` Wave 7)

| # | Item | State |
|:--|:--|:--|
| 7.1 | Phase 2 metering/billing (Stripe + idempotency keys) | [ ] (product Phase 2, unrelated to GLiNER) |
| 7.2 | API versioning contract (`/v1` pinning + deprecation) | [ ] |
| 7.3 | `quota_buckets` per tenant | [ ] |

---

## Phase 2.0 — the local understanding layer (GLiNER2.5) — **deferred**

> **Status: deferred.** The seam exists and is inert; the checkpoint is not part
> of the Phase 1.0 image and 8.9 (real-model validation) has not run. Do not
> count any of this as shipped until it is validated on a machine with the model
> artifact. This is the first item of Phase 2.0, alongside `PRODUCTIZATION` gap 7
> (metering/billing).

The layer, when enabled, is a cheap local classifier/extractor that answers
routing-intent and evidence-extraction questions without spending a routed call.
It is opt-in in both directions — off unless `MI_INTENT_BACKEND=gliner` /
`MI_EXTRACT_BACKEND=gliner` **and** the `[understanding]` extra is installed — so
Phase 1.0 behaviour is unchanged when it is absent.

| # | Activity | Validation | State |
|:--|:--|:--|:--|
| 2.0a.1 | `mininfer/understanding/` package: lazy loader, no import cost, returns `None` when the extra is absent | **under** | [x] (seam only) |
| 2.0a.2 | `[understanding]` optional extra in `pyproject.toml` (not in the universal lock — platform-resolved) | **py** + packaging guard | [x] |
| 2.0a.3 | Intent wiring: `_intent_llm()` prefers the local backend when `MI_INTENT_BACKEND=gliner` | **under** | [x] |
| 2.0a.4 | Ingestion wiring: opt-in `understanding` node before `llm_extract`; falls through to the LLM when it yields nothing | **under** | [x] |
| 2.0a.5 | `mi classify` — task + heads + entity extraction as JSON, reports which backend answered | **cli** | [x] |
| 2.0a.6 | Worker image opt-in build (`--build-arg WITH_UNDERSTANDING=1`) — **removed from the Phase 1.0 `Dockerfile`**, to be reintroduced with the model | **dockercfg** | [–] deferred |
| 2.0a.7 | Validate against a **real** GLiNER2 checkpoint (device, call shape, latency) | blocked on the model artifact | [!] deferred |

---

## Evidence log

### Phase 1.0b — shared cache and manifests (validation: **cache**, **manifests**, **py**)

```
$ python3 -m pytest tests/test_cache.py tests/test_deploy_manifests.py -q
17 passed

$ python3 -m pytest -q
494 passed, 1 skipped          # was 477 before this wave; full suite green
```

The cache is proven shared, not merely JSON-serialisable:

- `test_norms_scan_the_registry_once_per_ttl` — a warm window does not rescan;
- `test_norms_are_shared_across_replicas_through_redis` — a second "replica"
  reads the first replica's cached band and never touches its own store;
- `test_norms_key_is_per_registry` — two DB targets do not read each other's
  norms (the cache key includes the `Store.target`);
- `test_make_cache_falls_back_when_redis_is_unavailable` — same contract as
  `auth.make_limiter`: a missing package or server degrades, never 500s.

The manifests are pinned structurally: `replicas: 3` + `RollingUpdate`, `MI_DB`
and `MI_REDIS_URL` from `mininfer-secrets`, no registry PVC on the web pods, the
batch work in a `CronJob` with `concurrencyPolicy: Forbid`, and `MI_CA_BUNDLE`
set **and** mounted on both web and worker.

```
$ python3 -c "import yaml; print(len([d for d in yaml.safe_load_all(open('deploy/cloud/k8s.yaml')) if d]))"
6            # Secret, PVC(evidence), Deployment, CronJob, Service, Ingress

$ python3 -c "import tomllib,pathlib; d=tomllib.loads(pathlib.Path('deploy/cloud/fly.toml').read_text());
              print(d['processes']); print(d['http_service']['processes'])"
{'web': 'uvicorn mininfer.proxy:app --host 0.0.0.0 --port 8000',
 'worker': "sh -c 'while true; do mi ingest --force || true; mi metrics || true; sleep 21600; done'"}
['web']
```

**Validated on this tree:** both stages build, and both process types run.

```
$ docker build --target runner -t mininfer:web .      # web (uvicorn)
$ docker build --target worker -t mininfer:worker .   # worker (batch)

$ docker run --rm -v vol:/data mininfer:worker mi --help
==> No database found at /data/mininfer.db. Seeding catalog...
==> Seed database copied successfully (8171520 bytes).
==> Executing: mi --help
...

$ docker run -d -p 8766:8000 -v vol:/data mininfer:web   # LOCAL route: open
  /healthz 200 · /v1/models 200 · / 200

$ docker run -d -p 8767:8000 -e MI_API_KEYS=… -e MI_ADMIN_TOKEN=… mininfer:web
  /healthz 200 · /v1/models(no cred) 401 · /v1/models(tenant) 200
  /v1/stats(tenant) 401 · /v1/stats(admin) 200 · unknown path(no cred) 401
```

The last block is the 1.0a contract: a command passed to the container runs the
registry step first and then replaces the web server, so `worker = "mi …"`
reuses the same seeding path with no second Dockerfile — and the *same image*
enforces the cloud profile's access control.

**A build bug this exposed.** The first `docker build` failed at
`COPY mininfer.db /app/seed.db`: `.dockerignore` carried a comment saying
"Only `mininfer.db` is needed", immediately followed by a bare `mininfer.db`
pattern that **excluded** it. The file the Dockerfile copies could never reach
the context. Fixed to `!mininfer.db` (a negation, matching the comment's intent),
and pinned by `scripts/validate-variants.sh` building both targets. This is the
same class of failure `PRODUCTIZATION.md` §3a describes: a comment and a rule
drifting apart with nothing failing until a deploy.

```
$ ./scripts/validate-variants.sh
== 1. pytest  ==   97 passed
== 2. live    ==   27 passed, 0 failed
== 3. container == both stages build; worker + web smoke; cloud fails closed
ROUTES VALIDATED
```

### Phase 2.0 — the understanding seam (validation: **under**, **py**, **cli**)

```
$ python3 -m pytest tests/test_understanding.py tests/test_packaging.py -q
22 passed
```

The seam is proven inert and then proven wired, both against a fake backend — the
real checkpoint is not a test dependency (2.0a.7):

- absent: `available()` False, `classify/pick_task/extract/extract_models/
  as_intent_backend` all `None`, `require()` raises, and `_intent_llm()` returns
  `None` even with `MI_INTENT_BACKEND=gliner`;
- present: scores normalise to a share, `as_intent_backend()` drops straight into
  `intent.classify(llm=…)` and answers `sql_generation`, `extract_models` drops a
  nameless record, and the ingest node sets `extraction` so `llm_extract_node`
  becomes a no-op for that run.

```
$ python3 -m mininfer classify "Fix this SQL query and explain why it is
    producing duplicate rows" --no-extract
  "task": {"name": "sql_generation", "confidence": 0.667, "source": "cue"}
```

With no model installed it still answers, and it *says* it fell back rather than
presenting a cue guess as model output.

_Docker daemon: down (`docker info` fails) — image builds for this tree are
pending a machine with the daemon running._
