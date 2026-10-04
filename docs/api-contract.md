# API contract

What is promised, and what is not.

There is one HTTP surface: the OpenAI-compatible proxy. Most of it is a normal
OpenAI API — `POST /v1/chat/completions`, `GET /v1/models` — so an existing SDK
works by pointing `base_url` at MinInfer. The rest is MinInfer's own.

## Versioning

The path prefix `/v1` is the contract. `mininfer` is pre-1.0: **the `/v1` HTTP
surface is intended to be stable; the Python API is not** — import paths, function
signatures and dataclasses may change in a minor release. Pin the container image
or the PyPI version and talk HTTP.

| Status | Meaning |
|---|---|
| **stable** | Additive changes only within `/v1`. A breaking change needs a new prefix, and a deprecation window. |
| **experimental** | May change or disappear in a minor release. Safe to read; do not build a product on the shape. |
| **internal** | The dashboard and its assets. Not an API; may change at any time. |

A field being *added* to a stable response is not a breaking change. A field being
removed, renamed, retyped, or changing meaning is.

## Where the docs live

Three surfaces, all generated from the running app:

| Path | What |
|---|---|
| `/docs` | Swagger UI — every endpoint, with a request body you can fill in and send |
| `/redoc` | the same schema, laid out for reading |
| `/openapi.json` | the raw document, for SDK generation |

**All three are `admin`**, so with access control on they need `MI_ADMIN_TOKEN`
(a browser can send it as the password of an HTTP Basic prompt). That is
deliberate: the schema publishes every endpoint and an interactive client, which
is an operator surface, not a public one.

The request bodies are declared in `mininfer/api_schema.py` and attached with
`openapi_extra`. They describe the shape but do not validate it — a declared
Pydantic body would make FastAPI answer 422 in its own error format, and this
document promises the OpenAI envelope instead. `tests/test_api_contract.py`
asserts every POST declares a body, so Swagger cannot silently lose one.

This document, not the Swagger schema, is what is *promised*.

## Machine-readable inventory

The block below is parsed by `tests/test_api_contract.py`, which fails if it and
the application disagree — so a route added without documenting it is a red build
rather than a doc that quietly rots.

```text
GET    /healthz                                    stable
GET    /v1/models                                  stable
GET    /v1/models/explore                          experimental
POST   /v1/models/trial                            experimental
POST   /v1/chat/completions                        stable
POST   /v1/route                                   stable
POST   /v1/route-verdict                           experimental
POST   /v1/approve                                 experimental
POST   /v1/compact                                 experimental
POST   /v1/search                                  experimental
GET    /v1/session                                 stable
GET    /v1/session/messages                        stable
DELETE /v1/session/messages                        stable
GET    /v1/usage                                   stable
GET    /v1/savings                                 stable
GET    /v1/stats                                   experimental
GET    /v1/plan                                    experimental
GET    /v1/providers                               experimental
POST   /v1/providers/test                          experimental
GET    /v1/pushed-models                           experimental
POST   /v1/pushed-models                           experimental
GET    /v1/reviews                                 experimental
POST   /v1/reviews/decide                          experimental
GET    /v1/economics/overview                      experimental
GET    /v1/economics/providers                     experimental
GET    /v1/economics/quota                         experimental
GET    /v1/economics/history                       experimental
GET    /v1/economics/anomalies                     experimental
GET    /v1/economics/deployments/{deploy_id:path}  experimental
GET    /v1/local/probe                             experimental
POST   /v1/local/register                          experimental
GET    /                                            internal
GET    /legacy                                     internal
GET    /favicon.svg                                internal
GET    /mininfer-icon.svg                          internal
GET    /mininfer-icon.png                          internal
GET    /mininfer-logo.svg                          internal
GET    /mininfer-logo.png                          internal
GET    /docs                                       internal
GET    /docs/oauth2-redirect                       internal
GET    /openapi.json                               internal
GET    /redoc                                      internal
```

`/assets/*` is a static mount for the dashboard bundle, not an API.

## Error shape

Every error, including 401 and 429, uses the OpenAI error envelope, so existing
SDKs surface it without special-casing:

```json
{"error": {"message": "...", "type": "invalid_api_key", "code": "invalid_api_key"}}
```

`type` names the class (`invalid_api_key`, `rate_limit_exceeded`,
`request_too_large`, `no_candidates`, `auth_error`, `network_error`,
`tls_error`). A provider's own failure inside a 200 stream arrives as an SSE frame
carrying `error`, not as a truncated answer.

## Stability notes per group

**Routing (`/v1/chat/completions`, `/v1/route`).** The response is OpenAI-shaped,
with MinInfer's routing metadata added under a `mi` key. That key is additive —
ignore it and you have a plain OpenAI response.

**Sessions (`/v1/session*`).** Budgets are enforced *before* routing, so a
refused session cannot spend on a fallback chain. When access control is on, the
session id is scoped to the caller: a client-supplied id is a label, not a
boundary.

**Operator surface (`/`, `/legacy`, `/v1/stats`, `/v1/plan`, `/v1/providers`,
`/v1/economics/*`, `/v1/local/*`).** Exposes provider pricing, routing decisions
and caller IPs. Admin-only when `MI_ADMIN_TOKEN` is set, and it belongs behind a
network boundary as well.

**`POST /v1/local/probe`** fetches an arbitrary URL in order to test a local
Ollama or llama.cpp endpoint. It is admin-only by design and is an SSRF surface;
do not make it public.

## Deprecation

A stable endpoint or field is not removed in a minor release. The sequence is:
announce in `CHANGELOG.md`, keep it working for at least one minor release, then
remove it in the next major. Experimental endpoints may be removed at any time
with a changelog entry.

## What is deliberately absent

- **No idempotency keys and no request cancellation** for `/v1/chat/completions`.
  Retry safety for paying callers is not implemented.
- **No write API for the registry.** It is derived from provider catalogues by
  `mi refresh`; there is no endpoint that edits a price.
