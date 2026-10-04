# PRD — MinInfer OSS Quality & Test Coverage Roadmap

**Status:** Draft for approval
**Owner:** OSS release
**Scope:** `mininfer-oss` (the public engine + dashboard)
**Related:** `CONTRIBUTING.md`, `SECURITY.md`, `.github/workflows/ci.yml`

---

## 1. Problem statement

MinInfer's engine is well-tested, but the **dashboard has no end-to-end test
coverage at all**, and it is the layer that keeps breaking for users. During the
recent hardening pass, every UI-facing defect was found by a *human clicking the
dashboard* rather than by any automated layer:

1. the Overview footer claiming a hardcoded `23,799 models indexed`;
2. four separate provider-key handling bugs (compose `environment:` shadowing
   `env_file:`, the `-f` project-directory resolution, a blank key treated as a
   credential, and `/v1/providers/test` reading only the request body);
3. a stale browser bundle masking a fixed backend;
4. a missing `route` event in the frontend type that only `tsc` caught.

Each of these was a real user-facing failure that the existing 916 Python tests,
97 frontend unit tests, and 4 CI jobs all passed. The conclusion is not that the
tests are wrong — it is that **the layer between the browser and the API is
uncovered**, and that layer is where the wider audience lives.

This PRD defines a phased test suite that closes that gap, plus the
non-functional and release hygiene work needed to put MinInfer in front of a
wider audience.

---

## 2. Goals

- **G1.** Every defect class from §1 is caught automatically, in CI, before merge.
- **G2.** A deterministic, hermetic **browser E2E suite** exercises the real
  dashboard against the real API with stubbed providers — no network, no keys.
- **G3.** The dashboard never renders a blank page on error.
- **G4.** The dashboard meets a defined accessibility and responsive bar.
- **G5.** Routing/API performance has a regression baseline.
- **G6.** The project has release hygiene (changelog, versioning, contract) that
  a wider audience expects.

## 3. Non-goals

- Provider-side integration testing against real OpenRouter/Groq/etc. (done
  manually, and only where a key exists).
- A human/agent "clicking around" as the quality gate. Exploratory browsing may
  *inform* the suite, but the gate is deterministic CI.
- Load testing at scale (single-user OSS scope; revisit for the cloud variant).
- Rewriting or replacing the existing Python/frontend suites.

---

## 4. Current baseline

| Layer | State | Entry point |
|---|---|---|
| Python unit/integration | ✅ 916 tests | `pytest -q` |
| Dual-engine (SQLite + Postgres) | ✅ 881 pass | `./scripts/validate-postgres.sh` |
| Profiles booted in-process | ✅ | `tests/test_variants.py` |
| Frontend unit | ✅ 97 tests | `web`: `npm test` |
| Frontend typecheck | ✅ | `web`: `npm run typecheck` |
| Live profile acceptance | ✅ 27 checks | `./scripts/validate-phase0.sh` |
| Container + first-run bootstrap | ✅ | `./scripts/validate-variants.sh`, CI |
| **Browser E2E** | ❌ none | — |
| **Error boundary** | ❌ none | — |
| **A11y / responsive** | ❌ untested | — |
| **Perf baseline** | ❌ none | — |
| **Changelog / versioning** | ❌ none | — |

CI today: `test` (SQLite py3.11/3.12 + Postgres), `frontend`, `acceptance`,
`docker` — see `.github/workflows/ci.yml`.

---

## 5. Phase 1 — Browser confidence (the gap)

**Objective:** make the real dashboard testable end-to-end in CI, and make it
impossible for the app to blank silently.

### 5.1 Upstream stub seam

The proxy must be runnable with **no providers** while still answering chat
requests deterministically.

**R1.1** `MI_UPSTREAM_STUB=1` swaps the proxy's upstream callers (`try_fallbacks`,
`open_stream`, and the trial `Runner`) for a deterministic stub.

- **AC1:** A canned text completion is returned for `POST /v1/chat/completions`
  with `stream: false`.
- **AC2:** Canned SSE chunks are streamed for `stream: true`, ending in `[DONE]`.
- **AC3:** Per-model canned responses are selectable (e.g. model `a` returns text
  `A`, model `b` returns `B`).
- **AC4:** Error injection is available: `429`, `auth_error`, `empty_content`, and
  an upstream error returned as a 200 body.
- **AC5:** All stub activity is recorded and inspectable from a test-only
  endpoint (`GET /v1/__stub__/calls`) so specs can assert *what was routed*.
- **AC6:** The stub is inert in normal mode — off by default, unreachable unless
  the env var is set. Never available behind a deployment with auth on.

**Files:** `mininfer/proxy.py` (+ a new `mininfer/teststub.py` module).

**Dependency:** none. **Risk:** the stub must not weaken the real proxy — it is
gate-able and off by default.

### 5.2 Playwright harness

**R1.2** A `web/e2e/` workspace with Playwright, fixtures, and specs.

- **AC1:** A fixture builds the SPA (or serves `vite preview`) and boots the real
  `mininfer.proxy:app` on a free port with a **seeded registry** and
  `MI_UPSTREAM_STUB=1`; the fixture waits for `/healthz`.
- **AC2:** The seed registry is built deterministically (a small committed JSON
  or a `Store`-seeding script), covering at least 2 providers, free + paid arms,
  and one quota bucket.
- **AC3:** Tests never require a network or a real key. CI-only.

### 5.3 The six specs

Each spec is named after the defect class it pins.

| Spec | Assertions (min) |
|---|---|
| `overview.spec.ts` | page loads; stat cards show **live** counts (regression for the hardcoded footer); providers table and quota panel render; the `Live (Ns)` control is present |
| `models.spec.ts` | search/filter narrows the list; opening a model shows its details |
| `providers.spec.ts` | save a key; **Test** reports `Key: your saved key` and a stub result (regression for all four key bugs) |
| `playground.spec.ts` | send a message → streamed stub reply; **Compare** runs two arms and reports both, with a winner |
| `auth.spec.ts` | cloud profile: unauth → 401 challenge; Basic auth → dashboard loads; tenant key cannot reach `/v1/stats` |
| `responsive.spec.ts` | 375px and 768px viewports; no horizontal overflow on Overview/Models; key controls remain clickable |

**R1.3** The suite runs headed in dev (`npm run e2e`) and headless in CI.

### 5.4 Error boundary

**R1.4** A React error boundary wraps the app.

- **AC1:** A throwing component renders a visible fallback with the message and a
  "Reload" action — never a blank page.
- **AC2:** The boundary resets on navigation (a crash in one tab does not brick
  the app).
- **AC3:** A Playwright spec forces a render error (a test-only route) and asserts
  the fallback appears.

**Files:** `web/src/components/ErrorBoundary.tsx`, `web/src/App.tsx`.

### 5.5 CI wiring

**R1.5** A new `e2e` job in `.github/workflows/ci.yml` runs the Playwright suite.

- **AC1:** The job builds the SPA, installs Playwright browsers, boots the stubbed
  fixture, and runs the specs headless.
- **AC2:** A failure names the failing spec and the assertion — no "one big e2e
  pass/fail" blob.

**Phase 1 exit criteria:** all six specs green in CI; the error-boundary spec
red-then-green; no real provider call possible from the fixture.

---

## 6. Phase 2 — Wider-audience non-functionals

### 6.1 Accessibility

**R2.1** `@axe-core/playwright` runs on every route in the E2E suite.

- **AC1:** Zero **critical** and **serious** violations on Overview, Models,
  Providers, and Playground.
- **AC2:** Keyboard navigation: every nav item and the composer is reachable and
  operable without a mouse.
- **AC3:** Streaming content announces via `aria-live`; `prefers-reduced-motion`
  disables the pulse animations.

### 6.2 Responsive

**R2.2** The dense Overview tables degrade acceptably at 375px.

- **AC1:** No horizontal page overflow at 375px / 768px / 1440px.
- **AC2:** Tables scroll horizontally within their container rather than the page.

### 6.3 Performance baseline

**R2.3** A hermetic perf gate.

- **AC1:** `pytest tests/test_perf.py` times `GET /v1/plan?task=general_chat` and
  `POST /v1/route` on a ~2,000-deployment seeded registry and asserts an upper
  bound (initial: **500 ms** p95 on CI hardware; tune once measured).
- **AC2:** The benchmark-norms cache is proven warm on repeat calls within the
  120 s TTL (second call ≪ first).

**Phase 2 exit criteria:** axe clean on serious+, responsive specs green, perf
gate green in CI.

---

## 7. Phase 3 — Release hygiene

### 7.1 Changelog

**R3.1** `CHANGELOG.md` in Keep-a-Changelog format.

- **AC1:** Every user-visible change links to its PR and version.
- **AC2:** CI/scripts exist to nudge an unreleased-entry edit in PRs touching
  `mininfer/` or `web/src/`.

### 7.2 Versioning

**R3.2** Semantic versioning in `pyproject.toml` and `web/package.json`, kept in
sync.

- **AC1:** A release checklist documents bump → changelog → tag → build → publish
  (PyPI + Docker image).
- **AC2:** `mininfer --version` prints the package version.

### 7.3 API contract

**R3.3** A statement of the `/v1` contract.

- **AC1:** `docs/api-contract.md` lists which endpoints are stable, which are
  experimental, and the deprecation policy.
- **AC2:** A CI contract test asserts the OpenAPI covers every registered route
  and that no stable endpoint changed shape without a changelog entry.

### 7.4 Signals

**R3.4** Badges (CI, license, version) on the README; optional download stats.

**Phase 3 exit criteria:** a tagged release can be cut by following a checklist,
and every route is covered by the contract test.

---

## 8. Cross-cutting requirements

- **Hermeticity.** No layer in CI may call a real provider or need a key. The
  stub seam (§5.1) and the existing `try_fallbacks` stub are the only two ways
  a test answers a chat request.
- **Flakiness policy.** A test that flakes is a bug, not a retry target. CI
  retries are limited to 1; a second failure blocks merge.
- **Determinism.** Seed data, stub responses, and time (where used) are fixed.
  No `random` without a seeded RNG.
- **Fast feedback.** L0/L1 stay under ~10 s locally; L4 (E2E) is a separate job
  so it does not slow the primary loop.

---

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Stub seam weakens the real proxy | Gate behind `MI_UPSTREAM_STUB`; assert in a test that the route is absent without it |
| Playwright flakiness | Hermetic fixture, fixed seed, ≤1 retry, flaky = bug policy |
| E2E slows CI | Separate job; cache Playwright browsers and the SPA build |
| axe adds noise | Start with critical+serious only; baseline the rest, tighten later |
| Perf gate too strict on CI | p95 with generous first threshold; measure before enforcing |

---

## 10. Success metrics

1. **Regression capture:** each bug class in §1 has a failing test that predates
   the fix (proven by a red-then-green PR).
2. **Zero manual verification** of the dashboard before a release.
3. **CI green** on every PR touching `web/src/` or `mininfer/`.
4. **A11y:** 0 critical/serious violations.
5. **Perf:** `/v1/plan` p95 under the agreed bound on a 2k registry.
6. **First tagged release** cut by following the checklist without asking a human.

---

## 11. Sequencing & rollout

1. **Phase 1.1 + 1.4** — stub seam + error boundary (unblocks everything else).
2. **Phase 1.2/1.3** — Playwright harness + two specs (overview, providers — the
   two that burned users), then the remaining four.
3. **Phase 1.5** — CI `e2e` job.
4. **Phase 2** — a11y, responsive, perf (independent, parallelisable).
5. **Phase 3** — changelog/versioning/contract; cut the first tag.

Phases 1 and 2 are mergeable independently; Phase 3 blocks only the *release*,
not the code.

---

## 12. Open questions

- **O1.** Perf budget number — measure first, then pin (§6.3 uses 500 ms as a
  placeholder).
- **O2.** axe severity bar — start at critical+serious, or include moderate?
- **O3.** Is the seed registry committed as JSON or generated by a script?
- **O4.** Should the stub also cover `/v1/search` and the shadow-trial judge, or
  are chat/stream enough for the six specs?
- **O5.** PyPI + Docker publishing — who owns credentials; is PyPI in scope for
  the first release?

---

## 13. Out of scope

- Multi-tenant / cloud control plane (separate `mininfer-cloud` roadmap).
- Real-provider integration tests.
- Visual-regression (screenshot) testing — revisit if axe + responsive prove
  insufficient.
- The GLiNER2.5 understanding layer (already documented as deferred).
