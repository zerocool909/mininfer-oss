# Contributing to MinInfer

Thanks for helping. This document is the short version of how the codebase
expects to be changed. The long version is the code's own comments and the
validation scripts — read those before a large change.

## Development setup

```bash
git clone https://github.com/zerocool909/mininfer-oss
cd mininfer-oss

python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,dev]'      # [server] is what `mi proxy` needs
```

The base install (`pip install -e .`) is enough to build a registry and route,
but **not** to run the proxy: `mi proxy` needs FastAPI + Uvicorn and tells you so
(`pip install '.[server]'`) if they are missing.

## Build a registry and try it

```bash
python3 -m mininfer ingest            # Tier 0 sources need no credentials
python3 -m mininfer resolve
python3 -m mininfer stats
python3 -m mininfer route general_chat
```

`mi ingest` must fetch from provider APIs, so it needs network access. On a
machine whose TLS trust lives in the system keychain (macOS, corporate proxy),
`python` may not see the root CA that `curl` does — generate a bundle and point
`MI_CA_BUNDLE` at it:

```bash
./scripts/make_ca_bundle.sh
export MI_CA_BUNDLE="$PWD/.certs/bundle.pem"
```

Note that routing/execution needs at least one provider key. The *catalog* is
built keyless (that is the point of Tier 0), but no upstream answers inference
without a credential, so `mi route` reports `0 eligible` until you set one
(`OPENROUTER_API_KEY`, `GROQ_API_KEY`, …). That is expected, not a bug.

## Tests

```bash
pytest -q                       # the whole suite on SQLite
./scripts/validate-postgres.sh  # the same suite on a throwaway Postgres
./scripts/validate-phase0.sh    # boots both profiles and asserts every surface
./scripts/validate-variants.sh  # both routes in-process, live, and in the image
```

Optional extras gate some tests. They `skip` (never fail) when the extra is
absent, so a base install always runs the core suite:

| Extra | What it turns on |
|---|---|
| `server` | the proxy endpoints (`mi proxy`) |
| `agents` | the LangGraph ingest/extract nodes (`agent_ingest`, `agent_resolve`) |
| `understanding` | the GLiNER2.5 seam (real-checkpoint tests skip without it) |
| `sync` | `mi sync` to Postgres |

To run everything locally:

```bash
pip install -e '.[server,dev,agents,sync,understanding]'
```

Frontend:

```bash
cd web
npm ci
npm run typecheck        # must be clean — `npm run build` does not typecheck
npm test                 # vitest
npm run build
```

**Every behaviour change needs a test.** The suite is the specification; a fix
without one regresses the next time someone touches the same code. The two
storage engines are both first-class, so a change to SQL or to ranking must pass
on SQLite *and* Postgres (`./scripts/validate-postgres.sh`).

## The rules that are load-bearing

These are not style preferences; each exists because breaking it caused a real
bug. Please keep them.

1. **`Store` owns all SQL.** No other module writes a raw statement — the
   dialect seam (SQLite/Postgres) is exactly one file wide (`mininfer/db.py`).
   `tests/test_encapsulation.py` enforces this.

2. **Write portable SQL.** `mininfer/db.py:translate` handles placeholders,
   `%`, scalar `MAX/MIN` and `INSERT OR …`. It does **not** translate functions
   like SQLite's `datetime()`, `strftime()` or `julianday()` — using one works on
   SQLite and aborts the Postgres transaction, which then silently loses every
   later write in that request. Compare timestamps in Python, or with a lexically
   comparable ISO string (see `store._since`).

3. **Ordering must be total.** If you sort candidates or buckets, end the key
   with a stable, unique field such as `deploy_id`. A partial key leaves ties to
   the storage engine's row order, which differs between SQLite and Postgres —
   `rank_for_compare` takes `ranked[0]`, so the same registry would pick a
   different arm on each engine.

4. **The variants are data, not forks.** Local and cloud differ in
   `config/profiles/{local,cloud}.env` and `deploy/{local,cloud}/` — never in a
   second copy of the code. `tests/test_profiles.py` pins each profile's
   invariant (local open, cloud fail-closed, no usable credential committed).

5. **Access control stays opt-in.** With nothing configured, MinInfer is the
   local tool it always was. Do not make the guard depend on a config *file*
   existing.

6. **The registry is derived.** Raw payloads are persisted to `raw/` before
   parsing so the catalog is always re-derivable. Do not hand-edit registry
   tables or commit a `mininfer.db`.

## Adding a provider

1. Add a `SourceSpec` in `mininfer/ingest/catalogue.py` (tier, URL, adapter).
2. Add or extend an adapter under `mininfer/ingest/adapters/`; adapters register
   themselves (`_template.py` shows the contract).
3. Add the endpoint to `mininfer/execute.py` if it can serve inference.
4. Add a test with a recorded payload — do not hit the network in tests.

## Pull requests

- Keep the change focused; one idea per PR.
- Run `pytest -q`, `npm run typecheck` (if you touched `web/`), and
  `./scripts/validate-postgres.sh` before opening it.
- Update the docs a change makes wrong. The Markdown here is not decoration —
  `README.md`, `deploy/README.md` and the `*_ACTIVITY.md`/`*_CHECKLIST.md` files
  are the project's memory.
- CI runs the suite on both engines and the frontend typecheck. A red build is
  the first thing a reviewer sees.

## Code of conduct

Participation is covered by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## License

By contributing you agree your work is licensed under the [MIT License](LICENSE).
