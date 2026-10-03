# BACKLOG

Ordered by **value per day of work**, not by how satisfying the work is. Everything
here has been verified against the tree rather than inferred from a plan document;
where a claim came from the external architecture review at
`~/.gemini/antigravity/brain/.../implementation_plan.md`, the correction is noted.

**Store of record is SQLite.** No service dependency, no network required to answer
"why did the router pick that". Everything fetched is snapshotted to `raw/` *and*
recorded in the `snapshots` table. The one hosted thing in the tree is
`mi/sync.py`, which is opt-in and mirrors to Postgres — it is not on the critical
path and E4 below is a decision, not a task.

---

## Done

| # | Item | Why it mattered |
|:--|:--|:--|
| ✅ | **HTML snapshots were never cached** (`mi/fetch.py`) | `_find_recent` globbed `*.json`, so every `fetch_raw(ext="html")` snapshot was written and then never found again — a permanent cache miss that re-fetched on every call and silently degraded the metrics scraper and the ingestion agents, both HTML. Fix: glob any extension (the directory is already keyed by source + URL hash). 7 tests, negative-controlled. |
| ✅ | **`general_chat` + the default task** (`config/policy.yaml`, `mi/router.py`) | "Hello, how are you?" routed as `code_edit` — 32K context, 0.55 quality floor, `tools: true` required — with `source=default confidence=0.00`, i.e. *nothing* matched. New catch-all task with cues, and `DEFAULT_TASK` moved to `router.py` so it is no longer spelled out twice with the same default. |
| ✅ | **Search spend was outside every budget** (`mi/store.py`, `mi/proxy.py`) | The 1M cap was a *token* budget and a search has no tokens, so it bounded the free half of a request and was blind to the only line item that costs money (~$0.008/search vs $0.0000 for a free arm). `sessions` gained `searches` + `search_cost_usd` (with a migration), and a second budget: `session_cost_limit` in USD. `POST /v1/search` requires a session and reserves the paid tier's rate. |
| ✅ | **Search snapshots were not in SQLite** | `mi/search.py` wrote to `raw/` but never called `record_snapshot`, so the registry could not answer "what has this fetched". Now recorded (idempotent on sha256), including refusals — a bot wall is evidence. |
| ✅ | Compare mode runs both arms concurrently over one multiplexed SSE stream | Was serial, so `mi_options: 2` cost the sum of both calls. Concurrency proven with a two-party barrier, negative-controlled. |
| ✅ | Vendor-diverse compare options | Every OpenRouter row shares `upstream="openrouter"`, so the relaxation pass only compared weights — `z-ai/glm-5.3-prime` vs `z-ai/glm-5.3-air` was a legal "choice". |
| ✅ | `why` tags on decisions | The decision log now explains itself (`cheapest per success`, `on trial 0 of 3`, `leaderboard: Coding Index 68.1`), stored *with* the decision so history stays honest after the registry changes. |
| ✅ | Streamed calls recorded zero tokens and zero cost | The relay forwarded bytes and nothing else, so every streamed request contributed 0 to `routing_stats` — the dashboard's spend was structurally $0. |
| ✅ | Session token ledger + 1M cap | Reserve-based, not tripwire-based. Charges every billed attempt, both arms of a comparison, and the streaming path. |
| ✅ | Markdown rendering, per-answer timing, needs-approval prompt | `react-markdown` + GFM; the legacy page has an escape-first subset renderer. |
| ✅ | **Multi-turn was broken: the chat sent only the newest message** (`web/src/lib/chat.ts`) | A follow-up arrived with nothing to follow up on — "and the other one?" had no "other one" in it. `buildMessages()` now sends the last 5 turns, answers clipped to 1200 chars, older turns elided behind one system line that names the subject. Compacted rather than replayed: assistant answers are the token hogs and an unbounded history walks into the context window and the session budget. |
| ✅ | **Stop button** | `AbortController`, replaced per request so Stop can never cancel the wrong one. A stopped request keeps whatever text arrived and is marked `aborted` — distinct from a failure, because the user pressing Stop is not a fault. |
| ✅ | **Request log in the chat window** | The transcript used to *delete* the assistant turn on failure, so the only trace was a line under the composer that the next attempt overwrote. A failed turn now stays, carries the router's own error type (`session_budget_exceeded`, `no_api_key`, …), a timestamp, and its task — and the footer counts them. |
| ✅ | **Chat history** (`localStorage`, per session) | Threads survive a refresh, with a history menu and New chat. Capped at 20 sessions / 200 turns; every storage access is guarded, because private mode and a full quota both throw and a chat that refuses to render is worse than one that forgets. |
| ✅ | **The frontend has a test runner, and the SSE parsing is no longer duplicated** (`web/src/lib/sse.ts`) | The frame splitting lived inline in *two* places (`streamInto`, `compareInto`) which is exactly why it was untestable — and untested parsing does not crash, it silently finds no frames. Extracted to pure functions with 21 tests. `vitest` + jsdom added (`npm test`), and the stopgap `check-chat.mjs` deleted rather than left to drift. |
| ✅ | **Capability gating was inert** (`mi/schema.py`, `mi/router.py`) | `caps.tools` is null for 22,655 of 23,799 deployments and the policy was `allow`, so `require: {tools: true}` did nothing and a tool task routed to models nobody had confirmed support tools — failing silently. Now per-task (`TaskProfile.on_unverified_capability`), because the cost of the strict answer is 5 arms for `tools`/`structured` and 108 of 161 for `vision`. |
| ✅ | The chat now sends `X-MI-Session` | Without this the server-side budget existed but never applied to the thing that spends. The tally shown in the footer is read back from `/v1/session`, so the UI cannot disagree with the number the cap enforces. |

---

## P1 — do next

### P1.1 ✅ Capability gating, per task

`caps.tools` is null for **22,655 of 23,799** deployments, and the policy default was
`allow`, so `require: {tools: true}` was inert: a tool-requiring task routed happily
to a model nobody had ever confirmed supports tools, where it fails silently — no
error, the call simply does not do the thing.

The plan's fix was a global flip to `reject`. Measured, that is the wrong shape:

| task | requires | eligible (allow → reject) | cost of `reject` |
|:--|:--|:--|:--|
| `code_edit` | `tools` | 212 → 207 | **5 arms** |
| `agent_tools` | `tools` | 193 → 188 | **5 arms** |
| `sql_generation` | `structured` | 178 → 173 | **5 arms** |
| `extraction` | `structured` | 165 → 160 | **5 arms** |
| `shelf_image_audit` | `vision`, `structured` | 161 → **53** | **108 of 161** |

…and the chosen model was unchanged in every one of those tasks. So `reject` is
nearly free where a missing capability fails *silently* and expensive where the
answer is visible to whoever asked. `TaskProfile.on_unverified_capability` now
overrides the policy, set to `reject` on the four silent-failure tasks and left
inheriting on `shelf_image_audit`. Guarded by a config test, because this reverts
invisibly.

### P1.2 ✅ Search tier order — decided

The shipped order is already `wikipedia → duckduckgo → tavily`, which is the right
one now that DuckDuckGo's bot wall is measured: the *supported* free tier is tried
first, DuckDuckGo is best-effort, and Tavily is the paid fallback. Dropping DDG
would remove a tier that sometimes works for free and costs only one request when
it does not — and it reports itself blocked rather than claiming "no results", so
the failure is legible. **No change; recorded so it is not re-litigated.** If DDG's
block rate becomes the common case, it comes out of `FREE_PROVIDERS`.

### P1.3 ✅ Where search cost surfaces — decided

**Not in `cost_per_success`.** At $0.008 a search against arms that cost $0.0000 per
success, folding it in would make the metric describe search rather than models —
the router would start preferring worse models because they were paired with fewer
searches. It is attributed to the **session** (in dollars, via `sessions.search_cost_usd`,
with its own budget) and will be attributed per request when the tool loop lands.
The remaining gap is a *view*, not a decision: the per-task and per-period rollup is
P3.3.

---

## P2 — structural ✅ complete

### P2.1 ✅ `Store` owns the schema

Raw SQL outside `store.py` went from **10 modules / 18 call sites to zero**. Ten
named methods replace it: `deployment_prices`, `deployment_perf`,
`set_deployment_perf`, `weights_exists`, `weights_meta`, `weights_with_benchmarks`,
`all_benchmarks`, `providers_summary`, `recent_decisions`, `decisions_with_intent`,
`quota_rows`, `deploy_ids_for`, `table_rows`.

One of them was a **verbatim duplicate** — the provider rollup existed in both the
dashboard endpoint and `mi stats`, so one schema change was two edits waiting to
disagree. `_SKIP_COLS` also moved from `sync.py` to `store.py`, because "surrogate
ids are not copied" is a schema fact rather than a sync one.

Enforced by `tests/test_encapsulation.py`, which reads the source and fails with
the file and line for any `.conn.execute` outside `store.py`. A test rather than a
private `_conn`: Python privacy is advisory, so renaming would only have hidden the
leak, and it would have meant rewriting ~30 test call sites for no behavioural gain.
The guard is itself guarded — one test proves it notices a leak in a fixture.

### P2.2 ✅ The `router ↔ bandit` cycle is gone, not hidden

It was worked around twice — `bandit.py` guarded `from .router import Candidate`
behind `TYPE_CHECKING`, and `router.route()` imported `bandit_order` inside the
function. Both worked; both hid the cycle from a module-level import graph (an
earlier knowledge-graph pass reported "no import cycles" for exactly that reason).

`Candidate` moved to `schema.py`, which imports nothing from `mi`. That made the
dependency one-way, so both workarounds were deleted rather than kept:

```
import mi.bandit  ->  pulls in mi.schema only;  mi.router NOT loaded
import mi.router  ->  pulls in mi.bandit (one direction is fine)
deferred imports remaining in router.py: 0
```

Guarded by `tests/test_imports.py`, which asserts the direction in a **subprocess**
— an import order cannot be tested in-process because pytest has already imported
half the package by then. Negative-controlled: reintroducing the cycle fails it.

Also dropped `Candidate` from `cli.py`, where it had been an unused import since
the CLI stopped needing it. Python has no unused-import check, which is how that
survives.

### P2.3 ✅ `proxy.py` split — 1,951 → 1,252 lines

`mi/web/assets.py` (613 lines: the stylesheet and script) and `mi/web/legacy.py`
(186 lines: the page and its row rendering).

The template is now a **pure function** — `render_dashboard(counts=…, providers=…,
quota=…, decisions=…, tasks=…, task=…, routed=…)` — and the endpoint keeps only the
part that touches the store and the router. Extracted by script rather than retyped,
so 606 lines of strings could not be silently altered.

`streaming.py` and `routes.py` were **not** split out: 1,252 lines with the UI gone
is manageable, and those two would be churn for its own sake. Revisit when
`proxy.py` next needs a real change.

### P2.4 ✅ `ingest.py` split, and tested first

814 lines and 14 adapters behind **2 test functions**. So the tests came first —
`tests/test_ingest.py` went from 2 to 41 — and then the package:

```
mi/ingest/types.py       Bundle · Run · SourceSpec (a leaf, so adapters can import them)
mi/ingest/shared.py      the registry, price guards, row builders
mi/ingest/catalogue.py   the 28 sources, in tier order
mi/ingest/adapters/      14 providers + _template.py
```

The cross-module helpers lost their underscores (`deploy` → `make_deploy`,
matching the existing `make_deploy_id`) because they are a public surface now, and
each adapter imports only what it uses rather than the whole toolkit.

**Writing the tests first paid for itself twice.** They found that the catalogue has
**28** sources rather than the ~21 I had assumed, and that a wrong-typed payload
makes 13 of 14 adapters raise — which is *fine*, because `cmd_ingest` contains a
per-source failure, so the tests now pin that containment instead of a property the
code deliberately does not have. A third test asserts no adapter invents rows from
a payload it cannot read, since a fabricated row is believed forever.

`_template.py` documents the checklist, and `test_the_template_registers_nothing`
stops it being half-adopted into a fake `example` source.

---

## P3 — product and distribution

- **P3.1 Installability.** No PyPI package, no lockfile, no Docker image.
  Note the review's Dockerfile is broken twice: `python:3.12-slim` has no npm, and
  `mi proxy` defaults to `--host 127.0.0.1` (`cli.py:858`), so the published port
  would be unreachable. Needs a node build stage and `--host 0.0.0.0`.
- **P3.2 Auth and rate limiting on the proxy.** It is a local dev tool today; the
  moment it is exposed, `/v1/search` spends money and nothing authenticates it.
- **P3.3 Session cost/savings view.** "You spent $Z and avoided $Y by using free
  tiers." The data exists (`observations.cost_usd`, `sessions`); it needs an
  aggregation endpoint and a dashboard section.
- **P3.4 Server-side transcripts.** Chat history is client-side (`localStorage`) and
  `sessions` is a *spend* ledger, not a transcript. That means history is per-browser
  and lost on a cache clear, while the spend it caused is permanent. Either accept
  that split or add a `messages` table.
- **P3.5 Model explorer.** Browse the registry: filter by provider, capability,
  price; see success rates and benchmarks per model.

---

## P4 — decided against, for now

- **Tako Search (`vercel/tako/search`) as a search tier.** Investigated and
  declined in favour of the original plan. It is a Vercel AI Gateway *built-in
  tool*, not a search API and not a routable model: it is absent from
  `/v1/models` (391 models, zero matches) and marked `labs`. Two measured reasons
  not to wire it as a fourth tier: relayed in direct mode it records **no decision
  row** (so the log cannot show it happened) and **$0 cost** (no registry row means
  `_deploy_cost` returns `None`, so its spend is invisible to the session's dollar
  budget — reopening the exact hole P1.3 closed). It belongs in the tool registry
  when that lands, with a per-request price, not in `mi/search.py`.

- **Supabase/Postgres sync as a headline feature.** SQLite is the store of record and
  the whole premise is "local, auditable, no service required". `mi sync` stays as an
  opt-in mirror. Revisit only if a shared team registry becomes an actual need.
- **A local tool-calling model in the routing path.** Measured: `cactus-compute/needle`
  is a 121M/2-bit on-device model. On this registry's own task set it classified 4/6
  with one *confident* error (0.96 on the wrong task), and its argument filling
  inverted `is_free` on a paid model and wrote a context window into a price field.
  It parses every time — the grammar guarantee is real and irrelevant, because the
  failure mode is confidently wrong values, not malformed output. The pip path is
  also broken (PyPI 3.0.5 asks HF for a 3.0.2 wheel that does not exist). Two places
  it *would* pay off are recorded for later: embeddings to rank entity-resolution
  candidates, and a finetuned 4–8 layer subnet as the intent tie-break — for which
  the training data is `decisions.reason.intent`, currently **8 labelled examples**,
  so the volume does not exist yet.
- **`general_chat` cues for `summarise`/`in a nutshell`.** Tried; they belong to the
  `summarise` task, which has its own cost profile.
- **Bare `write` as a `general_chat` cue.** Tried; it took "write me a query to get
  the top 10 customers" from `sql_generation` (1.00 → 0.50 on the wrong task).

---

## Known gaps in verification

- **The frontend has a runner now, and the gaps it exposed are closed** — see Done.
  Still uncovered: `chat-02.tsx` itself (the request lifecycle, abort, session
  switching) needs a `fetch` stub and a render harness before it can be tested, and
  `chat-02-utils/*` is untested UI primitives.

- **`mi metrics --dry-run` still writes to `raw/`.** It skips `apply()` and
  `record_snapshot()`, but `fetch_raw` inside it persists the bytes first. So a
  dry run mutates the evidence lake while claiming to write nothing, and each one
  adds a snapshot file with no `snapshots` row. Either record the row too (the
  bytes are real evidence) or make dry-run reuse the cache without re-persisting.
- `mi bench --self-test` in the external review does not run — `family` is required.
  The working invocation is `mi bench all --self-test` (**42/42 pass**).
- Test coverage has no *dedicated* file for `execute.py`, `normalize.py` or
  `resolve.py`. `store.py` is exercised by ~20 files and directly by
  `tests/test_session.py`. The review's "zero tests" is about files, not coverage.
- `sync.py` interpolates table names into SQL (`f"SELECT * FROM {table}"`). The names
  come from an internal list, so it is a smell rather than an exploitable path — but
  it would become one the moment that list is ever derived from input.
- `bench.py` executing model-authored SQL is the *mechanism* of a verifiable-reward
  benchmark, not a vulnerability. It runs against `sqlite3.connect(":memory:")`.
  Reasonable hardening would be a timeout and blocking `ATTACH`/runaway joins (DoS),
  not removing the execution.
