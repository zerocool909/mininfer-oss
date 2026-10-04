# Releasing

A checklist, not a philosophy. Everything here is a command you run or a thing you
confirm, in order; the point is that a release is repeatable by someone who did not
write the code.

`mininfer` is pre-1.0. The `/v1` HTTP surface is stable (`docs/api-contract.md`),
the Python API is not.

---

## 0. Preconditions

- [ ] `main` is green in CI — all five jobs: `test` (SQLite py3.11/3.12 +
      Postgres), `frontend`, `acceptance`, `e2e`, `docker`.
- [ ] No open `test_fix.md` entry without a fix (that file is on the `dev` branch).
- [ ] Any `docs/api-contract.md` change is in the same commit as the route change
      — `tests/test_api_contract.py` enforces this, so a green `test` job means it
      holds.

## 1. Decide the version

Semantic versioning, pre-1.0:

| Change | Bump |
|---|---|
| A new endpoint, flag, provider adapter, or config key | **minor** (`0.1.0 → 0.2.0`) |
| A fix with no new surface | **patch** (`0.1.0 → 0.1.1`) |
| A breaking change to the Python API | **minor**, and say so in the changelog |
| A breaking change to `/v1` | **not allowed** without a new path prefix |

## 2. Bump it in both places

They must not drift:

```bash
# pyproject.toml
version = "0.2.0"

# web/package.json
"version": "0.2.0"

# mininfer/__init__.py — the string `mi --version` prints
__version__ = "0.2.0"
```

```bash
mi --version        # must print the new number
```

## 3. Write the changelog

In `CHANGELOG.md`, move the `Unreleased` entries under a new `## [x.y.z] — date`
heading, add the compare links at the bottom, and leave `Unreleased` empty.

Group by **Added / Changed / Deprecated / Removed / Fixed / Security**. Write what a
*user* would notice, not a commit list: "routing was broken on the Postgres
backend", not "fix datetime()".

## 4. Verify the artifact, not the tree

```bash
python3 -m pytest -q                      # SQLite
./scripts/validate-postgres.sh            # the same suite on Postgres
./scripts/validate-phase0.sh              # both profiles, live
(cd web && npm ci && npm run typecheck && npm test && npm run build)
./scripts/validate-variants.sh            # adds the container leg

python3 -m build                          # wheel + sdist
python3 -c "import zipfile,glob; z=zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[0]); print(len(z.namelist()),'files')"
```

Install the **wheel** into a clean venv and run a request against it — a green
source tree is not the same artifact as the one users get:

```bash
python3 -m venv /tmp/rel && /tmp/rel/bin/pip install dist/*.whl 'mininfer[server]'
/tmp/rel/bin/mi --version
/tmp/rel/bin/mi proxy --port 8899 &   # then curl /healthz and /v1/models
```

## 5. Tag and publish

```bash
git tag -a v0.2.0 -m "v0.2.0"
git push origin v0.2.0

docker build --target runner -t ghcr.io/zerocool909/mininfer-oss:0.2.0 .
docker push ghcr.io/zerocool909/mininfer-oss:0.2.0
```

GitHub release: paste the changelog section. If publishing to PyPI, build from the
tag, never from a working tree:

```bash
python3 -m twine upload dist/*        # from the tagged checkout
```

## 6. After

- [ ] `docker compose up` on a **fresh volume** shows a populated dashboard (the
      entrypoint bootstraps on an empty registry).
- [ ] The README's quick start still works verbatim from a fresh clone.
- [ ] Bump `Unreleased` back to empty and leave the compare links pointing at the
      new tag.
- [ ] If the release changed the container shape, update `deploy/cloud/` manifests
      — they pin nothing, so they drift silently otherwise.

---

## Rollback

Nothing in a release is destructive, so a bad one is superseded, not reverted:

1. Tag the previous commit as a patch release (`v0.2.1`) rather than deleting
   `v0.2.0` — a moved tag is a supply-chain hazard.
2. Say what was wrong in the changelog. A released bug is public either way; the
   only question is whether the explanation comes from you.

Never re-push a tag. Consumers pin it, and caches keep the old bytes.
