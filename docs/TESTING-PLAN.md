# Release-confidence test plan

The question this plan answers is not "are the tests green" but the two
questions an open-source maintainer actually has to answer before publishing:

1. **Will it always run?** — does a request reliably get an answer, on any
   supported platform, against any provider, with any registry state?
2. **Does it use the algorithm well?** — does the router pick *capable* arms,
   get *better* over time, and never get stuck on one arm while others idle?

Everything below is scoped to this tree: what already exists, what is missing,
what to build, and in what order. Each item names the failure class it
catches, and — where honest — whether it is a code fix or an evidence fix.

## 0. The rule this plan follows

The existing CI already states its own doctrine, and it is the right one:

> Every check here is one that caught a real bug on this tree, run by hand
> first and only then automated — that is the rule for adding a job.

So the plan is not "add coverage for coverage's sake". Every suite below is
either (a) a class of failure this repository has *already* produced, or
(b) a class of failure that an open-source user will produce on day one. A
suite that cannot name the incident it prevents does not get written.

| # | Incident already seen in this tree | Where it is fixed / tested |
| :-- | :-- | :-- |
| 1 | SQLite write txn held across a model call → concurrent requests 500 | `proxy.py`, `store.py`; `tests/test_sqlite_concurrency.py` |
| 2 | `NameError` in the shadow-trial handler rolled back all judge evidence | `proxy.py` (import) — **not yet covered by a test** |
| 3 | Half-initialised DB → server healthy, every route 500 | **not yet fixed** — see §4.1 |
| 4 | Rate-limit header overflow → 500 on a successful answer | `quota.py`; `tests/test_quota.py` |
| 5 | Pricing tests pinned to calendar dates → 12 failures unrelated to code | `tests/pricing/*` (dates relative to now) |
| 6 | `404` arms never retired on the serving path | `proxy.py`; `tests/test_proxy.py` |
| 7 | `mi refresh` exiting 0 after every source failed | `cli.py`; `tests/test_verify.py` |
| 8 | Postgres `datetime()` aborting the transaction | `db.py`; CI postgres leg |

---

## 1. Test taxonomy — coverage today, and the gaps

Legend: **●** solid · **◐** partial · **○** missing

| # | Type | What it protects | Today | Gap to close |
| :-- | :-- | :-- | :-- | :-- |
| 1 | **Unit / behavioural** | pure logic (pricing, quota math, intent, complexity, dossier fit) | ● 85 files, ~15k lines | add coverage floor (§6) |
| 2 | **Integration (in-process)** | proxy↔store↔router seams | ● | — |
| 3 | **Contract** | `/v1/*` shapes vs `docs/api-contract.md` | ● `test_api_contract.py` (doc is parsed, so it cannot drift) | add *behavioural* contract: status codes, headers, error bodies |
| 4 | **End-to-end (live providers)** | the real answer path | ◐ `tests/test_provider_probe.py` | promote to a scheduled canary (§3) |
| 5 | **Concurrency / load** | many requests, one registry | ◐ small (`test_sqlite_concurrency.py`, `test_router_perf.py`) | **● proxy-level N-way concurrency + no-lost-observation** (§4.2) |
| 6 | **Fault injection / chaos** | provider 5xx, 429 storms, timeouts, malformed bodies, partial SSE | ◐ `test_upstream_errors.py`, `test_stream_failures.py` | **● systematic fault matrix** (§4.3) |
| 7 | **Robustness / fuzz** | hostile or malformed input never 500s | ○ | **● payload + header fuzzing** (§4.4) |
| 8 | **Migration / upgrade** | an old registry opened by new code | ○ | **● legacy-DB fixtures** (§4.1) |
| 9 | **Data integrity / provenance** | prices come from evidence; nothing invented | ● `tests/pricing/*` (strong) | add *registry re-derivability* test (§5.1) |
| 10 | **Security** | auth, tenancy, SSRF, secret leakage, abuse | ◐ `test_auth.py`, `test_proxy_keys.py` | **● SSRF + tenant-isolation + log-hygiene suites** (§4.5) |
| 11 | **Privacy** | no prompt/response bodies stored by default | ○ | **● storage-audit test** (§5.2) |
| 12 | **Performance** | the routing hot path stays fast | ◐ `test_router_perf.py` | budget test in CI + the two known hot-path wins (§5.3) |
| 13 | **Routing quality** | the router picks *capable* arms and improves | ◐ `test_bench.py`, `test_bench_calibration.py` | **● golden-task regression + baseline comparison** (§5.4) |
| 14 | **Self-improvement** | the learning loop actually learns, and never loses evidence | ◐ `test_bandit.py`, `test_judge.py` | **● end-to-end learning-curve test** (§5.5) |
| 15 | **Reproducibility** | the registry is re-derivable from `raw/` | ◐ claim in README | **● rebuild-from-snapshot test** (§5.1) |
| 16 | **Platform / packaging** | Windows+Linux+macOS, fresh install, py3.11–3.13 | ◐ CI is Linux-only; `test_packaging.py` | **● OS matrix + fresh-venv install** (§4.6) |
| 17 | **Docs accuracy** | the README's claims are true | ◐ `test_doctor.py` | **● doc-claim tests** (§5.6) |
| 18 | **Supply chain / legal** | deps are clean; no redistribution of provider content | ○ | `pip-audit`, SBOM, licence check (§6) |

---

## 2. What "always runs" has to mean (the non-negotiables)

These are the properties an open-source user experiences as "it just works".
Each becomes a test.

| Property | Assertion | Test to write |
| :-- | :-- | :-- |
| **P1 — an answer or an honest error** | every request ends in 200 (with content) or a *coherent* 4xx/502/503/504 — never a bare 500, never a hang | proxy fault matrix (§4.3) |
| **P2 — no request loses its evidence** | every attempt, success or failure, is in `observations`; a 500 loses nothing | concurrency + fault suites |
| **P3 — the chain always advances** | a failed arm is retried, retired or demoted — never silently reused forever | `404`/`not_api_callable` retirement tests (exist) + 429 demotion test |
| **P4 — a fresh clone runs** | `pip install -e .` → `mi ingest` → `mi proxy` → a request answers, on a machine with no prior state | fresh-venv acceptance (§4.6) |
| **P5 — an old registry upgrades** | a registry written by any past release opens, migrates, and routes | migration fixtures (§4.1) |
| **P6 — the registry is re-derivable** | `rm mininfer.db` + re-ingest from `raw/` reproduces the same routing decisions | rebuild test (§5.1) |
| **P7 — no secret ever leaves** | no key in logs, responses, headers, DB, or the repo | security + secret-scan suites (§4.5) |
| **P8 — one slow arm cannot block the rest** | a hung provider cannot stall other requests | concurrency with an injected hang (§4.2) |
| **P9 — the health check means something** | `/healthz` is 200 **only** when the registry can actually route | **○ missing — see §4.1** |

> **P9 is the incident that cost the most this session.** The server served
> `{"ok": true}` while every routed request returned 500, because a
> half-initialised database had no `weights_resolved` view. A green health
> check on a broken server is worse than no health check: it tells an
> orchestrator not to restart the thing that needs restarting.

---

## 3. CI architecture (three cadences)

### 3.1 On every PR (fast gate, ≤ 20 min)

| Job | Covers |
| :-- | :-- |
| `pytest (sqlite, py3.11/3.12)` | existing unit+integration, **plus** the new concurrency, fault, fuzz, migration suites |
| `pytest (postgres)` | parity for every new suite; the storage seam is the one place SQLite≠Postgres |
| `frontend (typecheck, test, build)` | existing |
| `acceptance (both profiles, live)` | existing, **+ a fresh-clone smoke that asserts P4** |
| `python lint + typecheck` | **new** — `ruff check` + `ruff format --check` (fast), `mypy` on `mininfer/` in non-strict mode |
| `coverage floor` | **new** — `pytest --cov` with a ratcheting minimum (see §6) |
| `secret scan` | **new** — `gitleaks`/`detect-secrets` over the history, not just the tree |
| `no credentials committed` | existing (keep) |

### 3.2 Nightly (the "does it still work against the real world" run)

Runs on a schedule with **real provider keys** (secrets from the CI secret
store, never committed):

| Job | Covers |
| :-- | :-- |
| `canary: live providers` | one request per task profile per provider; assert 200 + content + latency budget; **publish the arm-by-arm health table** (the artefact §5.7 wants) |
| `canary: registry refresh` | `mi refresh` against the live catalogues; assert it *detects* a real price change, and that it exits non-zero if every source fails |
| `rebuild-from-raw` | wipe the registry, rebuild from `raw/`, assert routing decisions match (§5.1) |
| `soak (6h)` | continuous traffic; assert no memory growth, no file-handle leak, no quota drift, no stuck arm (§4.7) |
| `postgres parity (long)` | the full suite against Postgres under load |

### 3.3 Weekly / pre-release

* **Dependency sweep**: `pip-audit`, licence compatibility check, SBOM diff.
* **Multi-OS matrix**: Windows, macOS, Linux — the Windows leg matters
  (line-endings job exists for a reason; this session also showed OneDrive
  evicting a venv mid-command, and a Windows-only file-locking failure).
* **Provider-matrix run**: every adapter in `mininfer/ingest/` against its
  live endpoint, with the recorded response shape diffed against the parser
  (`tests/pricing/test_provider_contracts.py` is the model to follow).
* **Open-source dry run**: a fresh clone in a clean container, following only
  the README, must produce a working proxy.

---

## 4. Phase 1 — the suites that matter most (P0)

Ordered by *probability of a real incident × cost to fix late*.

### 4.1 Migration, health-check truth, and registry state  ← highest value

**Why:** an open-source user will bring a registry written by an older
release, or start with a partially-written file. This tree already produced
one such failure live (incident 3), and the health check actively hid it.

**Build:**
* `tests/fixtures/legacy/` — a registry at each past schema shape (v0: no
  `weights_resolved` view; v1: no `output_modalities`; v2: no `caps_state`;
  v3: no `sessions.tenant_id`; v4: partial WAL).
* `tests/test_migrations.py` — open each with today's `Store`; assert every
  view exists, every added column is present, the row counts survive, and the
  router still produces the same ranked arms.
* **Code fix required:** `Store.__init__` currently trusts a one-shot
  `_INITIALIZED_DBS` flag. If the DDL was interrupted, the process can believe
  the schema is ready when it is not. Fix: verify the critical objects after
  init (or make init idempotent-and-retryable) rather than trusting the flag.
* **Code fix required:** `/healthz` must reflect *routability*, not just
  "the process is up" — e.g. assert the registry can answer a route query,
  and report the reason in the body when it cannot.

### 4.2 Proxy-level concurrency

**Why:** the single most expensive defect found this session was concurrent
requests dying on a SQLite write lock — and it was invisible to every
existing test, because the tests are single-threaded.

**Build** `tests/test_proxy_concurrency.py`:
* N=20 concurrent requests through the real ASGI app with a fake runner;
  assert: no 500s; every request has a decision row; the number of
  `observations` equals the number of attempts; no request exceeds a latency
  bound; and **no write transaction is open while an upstream call is in
  flight** (the invariant pinned in `test_sqlite_concurrency.py`, now asserted
  at the proxy level).
* The same under Postgres (the CI leg runs it).

### 4.3 Fault matrix — the "never a bare 500" guarantee (P1)

**Why:** the live session found two ways a *successful* upstream call still
produced a 500 (a header overflow; a DB lock). The guarantee must be
systematic, not accidental.

**Build** `tests/test_fault_matrix.py`, a table-driven suite over
`(upstream behaviour → expected outcome)`:

| Injected fault | Expected |
| :-- | :-- |
| 429 + rate-limit headers | fallback to next arm; bucket drained; observation recorded |
| 429 with absurd reset header | recorded, reset dropped, **no 500** |
| 500 with an HTML body | classified `http_500`; fallback |
| 200 with `{"error": {...}}` | classified as a failure; fallback |
| 200 with empty content | `empty_content`; fallback |
| 200 with tool_calls only | **success** (not empty) |
| timeout mid-stream | stream ends cleanly; observation failed; no hang |
| connection reset before headers | classified; fallback |
| malformed SSE frames | relay what parsed; observation failed |
| provider returns a huge body | body cap enforced |
| every arm fails | coherent 502/504 naming the last error — **never 500** |

### 4.4 Fuzzing / hostile input

**Why:** the LLM-judge and scout parsers consume *model-written JSON*; a
malformed or hostile payload must not crash the process or the request.

**Build** `tests/test_fuzz_inputs.py`:
* `hypothesis` (new dev dep) over: judge verdict JSON, dossier facts,
  provider price payloads, rate-limit headers, SSE frames, request bodies.
* Invariants: no exception escapes the parser; the request still gets a
  coherent response; the parser never invents a price; a malformed dossier
  cannot lower the confidence gate.
* Byte-level fuzz of the SSE demultiplexer with random bytes.

### 4.5 Security suite

**Why:** the app fetches arbitrary URLs (`mi add-url`, `mi scout`) and
accepts caller-supplied keys per request — both are attack surfaces, and
neither is covered.

**Build** `tests/test_security.py`:
* **SSRF**: `mi add-url http://169.254.169.254/latest/meta-data/` and
  `file:///etc/passwd` must be refused; redirects to private ranges refused.
* **Auth**: every admin-only endpoint rejects an absent/incorrect token;
  the tenant key cannot read another tenant's usage.
* **Tenant isolation**: tenant A's session/transcript/usage is invisible to B.
* **Secret hygiene**: after a full request cycle, assert no key appears in
  `logs`, in the `decisions` table, in any response header/body, or in the
  transcript table.
* **Abuse**: rate limiting and the body-size cap are enforced under a burst;
  `MI_API_KEYS` gating cannot be bypassed by a header trick.

### 4.6 Packaging, platform, fresh-clone

**Why:** "it works on my machine" is the classic open-source failure, and the
first thing a user does is `pip install -e .` on a machine with nothing
installed.

**Build:** a CI job that creates a *fresh* venv, installs from the built
wheel (not the source tree), runs `mi --help`, `mi ingest --help`,
`mi doctor`, starts the proxy, and asserts one request answers (P4). Extend
the matrix to py3.11/3.12/3.13 and Windows/macOS/Linux.

### 4.7 Soak / resource-leak test

Six hours of synthetic traffic; assert: RSS flat, open file handles flat, the
`pushed_models` purge works, quota windows roll over correctly, and no arm
becomes permanently stuck (the `max_model_share_pct` cap actually caps).

---

## 5. Phase 2 — proving it uses the algorithm well

This is the second half of the question: *optimally and intelligently*. These
suites measure **behaviour**, not implementation.

### 5.1 Reproducibility and re-derivability (P6)

* `tests/test_rebuild_from_raw.py`: build a registry, snapshot it, delete the
  DB, rebuild from `raw/`, and assert the **routing decisions are identical**
  (same arms, same order) — the README's central claim, currently untested.
* `tests/test_evidence_provenance.py`: for every resolved price, assert a
  stored observation with a source, a timestamp and a confidence exists; and
  that no price exists without one.

### 5.2 Privacy audit

* Assert the default configuration stores **no** prompt or response bodies:
  scan every table for columns that hold message text and assert they are
  empty unless sessions/transcripts are explicitly enabled.
* Assert `X-MI-*` headers and the `decisions.reason` JSON never contain
  message content.

### 5.3 Performance budgets

* A budget test in CI: routing a task over a 25k-deployment registry must stay
  under a stated wall-clock (today's number is the baseline; the number
  itself is the deliverable).
* The two known hot-path wins, if taken: the blocking chat-search on the
  event loop, and the per-request full-table sweep.

### 5.4 Routing-quality regression (the "is it picking good models" suite)

* **Golden task set** (~40 prompts with human-rated expected answers and a
  required capability per task) in `tests/data/`.
* For each: the router must return an arm that is *capable* (the judge or a
  rubric says the answer is correct) — not merely cheap.
* **Baseline comparison**: the chosen arm must beat three naive strategies
  (cheapest, most-used, random) on quality-per-cent.
* **Guard rails**: `max_model_share_pct` is respected; no arm exceeds it; the
  free-first policy is respected when free headroom exists; a degraded arm is
  demoted after failures.

### 5.5 The self-improvement loop (does it actually get smarter?)

* A synthetic learning test: N arms with known quality, feed outcomes as they
  would arrive, and assert the posterior converges toward the true quality
  (the bandit is learning, not just shuffling).
* **Evidence-durability test** (the incident-2 regression): a judge that
  errors must not erase the batch's observations — assert the row count
  before/after a failing judge call.
* **Dossier prior calibration**: a dossier that names an ineligible task must
  not move the ranking (the `sound_when_to_use` gate, end to end through the
  scout).
* **Shadow-trial cost accounting**: shadow trials must cost the operator
  nothing — assert the cost recorded for a trial is zero and that the judge
  call itself is charged to the right tenant.

### 5.6 Documentation-claim tests

Every falsifiable claim in the README becomes a test: the endpoints exist with
the documented status codes, `mi doctor` lists the documented checks, the
CLI table matches `argparse`, the policy defaults match the documented
defaults, and the "no key-less path to inference" rule holds.

### 5.7 The operator-facing artefact

A nightly job that publishes a table: *provider → arms → answering / failing /
latency / quota*. It is the cheapest possible answer to "is this thing healthy
right now", and it turns provider outages into evidence rather than a support
ticket.

---

## 6. Engineering standards to raise the floor

| Change | Why |
| :-- | :-- |
| `dev = ["pytest>=8.0", "pytest-cov", "pytest-timeout", "hypothesis", "ruff", "mypy"]` | the missing tools for every gap above |
| **Coverage with a ratcheting floor** | start at the current number, never decrease; a new uncovered file fails the build |
| `pytest-timeout` on every test (default 60 s) | a deadlock should fail in a minute, not hold a CI slot for hours |
| `ruff` + `mypy` (non-strict) on `mininfer/` | catches the undefined-name class of bug *before* runtime (the `logging` incident) |
| `pip-audit` in CI + `requirements.lock` verification | supply-chain hygiene for a published package |
| A "no wall-clock dependency" lint over `tests/` | prevents incident 5 from ever recurring |

---

## 7. Release gates (the open-source go/no-go)

Nothing is published until every box is ticked.

**Correctness**
- [ ] All suites green on SQLite **and** Postgres, py3.11–3.13
- [ ] Concurrency suite: no 500s, no lost observations under N-way load
- [ ] Fault matrix: no bare 500 for any injected fault
- [ ] Migration suite: every legacy registry opens and routes
- [ ] `/healthz` is honest — it fails when routing cannot work

**Security & privacy**
- [ ] Secret scan over the **full git history** is clean
- [ ] SSRF, auth and tenant-isolation suites pass
- [ ] No prompt/response bodies stored by default (audited, not asserted by hand)
- [ ] `SECURITY.md` names a contact and a disclosure process
- [ ] Dependency audit clean; licences compatible with MIT

**Operational**
- [ ] Fresh clone → working proxy, following only the README
- [ ] `mi doctor` passes on a clean machine
- [ ] Docker image builds from a fresh clone and serves a request
- [ ] Crash-safe: killing the process mid-write leaves a readable registry

**Algorithmic**
- [ ] Routing-quality suite: chosen arms beat the naive baselines
- [ ] Learning-curve test: the bandit converges; no evidence is lost
- [ ] Free-first respected when headroom exists; share cap respected
- [ ] Published registry snapshot is normalised metadata only — no raw
      provider content redistributed

**Reproducibility**
- [ ] Registry rebuilt from `raw/` produces identical routing decisions

---

## 8. Execution order (suggested)

| Phase | Scope | Outcome |
| :-- | :-- | :-- |
| **1 — Stop the bleeding** (1–2 days) | §4.1 (migration + honest health check), §4.2 (concurrency), §4.3 (fault matrix) | the three incident classes that actually bite become impossible to regress |
| **2 — Raise the floor** (1–2 days) | dev tooling, coverage floor, ruff/mypy, timeout plugin, wall-clock lint, secret scan in CI | future bugs fail earlier and cheaper |
| **3 — Prove the quality** (2–3 days) | §5.4 routing quality, §5.5 learning curve, §5.1 re-derivability | evidence that the algorithm is *better*, not just present |
| **4 — Harden the edges** (2–3 days) | §4.5 security, §4.4 fuzz, §4.6 platform matrix, §5.7 canary artefact | open-source-ready |
| **5 — Publish** | release gates above | confidence to tag a release |

Each phase ends with its suites in CI and its gates in §7, so progress is
measurable rather than aspirational.

---

## 9. What this plan deliberately does not include

* **A universal "100% coverage" goal.** Coverage is a floor, not a target;
  the pricing and router logic deserve branch coverage, the CLI wrappers do
  not.
* **Provider-specific mocks for every adapter.** One contract test per
  adapter family plus a nightly live probe covers it; a mock per provider is
  a maintenance tax that decays.
* **Benchmarking against closed models.** The router's job is to pick the
  cheapest *capable* arm; the capability bar comes from public benchmarks and
  the judge, not from a private leaderboard.

## 10. Phase 1 — delivered (and what it found)

Phase 1 of this plan is implemented. It found four more defects, which is the
point of the exercise: none of them was visible to the existing suite.

| Delivered | Files | What it pins |
| :-- | :-- | :-- |
| §4.1 migration + honest readiness | `mininfer/store.py`, `mininfer/proxy.py`, `tests/test_migrations.py` | a registry missing objects is repaired on the next open; `/readyz` says *not ready* while that is true, and `/healthz` keeps meaning liveness |
| §4.2 proxy concurrency | `tests/test_proxy_concurrency.py` | no write transaction open across an upstream call; no lost observations; a slow arm cannot stall the others; **concurrent requests scale instead of collapsing** |
| §4.3 fault matrix | `tests/test_fault_matrix.py` | ten ways an upstream can misbehave, each of which must end in a coherent error — never a bare 500 — and each of which must still be recorded |
| §6 wall-clock lint | `tests/test_no_wallclock.py` | no test may pin a date close enough to now that the code will treat it as fresh, stale or expiring |
| §5.6 reasoning hygiene | `mininfer/execute.py`, `mininfer/proxy.py`, `tests/test_reasoning_leak.py` | a provider's visible `<think>` block is not the caller's answer |

### 10.1 The four defects Phase 1 found

**1. A lock convoy on the hot path (the big one).** Every request opens a
store, and every store open ran `PRAGMA journal_mode = WAL` under the process
write lock with the connection-level 10-second busy timeout. Sixteen
concurrent requests therefore queued behind each other's ten-second waits.
Measured on this tree, before → after:

| concurrency | before | after |
| --: | --: | --: |
| 1 | 15.6 req/s | 15.6 req/s |
| 4 | 6.4 req/s | 84.2 req/s |
| 8 | 2.5 req/s | 101.4 req/s |
| 16 | **0.7 req/s** (worst case 23.4 s) | **47.5 req/s** (worst case 0.32 s) |

The deeper cause was the app-level write lock: it inverted the lock order
(a connection holding SQLite's write lock waited for the process lock while
another thread held the process lock and waited for SQLite's), and it slept in
half-second steps between retries. SQLite's own locking arbitrates correctly
between connections; the fix lets it, with a real busy timeout and no
process lock on the write path.

**2. The body cap only applied when access control was on.** `MI_MAX_BODY_BYTES`
sat behind `cfg.enabled`, so an *open* deployment (no `MI_API_KEYS`, no admin
token) accepted an unbounded body. Abuse does not require authentication; the
cap now applies whenever it is configured.

**3. A reasoning model's thinking was served as the answer.** Some providers
put the model's `<think>` block *in the answer* rather than in a separate
field, so "what is the sum of 1 to 100" could return a truncated think-trace
with no number in it. The block is now stripped from the answer (the answer
after it is kept), and a stream whose only content was reasoning is recorded
as the failure it is, so the arm is demoted.

**4. The response builder discarded the cleaning.** The strip was applied to
the parsed result and then thrown away by the response builder, which echoed
the *raw* provider content. Found by live traffic, not by the unit test that
covered the strip — the test read the parsed result, the caller read the
response.

### 10.2 What Phase 1 deliberately left

* **The streaming path still relays a reasoning block.** A stream cannot
  un-say what it has already sent; suppressing it means buffering the block
  until it closes, which changes the SSE contract. The arm is demoted, but
  the caller still sees the trace. (Tracked as a known limitation.)
* **The routing hot path still sweeps every deployment** (§5.3) and the
  chat search still blocks the event loop. Both are real, both are measured
  now, and neither is a correctness defect.
* **`free_eligible: 0`** remains an evidence problem, not a code problem: the
  free tiers are real but the providers report no limit headers, so the
  router prices them at the paid floor on purpose.
