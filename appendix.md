# Appendix — how min(Infer) works, in detail

The README is the summary. This is the reference: the data model, every
algorithm, the tunables, and the reasoning behind the non-obvious choices. Where
a design looks arbitrary, the comment in the code explains it — this collects
those in one place.

- [1. Data model](#1-data-model)
- [2. Evidence, provenance and confidence](#2-evidence-provenance-and-confidence)
- [3. Entity resolution and the merge guard](#3-entity-resolution-and-the-merge-guard)
- [4. Routing](#4-routing)
- [5. Reputation: learning which arms fail](#5-reputation-learning-which-arms-fail)
- [6. Recommendation: primaries, backups, last resort](#6-recommendation-primaries-backups-last-resort)
- [7. Anti-fixation and load spreading](#7-anti-fixation-and-load-spreading)
- [8. The agents](#8-the-agents)
- [9. The model dossier (form guide)](#9-the-model-dossier-form-guide)
- [10. Search](#10-search)
- [11. Budgets and sessions](#11-budgets-and-sessions)
- [12. Configuration reference](#12-configuration-reference)
- [13. CLI reference](#13-cli-reference)
- [14. HTTP surface](#14-http-surface)
- [15. Testing, calibration and shadow mode](#15-testing-calibration-and-shadow-mode)
- [16. Known limitations](#16-known-limitations)

---

## 1. Data model

| Table | Holds | Notes |
|---|---|---|
| `weights` | the artifact (`hf:openai/whisper-large-v3`) | identity, params, benchmarks, license |
| `weight_aliases` | merges: `alias_id → canonical_id` | with `reason` + `confidence` |
| `deployments` | a callable thing (`groq:qwen/qwen3.8-27b`) | provider, price, caps, limits, status, `pricing_state` |
| `evidence` | one fact, with `source` + `observed_via` + `confidence` | append-only; the registry is derived from this |
| `pricing_evidence`, `price_resolution`, `price_history`, `pricing_transitions` | the price *belief* timeline | *our* timeline, distinct from the provider's |
| `quarantine` | a claim a validator refused | prices, merges |
| `decisions` | one row per request, whatever the outcome | `reason` is a JSON blob (funnel, why, intent, search, shadow) |
| `observations` | one row per arm attempt | `ok`, `error_class`, `latency_ms`, `tokens_*`, `cost_usd`, `signal_kind` |
| `sessions` | spend ledger per session | tokens, calls, searches, cost |
| `messages` | the session transcript | newest user turn + answer |
| `quota_buckets` | configured + observed free-tier limits | headroom |
| `provider_health` | last warm-tier probe per provider | `ok` / `auth_error` / `network_error` / `http_NNN` |
| `model_dossiers` | the form guide per free arm | factual fields + `warrior`/`story` |
| `search_usage_daily` | per-tenant daily search count | the "birthright" quota bucket |
| `snapshots` | every fetched byte, by sha256 | `raw/` on disk; replayable |

Derived **views** (never hand-edited): `routing_stats` (per deploy/task, with
infra errors excluded and `n_429`/`n_timeout` split out), `weights_resolved`
(alias → canonical), `deployments_priced`, `price_history_dated`.

`Store` owns all SQL; `tests/test_encapsulation.py` asserts no other module
writes it. The dialect seam (`mininfer/db.py`) translates one SQLite-dialect
query for Postgres, so there is one copy of each statement.

## 2. Evidence, provenance and confidence

Every fact is a claim with a source, not a truth. Confidence rungs
(`schema.CONFIDENCE`):

| `observed_via` | Confidence | Meaning |
|---|---|---|
| `manual` | 1.0 | an operator wrote it |
| `provider_api` | 0.95 | the provider's own API |
| `aggregator_api` | 0.85 | OpenRouter/Vercel-style catalogue |
| `provider_reported_probe` | 0.70 | a live call's `x-ratelimit-*` headers |
| `benchmark_site` | 0.60 | a leaderboard page |
| `llm_extraction` | **0.4** | an agent scraped it from a page — the lowest rung, by design |

Agent output is deliberately the *least* trusted input. A price scraped from a
page is cross-checked against the provider-API price for the same deployment;
disagreement is written to `quarantine` instead of overwriting
(`agent_ingest._PRICE_DISAGREE = 0.20`).

## 3. Entity resolution and the merge guard

The failure this section prevents: a silent, permanent wrong merge that attaches
one model's benchmarks and outcomes to another.

### Normalisation

`normalize()` lowercases, drops the provider prefix, collapses punctuation, and
strips **serving** suffixes — quantization (`fp8`, `bf16`, `awq`, `gguf`, `nvfp4`,
`tee`, …) and hosting (`free`, `nitro`). `-2507` (a release date) and `-thinking`
(a different fine-tune) are *identity* and survive.

Two token classes matter:

- **serve tokens** — always safe to strip.
- **variant tokens** — `turbo`, `thinking`, `beta`, `extended`, `online`. They
  *look* like serving noise but may name a different model (Whisper's `turbo` is
  a smaller model, not a hosting tier).

`normalize_identity()` strips only serve tokens, so it can tell the two apart.

### The guard

`resolve.propose()` auto-merges only an **exact normalised-name** match. Before
merging, it compares `normalize_identity()` on both sides: if they differ, the
merge hinges on a stripped variant token and goes to **review**, not to
auto-merge. The `Store.merge_weights` guard additionally refuses a merge when
both parameter counts are known and disagree, recording the refusal in
`quarantine`.

### Recovery

A bad merge that already landed is invisible to `propose()` (the absorbed row no
longer exists). `Store.variant_split_candidates()` finds it after the fact:
for every canonical that something was merged into, compare each deployment's
own model identity with the canonical's under `normalize()` (equal) and
`normalize_identity()` (different). `mi resolve --split-variants` recreates the
absorbed weights row, moves its deployments back, and repoints any alias that
names it (preferring an `hf:`-keyed row). `--dry-run` reports without writing.

Worked example: `hf:openai/whisper-large-v3` had been absorbed into
`...-turbo`. The guard stopped new instances; the recovery split 1 deployment
back and repointed `slug:whisper-large-v3` to the base.

## 4. Routing

Order of operations — there is deliberately **no weighted-sum formula**, because
a linear blend lets a cheap mediocre model beat a good one exactly when you
needed the good one:

1. **Hard filter** (`_reject`): status, **special-purpose role**, capabilities,
   context, price ceilings, quality floor. Nothing below this line is
   recoverable by a good score.

   The role gate (`special_role`) exists because a task that requires no
   capability admits *any* arm: `general_chat` has `require: {}`, so a free,
   untested safety classifier, TTS voice or music generator cleared every filter
   — and exploration, which favours under-observed arms, then served them for
   prose. Roles are read from the model id (`…-content-safety`, `whisper-…`,
   `lyria`, `orpheus`, `bge`…) since the registry has no "outputs" field. A task
   that wants one opts in with `allow_roles: [safety]`; the default is
   general-purpose generators only.
2. **p(success)**: Wilson lower bound, seeded with a benchmark prior
   (`prior_success`), blended with observations. `prior_strength` (6.0) is the
   prior's weight; a difficulty cell blends in with a capped weight
   (`effort_shrink_k`).
3. **Cost**: effective marginal cost *now* — a free arm whose bucket is drained
   costs the next best option, estimated from configured limits and the observed
   429 rate, whichever is worse.
4. **Rank** by cost-per-success, tie-broken by quality then latency.
5. **Diversify** (`_diversify`): #2 and #3 must not share a gateway *and* an
   upstream, because 429s and outages correlate by gateway, not by model.
   Relaxed progressively; the achieved level is reported, not hidden.

`Candidate` carries everything a decision needs to explain itself:
`p_lb` (ranking), `p_lb_evidence` (admissibility — before any dossier
adjustment), `availability`, `cost_per_success`, `rate_429`, `error_rate`,
`headroom`, `routing_sentiment` (human votes), `prior_tags`, `dossier_*`.

### The quality floor is judged on evidence

`_reject` uses `p_lb_evidence`, never the adjusted `p_lb`. A dossier (or any
future prior adjustment) can reorder the eligible arms but can never lift one
over the floor it does not actually clear.

## 5. Reputation: learning which arms fail

Every request writes an `observation` per attempt — success or failure — so the
failure history is complete.

**`Store.provider_outcome_stats()`** rolls outcomes up to the provider
(the `d.provider` string, i.e. gateway + upstream). A 429 or 5xx is usually the
provider, so the router penalises the provider once. It is gated on
`min_provider_obs` (5): below that, a burst on one arm stays attributed to that
arm.

The warm-tier probe feeds `provider_health`, and only outcomes that disqualify
the **credential** remove a provider (`Store._DISQUALIFYING_HEALTH` =
`auth_error`, `http_401`, `http_403`). A `network_error`/`tls_error` is our own
connectivity — the same reason `_counted_expr` refuses to count it against a
model — and excluding on it blackholed every arm behind a gateway for the whole
TTL after one probe lost the network.

In `build_candidates`, availability takes **the worse of arm and provider**:

```
availability = uptime_factor
             * (1 - max(arm_429_rate, provider_429_rate)
                  - max(arm_timeout_rate, provider_timeout_rate))
             * unknown_headroom_factor   # free arm with no configured bucket
```

`Store.reputation(days, task, min_sample)` is the reviewable rollup, per
`(provider, deploy, task)`:

| Field | Derivation |
|---|---|
| `n` | attempts that reached the model (infra errors excluded) |
| `availability` | `1 − (429 + timeout) / n` |
| `reliability` | `1 − other_errors / n` |
| `win_rate` | `wins / n` |
| `n_judge_rejected` | judge trials the arm failed |
| `verdict` | below |

Verdict, with **minimum-sample gating** (default `min_sample=5`):

- `insufficient` — fewer than `min_sample` attempts. No opinion.
- `retire` — availability < 0.5, or reliability < 0.5, or win rate < 0.5.
- `degraded` — availability < 0.8, or reliability < 0.8, or win rate < 0.7.
- `healthy` — otherwise.

The verdict **recommends**; `mi retire` / `mi disabled` and the hibernation
review are how a human acts on it. Nothing auto-retires.

## 6. Recommendation: primaries, backups, last resort

`router.recommend(store, task, policy)` (CLI `mi recommend`, HTTP
`GET /v1/recommend`) returns:

```
primary : 2–3 arms   the router's own diversified shortlist
backups : 2 arms     next best, distinct by gateway/upstream, ≥1 untested
last_resort          the reserved arm, if configured
```

Each arm carries a `why` list (`free`, `p(success) …`, `availability …`,
`untested …`, `quota exhausted`). Backups reserve one slot for an arm with
`n_obs < free_trial_obs`, so the router keeps generating evidence instead of
re-using the same winner.

The **last resort** (`policy.last_resort` / `MI_LAST_RESORT`) is never ranked and
never displaces a cheaper arm. The proxy appends it to the execution chain after
the ranked candidates, so the chain can run out of free arms but not out of an
answer; the decision records `last_resort`.

A recommendation is **not** a decision: `route()` is what serves a request.

## 7. Anti-fixation and load spreading

The failure: with every free arm at cost-per-success 0, the ranking collapses to
the benchmark prior, one arm takes every request, concentrates load on a single
quota, and inherits that provider's outages unhedged.

- **Traffic cap.** `traffic_share = n_obs / total_task_obs`; above
  `max_model_share_pct` (0.60) the arm is marked `traffic_capped` and the next
  uncapped eligible arm is rotated in (`strategy = anti_fixation_rotate`).
- **Exploration.** `explore_c` adds a UCB bonus within a cost tier;
  `exploration_eps` (5%) triggers a cold-start pick; both are bounded to
  free/cheapest arms.
- **Dossiers** are a bounded prior that fades with observations (`maturity`) and
  are **off** by default (`dossier_weight: 0.0`).
- **Agents rotate.** `AgentLLM` round-robins the whole chain and cools down an
  arm that just failed, rather than pinning to whichever answered last. Pinning
  burns one free tier's quota while the rest sit idle.

## 8. The agents

Three LLM agents; all share the `MI_AGENT_MODEL` / `MI_AGENT_MODELS` resolution
and the fallback chain. The `[agents]` extra (`langgraph`, `langchain-openai`,
`beautifulsoup4`) is required.

| Agent | Form | Entry | Does |
|---|---|---|---|
| `agent_ingest` | LangGraph (fetch → extract → understanding → llm_extract → normalize → validate → commit/quarantine) | `mi add-url URL` | turn a page into registry rows; facts at `llm_extraction` (0.4), prices cross-checked |
| `agent_resolve` | LangGraph (adjudicate → apply/summarize) | `mi resolve --adjudicate` | adjudicate near-duplicate identities; the param-count guard can veto |
| `scout` | single-shot pipeline + search tool | `mi scout`, `POST /v1/dossiers/refresh` | write per-free-arm dossiers |

### `agent_llm` — free-first fallback chain

`agent_models(primary)` resolves: `--agent-model` > `MI_AGENT_MODEL` >
`MI_AGENT_MODELS` > `_DEFAULT_CHAIN`. The defaults are free-first with a paid
last resort:

```
openrouter/novita:inclusionai/ling-3.1-flash
openrouter/nvidia:nvidia/nemotron-3-super-120b-a12b:free
openrouter/cohere:cohere/north-mini-code:free
groq:qwen/qwen3.8-27b          ← paid last resort
```

Ids carry the OpenRouter **upstream** prefix (`openrouter/<upstream>:<model>`);
the bare `openrouter:<model>` form routes to whichever upstream is cheap and was
observed 429ing.

`AgentLLM.invoke` starts at a cursor, tries each arm, and on failure records a
**cooldown** (`cooldown_s`, 60) so a dead arm is skipped rather than re-probed;
on success it advances the cursor. If every arm is cooling down the cooldown is
ignored rather than refusing to work. The arms that answered is recorded
(`scout` stores it as `facts.agent_model`).

**Not LiteLLM.** The repo has one provider seam (`execute.resolve_endpoint`) and
a deliberately lean image. LiteLLM would duplicate the seam and still not express
"another arm of the same task" — a list of deploy_ids does. If provider breadth is
ever needed, LiteLLM belongs *behind* `resolve_endpoint`, not beside it.

## 9. The model dossier (form guide)

`mi scout` writes one row per free arm into `model_dossiers`, shown in the
dashboard's **Free models** tab and `GET /v1/free-models` (which LEFT-joins, so
unscouted arms appear too).

**Pipeline per arm:** gather registry facts (price, status, caps, leaderboards,
outcomes) → decide whether a search is warranted → search → LLM synthesis.

**Search-necessity gate** (`_need_search`): search only for a first dossier,
changed facts, or sparse evidence; otherwise re-synthesise from the stored facts.
Recorded as `facts.search`.

**Search provider.** TinyFish **only** (`MI_SCOUT_SEARCH_PROVIDER`, default
`tinyfish`, structured JSON, 12k/day free) — the scout prefers an ungrounded
dossier to a thinly-sourced one. No key → no search, facts-only.

**Fields.** Factual: `core_competency`, `summary`, `strengths`, `weaknesses`,
`when_to_use`, `when_not_to_use`, `best_for` (policy task names), `confidence`.
Storied: `warrior` (an archetype) and `story` (an analogy) — colour only, never
asserting a fact the evidence does not support. `MI_SCOUT_TONE` = `epic`
(default) | `plain` | `deadpool`.

**Freshness.** A dossier is skipped when fresh and its facts unchanged. It is
re-synthesised (no search) when the facts moved, when the voice changed
(`facts.scout_tone`), or when it predates the storied fields.

**Routing tie-in (off by default).** `dossier_fit` adjusts the *ranking* prior
only, bounded by `dossier_weight`, gated on confidence and freshness, and faded
by observations. Admissibility is judged on `p_lb_evidence`. `dossier_shadow`
records the counterfactual without applying it; `mi dossier-report` scores it.

## 10. Search

Providers, tried free-first in `auto`:

| Provider | Key | Tier |
|---|---|---|
| Wikipedia | none | free |
| DuckDuckGo (lite) | none | free, unofficial (bot-check aware) |
| TinyFish | `TINYFISH_API_KEY` | free, 12k/day, resets 00:00 UTC; `402` when spent |
| Tavily | `TAVILY_API_KEY` | paid (~$0.008/search) |

- `MI_SEARCH_ALLOW_PAID=0` forbids the paid fallback; `search_cost("auto")`
  becomes 0 accordingly.
- Every response is snapshotted to `raw/` and recorded in `snapshots`.
- `GET /v1/search` is **tenant**-scoped, session-required, and budget-checked
  against the *worst case* provider price. It returns
  `search_quota: {daily_limit, used_today, remaining_today}`.
- `MI_SEARCH_DAILY_LIMIT` is the per-tenant daily free quota; exhausted →
  `429 search_daily_quota_exceeded`.
- **Chat grounding** (`MI_CHAT_SEARCH` = `off` | `auto` | `on`, or per-request
  `{"search": true}`): `chatsearch.needs_search()` is a deterministic cue layer
  (time words, URLs, years, explicit asks). When it fires, results are injected
  as a system message **before routing**, so one request is one model call. The
  decision records a `search` block and the reply carries `X-MI-Search*`.
- **Chat grounding prefers TinyFish** (`MI_CHAT_SEARCH_PROVIDER`, default
  `tinyfish`). Provider `auto` stops at the **first non-empty** answer, and
  Wikipedia will happily answer a news query with tangential encyclopedia pages
  ("weather in India" → *Mumbai*, *Gujarat*, *Jammu*), so the fresher tiers were
  never reached and the model was grounded in confident, irrelevant context.
  Without a `TINYFISH_API_KEY` it falls back to `auto` so search still works — a
  worse source beats none, but the keyless tiers are not the default.

## 11. Budgets and sessions

- A **session** (`X-MI-Session`, or the OpenAI `user` field) carries token and
  money budgets. The check **reserves** `prompt estimate + max_tokens` before the
  call, so the cap is a ceiling rather than "stop once you are already over it".
- `MI_SESSION_TOKEN_LIMIT` / `MI_SESSION_COST_LIMIT` override the policy.
- `MI_RATE_LIMIT_RPM` is per tenant; Redis-backed (`MI_REDIS_URL`) so it counts
  across replicas.
- `MI_MAX_BODY_BYTES` rejects oversized bodies before routing.

## 12. Configuration reference

| Variable | Default | Effect |
|---|---|---|
| `MI_DB` | `mininfer.db` | a path (SQLite) or a `postgresql://` DSN |
| `MI_POLICY` | `config/policy.yaml` | routing policy |
| `MI_REDIS_URL` | — | shared rate limiter + caches |
| `MI_AGENT_MODEL` | — | single agent arm (leads the chain) |
| `MI_AGENT_MODELS` | free-first default | comma-separated agent chain |
| `MI_SCOUT_TONE` | `epic` | dossier voice (`plain`/`epic`/`deadpool`) |
| `MI_SCOUT_SEARCH_PROVIDER` | `tinyfish` | the scout's search provider |
| `MI_CHAT_SEARCH` | `off` | chat grounding (`off`/`auto`/`on`) |
| `MI_SEARCH_ALLOW_PAID` | `1` | `0` forbids the paid search fallback |
| `MI_SEARCH_DAILY_LIMIT` | `0` | per-tenant daily search quota |
| `MI_INTENT_MODEL` | — | LLM tie-break for ambiguous task classification |
| `MI_LAST_RESORT` | — | reserved arm tried only after the chain |
| `MI_SESSION_TOKEN_LIMIT` / `MI_SESSION_COST_LIMIT` | from policy | session ceilings |
| `MI_RATE_LIMIT_RPM` / `MI_MAX_BODY_BYTES` | `0` | tenant limits |
| `MI_USER_AGENT` | built-in | override the Wikimedia-compliant UA |

Policy knobs (`config/policy.yaml`): `prior_strength`, `min_success_lb`,
`free_floor_exempt`, `free_trial_obs`, `min_provider_obs`, `max_model_share_pct`,
`explore_c`, `explore_band`, `exploration_eps`, `dossier_*`, `last_resort`,
`session_*`.

## 13. CLI reference

Key commands (see `mi --help`):

```
mi doctor                                            # first-run checklist + fixes
mi ingest | reconcile | verify | refresh | metrics   # registry + drift
mi resolve [--adjudicate] [--split-variants]         # identity
mi route TASK | mi explain TASK | mi recommend TASK  # planning
mi reputation [--task T] [--days D]                  # health verdicts
mi scout [--tone …] [--only-new] [--agent-model …]   # dossiers
mi dossiers [SUBSTR] | mi dossier-report             # form guide + calibration
mi add-url URL                                       # webpage → registry (agent)
mi search QUERY [--provider …]                       # web search
mi proxy                                             # serve
```

## 14. HTTP surface

`docs/api-contract.md` is the machine-readable inventory (parsed by
`tests/test_api_contract.py`). Newly added, all `experimental`:

- `GET /v1/free-models` — free tier + dossier summary (tenant)
- `GET /v1/recommend?task=` — primaries + backups (tenant)
- `GET /v1/reputation` — provider/model verdicts (**admin**)
- `GET /v1/dossiers` / `POST /v1/dossiers/refresh` (tenant / admin)

Auth is path-classified and **fails closed**: an unclassified path is `admin`.
`/v1/*` is tenant by default; operator surfaces are listed explicitly.

## 15. Testing, calibration and shadow mode

- `pytest` runs against SQLite by default; `MI_TEST_PG_DSN` redirects every
  `Store` to Postgres (one schema per test) so both engines are proven.
- The suite clears ambient tuning knobs (`conftest._no_ambient_tuning`) so a
  developer's `.env` cannot leak into a test.
- **Shadow mode** (`dossier_shadow`) records the counterfactual pick without
  applying it; `mi dossier-report` joins it to outcomes.
- `mi complexity --calibrate` sweeps thresholds against a labelled prompt set.
- Time-dependent fixtures use relative dates (`PRICE_FRESHNESS_DAYS = 30`
  otherwise silently ages a "canonical" price into "expired").

## 16. Known limitations

- **The reputation signal is thin.** Most `(deploy, task)` pairs have single-digit
  observations; the verdict is gated accordingly and is a recommendation, not an
  action.
- **"Wrong answer" is expensive to detect.** Only the judge and human votes
  exist; for free models the judge split is near 50/50. Provider errors and quota
  are the trustworthy signals.
- **Merges are guarded, not proven.** The param-count guard cannot fire when both
  counts are unknown; the variant guard covers the common case, not every case.
- **A single locked model is still possible** within the traffic cap for a task
  with few eligible arms.
- **Free arms 429 without warning.** Availability learning is a mitigation, not a
  cure; the fallback chain and the last resort are what keep an answer flowing.
- **`normalize()` is intentionally conservative** and will leave duplicates
  rather than risk a wrong merge — resolution is a maintenance loop, not a
  one-shot.
