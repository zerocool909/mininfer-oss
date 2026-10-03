# `Store` — Why It Is the Bridge of the MinInfer Graph

This document is the output of a graphify trace of the single most-connected node in
the MinInfer codebase: the `Store` class in `mi/store.py`. It answers one question —

> **Why does `Store` connect `Store Operations` to `Routing & Bandit Logic`,
> `Benchmark Suite`, `Agent Ingestion & Fetching`, `Agent Resolution`, `CLI Commands`,
> `Proxy API Server`, `Model Normalization`, `Database Sync`,
> `Deployment & Weights Schema`, `Fallback Handling`, `Quota Usage Tracking`, and the
> test communities?**

The short answer: **`Store` is the persistence boundary.** Every subsystem in MinInfer
either writes its facts into `Store` or reads the registry back out of it, so the node
sits on the edge of almost every community at once. The graph confirms this
structurally: 101 edges, 76 `EXTRACTED` and 25 `INFERRED`, reaching **17 of 23
communities**.

---

## 1. Node identity

| Field | Value |
|---|---|
| Graph id | `dr_store_store` |
| Label | `Store` |
| Source | `mi/store.py:183` |
| Community | 9 — **Store Operations** |
| Degree | 101 edges (the graph's #1 god node) |
| Betweenness | 0.353 (highest in the graph) |

`Store` is a single class — the SQLite registry. Its docstring states the contract:
*same SQL runs on Supabase Postgres* (PLAN.md §5), so the local SQLite file is a
stand-in for the production registry.

---

## 2. What `Store` actually is

`Store` owns the **normalised hot registry** (the "cold evidence" lives separately in
`raw/`, keyed by sha256). It wraps a `sqlite3` connection and exposes every mutation
and read the rest of the system needs:

| Area | Methods |
|---|---|
| Ingest / writes | `upsert_weights()`, `upsert_deployment()`, `record_snapshot()`, `_add_evidence()` |
| Quotas | `set_bucket()`, `set_limit()`, `_reset_expired_for()`, `reset_expired()`, `record_usage()`, `exhaust()`, `headroom()` |
| Observations | `observe()`, `stats()`, `record_decision()` |
| Reads | `deployments()`, `counts()` |
| Entity resolution | `unstaged_weights()`, `merge_weights()`, `merged_ok()`, `quarantine_rows()` |
| Lifecycle | `__init__()`, `close()`, `commit()` |

### Schema (8 tables, 2 views)

- **`weights`** — the artifact (benchmarks, capabilities, params, family, aliases)
- **`deployments`** — the callable thing (price, limits, latency, uptime, quantization)
- **`evidence`** — provenance rows; *nothing is stored without evidence*
- **`snapshots`** — pointers into the immutable raw lake, keyed by sha256
- **`quota_buckets`** — free-tier headroom per `(deploy_id, window, api_key_alias)`
- **`observations`** — append-only outcome records (never UPDATE, stats are derived)
- **`decisions`** — routing decision audit trail
- **`quarantine`** — disagreements refused at write time (e.g. a bad weight merge)
- **`weight_aliases`** — entity-resolution merges, kept auditable (alias, not delete)
- **`weights_resolved`** (view) — weights joined against aliases
- **`routing_stats`** (view) — wins / latency / 429s / cost per `(deploy_id, task)`

The design rules encoded here: benchmark data attaches to `weights`, economics attach
to `deployments`, `first_seen` is never overwritten, every ingest write records
evidence, and observations are append-only.

---

## 3. Edge anatomy — the 101 connections

| Relation | Count | Confidence | Meaning |
|---|---|---|---|
| `calls` | 36 | EXTRACTED | 5 production call-sites + 31 test call-sites |
| `imports` | 16 | EXTRACTED | 16 modules import `Store` |
| `method` | 23 | EXTRACTED | `Store` owns 23 methods |
| `contains` | 1 | EXTRACTED | `store.py` contains `Store` |
| `uses` | 25 | INFERRED | 25 model-reasoned edges needing verification |

**Direction:** 26 outgoing (23 `method` self-edges + 3 `uses` to schema types), 75
incoming (everyone else calls/imports/uses `Store`).

### Outgoing (Store → X)

- `uses` → `Evidence`, `Weights`, `Deployment` *(mi/schema.py)* — the three schema
  types `Store` persists. These three `uses` edges are INFERRED.

### Incoming (X → Store) — who depends on it

**Production call-sites (`calls`, EXTRACTED):**

| Caller | Module | Community |
|---|---|---|
| `commit_node()` | `mi/agent_ingest.py` | Agent Ingestion & Fetching |
| `validate_node()` | `mi/agent_ingest.py` | Agent Ingestion & Fetching |
| `apply_node()` | `mi/agent_resolve.py` | Agent Resolution |
| `_open()` | `mi/cli.py` | CLI Commands |
| `_store()` | `mi/proxy.py` | Proxy API Server |

**Importers (`imports`, EXTRACTED) — 16 modules:**

`agent_ingest.py`, `agent_resolve.py`, `bench.py`, `cli.py`, `proxy.py`, `quota.py`,
`resolve.py`, `router.py`, and 8 test modules (`test_agent_ingest`, `test_agent_resolve`,
`test_bandit`, `test_bench`, `test_proxy`, `test_quota`, `test_router`, `test_sync`).

**Inferred dependents (`uses`, INFERRED) — 25 edges, the interesting half:**

- Router: `benchmark_norms()`, `build_candidates()`, `route()`
- Quota CLI: `_targets()`, `seed()`, `describe()`
- Resolver/agents: `propose()`, `adjudicate()`, `_meta()`, `_pairs_from_proposals()`,
  `_cross_check()`, `_write_candidate()`, `_write_quarantines()`
- Bench: `_deploy_prices()`, `_cost_usd()`, `run_bench()`
- Proxy: `_deploy_cost()`, `try_fallbacks()`
- Test helpers: `_eligible()`, `_seed()`, `_seed_two_arms()`, `test_next_reset_advances_utc()`

These 25 are worth eyeballing — they are the graph's model-reasoned guesses about
data flow, not literal call edges. See §6.

---

## 4. Community reach — 17 of 23 communities

`Store`'s edges land in 17 communities (the 6 it does *not* touch: `Execution`,
`Execution Runner`, `Call Execution`, `CA Bundle Script`, `Package mininfer`, `Evidence
Schema` — where `Evidence Schema` is touched only via the outgoing `uses → Evidence`
edge's *target* community, i.e. it *does* reach it through `uses`).

| Community | Edges | Direction |
|---|---:|---|
| Store Operations | 14 | out (methods) |
| Routing & Bandit Logic | 11 | in |
| Agent Resolution | 9 | in |
| Deployment & Weights Schema | 9 | out 2 / in 7 |
| Benchmark Suite | 8 | in |
| Quota Tests | 8 | in |
| Agent Ingestion & Fetching | 7 | out 1 / in 6 |
| Fallback Handling | 6 | in |
| Quota Usage Tracking | 6 | out 5 / in 1 |
| CLI Commands | 5 | in |
| Store & SQLite | 5 | in |
| Proxy API Server | 4 | in |
| Evidence Schema | 4 | out |
| Database Sync | 2 | in |
| Proxy Queue Tests | 1 | in |
| Model Normalization | 1 | in |
| Proxy Tests | 1 | in |

### The bridge, explained per subsystem

- **Router** (`mi/router.py`) reads `deployments()` to build candidate lists, then
  `headroom()` to price free arms — so `Store` is the router's only source of truth.
- **Proxy** (`mi/proxy.py`) opens a `Store`, charges quota via `record_usage()`, and
  records every real call through `observe()` — success *or* 429.
- **Ingest** (`mi/ingest.py` → `agent_ingest.py`) writes `weights`, `deployments` and
  `evidence` rows; the validator cross-checks against the registry before committing.
- **Resolver** (`mi/resolve.py` → `agent_resolve.py`) reads `unstaged_weights()` and
  writes `merge_weights()` / `quarantine` — the merge guard refuses on param-count
  mismatch.
- **Quota** (`mi/quota.py`) seeds declared free-tier limits and reports live headroom.
- **Bench** (`mi/bench.py`) records verified observations that seed the router's
  Wilson priors.
- **Sync** (`mi/sync.py`) reads the SQLite registry to push to Supabase Postgres.
- **CLI** (`mi/cli.py`) is the front door — every `mi` subcommand ends at a `Store`.

This is why the node is central: **it is not a hub that *does* everything, it is the
single table everyone agrees to read and write.** Betweenness 0.353 is the graph
quantifying "single source of truth".

---

## 5. Honest caveats

1. **25 of 101 edges are INFERRED.** They are the graph's reasoned guesses (from AST
   context), not literal call edges. The 3 outgoing `uses → Evidence/Weights/Deployment`
   edges and the 22 incoming `uses` edges should be verified against the source. The
   genuinely load-bearing production paths (`calls` + `imports`) are all EXTRACTED.

2. **Graph health warnings** from the build (non-fatal, typical of AST extraction):
   82 dangling-endpoint edges, 1 self-loop, 186 collapsed multi-relation edges. These
   reflect the same node-pair having several relations (`calls` + `references` +
   `uses`), and external-reference targets that are not nodes in this corpus.

3. **1 `.sql` file contributed nothing** to the graph — `tree_sitter_sql` is not
   installed (`pip install "graphifyy[sql]"` to include SQL DDL parsing).

---

## 6. Suggested follow-ups

- Verify the 25 INFERRED `uses` edges, especially `route()` → `Store`,
  `build_candidates()` → `Store`, and `try_fallbacks()` → `Store`.
- Trace `Deployment` and `Weights` (the next two god nodes) — they are the *data*
  that flows through `Store`; together the three nodes form the schema spine.
- Trace `CallResult` / `try_fallbacks()` to see the fallback path that charges quota
  and records observations.

---

*Generated by graphify (code-only, AST extraction) against `graphify-out/graph.json` —
549 nodes, 1,495 edges, 23 communities.*
