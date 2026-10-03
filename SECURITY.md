# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**, not as a public issue.

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities)
on this repository (**Security → Report a vulnerability**). Include:

- what you did, what you expected, and what happened;
- the version/commit and how MinInfer was run (`mi proxy`, container, hosted);
- whether access control was on (`MI_API_KEYS` / `MI_ADMIN_TOKEN` set) — the
  default is **off**, and that changes the impact of almost everything below.

We aim to acknowledge within a few days and to ship a fix or a mitigation before
any public disclosure.

## Supported versions

MinInfer is pre-1.0. Fixes land on `main`; there are no maintained release
branches yet. If you depend on a pinned commit, say so in the report.

## The security model you are relying on

Understanding the defaults is most of understanding the risk:

- **Access control is opt-in and off by default.** With nothing configured, the
  proxy is a local development tool: no authentication, no rate limiting, an
  open dashboard, and `/v1/search` (which spends money) is reachable. This is
  deliberate and pinned by `tests/test_profiles.py::test_local_profile_is_open`.
  **Do not expose it to a network without setting `MI_API_KEYS` /
  `MI_ADMIN_TOKEN`** — see `config/profiles/cloud.env` and `PRODUCTIZATION.md`.

- **The admin surface is not for the public internet.** `/`, `/legacy`,
  `/v1/stats`, `/v1/plan`, `/v1/providers`, `/v1/economics/*` and
  `/v1/local/*` expose provider pricing, routing decisions and caller IPs. The
  `MI_ADMIN_TOKEN` gates them; keep them behind that token *and* a network
  boundary (VPN, Cloudflare Access) where possible.

- **`POST /v1/local/probe` fetches an arbitrary URL.** It exists to let an
  operator test a local Ollama/llama.cpp endpoint. It is admin-only and must
  stay that way; it is an SSRF surface by design and is documented as one in
  `PRODUCTIZATION.md` (§4, gap 10).

- **Provider keys.** MinInfer reads provider credentials from the environment
  (`.env`, platform secrets). The dashboard may keep *user-supplied* provider
  keys in the browser's `localStorage` and forward them per request via
  `X-User-API-Keys`; a key entered there is only as safe as the origin serving
  the dashboard. Prefer server-side `MI_*`/provider env vars for shared
  deployments.

- **Key handling.** When `MI_API_KEYS_FILE` is used, keys are compared as SHA-256
  digests, so the file never holds a usable credential. `config/profiles/cloud.env`
  ships placeholders only and deliberately contains `do-not-deploy`; a test fails
  if that stops being true.

## Out of scope

- A deployment that runs with access control off and is reachable from an
  untrusted network.
- Free-tier provider terms-of-service questions (see `PRODUCTIZATION.md` §6).
- Denial of service from your own provider quotas.
