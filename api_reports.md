# API Report — MinInfer

Verified against the running container and the full test suite.

## Status

| | |
|---|---|
| Tests | **829 passed, 1 skipped** |
| Container | `mininfer-router` (mininfer:latest), healthy, port 8000 |
| Registry | 1849 deployments · 1217 weights · 91,536 evidence rows |
| Derived | 3331 reconciled prices · 1849 pricing states · 18 anomalies |
| Auth | **OFF** — `MI_ADMIN_TOKEN` and `MI_API_KEYS` are both empty |

Base URL: `http://localhost:8000`

---

## 1. Economics API — the reconciled view

Everything the Overview page reads, in one call. These are the endpoints added by
the pricing/quota work; every one below was executed successfully.

| Method | Path | Returns |
|---|---|---|
| GET | `/v1/economics/overview` | The whole snapshot: counts, providers, quota, savings, reviews, anomalies, pricing states |
| GET | `/v1/economics/quota` | Bucket headroom, its `source` (configured/observed/hybrid), and a probabilistic exhaustion |
| GET | `/v1/economics/anomalies` | The incident log; `?status=open|acknowledged|resolved`, `?status=` for all |
| GET | `/v1/economics/providers` | Per-provider rollup with the **provenance** of each `min_in` |
| GET | `/v1/economics/deployments/{id}` | One arm's whole story: resolution, belief history, transitions, anomalies, quota |
| GET | `/v1/economics/history` | The belief timeline; `?as_of=` gives the belief in effect then |

```bash
curl -s localhost:8000/v1/economics/overview | jq
curl -s localhost:8000/v1/economics/quota | jq
curl -s localhost:8000/v1/economics/anomalies | jq
curl -s 'localhost:8000/v1/economics/anomalies?status=resolved' | jq
curl -s localhost:8000/v1/economics/providers | jq
curl -s 'localhost:8000/v1/economics/deployments/groq:qwen/qwen3.8-27b' | jq
curl -s 'localhost:8000/v1/economics/history?deploy_id=groq:qwen/qwen3.8-27b' | jq
curl -s 'localhost:8000/v1/economics/history?deploy_id=groq:qwen/qwen3.8-27b&as_of=2026-10-01T00:00:00+00:00' | jq
```

Two things worth knowing about the shape:

* `/v1/economics/overview` is a **superset of `/v1/stats`**, so a client swaps one
  call for another and cannot render a price beside a *different* moment's trust
  state. `deploy_id` is a `:path` parameter, because real ids contain slashes.
* `savings.price_basis` and `savings.priced_free_share` say how the counterfactual
  was priced. `priced_free_share` is *coverage* (the share of free calls that had a
  paid sibling), not a statistical confidence — hence the name.

## 2. Registry & ops

| Method | Path | Returns |
|---|---|---|
| GET | `/v1/stats` | Counts, providers, quota, recent decisions, savings |
| GET | `/v1/plan?task=` | The routing funnel and the chosen shortlist for one task |
| GET | `/v1/models` | OpenAI-compatible model list |
| GET | `/v1/models/explore` | Browse the registry: `q`, `provider`, `capability`, `free_only`, `max_price_out`, `min_context`, `sort`, `limit`, `offset` |
| GET | `/v1/providers` | Providers, their keys, and the models each serves |
| GET | `/v1/reviews` | Arms hibernated for review (were free, now charge) |
| GET | `/v1/savings` | Spend vs. the cheapest paid sibling; `?days=`, `?session=` |
| GET | `/v1/usage` | Per-tenant usage report |
| GET/POST | `/v1/pushed-models` | Operator pins: push / unpush / clear |
| GET | `/v1/session` | Server-side spend and token counts (**needs `X-MI-Session`**) |
| GET/DELETE | `/v1/session/messages` | The stored transcript (**needs `X-MI-Session`**) |
| POST | `/v1/approve` | Approve a shortlist after `needs_approval` |
| POST | `/v1/route-verdict` | Record whether a routing choice was good |
| POST | `/v1/reviews/decide` | Approve / reject a hibernated arm |
| GET | `/healthz` | Liveness (public) |
| GET | `/openapi.json`, `/docs` | The schema and an interactive client |

```bash
curl -s localhost:8000/v1/stats | jq
curl -s 'localhost:8000/v1/plan?task=general_chat' | jq
curl -s 'localhost:8000/v1/models/explore?limit=10&sort=price' | jq
curl -s localhost:8000/v1/reviews | jq
curl -s -H 'X-MI-Session: demo' localhost:8000/v1/session | jq
curl -s localhost:8000/openapi.json | jq '.paths | keys'
```

The eight task names are `general_chat, sql_generation, code_edit, extraction,
summarise, agent_tools, shelf_image_audit, hard_reasoning`. An unknown one is a
**404 that lists them** rather than a silent fallback.

## 3. Calling a model

```bash
# `auto` routes itself; the response names the chosen arm and its fallbacks
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say OK"}]}' | jq

# streaming (SSE)
curl -N localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"Count to 3"}]}'

# web search — a session header is required, because this one can spend
curl -s localhost:8000/v1/search -H 'Content-Type: application/json' \
  -H 'X-MI-Session: demo' -d '{"query":"cheapest llm api pricing"}' | jq

# compact a transcript into a brief
curl -s localhost:8000/v1/compact -H 'Content-Type: application/json' \
  -H 'X-MI-Session: demo' -d '{"messages":[{"role":"user","content":"hi"}]}' | jq
```

An unknown model id returns **502** with a legible reason ("all 1 candidate(s)
failed (last: no endpoint for provider 'x')"), not a 500.

## 4. UI

```
http://localhost:8000/         React dashboard
http://localhost:8000/legacy   server-rendered fallback
http://localhost:8000/docs     OpenAPI
```

## 5. Auth

Currently **off**: with `MI_ADMIN_TOKEN` and `MI_API_KEYS` both empty,
`AuthConfig.enabled` is `False` and the middleware returns early — so `/v1/stats`,
which `auth.classify` places on the *admin* surface, answers 200 with no
credential. The container binds `0.0.0.0:8000`, so anything on the same network can
call it and spend your provider credits.

To close it, either stop publishing it off-host:

```yaml
    ports:
      - "127.0.0.1:8000:8000"     # was "8000:8000"
```

or turn auth on:

```bash
MI_ADMIN_TOKEN="$(openssl rand -hex 32)"     # admin surface
MI_API_KEYS_FILE=/run/secrets/keys.json      # per-tenant keys (SHA-256 digests)
```

`test_no_route_is_public_by_accident` pins the public set, so a new endpoint cannot
be exposed silently.

## 6. CLI

```bash
mi stats                      # registry counters
mi reconcile                  # re-derive prices, states, anomalies, history
mi anomalies                  # the incident queue
mi anomaly <id> --acknowledge --note "known adapter bug"
mi quota show                 # buckets, headroom source, exhaustion estimate
mi transitions                # pricing-state history (free -> paid, ...)
mi history <deploy_id>        # the belief timeline; --as-of for a past instant
mi retire <deploy_id> --reason "..."   # take an arm out of routing for good
mi enable <deploy_id>         # ...and put it back
mi disabled                   # what is currently retired, and why
mi ingest --force             # pull every catalogue
mi verify --report /data/verify.json   # daily drift check
```

Inside the container:

```bash
docker exec mininfer-router mi stats
docker exec mininfer-router mi anomalies
docker exec mininfer-router mi disabled
```

## 7. What using it actually found

Seven bugs, all invisible from unit tests alone. Each is now pinned by a test.

| # | Found by | Bug | Fix |
|---|---|---|---|
| 1 | running the API | `_stream`'s `reason` parameter was shadowed by a rate-limit string, so a request that recovered on its second arm returned **500** | renamed the local to `rl_reason`; 14/14 streaming runs now 200 |
| 2 | running the API | `deepseek` was listed tier-0 (keyless) but returns **401** every ingest | `key_env` now outranks the tier; moved to tier 1 |
| 3 | running the API | `vllm`'s default port is ours: ingest read **our own** `/v1/models` and invented 36 "models" from task names | an `owned_by` guard, matching both names the project has had |
| 4 | Postgres run | `status_changed_at = CASE … THEN CURRENT_TIMESTAMP ELSE <text> END` — Postgres refuses a `CASE` mixing `timestamptz` and `text` (**235 tests failed**) | bind `:now` from Python |
| 5 | Postgres run | two subqueries in `FROM` had no alias; SQLite tolerates it, Postgres does not | aliased both |
| 6 | using the page | no way to retire an arm **on purpose** — only the router could, by failing on it | added `retire` |
| 7 | testing #6 | a hand-retired **`:free`** arm was resurrected by the next ingest, because `_spend_transition`'s still-free branch returns "leave it alone" and the incoming `live` then won | a status we decided outranks the catalogue, whatever its provenance |

## 8. Hosting

The engine runs on **Postgres** — validated by running the whole suite against a
real Postgres 14: **815 passed, 4 skipped** (SQLite: 829/1). That is what surfaced
bugs 4 and 5, neither of which is visible from SQLite.

`deploy/modal/app.py` is the serverless host: a `@modal.Cron` running
`ingest` then `reconcile`, and a `@modal.asgi_app()` returning the real proxy app —
not a copy of it. Thresholds are `PQS_*` env-driven, so retuning the reconciler is
a re-run rather than a release. See `deploy/modal/README.md`.

**Still to do (needs credentials, not code):** a Postgres DSN and `modal deploy`.

## 9. Known non-blockers

* `argparse` still uses `prog="mi"` in usage lines, so `mi retire --help` prints
  `usage: mi retire`. Cosmetic, and a rename artefact.
* The brand assets `/mininfer-icon.svg`, `/mininfer-logo.svg` (and `.png`) classify
  as `admin`, so under auth they 401 unless the browser holds a credential.
  `/favicon.svg` is public, because `PUBLIC_PREFIXES` already listed `/favicon`.
* Save-and-reuse: `mi sync` exists for the SQLite → Supabase mirror. With Postgres
  as the primary there is nothing to mirror.
