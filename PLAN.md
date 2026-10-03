# MinInfer — Dynamic Model Intelligence & Routing Engine


**Positioning:** *Free first. Pay only when free isn't good enough.*
Route every AI task to the cheapest deployment that is capable of doing it well — where "capable",
"cheap" and "available" are all measured, not assumed.

This document is the plan. `PLAN.md` is the source of truth for schema and policy decisions;
code implements it.

---

## 0. What the live probe told us (2026-09-26)

We probed every candidate source with **no credentials**. Findings that changed the design:

| Source | Endpoint | Auth | Models | Deployment-level data | Price unit |
|---|---|---|---|---|---|
| **OpenRouter** | `/api/v1/models` + `/models/{id}/endpoints` | keyless list | 458 (21 @ $0) | uptime 5m/30m/1d, quantization, discount, per-provider context | USD/token |
| **Vercel AI Gateway** | `https://ai-gateway.vercel.sh/v1/models` | keyless | 390 (4 @ $0) | tags, modalities, `supported_parameters`, ZDR | USD/token |
| **HuggingFace Router** | `https://router.huggingface.co/v1/models` | keyless | 139 | **`providers[]` with `is_free`, `first_token_latency_ms`, `throughput`** | USD/Mtok |
| **DeepInfra** | `https://api.deepinfra.com/v1/openai/models` | keyless | 187 | prompt-cache pricing, tags | USD/Mtok |
| **Featherless** | `https://api.featherless.ai/v1/models` | keyless | 22,031 | `concurrency_cost`, `is_gated` | USD/Mtok (subscription) |
| **Chutes** | `https://llm.chutes.ai/v1/models` | keyless | 14 | quantization, `max_model_len` | USD/Mtok + TAO |
| **Novita** | `https://api.novita.ai/v3/openai/models` | keyless | ~200 | — | 10⁻³ USD/Mtok |
| **SambaNova** | `https://api.sambanova.ai/v1/models` | keyless | 12 | pricing inline | USD/token |
| **NVIDIA NIM** | `https://integrate.api.nvidia.com/v1/models` | keyless | ~150 | — | free credits |

Three findings worth acting on:

1. **OpenRouter already publishes `hugging_face_id`.** That is the content-addressed weights identity
   we wanted, handed to us. No fuzzy matching needed for a large slice of the corpus.
2. **OpenRouter endpoints + HF `providers[]` already carry deployment-level uptime and latency.**
   We do not have to self-measure everything on day one. Our own probes become a *correction layer*,
   not the primary signal.
3. **Vercel encodes free tier in the model ID** (`poolside/laguna-s-2.1-free`), OpenRouter uses a
   `:free` suffix, Featherless uses a subscription. "Free" is therefore **four different things**:
   zero-price, zero-price-variant-of-a-paid-model, subscription-amortized, and quota-limited-trial.
   The schema models them separately. This is the single most important correction to the original design.

---

## 1. Core schema decision: split identity from economics

```
Weights        = the artifact      (benchmarks, capabilities that travel with the model)
Deployment     = a callable thing  (price, limits, latency, uptime, quantization, context)
Evidence       = provenance        (value, source, url, fetched_at, confidence)
```

Rules:

- **Benchmarks attach to `weights_id`.** They describe the model, not the host.
- **Price / limits / latency / uptime attach to `deploy_id`.** Same weights on Groq and on DeepInfra
  are two deployments with different unit economics.
- **`weights_id` resolution order:** `hf:<repo>@<rev>` → `hf:<repo>` → `slug:<normalized-name>`.
- **Nothing is stored without evidence.** Every field written by an ingest run also writes an
  `evidence` row. Fields that disagree across sources are stored per-source; the *resolver* picks,
  it does not overwrite.

This is what makes the registry survive: model names get silently repointed, `-latest` aliases move,
prices change weekly. Hash-keyed identity plus per-source evidence means a bad source degrades one
field, not the database.

## 2. "Free" is a quota problem, not a price problem

A free deployment's marginal cost is **zero until its bucket is empty**, then it is the price of the
next-best option. So the router never reads `is_free` directly. It computes:

```
effective_cost_per_call(deploy, now) =
    0                                   if free AND headroom > 0
    price_in*tok_in + price_out*tok_out  otherwise (or if headroom is unknown → conservative)
```

and then

```
cost_per_success = effective_cost_per_call / p_success_lower_bound
```

Free-first is therefore **emergent**, not a special case: free arms win because their
cost-per-success is 0, and quality only breaks ties. The moment a bucket empties, the arm's cost
jumps to the paid alternative and the router moves — without a human editing a preference.

`p_success` is a Wilson lower bound, seeded by external benchmark priors (§5), so 3 observations
cannot promote a model.

## 3. Fallbacks must be diversity-constrained

Top-3 free models are frequently behind the *same* gateway. A 429 is correlated by provider, not by
model. So candidate selection is:

1. hard filter (capability, context, license, price ceiling, `status`)
2. rank by `cost_per_success`
3. **greedy pick with a distinct-provider constraint** for #2 and #3

Failover is driven by error class (`429` → downgrade bucket, retry next candidate; `5xx`/timeout →
record observation, try next candidate), never by a user toggle.

## 4. Self-learning: three separate things

The original plan conflated these. They are different systems with different risks.

### 4a. Ingestion agents — *agents, not learning*
Turn arbitrary pages into schema rows. Only needed for non-JSON sources. Requirements:
structured output, mandatory provenance, and a **validator agent** that cross-checks a scraped price
against the provider's JSON price and quarantines disagreement instead of trusting it.

### 4b. Evaluation agents — *produce the ground truth*
`RouterBench`: tasks with **verifiable** rewards — SQL executes, JSON parses, tool schema validates,
unit tests pass, extraction matches a labelled set. This is the only reason an internal benchmark is
worth building. Subjective "user clicked accept" is a weak signal and is weighted as such.

### 4c. Policy learning — *the actual self-learning*
A **contextual bandit** over `(task_features → deployment)`, reward = verified success, penalty = cost.

- **Cold start is solved by the registry, not by traffic.** Every arm starts with a prior derived
  from external benchmarks (Artificial Analysis indices, arena Elo) expressed as pseudo-counts
  (`prior_strength ≈ 5`). An untried arm therefore has a real, non-zero belief — so we never get
  rich-get-richer lock-in where an arm wins only because it was tried.
- **Exploration is mandatory and budgeted.** ε-greedy at 5–10%, with exploration *never* allowed to
  exceed a hard monthly spend cap. A shadow arm (run the runner-up offline on sampled traffic) is the
  cheap version.
- **Observations are append-only.** `observations` never updates; stats are a view. So the policy can
  always be re-derived and audited.

Guardrail: the bandit may only reorder arms that already passed the hard filters. It can never
override a capability or price constraint. Cheap-but-incapable can never be learned into correctness.

## 5. Storage: split hot registry from cold evidence

```
raw/  or  s3://…/mininfer-evidence/<source>/<date>/<sha256>.json    immutable, append-only
Supabase Postgres (or local SQLite)                              normalized registry, hot
routing_stats (view) + decisions/observations                    time series
```

- **Never put raw payloads in Postgres.** Parsers change; you must be able to re-derive the registry
  from history. Raw blobs go to R2/S3 keyed by `sha256`, and `snapshots` in Postgres just points at them.
- **Local SQLite first, Supabase later.** Same SQL. `mi sync` pushes. No server needed to develop.
- **Supabase free tier** is enough for the normalized registry plus the public read-only dashboard
  (RLS: `SELECT` for anon on `deployments_public`). Serving real traffic would need the Pro tier for
  connection pooling — but by then the router is paying for itself.
- **Observations**: stay in Postgres until ~10M rows, then partition by month, then Parquet in R2 +
  DuckDB for analysis. Don't pre-optimize.

## 6. Provider roadmap

**Tier 0 — keyless, ships today** (`ingest.py`, no credentials):
OpenRouter, Vercel AI Gateway, HuggingFace Router, DeepInfra, Featherless, Chutes, Novita,
SambaNova, NVIDIA NIM.

**Tier 1 — free API key = real free quota** (config-only to enable):
Groq (fastest free tier), Google AI Studio / Gemini, Cerebras, Mistral La Plateforme,
GitHub Models (PAT), Cloudflare Workers AI (10k neurons/day), Cohere trial, OpenRouter (calls),
Together, Nebius, Hyperbolic, Kluster, Arliai.

**Tier 2 — local**: Ollama, LM Studio, llama.cpp, vLLM, SGLang. Marginal cost 0, capacity 1.
A deployment row with `provider: local`, no quota bucket. Often wins on privacy, loses on throughput.

The adapter layer is **LiteLLM** for actual calls — do not hand-write provider SDKs.

## 7. Roadmap

- **Phase 1 ✅** — schema, keyless ingest, resolver, Wilson+prior scoring,
  diversity-constrained top-3, CLI. Real data, real output.
- **Phase 2 ✅** — RouterBench-Lite (42 verifiable tasks across sql / extraction /
  tool_call / reasoning), eval harness, first real observations.
- **Phase 3 ✅** — declared free-tier limits (`config/quotas.yaml`), live quota
  buckets charged on every call, `429`-driven demotion.
- **Phase 4 ✅** — OpenAI-compatible `POST /v1/chat/completions` proxy with
  `model: auto`, diversified fallbacks, per-call observations, SSE streaming and
  tool-call passthrough; registered as a pi custom provider (`config/pi-provider.json`).
- **Phase 5 ✅** — LangGraph ingestion agents (`mi add-url`) + validator/quarantine
  workflow, plus the resolver's LLM adjudicator (`mi resolve --adjudicate`) for
  near-matches. The near-match scan is a sorted sliding window, not O(K²).
- **Phase 6** — policy learning and the view. ✅ contextual bandit (`mi bandit`,
  `objective: bandit`): Thompson sampling over eligible arms with bounded
  exploration. ✅ web dashboard: the proxy serves a unit-economics view at `GET /`
  and `GET /v1/stats`. ⏳ Supabase sync (needs a project and `DATABASE_URL`).

**Explicitly cut from Phases 1–2:** Playwright scraping, the URL admin UI, the bandit, the React
dashboard, Postgres. All are cheap to add once the schema has survived contact with real data, and
all are currently guesses at a schema that does not exist yet.
