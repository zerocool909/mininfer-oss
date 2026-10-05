# Deploying MinInfer

Two variants, **one codebase**. They differ in configuration and orchestration,
never in logic — so the code lives in `mininfer/` once, and everything here is
manifests and profiles.

| | **local** | **cloud** |
|---|---|---|
| For | your machine, one operator | other people's requests |
| Access control | off — open API and dashboard | on — fails closed |
| Bind | `127.0.0.1:8765` | `0.0.0.0:8000` behind TLS |
| State | `mininfer.db` beside the code | `/data` volume (Postgres from Phase 1) |
| Limits | none | rate + body caps |
| Profile | `config/profiles/local.env` | `config/profiles/cloud.env` |
| Run | `mi proxy` · `docker compose up` | `deploy/cloud/` (Fly, k8s, ECS) |

```
deploy/
  local/    docker-compose.yml                 ← SQLite, one process, auth off
            docker-compose.postgres.yml        ← the cloud shape, run locally
  cloud/    fly.toml, k8s.yaml, litestream.yml, keys.example.json
config/profiles/
  local.env, cloud.env                          ← the variants, as data
Dockerfile                                      ← shared image, both variants
docker-compose.yml                              ← `include:` shim → deploy/local/
```

## Registry backends

`MI_DB` takes a path **or** a DSN, and that is the only switch:

| `MI_DB` | Engine | Replicas |
|---|---|---|
| `mininfer.db` (or `/data/mininfer.db`) | SQLite | **1** — every request writes, and SQLite is single-writer |
| `postgresql://user:pw@host/db` | Postgres | as many as you like |

```bash
./scripts/validate-postgres.sh     # runs the whole suite on both engines
```

`deploy/local/docker-compose.postgres.yml` brings up Postgres + Redis alongside
MinInfer, with access control on — the cloud shape without the cloud.

## Why the root files stayed put

`Dockerfile` and `docker-compose.yml` are still at the repo root, deliberately:

- the **image is shared**. Cloud runs the same artifact with different
  environment and orchestration, so a second Dockerfile would be a copy that
  drifts — the exact failure this layout exists to avoid;
- `docker build .` and `docker compose up` are what everyone already types, and
  this README documents them. The root `docker-compose.yml` is now a
  one-line `include:` of `deploy/local/docker-compose.yml`, so the command is
  unchanged and there is still only one definition of the stack.

## Why profiles and not two folders

A second copy of the *code* drifts. This repository already has that scar: a
duplicated package was once kept for compatibility, and by the time it was
removed it carried a stale user agent, out-of-date environment variables, a
broken `sys.modules` alias that produced duplicate adapter registries, and a
failing test. A cloud fork of the package would reintroduce exactly that.

So the variants are **data**, and data is tested:

```bash
./scripts/validate-variants.sh   # both routes: in-process, live, and in the image
./scripts/validate-phase0.sh    # boots both profiles and asserts each surface
python3 -m pytest tests/test_variants.py -q   # the route matrix, no shell needed
python3 -m pytest tests/test_profiles.py -q
```

`tests/test_variants.py` parses the two profile files, boots the real ASGI app
under each one, and walks the whole surface matrix — local open, cloud fail
closed, per-tenant limits, the OpenAI error shape — plus the invariant that no
variant-specific code module exists. `validate-variants.sh` layers that on top
of the live socket checks and a container smoke test of both process types.

`validate-phase0.sh` *sources* `config/profiles/*.env`, and `test_profiles.py`
asserts each variant's invariants — local must stay open, cloud must stay locked
down and hold no usable credential. Weaken `cloud.env` and both fail. Fix the
router and both variants get it at once, because there is only one router.

## Verify the include shim still resolves

```bash
docker compose config | python3 -c "
import sys, yaml, re
d = yaml.safe_load(sys.stdin)
for svc in d.get('services', {}).values():
    env = svc.get('environment') or {}
    for k in list(env):
        if re.search(r'KEY|TOKEN|SECRET|PASSWORD|BUNDLE', k):
            env[k] = '<redacted>'
    svc['environment'] = env
s = d['services']['mininfer']
print(list(d['services']), s['build'])
"
```

`docker compose config` prints **resolved** `.env` values — including provider API
keys — and this Compose version has no `--no-interpolate` flag. Redact the
output whenever it might be logged or pasted; never paste it raw.
