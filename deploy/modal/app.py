"""MinInfer's economics service on Modal — the serverless host (P8).

Two functions, and only two:

  * `maintain` — a cron that ingests provider catalogues and re-derives the
    registry, by running the same `mi ingest` / `mi reconcile` the container's
    worker runs;
  * `api` — the FastAPI app, served as-is.

**Nothing is reimplemented.** `api` returns `mininfer.proxy:app`, so a fix to the
router or the reconciler lands on this host the moment it lands anywhere — the
same argument the Dockerfile makes for one image with two process types, applied
to a second platform.

Scope is decided by which secrets exist, not by a second codebase:

  * `MI_DB` (a `postgresql://` DSN) alone makes this a **read-only economics
    service**: `/v1/economics/*` answers, and every model call fails with
    `no_api_key` — which is exactly what the proxy already does for a provider
    whose key is absent.
  * Adding provider keys enables routing and chat.

So there is deliberately no "read-only mode" flag. Absence is already a supported
state, and a second mode would be a second thing to keep correct.

Deploy:

    modal secret create mininfer MI_DB=postgresql://... OPENROUTER_API_KEY=...
    modal deploy deploy/modal/app.py

The `MI_DB` DSN is the only required secret. Without a `mininfer` secret this
will not deploy, which is the intent: a serverless economics service with no
registry has nothing to serve.
"""
from __future__ import annotations

import pathlib

import modal

#: The repo root. `mininfer/proxy.py` computes its asset path from *its own*
#: location (`<root>/web/dist`), so the image layout has to be the one the code
#: expects rather than whatever an auto-mounted package would give it.
REPO = pathlib.Path(__file__).resolve().parents[2]

#: Ingest is the long pole: it fetches every keyless catalogue and every
#: OpenRouter endpoint document. 6-hourly matches the container worker's cadence,
#: because provider prices do not move faster than that and a tighter loop mostly
#: re-reads identical bytes.
SCHEDULE = "0 */6 * * *"

IMAGE = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "httpx>=0.27",
        "PyYAML>=6.0",
        "fastapi>=0.100",
        "uvicorn>=0.23",
        # The shared registry is Postgres; without the driver the image starts and
        # then cannot connect, which is a worse failure than not building. (The
        # same reasoning as the `cloud` extra in pyproject.toml.)
        "psycopg2-binary>=2.9",
    )
    .add_local_dir(str(REPO / "mininfer"), remote_path="/root/mininfer")
    .add_local_dir(str(REPO / "config"), remote_path="/root/config")
    .add_local_dir(str(REPO / "supabase"), remote_path="/root/supabase")
    .env(
        {
            "PYTHONPATH": "/root",
            "MI_POLICY": "/root/config/policy.yaml",
            # The evidence lake is the audit trail the whole design leans on
            # ("patches change; you must be able to re-derive the registry from
            # history"). It goes on a Volume rather than an ephemeral disk for the
            # same reason it goes to R2 in the container deployment.
            "MI_RAW": "/data/raw",
        }
    )
)

app = modal.App("mininfer-economics", include_source=False)

#: `create_if_missing` so a first `modal deploy` needs no manual `modal volume
#: create` step.
RAW_LAKE = modal.Volume.from_name("mininfer-raw", create_if_missing=True)

#: Named secrets, not literals. `mininfer` holds `MI_DB` plus whichever provider
#: keys this deployment is allowed to use — see the module docstring for why the
#: presence of those keys *is* the scope decision.
SECRETS = [modal.Secret.from_name("mininfer")]


@app.function(
    image=IMAGE,
    secrets=SECRETS,
    volumes={"/data": RAW_LAKE},
    schedule=modal.Cron(SCHEDULE),
    timeout=3600,
    # Ingest is a batch job, not a request path: one container, never concurrent
    # with itself, because two runs writing the same registry is the failure the
    # reconciler is designed to survive but should not be handed.
    max_containers=1,
)
def maintain() -> dict:
    """Ingest every keyless source, then re-derive the registry.

    Two commands, both the real CLI. `ingest` reconciles at the end of its own run
    (`_ingest_all`), so the explicit `reconcile` is belt-and-braces for the case
    where every source failed: nothing was written, so nothing would have triggered
    it, and the state machine would sit on 6-hour-old beliefs.
    """
    import os

    from mininfer import cli

    base = ["--db", os.environ["MI_DB"], "--policy", os.environ["MI_POLICY"]]
    ingest_rc = cli.main([*base, "ingest", "--force"])
    reconcile_rc = cli.main([*base, "reconcile"])
    RAW_LAKE.commit()
    return {"ingest_rc": ingest_rc, "reconcile_rc": reconcile_rc}


@app.function(
    image=IMAGE,
    secrets=SECRETS,
    volumes={"/data": RAW_LAKE},
    timeout=300,
    # Scale to zero between requests: the read API is a pure function of Postgres,
    # so a cold start costs a container boot and nothing else.
    scaledown_window=60,
)
@modal.asgi_app()
def api():
    """The whole API, served as-is — `mininfer.proxy:app`, not a copy of it."""
    from mininfer.proxy import app as proxy_app

    return proxy_app
