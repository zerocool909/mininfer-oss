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

## TLS / corporate root CAs

**On a normal network there is nothing to do here** — MinInfer verifies against
`certifi` (the Mozilla roots), which trusts every public provider. This section is
only for a network that intercepts TLS.

If `curl` works and Python does not (`CERTIFICATE_VERIFY_FAILED`), the root CA is
in the OS trust store and `certifi` cannot see it:

```bash
./scripts/make_ca_bundle.sh                       # writes .certs/bundle.pem
echo "MI_CA_BUNDLE=$PWD/.certs/bundle.pem" >> .env   # `mi proxy` on the host
```

`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` work as well, so a
machine already set up for Python or `curl` needs no MinInfer-specific setting.
The bundle is per-machine by design — it holds *your* network's interception
root — so `.certs/` is gitignored; never commit it, and never disable
verification in its place.

In the container no `.env` change is needed: the repo's `.certs/` is mounted at
`/certs` and `MI_CA_BUNDLE` defaults to `/certs/bundle.pem`. An absent bundle is
an empty directory, not an error — the code falls back to `certifi`.

Symptom without it: `/v1/plan` works (no upstream call) while
`/v1/chat/completions` returns 502 `network_error`.

## What this variant is not

It is not safe to expose. There is no authentication, no rate limit and no body
cap — `/v1/search` and `/`, `/v1/stats` and `/v1/local/*` are all open. For
anything reachable from the internet, use `deploy/cloud/`.
