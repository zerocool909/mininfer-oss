# Serverless economics service (Modal)

The economics layer, hosted without a long-running machine. Two functions:

| Function | Trigger | Runs |
|---|---|---|
| `maintain` | `@modal.Cron("0 */6 * * *")` | `mi ingest --force` then `mi reconcile` |
| `api` | `@modal.asgi_app()` | `mininfer.proxy:app`, as-is |

**Nothing is reimplemented.** `api` returns the same FastAPI app the container
serves, and `maintain` runs the same CLI the container's worker runs. A fix to the
router or the reconciler lands here the moment it lands anywhere — the same
argument `Dockerfile` makes for one image with two process types, applied to a
second platform.

---

## What is needed

### 1. A Postgres (required)

The shared registry. `Store` picks the engine from the target string, so a
`postgresql://` DSN is the only switch:

```bash
modal secret create mininfer MI_DB="postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres"
```

Use the **pooler** host. Newer Supabase projects expose the direct
`db.<ref>.supabase.co` host over IPv6 only, which fails on many networks. The
same note is in `.env.example`.

The schema is applied on first connect (`Store` runs `supabase/schema.sql` when
the target is Postgres) — 15 tables and 5 views, idempotent, so nothing to
migrate by hand.

### 2. Provider keys (optional, and they decide the scope)

There is deliberately **no read-only mode flag**. Scope is which secrets exist:

* **`MI_DB` alone** → a read-only economics service. `/v1/economics/*` answers;
  every model call fails with `no_api_key`, which is exactly what the proxy already
  does for a provider whose key is absent.
* **Add provider keys** → routing and chat turn on.

```bash
modal secret create mininfer \
  MI_DB="postgresql://…" \
  OPENROUTER_API_KEY="…" GROQ_API_KEY="…" GEMINI_API_KEY="…"
```

The keyless Tier-0 sources (OpenRouter catalogue, Vercel, HuggingFace, DeepInfra,
Novita, SambaNova, Chutes, NVIDIA) need no key at all — a cold start with only
`MI_DB` still produces a populated registry.

### 3. Access control, if it is public

```bash
modal secret create mininfer MI_DB="…" \
  MI_ADMIN_TOKEN="$(openssl rand -hex 32)" \
  MI_API_KEYS="sk-live-…:acme"
```

Without these the service is **open**. That is the same opt-in default the
container has, and it is the right one for a private
deployment and the wrong one for a public URL.

### 4. A raw-evidence Volume (already declared)

`MI_RAW=/data/raw` on a Modal Volume. The raw lake is the audit trail the whole
design leans on — *"parsers change; you must be able to re-derive the registry
from history"* — so it does not belong on an ephemeral disk. The app creates the
volume on first deploy (`create_if_missing=True`).

---

## Deploy

```bash
modal secret create mininfer MI_DB="postgresql://…"
modal deploy deploy/modal/app.py
```

Then populate the registry once (the cron would also do it within 6 hours):

```bash
modal run deploy/modal/app.py::maintain
```

`modal run` executes the function once in a temporary container rather than
waiting for the schedule.

---

## Retuning the reconciler

The thresholds are environment-driven, so changing one is a re-run rather than a
release — which is the point of a re-derivable decision:

| Variable | Default | Meaning |
|---|---|---|
| `PQS_SCALE_FACTOR` | `10` | deviation from the market median that **refuses** a price |
| `PQS_SPREAD_FACTOR` | `5` | deviation that flags one (`suspect`) |
| `PQS_CRITICAL_FACTOR` | `20` | severity band |
| `PQS_HISTORY_FACTOR` | `10` | jump from the arm's own previous value |
| `PQS_MIN_SOURCES` | `3` | independent sources before a market median exists |
| `PQS_FRESHNESS_DAYS` | `30` | age at which a belief reads `expired` |
| `PQS_EXHAUSTED_HEADROOM` | `0.05` | headroom at which a free bucket reads spent |

Ten is the Novita unit-error scale, so it must quarantine. Three sources, not two:
with two values the median *is* their mean, which a single outlier moves.

A malformed value falls back to the default — a typo in a secret must not take the
reconciler offline.

---

## What is *not* here

* **The React dashboard build.** The image ships `mininfer/` and `config/`, not
  `web/dist`. `/` therefore serves the dependency-free server-rendered dashboard.
  Add a built `web/dist` as a third `add_local_dir` if you want the React one.
* **`mi sync`.** It exists for the *local* workflow (SQLite → Supabase mirror). With
  Postgres as the primary there is nothing to mirror: Modal writes to it directly.
* **The `/v1/economics/reconcile` and `/v1/economics/evidence` POST endpoints.**
  Deliberately not built. A remote write surface
  for a capability that is already local and re-derivable is surface without value.

---

## Cost

The cron is a batch container ~4×/day; the read API scales to zero between
requests (`scaledown_window=60`). The only always-on cost is the Postgres. The
`max_containers=1` on `maintain` is not a cost decision — two concurrent ingests
writing one registry is the failure the reconciler is designed to survive but
should not be handed.
