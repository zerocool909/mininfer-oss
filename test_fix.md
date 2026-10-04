# test_fix.md — bugs found and fixed

A log of every defect found while hardening MinInfer for a wider audience, in the
order they were numbered. Same convention as the repo's other evidence logs: an
entry is listed only once its fix is **validated** — the "pinned by" column names
the test that fails if the bug returns.

Two things this file is for:

1. **Not repeating history.** Several of these are the same mistake in different
   clothes (a value arriving by one route and being read from another). Reading
   the list is faster than rediscovering the pattern.
2. **Being honest about how they were found.** Most were *not* caught by a test;
   they were found by running the thing. That is the argument for the coverage
   work in `TESTING_PRD.md`, and the "found by" line says which layer caught each.

| Found by | Bug numbers | Count |
|---|---|---|
| Existing suites (pytest / tsc / CI) | 1, 2, 3, 6, 10, 11, 12 | 7 |
| Running a fresh clone / container by hand | 4, 5, 7, 8, 9, 13, 16, 17, 19, 24, 27 | 11 |
| **Clicking the dashboard** | 15, 18, 20, 21, 22, 23, 25 | **7** |
| Writing a new test (the fix itself) | 26, 28 | 2 |
| Browser E2E (first run) | 14 | 1 |

The seven in the third row are the argument for `TESTING_PRD.md`: every one was a
user-visible failure that 926 Python tests, 101 frontend tests and four CI jobs
all passed.

---

## Engine & storage

### #1 — Postgres: a SQLite-only function aborted the transaction

**Symptom.** On the Postgres backend every routing request failed. 120 suite
tests failed, all with `InFailedSqlTransaction`, none naming the real cause.

**Root cause.** `store.get_pushed_models` purged stale rows with
`datetime(ts) < datetime('now', ?)`. Postgres has no `datetime()`; the failing
statement **aborts the transaction**, and the `except: pass` around it hid the
error — so every later query in the same request died. `router.route()` calls
this on the hot path, so *routing itself* was broken on Postgres.

**Fix.** Compare against an ISO cutoff in the format `_since` documents, computed
in Python where both engines agree.

**Pinned by** `tests/test_variants.py`, `tests/test_session.py` on Postgres ·
**Commit** `d3a572d`

### #2 — Ordering was not a total order, so the two engines picked different arms

**Symptom.** With two identically-priced arms, SQLite and Postgres selected a
*different* one first; `rank_for_compare` takes `ranked[0]`, so a comparison could
order its arms differently per engine.

**Root cause.** `router._sort_key` ended at
`(cost_per_success, -effective_p, latency_ms)`. A tie fell back to the stable
sort's input order — which comes from a `SELECT` with no `ORDER BY`, i.e.
insertion order on SQLite and planner order on Postgres.

**Fix.** `deploy_id` is the final tie-break in every branch, making the order
total and the result reproducible.

**Pinned by** `tests/test_stream_failures.py` on Postgres ·
**Commit** `d3a572d`

### #3 — The Postgres validation script hid its own failures

**Symptom.** `validate-postgres.sh` reported success logic while 120 tests were
failing.

**Root cause.** The run ended in `| tail -2`, so only the last two lines survived —
and a stale claim in `PRODUCTIZATION.md` ("379 passed on Postgres") had gone
unchecked for several releases.

**Fix.** Capture the log, print the failing test names on failure, and correct the
claim. The script is now honest about being wrong.

**Pinned by** the script itself, exercised by CI · **Commit** `d3a572d`

---

## Fresh clone & packaging

### #4 — The Docker image could not be built from a clean checkout

**Symptom.** `docker build` failed with
`failed to compute cache key: "/mininfer.db": not found`.

**Root cause.** The Dockerfile had `COPY mininfer.db /app/seed.db`, and
`mininfer.db` is gitignored — it is *derived*, not committed. It existed on the
maintainer's machine and nowhere else.

**Fix.** The engine creates an empty schema seed at build time. Baking a
catalogue was the anti-pattern the docs already warned about.

**Pinned by** the CI `docker` job (builds from a clone) · **Commit** `d3a572d`

### #5 — `.dockerignore` excluded the file the Dockerfile copies

**Symptom.** After fixing #4, the build still failed.

**Root cause.** `.dockerignore` carried the comment *"Only `mininfer.db` is
needed"* immediately above a bare `mininfer.db` pattern that removed it. The
comment and the rule had drifted apart; nothing failed until a build ran.

**Fix.** Exclude the registry (nothing needs it now) and keep the comment true.

**Pinned by** the CI `docker` job · **Commit** `d3a572d`

### #6 — A test failed without a frontend build

**Symptom.** `pytest` failed on a fresh clone until `npm run build` had been run.

**Root cause.** `test_no_route_is_public_by_accident` asserted the public route
set as an exact list including `/assets` — which only exists once `web/dist` is
built. The *app* deliberately degrades without a build; the test did not.

**Fix.** Assert the invariant instead: `/healthz` and the favicon are always
public, and nothing else may be.

**Pinned by** `tests/test_auth.py`, with and without `web/dist` · **Commit** `d3a572d`

### #7 — Build artifacts were one `git add` from being committed

**Symptom.** `python -m build` left `build/` and `dist/` (60+ files) staged.

**Fix.** Gitignored both.

**Pinned by** `git status` / the CI guard · **Commit** `d3a572d`

### #8 — The README referenced four files that did not exist

**Symptom.** `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and `SECURITY.md` were
linked from the README and absent; `LICENSE` was absent while the README said MIT.

**Fix.** Wrote real ones. `SECURITY.md` documents the opt-in auth default, the
admin surface, and the `/v1/local/probe` SSRF surface.

**Pinned by** review · **Commit** `d3a572d`, `b3e540d`

### #9 — The documented install cannot run the proxy

**Symptom.** `pip install -e .` then `mi proxy` fails: FastAPI and Uvicorn are in
the `[server]` extra.

**Fix.** The Quick Start says `pip install -e '.[server]'`. The CLI's own error
was already good (`pip install '.[server]'`) — the docs were the gap.

**Pinned by** the CI `acceptance` job, which installs `[server]` · **Commit** `d3a572d`

### #10 — A missing optional extra aborted the entire test run

**Symptom.** On a base install, `pytest` collected 0 tests and exited with
`2 errors`.

**Root cause.** `test_agent_ingest.py` / `test_agent_resolve.py` import `bs4` /
`langgraph` at module level. A missing extra during *collection* is a hard error,
so the other 800+ tests never ran.

**Fix.** `pytest.importorskip`, and CONTRIBUTING documents which extra gates what.

**Pinned by** a clean-clone run in CI · **Commits** `22eb04b`, `2f12f62`, `b3e540d`

### #11 — Five more tests coupled to the `[agents]` extra

**Symptom.** After #10, `test_understanding.py` failed (5 tests) on a base install.

**Fix.** Same `importorskip` guard on exactly those five.

**Pinned by** a clean-clone run · **Commit** `2f12f62`

---

## Frontend

### #12 — `npm run typecheck` failed

**Symptom.** `error TS2367: This comparison appears to be unintentional because
the types '"error" | "end" | "arm" | "delta" | undefined' and '"route"' have no
overlap.`

**Root cause.** The proxy emits `{"mi":{"event":"route",...}}` as the first frame
of a stream; `MiFrame.event` did not include `'route'`. `npm run build` never
noticed, because Vite strips types.

**Fix.** Added `'route'` plus the fields it carries (`deploy`, `candidates`,
`decision_id`).

**Pinned by** `npm run typecheck` (its own CI step) · **Commit** `d3a572d`

### #13 — The Overview footer claimed a hardcoded model count

**Symptom.** The footer read `23,799 models indexed • Local SQLite registry`
regardless of the registry — including when it was empty, and on Postgres.

**Root cause.** A literal string in `App.tsx`, sitting directly beneath a counts
grid that *was* live.

**Fix.** Read the counts from `/v1/stats`; drop the engine claim.

**Pinned by** `e2e/overview.spec.ts` (branch `testing/e2e-phase1`) · **Commit** `82d2cac`

### #14 — The key diagnostic was fetched, then dropped before rendering

**Symptom.** The Providers result panel always read `Key: none configured`, even
when the API had returned `"custom"` or `"env"`.

**Root cause.** `handleTestProvider` builds the result object field by field for
React state, and `key_source` was not among them. The type and the render were
both correct; the middle step lost it. This is why the diagnostic added to explain
*"Missing Authentication header"* never once reached a screen.

**Fix.** Pass `key_source` through.

**Pinned by** `e2e/providers.spec.ts` on branch `testing/e2e-phase1`
(**found on its first run**) · **Commit** `main`

---

## Compose & container

### #15 — Every provider key reached the container empty

**Symptom.** `Missing Authentication header` from OpenRouter, with a valid key in
`.env`.

**Root cause.** The service listed `- OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}`.
An `environment:` entry takes precedence over `env_file:`, so whenever the shell
had not exported the variable the explicit **empty string** won over the file.

**Fix.** Remove the credential lines from `environment:`; supply them via
`env_file`.

**Pinned by** `docker compose config` in `validate-variants.sh` · **Commit** `79b22ca`

### #16 — `-f deploy/local/docker-compose.yml` changed where `.env` is read

**Symptom.** Same as #15, but only with `-f`. The repo-root invocation worked.

**Root cause.** `-f` makes the **project directory** `deploy/local/`, so Compose
looked for `deploy/local/.env` and never saw the repo-root one.

**Fix.** Declare `env_file` with a path relative to the compose file
(`../../.env`, `required: false`), which resolves identically however Compose is
invoked and keeps a bare checkout working.

**Pinned by** both invocations in `validate-variants.sh` · **Commit** `79b22ca`

### #17 — A duplicated `MI_CA_BUNDLE` key in the worker service

**Symptom.** None visible — the second value silently won.

**Fix.** Deleted the duplicate.

**Pinned by** `docker compose config` · **Commit** `77076a0`

### #18 — A fresh volume produced an empty dashboard

**Symptom.** `docker compose up` on a new volume: every Overview panel read zero.
This was a regression introduced by the fix for #4.

**Root cause.** Removing the baked catalogue was right; nothing replaced the
first-run experience.

**Fix.** The entrypoint ingests once when the registry is empty, then starts the
server. `MI_BOOTSTRAP=0` opts out.

**Pinned by** `validate-variants.sh`, CI `docker` job · **Commit** `77076a0`

### #19 — The healthcheck failed while the bootstrap was ingesting

**Fix.** `start_period` 5s → 180s. A container that is ingesting is not unhealthy.

**Commit** `77076a0`

---

## Keys & auth

### #20 — A blank key was treated as a credential

**Symptom.** `Missing Authentication header` while a key was visibly saved.

**Root cause.** `resolve_endpoint` used `api_key if api_key is not None else
<env>`. A browser's stored keys can arrive as `{"openrouter": ""}`; that counted
as an explicit override, produced no `Authorization` header at all, and silently
shadowed the configured key. The provider's message reads as *bad* key when it
means *no* key.

**Fix.** `None`, `""` and whitespace all normalise to "no key" and fall back to
the provider's variable. A real key still wins.

**Pinned by** `tests/test_proxy_keys.py` · **Commit** `659c238`

### #21 — The provider Test read the key from the wrong place

**Symptom.** A key showing as **"Custom Key Active"** was invisible to the Test
button.

**Root cause.** `/v1/providers/test` read `payload["api_key"]` only. The dashboard
sends saved keys as `X-User-API-Keys` — the header the chat path reads in three
places. The test ignored it.

**Fix.** Fall back to that header; the body still wins when present.

**Pinned by** `tests/test_provider_probe.py` · **Commit** `d2d2a10`

### #22 — The Test invented a model named `test`

**Symptom.** `Connectivity failed — Model: test — The model 'test' does not exist`.

**Root cause.** When a provider had no deployment in the registry the handler
fabricated `{"deploy_id": "{provider}:test", "provider_model_id": "test"}` and ran
a chat completion against it. No provider serves a model called `test`, so the
answer was always a 404 — and it said nothing about the key or the network.

**Fix.** Probe the provider's own `GET /models` instead, which is what "test
connectivity" promises. No key → an actionable message naming the variable and
`mi refresh`; bad key → the provider's real 401.

**Pinned by** `tests/test_provider_probe.py` · **Commit** `fdd6249`

### #23 — A rate-limited free arm was reported as a failure

**Symptom.** `Connectivity failed — 429 — Provider returned error` for a provider
that was working.

**Root cause.** The endpoint called exactly one arm. A `429` proves connectivity —
the request authenticated and the provider answered — but it was treated as a
failure.

**Fix.** Walk up to four arms, stopping at the first that is not a 429. Any other
error still stops immediately.

**Pinned by** `tests/test_provider_probe.py` · **Commit** `67b4e89`

### #24 — The Test preferred a stale saved key over the one being typed

**Symptom.** Paste a fresh key, press Test, and be told about the *old* key's
failure.

**Root cause.** `userKeys[providerId] || keyInputs[providerId]` — the saved value
was checked first, so the more recent intent lost.

**Fix.** The typed value wins.

**Pinned by** `e2e/providers.spec.ts` (branch `testing/e2e-phase1`) · **Commit** `e4f4e9b`

---

## Routing & catalogue

### #25 — A fallback chain could be three arms of one gateway

**Symptom.** An expired OpenRouter key failed the whole request while a working
Groq key sat unused in the same candidate list.

**Root cause.** `require_distinct_provider` relaxes in passes, and the second pass
was "distinct upstream". That is no separation when a gateway aggregates many
upstreams: with fewer than `top_k` gateways eligible — the normal free-tier case —
it returned every arm from the *same* gateway. The funnel even reported
`fallback separation achieved: upstream`, which was true and useless.

**Fix.** The relaxation round-robins across gateways, repeating one only once every
gateway has an arm: `[openrouter, vercel, openrouter]`.

**Pinned by** `tests/test_router.py` · **Commit** `c1d6ea4`

### #26 — Bringing a provider key did not widen the catalogue

**Symptom.** Export `GROQ_API_KEY`, run `mi ingest`, and no Groq model appears —
so the router can never rank it.

**Root cause.** `_ingest_all`'s default sweep filtered on `tier == 0`,
contradicting its own docstring ("every available source") and ignoring
`SourceSpec.available`, which is already key-aware.

**Fix.** The default follows `available`: tier 0 plus any keyed source whose key
is set. Tier-2 local runtimes stay out, so a scheduled refresh does not log a
failure for a runtime that is not running.

**Pinned by** `tests/test_ingest.py` · **Commit** `03c41d2`

---

## TLS & trust

### #27 — Only `MI_CA_BUNDLE` was honoured, on macOS only

**Symptom.** "It worked with curl" was untrue: `SSL_CERT_FILE`,
`REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` had no effect, and the helper script
failed on Linux and printed a bogus path for an absolute argument.

**Root cause.** `fetch._verify()` read `MI_CA_BUNDLE` alone; `make_ca_bundle.sh`
used `security` and `/etc/ssl/cert.pem` unconditionally.

**Fix.** `_verify` honours the standard variables (first existing path wins);
the script is cross-platform, folds in whatever those variables already point at,
and fails loudly on an empty bundle.

**Pinned by** `tests/test_fetch.py` · **Commit** `4307a6d`

---

## Judge

### #28 — `BEST` could name a candidate the judge had failed

**Symptom.** Found by the new test, before release: a batched verdict could pick a
failing candidate as the winner.

**Root cause.** The `BEST:` line was accepted whatever its verdict, and the system
prompt explicitly asked for "the least-bad one" when all failed.

**Fix.** `BEST` must name a candidate marked PASS, or be `none` — "choose the best
performing model" cannot mean "choose one that failed". The prompt says so too.

**Pinned by** `tests/test_judge.py` · **Commit** `35126db`

---

## The pattern, in one line

`#6`, `#13`, `#14`, `#20`, `#21`, `#24` are all the same mistake: **a value
arrives by several routes and each reader looks at a different one** (the route
set, the registry count, the body vs the header, the saved vs the typed key). The
durable fix for the class is the one in `TESTING_PRD.md` §5 — drive the real UI
against the real API and assert the value that reaches the screen.
