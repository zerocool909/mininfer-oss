# IMPLEMENTATION CHECKLIST

Continues `BACKLOG.md` and `PRODUCTIZATION.md`. One line per activity, with the
**validation method** that proves it, and its state. A line is only marked ✅
when its validation has been run and passed on this tree — not because the code
"looks done".

Convention: `[ ]` todo · `[~]` in progress · `[x]` done+validated · `[!]` blocked

Validations used below, so the short names are unambiguous:

| Name | Command | Proves |
|:--|:--|:--|
| **py** | `pytest -q` | SQLite suite (the default backend) |
| **pg** | `./scripts/validate-postgres.sh` | the same suite on Postgres |
| **phase0** | `./scripts/validate-phase0.sh` | the auth/rate-limit/body-cap acceptance test |
| **web** | `cd web && npm test && npx tsc --noEmit && npm run build` | frontend unit tests, types, production bundle |
| **install** | `./scripts/validate-install.sh` | a clean venv installs from `requirements.lock` and both entry points run |
| **bench** | `python3 -m mininfer.cli bench all --self-test` | the verifiable-reward benchmark's gold answers |
| **e2e** | boot the proxy against a copy of the registry, make a real routed call and a streamed one | the whole path, live, against a provider |
| **docker** | `docker build -t mininfer:verify .` then run it and curl | the image builds, boots non-root, seeds and serves; auth works in the container |
| **local** | host process + `docker compose up` from `config/profiles/local.env` | the local variant is open, unlimited, seeded |
| **cloud** | `docker compose -f deploy/local/docker-compose.postgres.yml up` + a container on `cloud.env` | the cloud variant fails closed, limits via Redis, writes to Postgres |

---

## Wave 0 — restore a green suite (blocker: everything else is measured on it) ✅

| # | Activity | Validation | State |
|:--|:--|:--|:--|
| 0.1 | Reproduce the flaky failure deterministically | `test_both_arms_of_a_comparison_are_charged` looped until red; `--durations` pinned one 31.6 s call | ✅ |
| 0.2 | Root-cause it (not guess) | `faulthandler_timeout` traceback: two worker threads in a lock-order inversion | ✅ |
| 0.3 | Fix `db.py`: never hold the process write lock while waiting on SQLite | `tests/test_sqlite_concurrency.py::test_two_writers_do_not_deadlock` — **31.65 s/fail before, 0.32 s/pass after** | ✅ |
| 0.4 | Prove the charge is not lost | `test_session.py` looped 10× → 10/10 green | ✅ |
| 0.5 | Full suite green | **py** → 433 passed, 1 skipped (was 34.6 s wall with the 30 s stall; now ~4 s) | ✅ |

## Wave 1 — P3.5 Model explorer ✅ (this wave's shipped item)

| # | Activity | Validation | State |
|:--|:--|:--|:--|
| 1.1 | `Store.explore_models` + `_capability_facets`: filter by text/provider/capability/free/price/context, whitelisted sort, paged, read-only | `tests/test_explore.py` (16 tests) — **py** and **pg** | ✅ |
| 1.2 | One capability vocabulary (`schema.CAP_KEYS`), shared with the extraction agents | `test_explore.py::test_facets_report_confirmed_capabilities` + `agent_ingest` imports it (no second list) | ✅ |
| 1.3 | `GET /v1/models/explore` endpoint; tenant-classified (fails closed if unlisted) | `test_explore.py::test_endpoint_serves_the_explorer`; **phase0** 27/27 | ✅ |
| 1.4 | React **Models** tab: search, provider/capability/free filters, sort, pagination | **web** — `lib/explore.test.ts` (16 tests), `tsc` clean, `vite build` clean | ✅ |
| 1.5 | Query/serialisation logic is a pure, unit-tested module, not inline in the component | `web/src/lib/explore.ts` + `explore.test.ts` | ✅ |
| 1.6 | Benchmarks per model, not just success rates | `test_explore.py::test_benchmarks_travel_with_the_model`; `explore.test.ts` `benchmarkTags` | ✅ |

## Fixes surfaced while validating ✅ (all pre-existing)

| # | Defect | Why it mattered | Validation | State |
|:--|:--|:--|:--|:--|
| F1 | `pushed_models` missing from `supabase/schema.sql` | Postgres registry had no such table: `push_model` could not work at all, and the explorer's join raised `UndefinedTable` | added to the DDL + pinned in `test_sync.py`; **pg** green | ✅ |
| F2 | `push_model` used SQLite-only `INSERT OR REPLACE` | Would fail on Postgres even once the table existed | rewritten as `ON CONFLICT (deploy_id) DO UPDATE`; `test_explore.py`/`test_sync.py` on **pg** | ✅ |
| F3 | `build_candidates` returned DB row order | No `ORDER BY`, so SQLite (insertion order) and Postgres agreed by luck; `rank_for_compare` takes `ranked[0]` as arm 0, so arm 0 differed per engine — 2 `test_compare` tests failed on **pg** only | sort by `_sort_key` at the end; **pg** 4/4 runs green | ✅ |
| F4 | `tests/conftest.py` created Postgres schemas without a lock | The multiplex test builds two Stores from two threads; concurrent `create schema if not exists` races in the catalog → intermittent **pg** failure | schema creation now locked and once-per-schema; **pg** 4/4 runs green | ✅ |
| F5 | Write paths disagreed on the session key | `_handle` wrote spend under `tenant/session` (auth on) while `GET /v1/session` and `/v1/search` read the bare id, so the footer's number was not the cap's number and search spend landed in a different ledger | one `_request_session` used by all four call sites | ✅ |
| F6 | `pip install .` shipped a package with no `mininfer/web/` or `mininfer/ingest/` | `pyproject.toml` used a bare `packages = ["mininfer"]`; setuptools then omits subdirectories, so the installed CLI could not import `ingest` and the proxy could not import `web.legacy` | wheel contents before/after (MISSING → PRESENT); `tests/test_packaging.py`; **install** | ✅ |
| F7 | **`MI_DB` as a Postgres DSN never worked via the proxy or CLI** | `_default_db()` returned `pathlib.Path(env_db)`, collapsing `postgresql://…` to `postgresql:/…`; psycopg2 rejects that, so every request 500'd. `is_postgres` matches the collapsed form, which turns the old silent SQLite fallback into a loud failure — but it still could not connect | `db.target_from_env`; `tests/test_db.py` (3 tests); **cloud shape** shows 200s | ✅ |
| F8 | Image had **no `psycopg2` and no `redis`** | psycopg2 lived only in the `[sync]` extra and `redis` was undeclared, so the image could not open a Postgres registry and the limiter silently fell back to *per-process* counting — on replicas, not a limit at all | new `[cloud]` extra; lock regenerated; `test_packaging.py` pins both in the lock; **install** asserts they import | ✅ |
| F9 | `entrypoint.sh` treated a DSN as a file path | `mkdir -p $(dirname postgresql://…)` created a literal `/app/postgresql:/…` tree and copied the 8 MB seed to a file Postgres never read, while reporting "seed copied successfully" | DSN branch initialises the schema and skips seeding; litter count 0; **cloud shape** shows 12 tables created at boot | ✅ |
| F10 | `mi sync` crashed on any registry with sessions | `sessions` is in `_KEYED` but had no `_PK` entry → `KeyError: 'sessions'`. Empty tables returned early before the lookup, which hid it | `_PK["sessions"]` (+ `pushed_models`); `test_sync.py::test_every_keyed_table_declares_a_primary_key` | ✅ |
| F11 | Cloud-shape compose passed **no provider keys** | The Postgres/Redis stack could only serve 401s and `no_api_key`, so it never exercised the Postgres write path | same passthrough as `deploy/local/docker-compose.yml` | ✅ |
| F12 | **`routing_stats` counted our own infrastructure errors as model losses** | `n = COUNT(*)`, `wins = SUM(ok)`, so a `network_error` (missing CA bundle), `no_api_key` or `auth_error` was a *model* failure. A container without the corporate root CA recorded **32** such losses, dragging down exactly the arms that worked; worst on low-evidence tasks (`hard_reasoning` 3/1 → really 1/1). Fixed in the derived view (excluded from `n`/`wins`, exposed as `n_infra`/`n_all`) — history stays append-only, no backfill | `tests/test_routing_stats.py` (21 tests) on **py** + **pg**; live DB: `general_chat` 38/28 → `n_all=40, n=34, wins=30, n_infra=6` | ✅ |
| F13 | **A TLS failure was indistinguishable from the provider being down** | `httpx.HTTPError` → a flat `network_error`, so a bad CA bundle looked like a model outage and could not be separated from a genuine DNS/connect failure | new `_network_class`: `ssl.SSLError` in the cause chain → `tls_error`; `test_a_certificate_failure_is_a_tls_error` | ✅ |
| F14 | **A gateway error inside an HTTP 200 was read as an empty success** | OpenRouter reports an overloaded upstream as `200 {"error":{…"code":503}}`, including mid-stream as a frame with an empty `choices`. The parser only read `choices[0].message.content`, so it looked like a model that answered nothing: the comparison drew a **blank pane** and the arm was recorded `ok=true` (a false success that also fed the routing stats) | `classify_error_payload` (int/string codes → `5xx`/`429`/…); non-stream `call()` reads the body error first; `_sse_error` wired into the multiplex arms and the single stream, so a failed arm is replaced or surfaced as an error frame | `tests/test_upstream_errors.py` (21 tests), incl. the exact payload from the report; **py** + **pg** | ✅ |
| F15 | **A 200 stream that never emitted `content` completed as a success** | A reasoning model can spend its whole budget on `delta.reasoning` (which `_sse_deltas` ignores) and finish with no text, or an arm can return an empty stream — both rendered blank and counted as a win. The non-stream path already treated this as `empty_content`; the stream paths did not | empty-stream guard on both stream paths, exempting a stream that carried `tool_calls` (`_sse_has_tool_calls`); `test_a_reasoning_only_stream_is_an_empty_answer`, `test_a_tool_call_stream_is_not_an_empty_answer` | ✅ |
| F16 | **A truncated answer was relayed as if complete** | `qwen/qwen3.8-27b` on Groq intermittently emits EOS mid-answer — always after a heading / `:` / unclosed `**` — and reports `finish_reason: "stop"`, so the provider cannot tell us it was cut. MinInfer discarded `finish_reason` entirely, so a half-answer was stored and shown as whole (the user's "answers are getting half complete") | `finish_reason` is captured (stream + `CallResult`); `_looks_truncated` recognises the markdown-boundary shapes and `length`; both stream paths and the non-stream path **continue the same model once** (`_continuation_messages`), summing usage and emitting a single `[DONE]`. `tests/test_truncation.py` (16 tests); live: `### Step` cut → continued to a full answer | ✅ |
| F17 | **The non-stream path silently capped every answer at 1024 tokens** | `Runner`/`try_fallbacks` defaulted `max_tokens=1024` and always sent it, so an API client that did not set one got a truncated long answer with no signal — the streaming path never set the cap, so the two disagreed | no implicit cap; a caller that sets `max_tokens` still gets it; `test_runner_does_not_implicitly_cap_tokens` | ✅ |
| F18 | **The router ranked arms the proxy could not dial** | `available_providers` admitted `google` unconditionally ("first-class on leaderboard"), so `require_callable` never rejected `google:*`. The router put one first, the proxy skipped every one for `no_api_key`, and the **decision log recorded a model that was never called** — which is why "most answers are Qwen": the recorded choice and the answering model disagreed on nearly every `general_chat` request | reachability is now one question in both places (`execute.available_providers` requires a key or a local engine); `tests/test_reachability.py` (4 tests) | ✅ |
| F19 | **Exploration silently did nothing** | The UCB band was computed across *every* eligible arm, so a high-prior **paid** arm (which ranks below every free arm on cost anyway) set the bar so high no free arm qualified. It is now per cost-per-success tier — the tier a candidate actually competes in | `tests/test_exploration.py` (5 tests); live: `sql_generation` and `code_edit` now test near-miss arms, `general_chat` keeps its evidence-backed leader | ✅ |
| F20 | **A free model that started charging kept being used, silently** | The next ingest overwrote `zero_price` and moved on: the router kept choosing the arm on a price it no longer had, and every request cost money. Now a **free → paid transition hibernates** the deployment (`status='hibernated'`, dropped by `_reject`), lists it at `GET /v1/reviews`, and it is resolved with `POST /v1/reviews/decide` (admin) or `mi review`. Free-again auto-clears; an operator's decision survives later ingests | `tests/test_hibernation.py` (9 tests); flow verified end-to-end via CLI | ✅ |

## Wave 2 — P3.4 Server-side transcripts ✅ (this wave's shipped item)

Decision recorded: **add the table, and keep the split.** `sessions` stays a spend
ledger; `messages` is the content half. Two bounds make it safe to keep: only a
*named* session is recorded (a one-shot stores nothing — the same opt-in rule
spend follows), and each session keeps at most `TRANSCRIPT_LIMIT` (200) messages.
Content is **not** mirrored by `mi sync` unless Postgres is the primary, so chat
text stays in the local store of record. Erasing a transcript leaves the spend
ledger alone: the caller may delete what it said, not what it was billed for.

| # | Activity | Validation | State |
|:--|:--|:--|:--|
| 2.1 | `messages` table on both engines, indexed by `(session_id, id)` | `tests/test_transcripts.py` — **py** and **pg**; pinned in `test_sync.py` | ✅ |
| 2.2 | `Store.record_messages` / `session_messages` / `clear_messages` + per-session prune | 6 Store tests (round trip, role/blank filter, truncation, prune, clear) | ✅ |
| 2.3 | Record the turn on non-stream *and* streamed completions; only the newest user message, never the re-sent context | `test_a_non_streamed_turn_is_recorded`, `test_a_streamed_turn_records_the_relayed_answer`, `test_only_the_newest_user_message_is_stored` | ✅ |
| 2.4 | `GET`/`DELETE /v1/session/messages`; 400 without a session | `test_messages_endpoint_requires_a_session`, `test_delete_clears_only_the_transcript` | ✅ |
| 2.5 | One session-key function, so ledger, transcript and envelope agree | `_request_session` used by `_handle`, `/v1/search`, `/v1/session`, `/v1/session/messages`; fixes the `tenant/session` drift | ✅ |
| 2.6 | Chat hydrates from the server when localStorage is empty; "forget chat" clears both | `serverTurns` tests (4) + **web**; silent on failure | ✅ |

## Wave 3 — remaining backlog

## Wave 3 — P3.3 Cost/savings view ✅ (this wave's shipped item)

The definition is the decision, so it is stated here and in the code:

> For a successful call, `saved_usd` = what the **same tokens** would have cost
> at the **cheapest *paid* deployment of the same `weights_id`**, counted only for
> calls that themselves cost nothing. A paid call saves nothing by this measure.
> A model with no paid sibling saves nothing either, and is **counted** as
> `unpriced_free_calls` so the total never reads as complete when it is not.

Prices are the registry's *current* ones, not call-time ones, so the figure is an
estimate and is labelled as one everywhere it appears.

| # | Activity | Validation | State |
|:--|:--|:--|:--|
| 3.1 | `observations.session_id` (+ migration + index), so spend is attributable to a session at all — it previously was not | `test_savings.py::test_the_column_is_added_to_an_existing_registry`; **py**/**pg** | ✅ |
| 3.2 | `Store.spend_savings(session_id=, days=)` — cheapest paid sibling via a correlated `MIN` over the whole `price_in·tin + price_out·tout` expression (not the two prices independently) | 6 Store tests incl. cheapest-sibling, paid-saves-nothing, window, no-token exclusion | ✅ |
| 3.3 | `savings` block on `/v1/session`; `GET /v1/savings` (admin) and `savings` on `/v1/stats` | `test_the_session_envelope_carries_its_own_savings`, `test_savings_endpoint_serves_the_totals`, `test_stats_carries_the_registry_totals`; **phase0** | ✅ |
| 3.4 | Dashboard savings card + chat footer (`· saved $…`), caveat shown with the number | **web** clean | ✅ |

## Wave 4 — P3.1 Installability ✅ (this wave's shipped item)

| # | Activity | Validation | State |
|:--|:--|:--|:--|
| 4.1 | **`pip install .` shipped a broken package**: `pyproject.toml` said `packages = ["mininfer"]`, which includes the top-level modules and *not* `mininfer/web/` or `mininfer/ingest/` | built a wheel before/after: `mininfer/web/legacy.py` and `mininfer/ingest/adapters/*.py` **MISSING → PRESENT**; `tests/test_packaging.py` (3 tests) pins the discovery config | ✅ |
| 4.2 | `requirements.lock` — a universal pin of runtime+server deps, generated with `uv pip compile --universal --python-version 3.11` | `scripts/validate-install.sh` installs exactly the lock into a clean venv and exercises both entry points | ✅ |
| 4.3 | Image builds from the lock (`pip install -r requirements.lock && pip install --no-deps .`) | **docker** — `docker build` 44 s; container healthy, non-root uid 1001, 60 MB, subpackages importable inside the image | ✅ |
| 4.4 | Build context hygiene: `.dockerignore` sent ~340 MB of DB backups to the daemon on every build | context trimmed by `*.db.*` / `*.bak` / `mininfer.db`; build still seeds from `mininfer.db` | ✅ |

**The trap this closed.** The first run of `validate-install.sh` printed "import ok"
for a wheel missing half the code, because pytest/CLI ran from the repo root and
`import mininfer` resolved to the *source tree*. The script now `cd`s to a
neutral directory first, and the guard test reads `pyproject.toml` rather than
importing, so it holds regardless of cwd.

## Wave 5 — Phase 1b: `tenant_id` + `/v1/usage` ✅ (this wave's shipped item)

**Decision: shared learning, tenant-attributed usage.** The router still ranks on
every tenant's observations — quality and price are properties of the deployment,
not the caller, and isolating them would give each new tenant a cold start and
discard the accumulation that makes free-first work. What *is* per-tenant is
spend, headroom and transcripts, and those are scoped. `quota_buckets` stays
keyed by `api_key_alias` (which *upstream* key) — a separate axis, deliberately
not conflated with `tenant_id`.

| # | Activity | Validation | State |
|:--|:--|:--|:--|
| 5.1 | `tenant_id` on `observations`, `decisions`, `sessions` (+ migrations + indexes created *after* the ALTER) | `test_tenant_usage.py::test_tenant_columns_are_added_to_an_existing_registry`; **py**/**pg** | ✅ |
| 5.2 | Attribution threaded from `request.state.tenant` through `try_fallbacks` / `_stream` / `_multiplex_stream` / `_record` / search / approve / verdict | `test_a_session_carries_its_tenant`; **e2e** on **pg** and **py** | ✅ |
| 5.3 | `Store.usage_report(tenant_id=, days=)` — calls, tokens, spend, savings, searches, decisions, `by_task`, `by_deploy` | 4 Store tests incl. the two timestamp shapes and per-tenant savings | ✅ |
| 5.4 | `GET /v1/usage` — a tenant reads **only its own**, even when it asks for another; admin may name one or read all | `test_a_tenant_reads_only_its_own_usage`, `test_an_admin_can_read_one_tenant_or_all`, `test_usage_requires_a_credential_when_auth_is_on` | ✅ |
| 5.5 | The alias-rows that were undeclared: `observations.session_id` in the SQLite DDL too, not only the migration | **py**/**pg** — a fresh registry now matches an upgraded one | ✅ |

**Caught by the window test.** Observations are stamped `T…+00:00`, sessions by
the database as `YYYY-MM-DD HH:MM:SS`. Comparing the two lexically is *silently*
wrong (`' '` sorts before `'T'`), so `_since_sql` produces each table's own shape.
A same-instant test would have passed either way; this one does not.

## Wave 6 — cloud & local variants verified ✅

One codebase, two profiles, both driven from `config/profiles/*.env` and the
compose/manifests under `deploy/`. The point of this wave was to stop trusting
that and actually run both shapes.

| Check | LOCAL | CLOUD (container) | CLOUD shape (Postgres+Redis) |
|:--|:--|:--|:--|
| Access control | off (12/12 open) | on — 401 no cred, 200 tenant key, admin-only surface | on — same |
| `/docs`, `/openapi.json` | 200 | 401 | 401 |
| Rate limit | none (12/12 200) | applied (3 allowed, then 429) | applied via **Redis** (`mininfer:rl:…`) |
| Body cap | none (2 MiB accepted → 502 from routing, not 413) | 413 at 1 MiB | 413 at 1 MiB |
| Registry | seeded SQLite, 1703 deployments | container SQLite | **Postgres**, seeded via `mi sync` |
| Auth off ⇒ tenant | n/a | n/a | `/v1/usage` → whole registry |
| Writes | SQLite | SQLite | **observations / sessions / decisions / messages in Postgres, `tenant=local`** |
| `/v1/usage` | whole registry | — | `calls 1 ok 1 tokens 19 sessions 1 decisions 1 saved $0.000013` |
| Non-root | uid 1001 | uid 1001 | uid 1001 |
| Litter | `/data` = `mininfer.db` only | — | 0 (`F9`) |

Manifests parse: root + both compose files (`docker compose config`), `fly.toml`,
`k8s.yaml` (was 5 docs; now 6 — Postgres/Redis shape, `replicas: 3` + a worker
CronJob, see `CLOUD_ACTIVITY.md` §1.0), `litestream.yml`. Local profile driven
*from the file* on the host (HOST/PORT/limits read out of `local.env`).

End-to-end on the cloud shape: `POST /v1/chat/completions` → **200**, routed to the
free `groq:qwen3.8-27b`, answer `pong`; the transcript (`user`/`assistant`) and the
tenant-attributed accounting rows were then read straight out of Postgres.

## Wave 7 — remaining backlog

| # | Item | Notes / proposed validation | State |
|:--|:--|:--|:--|
| 7.1 | **Product Phase 2 metering/billing** (not GLiNER — see `CLOUD_ACTIVITY.md`) | Depends on 5.x — `/v1/usage` is the aggregation it needs; add Stripe + idempotency keys | [ ] |
| 7.2 | **API versioning contract** | `/v1` pinning + deprecation policy (`PRODUCTIZATION` gap 9) | [ ] |
| 7.3 | **`quota_buckets` per tenant** | Only if the operator runs shared upstream keys across tenants; today the alias is the key's identity | [ ] |
| 7.4 | **CA bundle into the container** | done with the Phase 1.0 cloud manifests: k8s mounts the optional `mininfer-ca` ConfigMap and sets `MI_CA_BUNDLE` on web + worker; Fly expects `/data/ca-bundle.pem`. See `CLOUD_ACTIVITY.md` §1.0c | ✅ **manifests** |

## Wave 8 — cloud topology + Phase 2.0 split → `CLOUD_ACTIVITY.md`

The "one image, two process types" split, shared Redis state, multi-replica
cloud manifests, and the decision to defer the GLiNER2.5 understanding layer to
**Phase 2.0** live in `CLOUD_ACTIVITY.md`, which continues this checklist with
the `cache` / `manifests` / `under` validations. The short version: **Phase 1.0**
(local + cloud deployable, no ML runtime) is the shipping boundary; the
understanding layer is Phase 2.0 and is not built into the image.

---

## Evidence log

### Wave 0 — the lost-charge deadlock

```
FAILED tests/test_session.py::test_both_arms_of_a_comparison_are_charged
assert (1 == 2)                     # two arms, one charged
31.64s call  ...::test_both_arms... # a 30 s stall, then a swallowed exception
```

```
Thread A  mininfer/db.py:185 execute   ← holds _SQLITE_WRITE_LOCK, waits on SQLite
          mininfer/store.py:512 observe
Thread B  mininfer/db.py:184 execute   ← holds the SQLite write txn, waits on the lock
          mininfer/store.py:936 add_session_usage
```

Arm A held SQLite's write lock with an uncommitted `INSERT`; arm B held the
process lock. Neither could move. After retries, `except Exception: pass` dropped
the charge — a **silent billing under-count**, worse than the flaky test.

Fix: `mininfer/db.py` — a write that finds SQLite busy releases
`_SQLITE_WRITE_LOCK`, sleeps *outside* it, and retries (`_retry_locked`), with
`busy_timeout` dropped from 5000 ms to 50 ms so the wait happens outside the
lock. Reads are unchanged.

### Wave 1 — model explorer

```
py  : 451 passed, 1 skipped
pg  : 449 passed, 3 skipped   (validate-postgres.sh: POSTGRES BACKEND VALIDATED)
phase0: 27 passed, 0 failed
web : 85 passed; tsc clean; vite build clean
bench: 42/42 gold answers verify
install: INSTALL VALIDATED (clean venv from requirements.lock; psycopg2 + redis importable)
=== full end-to-end sweep (live server, real provider call) ===
healthz 200 · openapi 200 · / 200 (SPA) · models 200 (36 ids)
explore 200 (total 168 free) · stats.savings 200 · /v1/savings 200
session/messages/DELETE without a session -> 400 (no_session), never a 500

POST /v1/chat/completions  (auto, session e2e-1)
  http 200 in 0.83s -> groq:qwen/qwen3.8-27b, task general_chat
  answer "pong" · usage 17+2 tok · $0.000000
  why: ['cheapest per success', 'free (zero_price)', 'on trial', 'leaderboard: Intelligence Index 40.9']
POST stream=true           (session e2e-2)
  http 200, 4 data frames, [DONE] present
  transcript: [('user','Say the single word: stream'), ('assistant','stream')]

ledger     e2e-1 calls=1 tokens=19 cost=$0.000000 saved=$0.000013
ledger     e2e-2 calls=1 tokens=20 cost=$0.000000 saved=$0.000014
decisions  both rows recorded with their `why` tags
server log no tracebacks, no errors
```

Real-registry smoke (`mininfer.db`, 1,703 deployments): default page 5 rows,
`free_only` 168, `capability=tools` 1,026, all queries ≤ 21 ms for the batch.

New/changed:
- `mininfer/store.py` — `explore_models`, `_capability_facets`, portable `push_model`, transcript methods, `spend_savings`, `usage_report`, `observations.session_id`/`tenant_id`
- `mininfer/proxy.py` — `GET /v1/models/explore`; `_request_session` / `_caller_tenant`; transcript endpoints + recording; `/v1/savings`; `/v1/usage`; savings in the session envelope and `_stats_data`
- `mininfer/schema.py` — `CAP_KEYS` (moved here from `agent_ingest`)
- `mininfer/router.py` — deterministic `build_candidates` order
- `mininfer/auth.py` — `/v1/savings` on the admin surface
- `supabase/schema.sql` — `pushed_models`, `messages`, `observations.session_id`/`tenant_id`, `decisions.tenant_id`, `sessions.tenant_id`
- `tests/test_explore.py`, `tests/test_transcripts.py`, `tests/test_savings.py`, `tests/test_tenant_usage.py`, `tests/test_sqlite_concurrency.py`, `tests/conftest.py`, `tests/test_sync.py`
- `PRODUCTIZATION.md` — endpoint list, gap table, Phase 1b marked shipped with the decision
- `mininfer/db.py` — `target_from_env` (a DSN is never coerced to a `Path`); `mininfer/cli.py`, `mininfer/proxy.py` — use it for `MI_DB`
- `mininfer/sync.py` — `_PK` for `sessions` and `pushed_models`
- `docker/entrypoint.sh` — a DSN initialises the schema instead of seeding a file
- `deploy/local/docker-compose.postgres.yml` — provider-key passthrough
- `pyproject.toml` — `[cloud]` extra (`psycopg2-binary`, `redis`); `requirements.lock` regenerated with it
- `tests/test_db.py`, `tests/test_sync.py`, `tests/test_packaging.py` — regression guards for the four bugs
- `web/src/lib/explore.ts` (+ test), `web/src/components/ModelsTab.tsx`, `web/src/App.tsx`
- `web/src/lib/chat.ts` — `serverTurns` (+ tests), `web/src/components/ui/chat-02.tsx` — hydration + savings footer
- `web/src/components/Overview.tsx` — savings card
- `pyproject.toml` — `packages.find` (was a bare list that omitted subpackages), `requirements.lock`
- `Dockerfile` — installs from the lock, package with `--no-deps`
- `scripts/validate-install.sh`, `tests/test_packaging.py`, `.dockerignore`

### Wave 2 — transcripts

The turn is stored in the *same transaction* as its accounting commit, so a
transcript can never record an answer the caller was not charged for (or the
reverse). Compare mode stores only the question: there is no single answer until
`/v1/approve`, and recording a guess would put an unchosen answer in the thread.

`GET /v1/session/messages`:

```json
{"session_id": "s-1", "messages": [
  {"id": 1, "role": "user",      "content": "hi",         "deploy_id": null},
  {"id": 2, "role": "assistant", "content": "the answer", "deploy_id": "openrouter:m"}
]}
```

### Wave 3 — savings

Real-registry smoke (`mininfer.db`, 82 successful calls, on a copy):

```
spend   $0.000368
avoided $0.048893   (75 free calls)
unpriced free calls: 39   (no paid sibling — the total is a floor, and says so)
```

The `MIN` is over `price_in·tin + price_out·tout` as one expression, not over the
two prices separately: the cheapest input price and the cheapest output price can
belong to two different siblings, and picking them independently would price a
deployment that does not exist.

### Wave 4 — image + install

`docker build -t mininfer:verify .` — 44 s, 60 MB, node build stage + python
runtime. Then `docker run -p 8902:8000`:

```
identity     mininfer uid=1001            (non-root, as the Dockerfile promises)
healthcheck  healthy                      (the Dockerfile's own HEALTHCHECK)
/healthz 200 · / 200 (SPA) · /v1/models 200 (36) · /v1/models/explore 200
/v1/savings 200 · /v1/session (none) 400 no_session
subpackages  import mininfer.web.legacy, mininfer.ingest.adapters → OK   (F6, in-image)
```

Same image with the cloud profile (`MI_API_KEYS`, `MI_ADMIN_TOKEN`, `MI_RATE_LIMIT_RPM=3`):

```
/healthz public 200 · /v1/models no cred 401 · tenant key 200 · bad key 401
/ admin: no cred 401 · Basic (token as password) 200 · WWW-Authenticate: Basic realm="mininfer"
/v1/stats tenant key 401 · admin token 200
/v1/savings tenant key 401 · admin token 200        (the new route is admin-only)
rate limit: 200 200 429 429 · Retry-After: 45
401 body is the OpenAI shape: {"error":{..."code":"invalid_api_key"}}
```

`.dockerignore` was sending ~340 MB of registry backups to the daemon on every
build; `*.db.*` / `*.bak` / `mininfer.db` now exclude them while `mininfer.db`
still reaches `COPY mininfer.db /app/seed.db`.

### Wave 5 — tenancy

```
GET /v1/usage   (tenant key sk-a:acme)        -> {"tenant_id":"acme","calls":1,...}
GET /v1/usage?tenant=beta  (same acme key)    -> {"tenant_id":"acme",...}   ignored
GET /v1/usage?tenant=beta  (admin token)      -> {"tenant_id":"beta",...}
GET /v1/usage              (admin token)      -> {"tenant_id":null,"calls":2,...}
GET /v1/usage              (no credential)   -> 401
```

Attribution is written by production code, not asserted from a fixture: the
tests drive the real `try_fallbacks`, so the `observations`, `decisions` and
`sessions` rows a request produces all carry the same tenant.

### Wave 6 — the variants, and the four bugs they exposed

Every cloud failure below was invisible on the host because the host tests build
`Store(dsn)` directly and never go through `_default_db()`, the entrypoint, or
the image's dependency set. Running the actual variant is what found them.

```
MI_DB=postgresql://… , served by the proxy:
  before: psycopg2.ProgrammingError: invalid dsn: missing "=" after
          "postgresql:/mininfer:mininfer@postgres:5432/mininfer"
  after : POST /v1/chat/completions 200 in 0.68s -> groq:qwen/qwen3.8-27b "pong"
          observations: 1 for tenant=local (212 total)
          sessions:     1 for tenant=local
          decisions:    1 for tenant=local
          transcript:   user: Reply with exactly: pong / assistant: pong
          /v1/usage:    tenant local calls 1 ok 1 tokens 19 sessions 1 decisions 1

entrypoint with a DSN:
  before: ==> No database found at postgresql://… Seeding catalog…
          ==> Seed database copied successfully (8028160 bytes).   (into /app/postgresql:/…)
  after : ==> Registry is Postgres; initialising the schema (idempotent)…
          postgres tables: 12 · DSN-as-path litter: 0

image deps:  import psycopg2 -> ModuleNotFoundError   (now: OK)
             import redis    -> ModuleNotFoundError   (now: OK)
mi sync:     KeyError: 'sessions'                      (now: 9 tables synced)
```

Local variant, for contrast (driven from `config/profiles/local.env`):

```
12/12 open · no 413 on a 2 MiB body · /docs 200 · 1703 deployments · /data = mininfer.db only
```
