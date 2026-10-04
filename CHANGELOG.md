# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`mininfer` is pre-1.0: the `/v1` HTTP surface is intended to be stable, the Python
API is not yet. See `docs/api-contract.md` for what that means per endpoint.

---

## [Unreleased]

### Accessibility

- The navigation carried no accessible name below `md`, because its label is
  hidden at that breakpoint and the icon said nothing — four unnamed buttons on a
  phone.
- Provider cards rendered 25 icon-only buttons and two `<select>`s with no
  accessible name.
- Muted text drawn at 70% opacity measured 4.44:1 against a 4.5 threshold.
- `prefers-reduced-motion` covered only skeleton shimmers; card transitions, hover
  lifts and the live-indicator ping all still ran for a reader who had asked for
  less motion. It now honours the preference throughout.

### Fixed

- A render error unmounted the whole app, leaving a blank page. A boundary now
  shows the error with a reset, and a second one scoped to the active tab keeps
  the navigation alive.
- `mi --version` was missing.

## [0.1.0] — first deployable release

The shipping boundary: local and cloud deployable from one image, no ML runtime.

### Added

- **OpenAI-compatible routing proxy** — `/v1/chat/completions`, `/v1/models`,
  `/v1/route`, `/v1/search`, `/v1/session`, `/v1/usage`, `/v1/stats` and the
  economics surface.
- **Evidence-driven model registry**, re-derivable from the raw payloads it was
  parsed from. 27 sources across three tiers, eight of them keyless so a cold
  start needs no credentials.
- **Cost-per-success routing** with free-first ordering, a quality floor, quota
  headroom, and fallback separation that spans **gateways**, not just upstreams.
- **Bring your own key** — per-request `X-User-API-Keys`, and a configured key
  widens the catalogue, because ingest follows the sources you can actually call.
- **`mi refresh`** — verify → reconcile → metrics, the single command a scheduler
  runs.
- **Two profiles, one codebase** — `config/profiles/{local,cloud}.env`; opt-in
  access control, per-tenant rate limits, body caps, surface classification.
- **Both storage engines are first-class** — SQLite by default, Postgres via a
  `MI_DB` DSN, with the same suite green on each.
- **A batched LLM-as-judge** for shadow trials: several candidates compared in one
  model call, with the winner recorded.

### Fixed

The defects a user would have felt, found while hardening this release:

- routing was broken on the Postgres backend — a SQLite-only `datetime()` aborted
  the transaction and a surrounding `except: pass` hid it;
- the same registry could rank a different arm first on each engine, because the
  sort key was not a total order;
- the container shipped with every provider key empty, so it called providers with
  no `Authorization` header at all;
- a blank key was treated as a credential, shadowing the configured one;
- the provider connectivity test read only the request body, so a key saved in the
  dashboard was invisible to it;
- that same test invented a model named `test` when nothing was ingested, so it
  could only ever report a 404;
- a rate-limited `:free` model was reported as a connectivity failure;
- a fallback chain could be three arms of one gateway, so one bad key failed a
  request that another provider could have served;
- `mi ingest` ignored configured providers, so bringing your own key did not widen
  the catalogue;
- the image could not be built from a clean checkout, and a fresh volume produced
  an empty dashboard.

[Unreleased]: https://github.com/zerocool909/mininfer-oss/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zerocool909/mininfer-oss/releases/tag/v0.1.0
