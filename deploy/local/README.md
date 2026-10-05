# LOCAL variant

MinInfer as a single-process tool on your own machine. Open API, open
dashboard, no credentials, no limits. This is the developer experience the
project shipped with, and it is pinned by `tests/test_profiles.py` so a
cloud-hardening change can never quietly break it.

## Run it

```bash
# direct — needs the repo root on sys.path, which is why you run it from here
mi proxy                       # or: python3 -m mininfer proxy

# containerised
docker compose -f deploy/local/docker-compose.yml up -d --build
# or from the repo root, which includes the same file:
docker compose up -d --build
```

On first run the container finds an empty registry and **ingests the provider
catalogues once** before starting, so the dashboard has data instead of zeroes.
It is a one-time cost on a fresh volume; set `MI_BOOTSTRAP=0` to skip it when the
catalogue arrives another way (a restored volume, a scheduled job, a Postgres you
already maintain).

Default is SQLite — one file, one replica. To run the *cloud* shape locally
(registry in Postgres, limits in Redis, auth on):

```bash
docker compose -f deploy/local/docker-compose.postgres.yml up -d --build
```

Then open <http://127.0.0.1:8765/> — or `:8000` in the container.

## Setup, step by step

From a fresh clone, in order:

```bash
# 1. Provider keys. The catalogue builds with none, but nothing routes until at
#    least one is set. Copy the template and fill in what you have.
cp .env.example .env

# 2. The engine, and the proxy (`server` adds FastAPI/uvicorn — without it
#    `mi proxy` exits with a message saying exactly this).
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[server]'

# 3. Pull the provider catalogues into the registry.
mi ingest

# 4. Resolve identities across sources.
mi resolve

# 5. Declare the free-tier limits in `config/quotas.yaml`. The Docker entrypoint
#    runs this for you after step 3; a native install does not, which is why an
#    un-seeded registry shows an empty "Quota headroom" card. The dashboard's
#    "Seed quotas" button runs exactly this.
mi quota seed

# 6. Serve the proxy and dashboard.
mi proxy                      # http://127.0.0.1:8765
```

In the container, steps 2–5 are the image and the entrypoint's job, so only
step 1 is yours:

```bash
cp .env.example .env          # required: the file is bind-mounted at /app/.env
docker compose up -d --build  # builds, ingests, seeds quotas, serves :8000
```

## Configuration

`config/profiles/local.env` is the declared profile. It is not read
automatically; it exists so the variant is stated in one place and can be
asserted. To adopt it:

```bash
set -a; . config/profiles/local.env; set +a
mi proxy
```

Provider keys go in `.env` (gitignored — see `.env.example`). Create it before the
container starts — it is bind-mounted at `/app/.env`, so a key saved from the
dashboard's **To server** action is written to the repo's `.env` and survives a
`docker compose up` recreate:

```bash
cp .env.example .env
```

On a Linux host the container runs as uid 1001, so `.env` has to be writable by
that user for **To server** to succeed (`chmod 666 .env`, or accept that the write
is refused and set the key on the host instead). On Docker Desktop the file-sharing
layer handles it.

## The one place the container differs

The profile binds `127.0.0.1`, which is the right default with no container
boundary. Inside a container the bind address must be `0.0.0.0` or the port
mapping cannot reach it, so `docker-compose.yml` sets that explicitly. Same
semantics, different network namespace.

## What this variant is not

It is not safe to expose. There is no authentication, no rate limit and no body
cap — `/v1/search` and `/`, `/v1/stats` and `/v1/local/*` are all open. For
anything reachable from the internet, use `deploy/cloud/`.
