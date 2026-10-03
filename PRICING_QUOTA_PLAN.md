# Accurate Pricing & Quota Service — Plan

> A serverless **Pricing & Quota Service (PQS)** that owns the one number the
> whole product rests on: *what does this deployment actually cost right now, and
> how much free headroom does it actually have?* Every free/cost element on the
> Overview page reads from it.

This document is the plan. `PLAN.md` remains the source of truth for schema and
routing policy; this extends it with a dedicated economics layer.

---

## 0. Why this exists — the Novita case as the specification

The bug that motivated this:

```
Novita API  input_token_price_per_m = 750
     ↓  adapter assumed /1000
$0.75 / Mtok          ← 10× the truth
     ↓
router ranks the arm as expensive
     ↓
wrong economic decision (and a bogus "was free, now $0.75" hibernation)
```

The correct conversion is `/10000` (`750 → $0.075/Mtok`), validated against four
other providers. `tests/test_price_units.py` now pins it. But pinning *one*
adapter is not a system. The same class of bug — **a plausible number in the
wrong unit or from a stale/duplicate source** — can still enter through any of:

- a new adapter with a wrong factor (nothing declares units machine-readably),
- a provider changing the unit it publishes,
- two providers disagreeing on the same weights with no arbiter,
- `upsert_deployment`'s `COALESCE(excluded.price_in, deployments.price_in)`
  letting whichever adapter ran last silently win,
- a price that is simply old.

The service below makes that class of bug **structurally detectable**, not just
test-caught.

---

## 1. What exists today (verified in code)

```
provider APIs
  → mininfer/ingest/adapters/*.py        # each hardcodes its own unit math
  → Deployment.price_in/price_out         # $ per Mtok (schema.py)
  → Store.upsert_deployment               # store.py: last-writer-wins COALESCE
  → deployments table (+ evidence rows,  # evidence is WRITE-ONLY today
                        never read back)
  → Router._cost_per_call / build_candidates   # router.py:497
  → Store.providers_summary / spend_savings / quota headroom
  → proxy.py:/v1/stats, /v1/plan, /v1/reviews
  → web/src/components/Overview.tsx
```

**The one piece already in our favour:** the `evidence` table
(`schema.Evidence`, `supabase/schema.sql`) already carries exactly the shape the
reconciliation model needs — `(entity_kind, entity_id, field, value, source, url,
fetched_at, confidence, observed_via)`. It is being written on every ingest and
never read. **The plan is to start reading it, and to make it authoritative.**

### The eight gaps

| # | Gap | Where |
|---|-----|-------|
| 1 | Units are per-adapter prose, not declarative data | `ingest/adapters/novita.py:26`, `catalogue.py:34` |
| 2 | Last-writer-wins price merge, no arbitration | `store.py:530` `COALESCE(excluded.price_in, …)` |
| 3 | No cross-source reconciliation of `price_*` | `evidence` written, never read |
| 4 | Anomaly checks are per-adapter plausibility bands only | `ingest/shared.py:validate_prices` |
| 5 | No staleness / ageing of a price | `fetched_at` stored, unused |
| 6 | Free/paid/trial collapsed into 4 booleans, no per-state timestamp | `schema.Deployment` |
| 7 | Quota limits are **configured** (static yaml), not **observed** | `config/quotas.yaml`, `store.headroom` |
| 8 | Savings priced against "current registry prices" (admitted estimate) | `store.py:706` docstring |

---

## 2. Architecture

### 2.1 Shape

```
                 ┌────────────────────────────────────────────┐
   cron ────────▶│  Ingest (per provider adapter)              │
  (Modal Cron)   │  → normalize to RAW value + declared unit   │
                 │  → append pricing_evidence rows             │
                 └───────────────────┬────────────────────────┘
                                     │
                 ┌───────────────────▼────────────────────────┐
                 │  Reconciler (Python, pure functions)        │
                 │  1. unit-normalize every observation        │
                 │  2. group peers by weights_id (signal only) │
                 │  3. detect anomalies (scale/spread/stale…)  │
                 │  4. resolve DEPLOYMENT-level canonical state│
                 │  5. project quota headroom + exhaustion     │
                 └───────────────────┬────────────────────────┘
                                     │  writes
                 ┌───────────────────▼────────────────────────┐
                 │  Supabase Postgres (shared read model)      │
                 │  canonical_prices · anomalies · quota_state │
                 └───────────────────┬────────────────────────┘
                                     │  reads (read-only, cached)
                 ┌───────────────────▼────────────────────────┐
                 │  PQS API  (Modal FastAPI endpoint)          │
                 │  GET /v1/economics/overview                 │
                 └───────────────────┬────────────────────────┘
                                     │
        ┌────────────────────────────┴─────────────────────────────┐
        ▼                                                           ▼
  Overview.tsx (Vercel-hosted)                          FastAPI proxy passthrough
  api.economics()                                       (dev / same-origin prod)
```

### 2.2 Why serverless, and which one

**Compute → Modal (Python), not Vercel (TS).** The reconciler must import the
existing Python domain (`mininfer.schema`, `mininfer.ingest.shared`,
`mininfer.normalize`) and re-derive prices from `raw/` snapshots. Rewriting a
unit registry and a peer-comparison engine in TypeScript duplicates the domain and
re-introduces exactly the drift this plan removes.

| Piece | Host | Why |
|---|---|---|
| Ingest + reconcile jobs | **Modal** `@modal.Cron` (every 30 min) | Pure Python, reuses `mininfer/*`, scales to zero between runs |
| Read API (`/v1/economics/*`) | **Modal** `@modal.fastapi_endpoint` | Same domain, read-only, cacheable |
| Dashboard | Vercel (Vite build) | Static, already builds to `web/dist` |
| Store | Supabase Postgres | Already the project's Postgres target (`supabase/schema.sql`) |
| Dev / same-origin | `web/vite.config.ts` proxy + FastAPI passthrough | No CORS, no second server locally |

> **Vercel-only alternative** (if a second Python host is unacceptable): run the
> reconciler as a Vercel Cron hitting `api/economics/reconcile.ts`, which shells
> out to… nothing — it would have to reimplement the domain. Not recommended.
> Vercel's role is the dashboard; Modal's role is the economics engine.

### 2.3 Statelessness

Every endpoint is a pure read over Postgres. The only writes are the cron jobs.
That means the read API can be edge-cached, horizontally scaled, and needs no
session or in-process state — which is what lets the Overview poll it every 5s
without cost.

---

## 3. Domain model

### 3.1 Unit registry (fixes gap #1)

One declarative table. An adapter records **what it read and in what unit**;
exactly one function converts.

```python
# mininfer/pricing/units.py
@dataclass(frozen=True)
class UnitSpec:
    name: str            # "per_token" | "per_mtok" | "per_myriad_mtok" | "per_ktok"
    to_usd_per_mtok: float

UNITS = {
    "per_token":        UnitSpec("per_token",        1_000_000.0),
    "per_mtok":         UnitSpec("per_mtok",                 1.0),
    "per_myriad_mtok":  UnitSpec("per_myriad_mtok",       1e-4),   # Novita: 750 -> 0.075
    "per_ktok":         UnitSpec("per_ktok",             1_000.0),
}

# Each provider declares its unit ONCE. The adapter never divides by a literal.
PROVIDER_PRICE_UNITS = {
    "novita":     {"input": "per_myriad_mtok", "output": "per_myriad_mtok"},
    "openrouter": {"input": "per_token", "output": "per_token"},
    "deepinfra":  {"input": "per_mtok",  "output": "per_mtok"},
    "chutes":     {"input": "per_mtok",  "output": "per_mtok"},
    "sambanova":  {"input": "per_token", "output": "per_token"},
    # …every provider in catalogue.SOURCES
}

def to_usd_per_mtok(raw: float, provider: str, side: str) -> float:
    return raw * UNITS[PROVIDER_PRICE_UNITS[provider][side]].to_usd_per_mtok
```

`tests/test_price_units.py` grows from "Novita is `/10000`" to a table test over
**every entry in `PROVIDER_PRICE_UNITS`** — and a test asserting every adapter in
`catalogue.SOURCES` has an entry, so adding a provider without a unit is a test
failure, not a 10× bug.

### 3.2 Pricing evidence (fixes gaps #2, #5, #6)

Reuse `evidence`; add a normalized companion table so the reconciler reads typed
rows instead of JSON blobs.

```sql
create table if not exists pricing_evidence (
  id            bigint generated by default as identity primary key,
  deploy_id     text not null,
  weights_id    text not null,
  provider      text not null,
  side          text not null,            -- 'in' | 'out' | 'cached_in'

  raw_value     double precision,         -- exactly what the provider published
  raw_unit      text not null,            -- UnitSpec.name
  usd_per_mtok  double precision,         -- normalized, the ONLY compared value

  pricing_type  text not null,            -- 'free'|'paid'|'trial'|'subscription'|'unknown'
  source        text not null,            -- 'provider_api'|'aggregator_api'|'probe'|'manual'
  url           text,
  observed_at   timestamptz not null,
  expires_at    timestamptz,              -- trial credits expire; list prices do not
  confidence    double precision not null,
  superseded_by bigint                    -- pointer, so history is never mutated
);
create index on pricing_evidence (deploy_id, side, observed_at desc);
create index on pricing_evidence (weights_id, side, observed_at desc);
```

This is the user's `PricingEvidence`, with the unit made explicit and the raw
value retained (so the reconciler can re-derive without a re-fetch).

**Pricing is multidimensional from the start.** `side` is not `'in' | 'out'`; it
is the billing dimension vocabulary already declared in P0 at
`pricing.schema.PRICING_KINDS`:

```
input · output · cached_input · reasoning · image_input · audio_input · video_input · tool_call
```

Only `input`/`output`/`cached_input` are populated by today's providers. The rest
are declared now so a provider that starts billing reasoning tokens has somewhere
to go that is not a new column and a new adapter literal. Each observation is a
`PriceObservation` in `pricing.schema`: `{kind, raw_value, raw_unit, usd_per_mtok,
pricing_type, is_sentinel}`.

### 3.3 Canonical price (fixes gaps #2, #3)

```sql
create table if not exists canonical_prices (
  deploy_id     text not null,
  side          text not null,
  usd_per_mtok  double precision,
  pricing_type  text not null,            -- resolved free/paid/trial/unknown
  confidence    double precision not null,
  n_sources     integer not null,
  spread_ratio  double precision,         -- max/min across independent sources
  basis         text not null,            -- 'single_source'|'multi_source'|'manual'|'expired'|'quarantined'
  reconciled_at timestamptz not null,
  primary key (deploy_id, side)
);
```

`deployments.price_in/price_out` **stops being written by ingest.** It becomes a
generated/derived read (a view `deployments_priced`) so there is exactly one
source of truth. Any code reading `d["price_in"]` in the router continues to work
via the view.

### 3.4 Quota state (phase P5, shipped)

**No separate `quota_state` table was needed.** The observed numbers live on
`quota_buckets` beside the configured ones, which is what keeps the two-limits
rule expressible in one row — and keeps `headroom` a single implementation
instead of two that can disagree.

```sql
-- existing columns: limit_n (CONFIGURED policy), used_n, reset_at, source
-- added in P5:
observed_limit_n     integer,
observed_remaining_n integer,
observed_reset_at    text,
observed_at          text,
observed_confidence  double precision
```

`limit_source` is **derived, not stored** (`configured` | `observed` | `hybrid`),
because it is a pure function of which columns are populated and a stored copy
could drift from them.

Headroom is the **minimum of two independent limits**, because MinInfer's policy
is not the provider's:

```
effective_headroom = min( provider_observed_headroom, configured_policy_headroom )
```

Without this, a policy that allows 1000 calls can consume a provider's entire
500-call free tier and starve the account for everything else.

A `429` is **not** proof of exhaustion. It is classified before it is acted on:

```
rate_limit_reason: rpm | rpd | tpm | concurrency | account | unknown
```

When the reason is `unknown`, `remaining` stays `unknown` — the same principle
the schema already applies to capabilities and prices. If exhaustion is
projected, it is projected *probabilistically* (`estimated_exhaustion_at`,
`confidence`, `basis = {remaining, observed_rate, window}`) rather than as a
fact, because usage is stochastic and a false "lasts until 16:32" is worse than
no estimate.

### 3.4b Not-callable deployments (P5, runtime-only)

`schema.NON_MODEL_ERRORS` gains `not_api_callable`, and `deployments` gains
`status_source`.

Some providers serve a model only to allowlisted applications — OpenRouter
answers a plain API client with 401 *"only available on agentic harnesses"*. Two
facts shape the fix, and the first was established by inspecting the cached
catalogue rather than by guessing:

* **There is no ingest-time signal.** In `/api/v1/models`,
  `thinkingmachines/inkling-small:free` is structurally identical to any other
  `:free` model: `pricing {prompt: "0", completion: "0"}`, `is_moderated: false`,
  no restriction field. The only "agentic" string is
  `artificial_analysis.agentic_index`, an unrelated benchmark. So this **cannot**
  be a catalogue filter.
* **It is about the deployment, not our key.** Leaving it as `auth_error` would
  exclude it from the statistics *and* leave the arm retried on every request,
  forever.

So the restriction is discovered by calling and recorded as a retirement:

```
401/403 + "agentic harness" in the body  ->  error_class = not_api_callable
                                         ->  status      = deprecated
                                             status_source = 'runtime'
```

`status_source` is what makes it stick. Ingest runs on a schedule and the
catalogue reports the model `live`, so without it the arm returns on the next
sync and every request pays an attempt to rediscover that it cannot be called.
The precedence rule, in one place:

```
existing status_source == 'runtime' and status != 'live'   ->  keep existing
existing status_source in ('review', 'runtime')            ->  keep existing
otherwise                                                  ->  take the catalogue's
```

The middle line is a bug fixed alongside this: `paid -> paid` used to keep the
existing status *unconditionally*, so a provider retiring a model — or an
endpoint going `degraded` — could never take effect on a row that was already
paid. `status_source` is what makes the rule expressible, because it is the only
way to tell an operator's decision from the catalogue's. `mi disabled` lists what
was retired and `mi enable` restores it, so stickiness is never permanent.

### 3.5 Anomalies (phase P3, shipped)

The reconciler's *record*. One row per ongoing **incident**, opened when a price is
refused or flagged and closed when it returns to canonical — not one row per
observation.

```sql
create table if not exists price_anomalies (
  anomaly_id     text primary key,   -- content-derived; sync drops surrogate ids
  deploy_id      text not null,
  weights_id     text,
  dimension      text not null,      -- input | output | cached_input  (the price)
  kind           text not null,      -- unit_scale | spread | history_jump (the fault)
  severity       text not null,      -- info | warning | high | critical
  state          text not null,      -- the price_resolution state that raised it
  expected       double precision,   -- the market median, or the previous value
  observed       double precision,   -- the value refused or flagged
  factor         double precision,
  source_count   integer not null default 0,
  resolved_value double precision,   -- what the deployment kept
  detail         text,               -- the reconciler's own reason string
  evidence       text default '{}',  -- coordinates, NOT rowids (see below)
  status         text not null default 'open',  -- open | acknowledged | resolved
  opened_at      text not null,
  last_seen_at   text not null,
  resolved_at    text,
  resolution     text
);
```

Three decisions worth naming:

* **`dimension` is separate from `kind`.** `kind` is the *fault*
  (`unit_scale`), `dimension` is the *price it applies to* (`input`). They were one
  column first, and the refresh lookup compared `input` against `unit_scale` and
  never matched — so every reconcile re-opened duplicates instead of refreshing.
* **`anomaly_id` is content-derived, not a rowid.** `sync` drops surrogate `id`
  columns, so a rowid would make two mirrors disagree about which incident a
  reference points at. The id is `deploy|dimension|<microsecond>`; microseconds so
  that closing a still-wrong anomaly and re-reconciling opens a *new* row rather
  than colliding with the one just closed.
* **`evidence` holds coordinates, not `pricing_evidence.id`** — the source,
  `observed_at` and value of the observation that was refused and the one kept.
  Rowids do not survive a sync, so an id reference would be dead in the mirror.

### 3.6 Pricing state machine (phase P4)

An observation's `pricing_type` is not a deployment's state. The deployment state
is explicit and has transitions, so the Overview can say *what changed and when*
rather than re-deriving it from a `status_reason` string:

```
UNKNOWN → FREE → FREE_WITH_QUOTA → TRIAL → PAID → PAID_AFTER_QUOTA
                                   ↘ HIBERNATED → DEPRECATED
```

A transition is a first-class record:

```json
{
  "from": "FREE_WITH_QUOTA", "to": "PAID",
  "detected_at": "2026-09-29T…",
  "evidence_before": "<observation id>",
  "evidence_after":  "<observation id>",
  "confidence": 0.97
}
```

This replaces the current derived string (`"was free, now $0.075/$0.22 per
Mtok"`) with a queryable fact, and is what lets the overview render *"Free tier
disappeared on Sep 29"*.

### 3.7 The price belief timeline (phase P6, shipped)

"What did MinInfer believe the price was on September 14?" needs a different
clock from the one the evidence log keeps, and the difference is the whole design:

* `pricing_evidence.observed_at` is the **provider's** timeline. A price can have
  been true upstream for a week before we ever saw it.
* `price_history.effective_from` is **ours** — when the reconciler adopted the
  belief — and that is what a past routing decision was actually made against.

Conflating them would make the history claim knowledge we did not have at the
time, so `test_as_of_returns_the_belief_not_the_nearest_observation` pins the
distinction: evidence stamped 2020, belief adopted 2026, and `as_of` 2021 is
correctly **empty**.

```sql
create table price_history (
  history_id     text primary key,   -- content-derived; sync drops rowids
  deploy_id      text not null,
  kind           text not null,
  usd_per_mtok   double precision,
  state          text not null,
  reason         text,
  source         text,
  observed_at    text,
  confidence     double precision,
  effective_from text not null
);
```

Four decisions:

* **`effective_to` is not stored.** It is the next row's `effective_from`, exposed
  by the `price_history_dated` view with `LEAD(...) OVER (PARTITION BY deploy_id,
  kind ...)`. Storing it would need an UPDATE on every change, and an append-only
  log that is sometimes updated is no longer a log. `effective_to IS NULL` means
  "current", so one view answers both "now" and "then".
* **Append-only *and* change-only.** A row is written only when the belief changes;
  an ingest that re-confirms $0.075 writes nothing. Without that the table becomes
  a second copy of `pricing_evidence`, which is exactly what §3.3 warned against.
* **The belief is the price *and* whether we trust it.** The same value moving
  `canonical -> quarantined` is news even though no digit changed.
* **`effective_from` is microsecond-precise**, unlike every other timestamp here,
  because two reconciles can land in the same second and "the belief in effect at
  T" has to tell them apart. It still compares correctly against a
  second-precision `as_of`, since `"."` sorts after `"+"`.

Read surface: `Store.price_history(deploy_id=None, as_of=None)` and
`GET /v1/economics/history`.

### 3.8 Cost and savings (phase P7)

The current `spend_savings` prices its counterfactual against *current* registry
prices, which is not what a session avoided. It is renamed and split so the two
numbers are never confused:

```json
{
  "actual_cost": 0.0,
  "counterfactual_cost": 0.0042,
  "estimated_avoided_cost": 0.0042,
  "basis": "lowest_verified_paid_alternative",
  "confidence": 0.81
}
```

*Actual* is what a provider charged. *Estimated* is what the cheapest verified
paid alternative **would** have charged. Kept separate so the Overview's
"Avoided" figure never reads as money that changed hands.
```

---

## 4. Calculation rules

### 4.1 Unit normalization

`usd_per_mtok = raw_value × UNITS[declared_unit].to_usd_per_mtok`. Declared unit
comes from provider config, **never** from the adapter's own arithmetic. Sentinels
(`< 0`) are dropped as `unknown` before this step (existing `price_sentinel`).

### 4.2 Cross-provider comparison — **an anomaly signal, never a price**

The central rule of this design, stated before anything else is built:

> **The cross-provider median is an anomaly signal. It is never the canonical price.**

Deployments genuinely price differently. Vercel and OpenRouter state they pass
provider list pricing through without markup, but the *deployment* is what is
priced, and two hosts serving the same weights are two economic objects. A
median over honest, differing prices is a number that **no provider publishes**:

```
Novita       $0.075
DeepInfra    $0.060
OpenRouter   $0.060
Vercel       $0.075
─────────────────────
median       $0.0675   ← exists nowhere
```

Canonicalising `$0.0675` would overwrite four correct prices with one invented
one, and the router would charge against a price that cannot be paid. So the
two outputs are kept strictly separate:

* **`DeploymentPricing`** — what *this* deployment charges, from its own
  evidence. This is the only thing the router may act on.
* **`PricingAnomaly`** — whether that price looks suspicious against independent
  peers. This never feeds the router; it feeds the operator and the quarantine.

```
# The market reference for one (weights_id, kind): one value per distinct
# source, as of each source's newest observation. Self is included — it is a
# member of the market — which is what makes the median robust when one of three
# sources is bad. Excluding self instead drags the reference halfway to the bad
# value and flags the two *honest* arms as deviant.
#
# ZERO-PRICED SOURCES ARE EXCLUDED. The deviation is a *scale* comparison, and
# scale is undefined relative to zero: `paid / 0` is infinity, so one free peer
# would quarantine every paid deployment of the same artifact. Whether an arm is
# still free is a *state* question (P4's free -> paid transition), not a
# unit-error one — and a unit error cannot turn a free price nonzero anyway.
reference = median(newest nonzero value per distinct source)   # >= 3 sources
factor    = max(value / reference, reference / value)          # >= 1

if reference is None or value == 0:   # too few sources, or a free tier
    if history >= 10x:           → suspect   (value kept)
else:
    if factor >= 10:             → quarantined (fall back to OWN previous value)
    elif factor >= 5:            → suspect   (value kept)
    elif history >= 10x:         → canonical (repricing confirmed across sources)

if state in (canonical, suspect) and age > 30d:
                                 → expired   (value kept, flagged)
```

Shipped thresholds: `PRICE_MIN_SOURCES = 3`, `PRICE_PEER_SUSPECT_FACTOR = 5`,
`PRICE_PEER_QUARANTINE_FACTOR = 10`, `PRICE_HISTORY_FACTOR = 10`,
`PRICE_FRESHNESS_DAYS = 30`. The 10× is the Novita unit-error scale, so it must
quarantine. Thresholds are module constants and the decision is re-derivable, so
changing one is `mi reconcile`, not a migration.

**Why the reference must include self.** With three sources
(`novita $0.75 (bad)`, `openrouter $0.075`, `deepinfra $0.075`), a peers-only
median over `openrouter`'s peers is `(0.75 + 0.075) / 2 = 0.4125`, which flags
`openrouter` and `deepinfra` as 5.5× deviant. Including self gives
`median(0.075, 0.075, 0.75) = 0.075`, so both honest arms sit at factor 1.0 and
only the bad source is refused. This is pinned by
`test_the_honest_arms_are_not_flagged_by_one_bad_source`.

A deployment whose price is out of band is **quarantined**, not rewritten: its
previous verified value stays canonical, the suspect observation is retained for
diagnosis, and the arm is flagged. The Novita 10× is then caught before the
router sees it *and* — because the fix is anchored on evidence rather than on an
adapter literal — the false "was free, now $0.75" hibernation is *undone* once
the reconciler refuses the observation.

### 4.3 Deployment-level canonical state

What the router reads is resolved **per deployment**, from that deployment's own
evidence — never from a peer statistic:

```
observation
    ↓ same deployment?
    ↓ source authority      (provider_api > aggregator_api > probe > llm)
    ↓ freshness             (newest in-band within the window)
    ↓ historical consistency(last known value vs. this one)
    ↓ peer comparison       (signal only — may quarantine, never overrides)
    ↓
CANONICAL | SUSPECT | QUARANTINED | EXPIRED | SUPERSEDED
```

`SUPERSEDED` is why the state is append-only: the previous value is kept, so
reconciliation is auditable and a disputed canonical value can be traced to the
evidence that produced it.

**`SUPERSEDED` is per-observation, not per-deployment.** Every `pricing_evidence`
row that is not the winner is superseded by it. P2 therefore stores four
deployment states — `canonical`, `suspect`, `quarantined`, `expired` — and not a
fifth: modelling `superseded` as a deployment's current state would say nothing
about the price it currently has.

### 4.3 State resolution (free / paid / trial / unknown)

```
type(e) = free         if e.usd_per_mtok == 0 and e.pricing_type in (free, paid)
type(e) = trial        if e.expires_at is not None and now < e.expires_at
type(e) = unknown      if e.usd_per_mtok is None or superseded
type(e) = paid         otherwise
state(deploy) = type of the newest non-superseded, in-band evidence
```

A **free → paid transition** is then not a guess: it is two evidence rows with
`observed_at` order, and the Overview can show *when* it happened and *from which
source*, instead of the current derived `status_reason` string.

### 4.4 Quota headroom

```
limit, source = observed_limit ?? configured_limit ?? (None, 'unknown')
headroom      = (limit - used) / limit            # None when limit is unknown
exhausts_at   = now + (limit - used) / observed_rate_per_second   # projection
```

The projection (`exhausts_at`) is new and is the accurate-quota differentiator:
the Overview can say *"resets in 4m"* or *"will exhaust in 12m"* rather than a
bare percentage. Rate is derived from `observations` in the current window.

### 4.5 Cost-per-success and savings — made honest

`Router._cost_per_call` already computes
`(pin·tok_in + pout·tok_out)/1e6` and `cost_per_success = cost_per_call ×
estimated_calls`. The change is to source `pin/pout` from **`canonical_prices`**
(confidence-weighted) rather than the raw column, and to attach the basis to the
API response:

```
cost_per_success: 0.0004
cost_basis: "reconciled (3 sources, spread 1.2×, confidence 0.95)"
```

`spend_savings` keeps its definition (cheapest *paid* sibling of the same
artifact) but now prices against canonical prices and exposes
`savings_confidence` + `unpriced_free_calls`, so the Overview's "Avoided" figure
carries its own provenance.

---

## 5. API contract

### 5.1 Endpoints (shipped)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/economics/overview` | **The Overview page's single source.** Everything below, in one call. |
| `GET` | `/v1/economics/deployments/{id}` | One arm's whole story: resolution, belief history, transitions, anomalies, quota |
| `GET` | `/v1/economics/quota` | Bucket headroom, its source, and a probabilistic exhaustion |
| `GET` | `/v1/economics/anomalies` | The incident log, filterable by status |
| `GET` | `/v1/economics/providers` | Per-provider rollup with `min_in` provenance |
| `GET` | `/v1/economics/history` | The belief timeline; `as_of=` the belief in effect then |

**Two sketched endpoints were deliberately not built.** A `POST /v1/economics/reconcile`
and a `POST /v1/economics/evidence` both add a network write surface for something
that already has a local one — `mi reconcile` re-derives, and ingest appends evidence
as part of the run that fetched it. An HTTP write path would need its own auth story
for no capability, because the reconciler is re-derivable and the evidence is
append-only: there is nothing a remote writer could do that a local one cannot.

`{deploy_id}` is a `:path` parameter, because a real id contains a slash
(`openrouter/novita:model:free`).

### 5.2 `/v1/economics/overview` response (shipped)

A **superset of `/v1/stats`** — same counts, providers, quota, decisions, tasks and
savings, plus what P3–P6 added. The page swaps one call for another rather than
merging two payloads, so a price and its trust state are always the same reading.

```ts
interface EconomicsOverview extends Stats {
  generated_at: string

  providers: Array<{                 // existing ProviderRow + provenance
    provider: string; n: number; free_n: number; min_in: number | null
    min_in_source: string | null     // which source the cheapest price came from
    min_in_state: string | null      // canonical | suspect | quarantined | expired
  }>

  quota: Array<{                     // existing QuotaRow + provenance
    deploy_id: string; window: string; used_n: number
    limit_n: number | null           // the CONFIGURED policy limit (may be null)
    headroom: number | null          // min(configured, observed), server-side
    headroom_source: 'configured' | 'observed' | 'hybrid' | 'unknown'
    observed_limit_n: number | null; observed_remaining_n: number | null
    observed_at: string | null
    exhaustion: {                    // probabilistic, never a bare timestamp
      estimated_exhaustion_at: string | null
      confidence: number             // rises with observations, not with time
      basis: Record<string, unknown>
    }
  }>

  savings: Savings & {               // existing fields +
    price_basis: string              // how the counterfactual was priced
    priced_free_share: number | null // *coverage*, not a statistical confidence
  }

  reviews: Array<Review & {
    pricing_type_before: string | null   // the state it hibernated from
  }>

  anomalies: PricingAnomaly[]        // drives the Overview banner
  pricing_states: Record<string, number>
}
```

Two renames from the sketch, both for honesty:

* `min_in_confidence` → **`min_in_state`**. The reconciler produces a *state*, not a
  number in [0,1]; inventing a confidence would have been a second, weaker encoding
  of the same judgement.
* savings `confidence` → **`priced_free_share`**. It measures the fraction of free
  calls that had a paid sibling to price against — a coverage measure. Calling that
  "confidence" would have implied a statistical claim it does not make.

### 5.2 `/v1/economics/overview` response

Deliberately **shape-compatible** with today's `Stats` + `Savings` + `Review`
types, so the Overview swaps data source with no visual rework — plus provenance
fields that were previously missing.

```ts
interface EconomicsOverview extends Stats { /* see §5.2 */ }
```

### 5.3 Overview element → field mapping

Every free/cost element on the page, and where it now comes from:

| Overview element (today) | Today's source | New source |
|---|---|---|
| Savings card — **Spend** | `stats.savings.actual_cost_usd` | `economics.savings.actual_cost_usd` |
| Savings card — **Avoided** | `stats.savings.saved_usd` | `economics.savings.saved_usd` + `price_basis` / `priced_free_share` |
| Savings card — free-call caveat | `stats.savings.unpriced_free_calls` | same, plus `price_basis` |
| Deployments by provider — **min $/Mtok in** | `stats.providers[].min_in` | `economics.providers[].min_in` + `min_in_source` / `min_in_state` badge |
| Deployments by provider — **free-ish** | `stats.providers[].free_n` | `economics.providers[].free_n` |
| Quota headroom — **used / limit** | `stats.quota[].used_n/limit_n` | `economics.quota[]` + `headroom_source` |
| Quota headroom — **head bar** | `stats.quota[].headroom` | same + `exhaustion.estimated_exhaustion_at` tooltip |
| Selected shortlist — **$ / success** | `plan.chosen[].cost_per_success` | `price_resolution`-backed, same field |
| Selected shortlist — **headroom** | `plan.chosen[].headroom` | `economics.quota` (same `effective_headroom` implementation) |
| Hibernation banner | `api.reviews()` | `economics.reviews[]` + `pricing_type_before` |
| **NEW** anomaly banner | — | `economics.anomalies[]` |

The routing funnel and decisions log are unchanged — they are routing telemetry,
not economics.

---

## 6. Anomaly detection (the Novita regression, generalised)

Every anomaly carries a `kind`, a banded `severity`, the `deployment` and
`dimension`, `observed` vs `expected`, the `factor`, the evidence coordinates, and
a resolution. Severity is a banded *factor* against the market median, or a plain
`warning` when there is no factor to band (a history jump):

```
20x   critical   two orders of magnitude; refuse and alert
10x   high       the unit-error scale; refuse
5x    warning    suspicious; flag, keep routing
< 5x  info       reserved; flagged states do not reach here today
```

Kinds — derived from the decision already made, not re-measured:

```
unit_scale    quarantined: >= 10x the market median
spread        suspect with a factor: 5-10x the market median
history_jump  suspect with no market: a sudden change from the arm's own history
```

**`stale` is deliberately not a kind.** A stale price is a freshness fact —
already a `price_resolution` state, and fixed by re-ingesting — not a
disagreement a human has to adjudicate. Opening one anomaly per aged-out
deployment would bury the real ones.

**Quarantine is a state, not a discard.** A refused observation is retained so the
incident is diagnosable; the deployment keeps its own previous verified value.
The states are:

```
canonical  |  suspect  |  quarantined  |  expired
```

and the incident lifecycle on top of them:

```
flagged  ->  open  ->  acknowledged (a human has seen it; still wrong)
                   ->  resolved     (the price returned to canonical)
a `resolved` incident that is still flagged reopens as a NEW row
```

`tests/test_price_units.py` is complemented by three tiers in `tests/pricing/`,
matching the structure below.

**Tier 1 — unit conversion (one provider, one factor).**
```python
def test_novita_prices_are_per_myriad_mtok(): ...
@pytest.mark.parametrize("provider,raw,expected", ALL_PROVIDER_UNIT_CASES)
def test_every_declared_unit_converts(...): ...
def test_every_adapter_declares_a_unit(): ...   # catalogue.SOURCES ⊆ PROVIDER_PRICE_UNITS
```

**Tier 2 — cross-provider consistency (same weights, independent sources).**
```python
def test_novita_matches_openrouter_for_the_same_weights(): ...
def test_reconciler_agrees_across_three_providers(): ...
```

**Tier 3 — anomaly detection (inject the failure, assert it is caught).**
```python
def test_a_10x_price_is_flagged_unit_scale_and_never_canonical():
    # 5 peers at $0.075, one at $0.75 (the old /1000 bug)
    # assert: anomaly kind == 'unit_scale', severity == 'critical'
    # assert: the suspect observation is QUARANTINED, the previous
    #         verified deployment price is unchanged
    # assert: the peer median never becomes any deployment's price

def test_a_free_page_and_a_paid_feed_are_reconciled_not_guessed():
    # Novita page: free; API feed: $0.075. Two evidence rows, newest wins the
    # state, the older is superseded — and the Overview shows the transition.
```

That third test is the one that would have caught this incident **before** the
false hibernation, which is the real product claim.

---

## 7. Serverless deployment

### 7.1 Modal (compute + read API)

```python
# deploy/modal/app.py  (new)
import modal
app = modal.App("mininfer-pqs")

image = (
    modal.Image.debian_slim()
    .pip_install_from_pyproject("pyproject.toml")
    .add_local_python_source("mininfer")
)

@app.function(image=image, schedule=modal.Cron("*/30 * * * *"), secrets=[...])
def reconcile_job():
    from mininfer.pricing.reconcile import run_all
    return run_all()

@app.function(image=image, secrets=[...])
@modal.fastapi_endpoint(method="GET")
def overview():
    from mininfer.pricing.snapshot import overview
    return overview()   # reads canonical_prices / quota_state / price_anomalies
```

- Cron every 30 min: ingest → evidence → reconcile → write canonical + anomalies.
- `secrets` holds `SUPABASE_DB_URL`. No provider keys needed for Tier-0 sources.
- Endpoint is a pure read → cacheable, no cold-start cost paid by the dashboard
  because the dashboard can be pointed at a cached edge route.

### 7.2 Vercel (dashboard)

`web/` builds to `dist/` and is already served. Two integration choices:

- **Same-origin (recommended for prod):** FastAPI proxy adds a passthrough
  `GET /v1/economics/overview` → Modal endpoint, cached 30s. The dashboard code
  is unchanged in origin terms; no CORS.
- **Direct (simplest for a Vercel-hosted dashboard):** set `VITE_PQS_URL` and have
  `api.ts` call Modal directly with `Access-Control-Allow-Origin` for the Vercel
  domain.

### 7.3 Config

```bash
SUPABASE_DB_URL=…            # already used by `mi sync`
PQS_RECONCILE_MINUTES=30
PQS_FRESHNESS_DAYS=30
PQS_SCALE_ANOMALY_FACTOR=20  # critical threshold
PQS_SPREAD_ANOMALY_FACTOR=5  # warn threshold
VITE_PQS_URL=                # empty = same-origin passthrough
```

---

## 8. Frontend integration (exact surface)

1. `web/src/lib/api.ts`
   - add `EconomicsOverview`, `EconomicsProviderRow`, `EconomicsQuotaRow`,
     `Anomaly` types;
   - add `api.economics()` → `GET /v1/economics/overview`.
2. `web/src/components/Overview.tsx`
   - fetch `api.economics()` alongside `api.stats()` in `refresh()`;
   - bind the **Savings card**, **Deployments by provider** (`min_in`,
     `free_n`), and **Quota headroom** tables to `economics` (fields are
     name-compatible, so the JSX barely changes);
   - add provenance affordances: a `source` badge on `min_in`, a
     `headroom_source` chip, an `exhausts_at` tooltip;
   - add an **Anomalies banner** modelled on the existing hibernation banner
     (`reviews.length > 0`), showing `economics.anomalies`.
3. `web/src/components/ModelsTab.tsx` / `explore.ts` — optional: surface
   `canonical price + confidence` in the explorer.
4. Tests: extend `web/src/lib/explore.test.ts`-style unit tests; the pricing
   logic itself is tested in Python.

---

## 9. Phasing

> **P0–P8 are implemented; P8's deploy is the remaining step and needs two things
> from you** — a Postgres DSN and a go-ahead to deploy to your Modal account.
> P0–P7 are the economics layer (units → evidence → reconciler → anomalies → states
> → quota → belief history → read API + Overview). P8 is the serverless host and,
> more importantly, the **Postgres validation** that found three pre-existing bugs
> which would have broken a hosted deployment on day one.

| Phase | Deliverable | Ships |
|---|---|---|
| **P0** ✅ | `pricing/{units,schema,providers}.py`; every priced adapter reads through a contract; golden fixtures; table-driven unit, contract, cross-provider and no-local-conversion tests | Kills the unit-bug class; no infra, no DB change |
| **P1** ✅ | `pricing_evidence` append-only table; `Bundle.prices`; `Store.upsert_deployment(prices=…)` records observations and resolves the stored price (provisional `newest per kind`); adapters stop setting `price_*` | Evidence becomes authoritative input |
| **P2** ✅ | `Store.reconcile_prices()`: authority + freshness + own-history + an independent-source **market median used only as a signal**; `price_resolution` with `canonical/suspect/quarantined/expired`; `mi reconcile`; the free→paid transition moved behind one shared decision | Deployment-level canonical state, and the Novita false-hibernation is undone |
| **P3** ✅ | `price_anomalies` incident log (`unit_scale`/`spread`/`history_jump`, banded severity, evidence coordinates); open → acknowledged → resolved lifecycle; `mi anomalies`, `mi anomaly` | Anomalies become a reviewable log |
| **P4** ✅ | `deployments.pricing_state` derived by `_sync_pricing_states`; `pricing_transitions` log; `mi transitions` | "Free tier disappeared on Sep 29" as a query |
| **P5** ✅ | Observed rate-limit headers (`quota.parse_rate_limit_headers`), `429` classification, `min(configured, observed)` headroom, probabilistic `estimated_exhaustion_at`, runtime retirement of not-callable deployments | Accurate, not just configured, quota |
| **P6** ✅ | `price_history` belief timeline (append-only, change-only, `effective_from` = *our* clock); `price_history_dated` derives `effective_to`; `mi history --as-of`; `GET /v1/economics/history` | "What did we believe on Sep 14?" as a query |
| **P7** ✅ | `/v1/economics/{overview,deployments,quota,anomalies,providers}`; `_stats_data` reuses one store; Overview binds the single snapshot; anomaly banner; provenance badges | The page populates from the service |
| **P8** | Serverless host: `deploy/modal/app.py` (cron + `@modal.asgi_app`) serving the *real* proxy app, `PQS_*` env thresholds, raw-lake Volume, worker loops given an explicit `mi reconcile`; **full suite validated on Postgres** | Hosted, or one `modal deploy` away |

P0 is pure Python and lands behind the existing tests with no infrastructure and
no schema change. Every later phase can be developed locally first; Modal,
Supabase and Vercel are a *deployment* decision, not an architecture one, and are
therefore last.

---

## 10. Risks & open decisions

1. **Two writers to `deployments.price_*` during migration.** Mitigation: make
   ingest stop writing the column in P1 and expose `deployments_priced` as a view
   so every existing reader (router, explore, providers_summary, savings) is
   untouched. Do not run both writers.
2. **Peer comparison needs ≥ 2 independent providers for a `weights_id`.** Many
   weights are single-provider. There the comparison degrades to
   `basis='single_source'` with lower confidence, and `unit_scale` detection is
   unavailable — so the per-provider `validate_prices` band still runs as the
   backstop. The *deployment's own price* is unaffected either way: peer
   comparison never sets it.
3. **`weights_id` matching across providers.** `normalize.weights_id_for` +
   `openrouter.hugging_face_id` already do most of this; the reconciler should
   only group on exact `weights_id` (never fuzzy) so it cannot compare a 7B to a
   70B and call it a 10× anomaly.
4. **Observed quota limits are provider-specific header names.** Start with the
   providers we actually call (OpenRouter, Groq, Google, Novita); unknown
   providers keep `limit_source='configured'`.
5. **Modal vs. Vercel ownership of the read endpoint.** Recommend Modal for the
   engine, proxy-passthrough for the read path. Revisit if the dashboard
   migrates fully to Vercel and a Modal hop is unacceptable.
6. **Cost.** Cron every 30 min over ~14 keyless sources is cheap; the reconciler
   is O(evidence rows) in one Postgres pass. No per-request upstream calls.

---

## 11. Definition of done

**P0 — done:**

- ✅ Every priced adapter names a provider and calls `read_prices()`; no adapter
  contains a conversion factor (`tests/pricing/test_no_local_conversion.py`).
- ✅ Adding a priced provider without a contract raises rather than guessing.
- ✅ Every provider's field path and unit are pinned by a golden payload.
- ✅ Novita's `/10000` is a regression test at both the contract and adapter level.
- ✅ A second provider's price confirms the unit, and the peer **median is pinned
  as an anomaly signal that is never a published price**
  (`test_peer_median_is_not_a_published_price`).

**P1 — done:**

- ✅ An adapter cannot set `Deployment.price_*` — enforced by an AST guard, not a
  convention (`test_no_adapter_sets_a_price_on_the_deployment`).
- ✅ `pricing_evidence` is append-only: a re-read adds a row, never edits one.
- ✅ Resolution is **deployment-level** and picks `observed_at DESC, id DESC`, so a
  replayed older snapshot cannot win by insertion order
  (`test_resolution_reads_only_this_deployment`, `…replayed_older_snapshot…`).
- ✅ `deployments_priced` exposes which source produced the price, when, and with
  what confidence.
- ✅ A pre-P1 registry gains the table and view on open, with rows intact and the
  view querying cleanly over legacy rows (validated against the 1,703-deployment
  local registry).
- ✅ The router reads the store-resolved price, verified end to end.
- ✅ `translate()` is a no-op for the Postgres view, so the mirror cannot corrupt it.

**P2 — done:**

- ✅ A source at 10× the market is `quarantined` and its value falls back to the
  deployment's **own** previous observation — never the market median
  (`test_the_quarantine_never_adopts_the_market_price`,
  `test_quarantine_falls_back_to_the_arm_own_previous_value`).
- ✅ The original incident is fixed end to end: a bogus 10× read of a *free* model
  produces a provisional "was free, now $0.7500" hibernation, and `reconcile_prices`
  refuses the observation, restores the free price, and returns the arm to `live`
  (`test_a_false_hibernation_is_undone_by_reconcile`).
- ✅ One bad source does not flag the honest arms: the reference is the market
  median *including self*, so the good arms sit at factor 1.0. A peers-only median
  would have flagged them as 5.5× deviant.
- ✅ A gateway gets **one vote**: `openrouter/a` cannot vouch for `openrouter/b`.
- ✅ A big change the whole market agrees with is a `canonical` repricing, not a bug.
- ✅ With too few sources to form a market, only the history rule applies (`suspect`,
  value kept).
- ✅ A stale observation is `expired` but kept.
- ✅ Reconcile is idempotent and **re-derivable**: changing a threshold and re-running
  `mi reconcile` changes the decision with no migration.
- ✅ `mi ingest` reconciles once, after every source, so the market is complete
  (`test_ingest_reconciles_once_after_every_source`).
- ✅ The free→paid transition is one pure function used by both writers, so the
  evidence path and the caller-supplied path cannot drift apart.
- ✅ A P1-era registry gains `price_resolution` and the rewritten view on open, with
  1,703 deployments intact (validated in a fresh interpreter).
- ✅ `translate()` is a no-op for the Postgres view.

**P4 — done:**

- ✅ Nine states derived in one pure function with an explicit priority order:
  `unknown`, `free`, `free_with_quota`, `paid_after_quota`, `trial`,
  `subscription`, `paid`, `hibernated`, `deprecated`.
- ✅ `trial` and `subscription` are **not** `free` — a depleting balance and a
  prepaid fee are the two cases where calling an arm free spends money.
- ✅ `hibernated` and `deprecated` win over any price: the router drops those arms,
  so describing them by price would be a lie the UI repeats.
- ✅ Every change is logged with `from_state`/`to_state`, the prices, and the
  coordinates that produced it; the first state records `from_state = NULL`.
- ✅ The pass covers **every** deployment, not only the priced ones, so a seeded or
  agent-ingested row is described too.
- ✅ Validated against the real 1,703-deployment registry: the column is added by
  `ALTER` on open, the table is created, and one reconcile derives a state for
  every row (1326 paid, 177 unknown, 101 trial, 51 free_with_quota, 35 deprecated,
  13 free).
- ✅ A free→paid change lands in `hibernated`, not `paid` — the spend event and the
  state machine agree because they share `_spend_transition`.
- ⚠️ A transition is detected *between reconciles*: `detected_at` is when we
  noticed, not when the provider changed. Replaying evidence to reconstruct a past
  belief is P6.

**P5 — done:**

- ✅ Real header families parse: `x-ratelimit-limit-{-requests,-tokens}`, the bare
  OpenRouter trio, the `anthropic-ratelimit-*` trio, and a bare `retry-after`.
  Three duration spellings are handled — plain seconds, Groq's compound
  `2m59.56s`, and an HTTP date.
- ✅ A metric gets **one vote**: a provider sending both the specific and the bare
  header is reported once, not twice.
- ✅ Headroom is `min(configured, observed)` through **one** implementation
  (`store.effective_headroom`) shared by the router and the operator view, so they
  cannot disagree about whether an arm is running out. A policy of 1000/day cannot
  spend a provider's observed 20/min; a provider's generous limit cannot override a
  tight policy.
- ✅ `remaining` is stored exactly as reported, including negative — clamping would
  hide a misconfigured bucket behind a plausible number.
- ✅ A `429` is classified (`rpm`/`rpd`/`tpm`/`concurrency`/`account`/`unknown`) and
  the reason is stored on the observation. `unknown` is a real answer: a 429 with
  remaining > 0 is not a request-limit hit, and guessing `rpm` sends the operator
  to fix the wrong limit.
- ✅ Exhaustion is *probabilistic*: `estimated_exhaustion_at`, a `confidence` that
  rises with the number of observations, and the `basis` it came from. No estimate
  is given when the window refills first.
- ✅ A not-callable deployment is retired at runtime and the retirement is sticky
  across re-ingest (`status_source='runtime'`), reversible with `mi enable`, and
  listed by `mi disabled`.
- ✅ Adjacent regression fixed: `paid -> paid` kept the existing status
  unconditionally, so a source-reported `deprecated`/`degraded` could never land.
- ✅ Validated against the real 1,703-deployment registry: all three new columns are
  added by `ALTER` on open, every legacy row reads as `status_source='source'`, and
  reconcile still derives 1,703 states.

**P6 — done:**

- ✅ `price_history` is append-only *and change-only*: a re-confirmed price writes
  nothing, so it answers the question instead of duplicating the evidence log.
- ✅ `effective_to` is **derived** (`LEAD` in a view), not stored, so the table is
  never updated — and `effective_to IS NULL` is the current belief.
- ✅ `effective_from` is *our* clock and microsecond-precise; `observed_at` stays the
  provider's. Pinned by a test where the evidence is stamped 2020, the belief is
  adopted in 2026, and `as_of` 2021 is empty.
- ✅ A trust change at the same price (canonical -> quarantined, value unchanged) is
  a new belief.
- ✅ `as_of` returns the belief in effect, not the nearest observation; a far-past
  `as_of` is an empty answer rather than an error.
- ✅ Exposed as `mi history [deploy_id] [--as-of T]` and
  `GET /v1/economics/history`.
- ✅ Validated against the real 1,703-deployment registry: the table and view are
  created on open with rows intact, and the legacy registry reconciles cleanly with
  zero history rows (it predates `pricing_evidence`).

**P7 — done:**

- ✅ `/v1/economics/overview` is a **superset of `/v1/stats`**, so the page swaps one
  call for another and can never render a price beside a *different* moment's trust
  state. Pinned by `test_the_overview_is_a_superset_of_stats`.
- ✅ `/v1/economics/{deployments/{id},quota,anomalies,providers}` fill out the family;
  `{id}` is a `:path` parameter because real ids contain slashes.
- ✅ `providers_summary` now carries `min_in_source`/`min_in_state`, and `reviews()`
  carries `pricing_type_before` — a reconciled price is a claim, and a claim with no
  provenance is one the reader has to take on faith.
- ✅ The Overview fetches `api.economics()` once, renders an **anomaly banner** above
  the fold, and shows provenance chips on the provider `min_in`, the quota headroom
  source, and the hibernation transition.
- ✅ `api.reviews()` was removed as dead once the page read reviews from the single
  snapshot; the `/v1/reviews` endpoint stays for API clients.
- ✅ Two sketches were deliberately *not* built, and the plan says so: no `POST
  /v1/economics/reconcile` and no `POST /v1/economics/evidence`. Both add a remote
  write surface for a capability that is already local and re-derivable.
- ✅ Two names changed from the sketch for honesty: `min_in_confidence` →
  `min_in_state` (the reconciler emits a state, not a number in [0,1]) and savings
  `confidence` → `priced_free_share` (coverage, not a statistical claim).
- ✅ Validated against the real 1,703-deployment registry: `providers_summary` 1 ms,
  `_stats_data` on one store 11 ms, and the state distribution matches P4's.
- ✅ `tsc --noEmit` clean; 88 frontend tests pass.

**P8 — done, except the deploy itself:**

- ✅ **The whole suite passes on Postgres: 815 passed, 4 skipped** (SQLite: 818/1).
  That is the validation that matters, and it found three **pre-existing** bugs that
  only appear on the hosted engine:
  - `status_changed_at = CASE … THEN CURRENT_TIMESTAMP ELSE <text> END` — Postgres
    refuses a `CASE` whose branches are `timestamptz` and `text` (a bare value
    assignment casts; a `CASE` will not). 235 tests failed on this one line.
  - Two unaliased subqueries in `FROM` — Postgres requires an alias, SQLite does
    not, so the bug only exists on the hosted engine.
  - Both were invisible from SQLite, which is the argument for running the suite on
    the engine you deploy to.
- ✅ `deploy/modal/app.py`: a `@modal.Cron` (`mi ingest --force` then `mi reconcile`)
  and `@modal.asgi_app()` returning **`mininfer.proxy:app`** — not a copy of it.
  A structural test asserts the app reaches into neither the reconciler nor its
  thresholds, so it cannot drift into a second engine.
- ✅ Scope decided by *secrets*, not a mode flag: `MI_DB` alone is a read-only
  economics service (model calls fail with `no_api_key`, which the proxy already
  does); provider keys turn routing on. No second mode to keep correct.
- ✅ Thresholds are `PQS_*` env-driven, so retuning the reconciler is `mi reconcile`
  rather than a release. A malformed value falls back to the default — a typo in a
  secret must not take the reconciler offline.
- ✅ The worker loops (`fly.toml`, `k8s.yaml`) now run an explicit `mi reconcile`
  after `mi ingest`: ingest reconciles at the end of its own run, so the explicit
  pass covers the case where *every source failed* and nothing triggered it.
- ✅ The Modal image needs no frontend build — verified that with no `web/dist` the
  proxy still imports and serves `/` (server-rendered fallback), `/healthz` and
  `/v1/economics/overview`.
- ⏳ **Your step:** a Postgres DSN and `modal deploy`. Both are in
  `deploy/modal/README.md`, together with the cost shape and what is deliberately
  not included (the React build, `mi sync`, the two POST endpoints).

**Later phases:**

- Anomalies become a reviewable history (`price_anomalies`). **Done — P3.**
- The pricing *state machine* (`FREE_WITH_QUOTA`, `TRIAL`, `PAID_AFTER_QUOTA`, …) and
  recorded transitions.
- Every free/cost element on the Overview reads from `/v1/economics/overview`.
- Quota headroom reports its `source` (`observed` vs `configured`), is the minimum
  of the provider's and MinInfer's limits, and projects exhaustion with a
  confidence rather than as a fact.
- Savings carries `actual_cost`, `counterfactual_cost`, `estimated_avoided_cost`,
  `basis` and `confidence`.

**P3 — done:**

- ✅ One row per ongoing incident, not per reconcile: a second pass reports
  `refreshed`, never `opened` (`test_re_reconciling_refreshes_rather_than_duplicating`).
- ✅ An acknowledged incident survives every later reconcile — the refresh touches
  neither `status` nor `opened_at`.
- ✅ Acknowledging changes the alert, never a price.
- ✅ Closing a still-wrong incident reopens it as a **new** row, so resolving is not
  silencing.
- ✅ Severity is banded (20× critical, 10× high, 5× warning) and the log is ordered
  worst-first.
- ✅ The evidence record carries coordinates (source, `observed_at`, value), not
  `pricing_evidence.id`, which `sync` drops.
- ✅ An incident closes automatically when the price returns to canonical.
- ✅ `stale` is deliberately not a kind — expiry is a freshness state, not a
  disagreement, and logging it would drown the real anomalies.
- ✅ A P2-era registry gains the table on open, with 1,703 deployments intact.
