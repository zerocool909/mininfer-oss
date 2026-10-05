"""OpenAI-compatible proxy (Phase 4).

`POST /v1/chat/completions` routes each request through the registry and executes
via the shared caller in `mi/execute.py`, recording an append-only observation for
every call — success *and* failure. That is what turns live traffic into the
"personal router" signal.

Model selection is by the request's `model` field, so any OpenAI client (pi, the
OpenAI SDK, curl) routes with zero code changes:

    "model": "auto"          -> route the default task (MI_DEFAULT_TASK)
    "model": "sql_generation"-> route that task profile
    "model": "openrouter/x"  -> direct call to one deployment (no routing)

The client's own request body (`tools`, `tool_choice`, `response_format`, ...) is
forwarded unchanged apart from the model id, and both non-streaming JSON and SSE
streaming responses pass through. Explainability rides along in the `mininfer`
extension (JSON) or in `X-MI-*` headers (streaming).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth as auth_mod
from . import db as db_mod
from . import intent as intent_mod
from . import env as env_mod
from .env import load_env
from .execute import (CallResult, Runner, classify_error_payload, key_env_for,
                      open_stream, resolve_endpoint, ENDPOINTS, available_providers,
                      _network_class, _TLS_HINT)
from .fetch import utcnow, _verify as _tls_verify
from .quota import classify_429, describe as quota_describe, record_from_headers
from . import quota as quota_mod
from . import search as search_mod
from .router import DEFAULT_TASK, Policy, rank_for_compare, route
from .store import Store
from .web.legacy import render_dashboard
from . import api_schema as _api_schema

# Load `.env` before anything reads the environment. This module is imported
# directly by uvicorn's --reload child, which never runs `mi.cli.main`.
load_env()

def _default_db() -> pathlib.Path:
    env_db = os.environ.get("MI_DB")
    if env_db:
        # A `postgresql://` DSN must not be coerced to a Path: that collapses the
        # slashes and psycopg2 rejects the result.
        return db_mod.target_from_env(env_db)
    # One path now: the previous generations' database filenames went with the
    # names they belonged to.
    return pathlib.Path("mininfer.db")


def _default_policy() -> pathlib.Path:
    env_policy = os.environ.get("MI_POLICY")
    if env_policy:
        return pathlib.Path(env_policy)
    return pathlib.Path("config/policy.yaml")


DB = _default_db()
POLICY = _default_policy()
INTENT_MODEL = os.environ.get("MI_INTENT_MODEL", "")
# Which backend answers the ambiguity tie-break: "gliner" (local, free) or ""
# (the configured LLM). Unset keeps the pre-existing behaviour exactly.
INTENT_BACKEND = os.environ.get("MI_INTENT_BACKEND", "").strip().lower()
INTENT_MERGE_BELOW = float(os.environ.get("MI_INTENT_MERGE_BELOW", "0.6"))

app = FastAPI(title="MinInfer", version="0.1.0",
              description="MinInfer — the cheapest capable model for every task")

# The React dashboard, when it has been built (`cd web && npm run build`).
# Serving it from this process keeps the whole thing one origin — no CORS, no
# second server — and it stays optional: without a build, `/` falls back to the
# dependency-free server-rendered page.
WEB_DIST = pathlib.Path(__file__).resolve().parent.parent / "web" / "dist"
#: The server's `.env`, and the template a missing one is seeded from. A key typed
#: in the dashboard reaches this file only after a provider has accepted it.
ENV_PATH = pathlib.Path(".env")
ENV_TEMPLATE = pathlib.Path(".env.example")
if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="web-assets")

@app.get("/favicon.svg", tags=["dashboard"])
@app.get("/mininfer-icon.svg", tags=["dashboard"])
@app.get("/mininfer-icon.png", tags=["dashboard"])
@app.get("/mininfer-logo.svg", tags=["dashboard"])
@app.get("/mininfer-logo.png", tags=["dashboard"])
async def static_brand_asset(request: Request):
    filename = request.url.path.lstrip("/")
    target = WEB_DIST / filename
    if target.exists():
        media_type = "image/svg+xml" if filename.endswith(".svg") else "image/png"
        return FileResponse(target, media_type=media_type)
    return Response(status_code=404)


# --------------------------------------------------------------------------- #
# access control (opt-in)
# --------------------------------------------------------------------------- #
#
# Inert until a deployment says who its callers are: with no MI_API_KEYS,
# MI_API_KEYS_FILE or MI_ADMIN_TOKEN set, `AuthConfig.enabled` is False, the
# guard passes every request straight through, and nothing is rate limited.
# That default is load-bearing — this is a local dev tool first — so the guard
# is one early return rather than a dependency threaded through 15 handlers.

_LIMITER = auth_mod.RateLimiter()          # in-process default; tests reset this
_LIMITER_CACHE: tuple[str, object] | None = None


def _limiter():
    """The limiter to enforce with: Redis when MI_REDIS_URL is set, else local.

    Resolved per request so setting the URL takes effect without a restart, and
    cached by URL so the Redis client is built once.
    """
    global _LIMITER_CACHE
    url = os.environ.get("MI_REDIS_URL") or ""
    if not url:
        return _LIMITER
    if _LIMITER_CACHE is None or _LIMITER_CACHE[0] != url:
        _LIMITER_CACHE = (url, auth_mod.make_limiter(url))
    return _LIMITER_CACHE[1]


def _cors_origins() -> list[str]:
    raw = os.environ.get("MI_CORS_ORIGINS") or ""
    return [o.strip() for o in raw.split(",") if o.strip()]


from fastapi.middleware.cors import CORSMiddleware

_cors_list = _cors_origins() or [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8765",
    "http://localhost:8765",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_list,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-MI-Deploy", "X-MI-Task", "X-MI-Policy", "X-MI-Candidates", "X-MI-Needs-Approval", "X-MI-Decision-Id", "X-MI-Intent"],
)


@app.middleware("http")
async def _guard(request: Request, call_next):
    cfg = auth_mod.load_config()
    if not cfg.enabled:
        return await call_next(request)

    if cfg.max_body_bytes:
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > cfg.max_body_bytes:
            return JSONResponse(
                status_code=413,
                content={"error": {"message": f"body exceeds {cfg.max_body_bytes} bytes",
                                   "type": "invalid_request_error", "code": "request_too_large"}})

    kind = auth_mod.classify(request.url.path)
    if kind == "public":
        return await call_next(request)

    token = auth_mod.presented_token(request)

    if kind == "admin":
        if not cfg.is_admin(token):
            return auth_mod.unauthorized(
                "an admin token is required for the registry and telemetry surface",
                admin=True)
        request.state.tenant = "admin"
        request.state.auth = "admin"
        return await call_next(request)

    tenant = cfg.authenticate(token)
    if tenant is None:
        # An admin is a superset. `/v1/models`, `/v1/session` and the playground's
        # `/v1/chat/completions` are *tenant* endpoints that the operator's own
        # dashboard calls, so refusing the admin token here would lock the console
        # out of itself. Admin traffic is not tenant-rate-limited.
        if cfg.is_admin(token):
            request.state.tenant = "admin"
            request.state.auth = "admin"
            return await call_next(request)
        return auth_mod.unauthorized(
            "a MinInfer API key is required; send `Authorization: Bearer <key>`")
    allowed, retry_after = _limiter().allow(tenant.name, tenant.rpm or cfg.default_rpm)
    if not allowed:
        return auth_mod.too_many(
            retry_after, f"rate limit exceeded for tenant {tenant.name!r}")
    request.state.tenant = tenant.name
    request.state.auth = "tenant"
    return await call_next(request)


def _db() -> pathlib.Path:
    return _default_db()


def _policy_path() -> pathlib.Path:
    return _default_policy()



def _distinct_options(ranked: list, allowed: list[str], n: int) -> list[str]:
    """Pick up to `n` deployments that differ in *maker*, not just in deployment.

    `route()` separates upstreams, which is the right rule for failover and the
    wrong one here: the same weights served through two gateways produce two
    identical answers, so comparing them tells the approver nothing.

    Three axes, relaxed in this order, because a thin pool still beats returning
    a single option:

    1. weights + upstream + vendor — independent, and a different maker
    2. weights + vendor           — same gateway, still a different maker
    3. weights only               — only one maker is available at all

    Vendor separation cannot be inferred from the upstream: every OpenRouter row
    shares upstream 'openrouter', so pass 1 collapses for all of them and the old
    two-pass version fell straight to `weights` alone. That is how a comparison
    could offer `z-ai/glm-5.3-prime` against `z-ai/glm-5.3-air` — two variants of
    one model, presented as a choice.
    """
    ok = set(allowed)
    out: list[str] = []
    seen_w: set[str] = set()
    seen_up: set[str] = set()
    seen_v: set[str] = set()
    for need_upstream, need_vendor in ((True, True), (False, True), (False, False)):
        if len(out) >= n:
            break
        for c in ranked:
            if len(out) >= n:
                break
            if c.deploy_id not in ok or c.weights_id in seen_w:
                continue
            if need_upstream and c.upstream in seen_up:
                continue
            if need_vendor and c.vendor in seen_v:
                continue
            seen_w.add(c.weights_id)
            seen_up.add(c.upstream)
            seen_v.add(c.vendor)
            out.append(c.deploy_id)
    return out


def _store() -> Store:
    return Store(_db())


# --------------------------------------------------------------------------- #
# sessions — a server-side spend ledger
# --------------------------------------------------------------------------- #
# The proxy stays stateless for callers that do not opt in: no session id means
# no accounting and no cap, exactly as before. Opting in is one header (or the
# OpenAI `user` field, which is the closest standard equivalent).
def _session_id(payload: dict, header: str | None) -> str | None:
    """Which session this request belongs to, or None for a one-shot."""
    for candidate in (header, payload.get("session_id"), payload.get("user")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:200]
    return None


def _request_session(request, session_header: str | None,
                     payload: dict | None = None) -> str | None:
    """The session a request belongs to, namespaced by tenant when auth is on.

    A client-supplied session is a label, not a boundary: two tenants naming
    `shared` must not share (or exhaust) one budget, and an authenticated request
    that names nothing gets the tenant itself so a per-tenant cap still applies.

    One function because the *spend ledger*, the *transcript* and the *response
    envelope* all have to agree on the key. They had drifted: `_handle` wrote
    under `tenant/session` while `GET /v1/session` read the bare `session`, so
    with auth on the footer showed a different number than the cap enforced.
    """
    session_id = _session_id(payload or {}, session_header)
    tenant = getattr(getattr(request, "state", None), "tenant", None)
    if tenant and tenant != "admin":
        return f"{tenant}/{session_id}" if session_id else tenant
    return session_id


def _caller_tenant(request) -> str | None:
    """The tenant a request's writes are attributed to.

    `admin` and anonymous local calls get None — the operator is not a tenant,
    and an unattributed call has to stay distinguishable from a tenant's.
    """
    who = getattr(getattr(request, "state", None), "tenant", None)
    return who if who and who != "admin" else None


def _turn(messages: list | None, answer: str | None) -> list[dict]:
    """The messages a single turn contributes to the server transcript.

    Only the *newest* user message, not the whole request list: the client
    re-sends the last few turns for context on every call, so storing the list
    verbatim would duplicate history each time. The answer is appended when there
    is one — a turn with no answer stores the question only, which is why a
    failed turn still leaves a trace.
    """
    turn: list[dict] = []
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user" and m.get("content"):
            turn.append({"role": "user", "content": m["content"]})
            break
    if answer:
        turn.append({"role": "assistant", "content": answer})
    return turn


def _session_limit(policy) -> int:
    """Token ceiling for a session. 0 means uncapped."""
    raw = os.environ.get("MI_SESSION_TOKEN_LIMIT", "")
    if raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return max(0, int(getattr(policy, "session_token_limit", 0) or 0))


def _session_cost_limit(policy) -> float:
    """USD ceiling for a session. 0 means uncapped."""
    raw = os.environ.get("MI_SESSION_COST_LIMIT", "")

    if raw.strip():
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return max(0.0, float(getattr(policy, "session_cost_limit", 0.0) or 0.0))


def _prompt_tokens(messages: list) -> int:
    """Rough size of the prompt, without a tokenizer dependency.

    Deliberately generous (~3 chars per token rather than the usual 4): the cap
    should refuse a call it is unsure about, not admit one that overshoots. Only
    ever used for the reservation below, never for accounting.
    """
    chars = 0
    for m in messages or []:
        if isinstance(m, dict):
            chars += len(str(m.get("content") or ""))
    return max(1, chars // 3)


def _session_block(policy, store: Store, session_id: str | None, *,
                   messages: list | None = None,
                   max_tokens: int = 1024,
                   reserve_usd: float = 0.0) -> JSONResponse | None:
    """A refusal when the session cannot afford another call, else None.

    Reserves `prompt estimate + max_tokens` before starting, so the ceiling is a
    real ceiling rather than "stop once you are already over it" — checking only
    `used >= limit` lets a session overshoot by a whole call every time. The
    completion half of the reservation is an exact upper bound (`max_tokens` is
    passed to the upstream), and the prompt half is a documented over-estimate.

    `reserve_usd` is the worst-case price of everything else the call will spend,
    which for a search is the paid provider's rate. Two budgets, checked
    independently: tokens bound model usage, dollars bound all of it.

    Checked before routing so a refused session cannot spend on a fallback chain,
    and returned as 429 because that is the status an OpenAI client already knows
    how to back off from.
    """
    if not session_id:
        return None
    limit = _session_limit(policy)
    cost_limit = _session_cost_limit(policy)
    if not limit and not cost_limit:
        return None
    used = store.session_usage(session_id)
    reserved = _prompt_tokens(messages) + max(0, max_tokens)
    if limit and used["tokens"] + reserved > limit:
        return _error(
            429, "session_budget_exceeded",
            f"session {session_id!r} has spent {used['tokens']:,} of its {limit:,} token"
            f" budget across {used['calls']} call(s), and this call needs up to"
            f" {reserved:,} more; start a new session or raise session_token_limit")
    if cost_limit and used["cost_usd"] + reserve_usd > cost_limit:
        return _error(
            429, "session_budget_exceeded",
            f"session {session_id!r} has spent ${used['cost_usd']:.4f} of its"
            f" ${cost_limit:.4f} budget, and this call needs up to"
            f" ${reserve_usd:.4f} more; start a new session or raise"
            f" session_cost_limit")
    return None


def _session_payload(policy, store: Store, session_id: str | None) -> dict | None:
    """Post-call session state, for the response envelope and the stream header."""
    if not session_id:
        return None
    used = store.session_usage(session_id)
    limit = _session_limit(policy)
    cost_limit = _session_cost_limit(policy)
    return {**used, "limit": limit or None,
            "remaining": (limit - used["tokens"]) if limit else None,
            "cost_limit": cost_limit or None,
            "cost_remaining": (round(cost_limit - used["cost_usd"], 6)
                               if cost_limit else None),
            # What this session *avoided* by taking the cheap arm, priced at the
            # cheapest paid sibling of the same artifact. Reported here rather
            # than folded into `cost_usd`: the two describe different things
            # (money spent vs. a counterfactual), and a client that disagrees
            # with the estimate can still read the number it was billed.
            "savings": store.spend_savings(session_id=session_id)}


def _intent_llm():
    """Optional tie-break callable for `classify`, or None when unconfigured.

    Injected rather than imported so `mi.intent` stays pure — and so a
    specialised classifier can replace this without touching the router.

    Two backends, cheapest first:

      * the local understanding layer (GLiNER2.5) when `MI_INTENT_BACKEND=gliner`
        and the `[understanding]` extra is installed — no network, no spend;
      * otherwise the configured LLM (`MI_INTENT_MODEL`).

    Both expose the same `(prompt, candidates) -> task | None` shape, so the
    router cannot tell them apart, and a missing model falls through to the LLM
    rather than failing the request.
    """
    from . import understanding

    if INTENT_BACKEND in ("gliner", "local", "understanding"):
        local = understanding.as_intent_backend()
        if local is not None:
            return local

    if not INTENT_MODEL:
        return None

    def ask(prompt: str, candidates: list[str]) -> str | None:
        try:
            runner = Runner()
            question = (
                "Classify the request into exactly one of these tasks. "
                "Reply with only the task name, nothing else.\n"
                f"Tasks: {', '.join(candidates)}\n"
                f"Request: {prompt[:2000]}"
            )
            res = runner(INTENT_MODEL, [{"role": "user", "content": question}],
                         max_tokens=16, temperature=0.0)
            if not res.ok:
                return None
            word = (res.text or "").strip().split()
            picked = word[0].strip(".,`\"'") if word else ""
            return picked if picked in candidates else None
        except Exception:
            return None

    return ask


def _keys_available(
    deploy_id: str,
    user_keys: dict[str, str] | None = None,
    local_endpoints: dict[str, str] | None = None,
) -> bool:
    """True when the deployment's provider has a configured API key or is local.

    `route()` ranks on price and quality, so it can put a *free* arm first whose
    provider has no key — an attempt guaranteed to fail with `no_api_key`, which
    then falls through to a paid arm. The proxy skips those (and says so) rather
    than burning an attempt and reporting a misleading failure.
    """
    head = deploy_id.partition(":")[0].split("/")[0]
    custom_key = (user_keys or {}).get(head)
    custom_base = (local_endpoints or {}).get(head)
    ep = resolve_endpoint(deploy_id, api_key=custom_key, base_url=custom_base)
    return ep.error is None and bool(ep.api_key)


# --------------------------------------------------------------------------- #
# fallback execution
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Attempt:
    deploy_id: str
    ok: bool
    error_class: str | None
    latency_ms: float | None
    tokens_in: int
    tokens_out: int
    # The provider's own message, kept so an operator sees *why* rather than a
    # classification that flattens "model retired" and "API not enabled" together.
    detail: str | None = None


def _has_tool_calls(res: CallResult) -> bool:
    """A tool-call turn legitimately has empty text; it is not an empty answer."""
    try:
        return bool(res.raw["choices"][0]["message"].get("tool_calls"))
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


def try_fallbacks(
    deploy_ids: list[str],
    messages: list[dict],
    runner,
    *,
    task_name: str,
    store: Store | None,
    max_tokens: int | None = None,
    temperature: float = 0.0,
    session_id: str | None = None,
    tenant_id: str | None = None,
) -> tuple[str | None, CallResult, list[Attempt]]:
    """Call candidates in order; return (selected, last_result, attempts).

    A 429 / 5xx / timeout on the primary moves to the next candidate — the
    diversity-constrained top-K from `route()` are already ordered primary,
    fallback, backup. Every attempt is recorded as an observation, so a 429 today
    demotes that arm tomorrow.

    Every attempt is also charged to `session_id`, because every attempt was
    billed — not just the one that answered.
    """
    attempts: list[Attempt] = []
    last = CallResult("", error_class="no_candidates")
    for did in deploy_ids:
        res = runner(did, messages, max_tokens=max_tokens, temperature=temperature)
        # An HTTP 200 with no usable text and no tool call cannot serve the caller,
        # so it is a failed attempt and must fall through like a 429 would.
        if res.ok and not (res.text or "").strip() and not _has_tool_calls(res):
            res = CallResult(did, error_class="empty_content",
                             latency_ms=res.latency_ms, tokens_in=res.tokens_in,
                             tokens_out=res.tokens_out, raw=res.raw)
        attempts.append(Attempt(did, res.ok, res.error_class, res.latency_ms,
                                res.tokens_in, res.tokens_out, res.error_detail))
        if store is not None:
            # Every attempt consumes quota; a 429 drains the minute bucket so the
            # next route sees the headroom and demotes before retrying blind.
            store.record_usage(did)
            reason = _record_rate_limits(
                store, did, headers=res.rate_limit_headers,
                error_class=res.error_class, error_detail=res.error_detail)
            if res.error_class == "429":
                store.exhaust(did, window="minute")
            _retire_if_uncallable(store, did, error_class=res.error_class,
                                  error_detail=res.error_detail)
            cost = _deploy_cost(store, did, res.tokens_in, res.tokens_out)
            store.observe(
                did, task_name, ok=res.ok, ts=utcnow(), error_class=res.error_class,
                latency_ms=res.latency_ms, tokens_in=res.tokens_in,
                tokens_out=res.tokens_out, cost_usd=cost,
                signal_kind="provider_reported", signal_value=1.0 if res.ok else 0.0,
                session_id=session_id, tenant_id=tenant_id,
                rate_limit_reason=reason,
            )
            if session_id:
                store.add_session_usage(session_id, tokens_in=res.tokens_in,
                                        tokens_out=res.tokens_out, cost_usd=cost,
                                        tenant_id=tenant_id)
        last = res
        if res.ok:
            store.commit() if store is not None else None
            return did, res, attempts
    if store is not None:
        store.commit()
    return None, last, attempts


def _retire_if_uncallable(store, deploy_id: str, *, error_class: str | None,
                          error_detail: str | None) -> bool:
    """Take an arm out of routing when the provider says it is not callable.

    The only durable way to remove such a model. The catalogue reports it `live`
    (there is no field for the restriction), so without a *sticky* retirement the
    next ingest puts it straight back and every request pays an attempt to
    rediscover that it cannot be called. `Store.retire_deployment` records
    `status_source='runtime'`, which is what survives the re-ingest.
    """
    if store is None or error_class != "not_api_callable":
        return False
    reason = (error_detail or "not callable via the API").strip().splitlines()[0][:200]
    return store.retire_deployment(deploy_id, reason)


def _record_rate_limits(store, deploy_id: str, *, headers, error_class: str | None = None,
                        error_detail: str | None = None) -> str | None:
    """Persist what the provider said about its limits; name a 429's reason.

    One helper for both the streaming and non-streaming paths, so a limit observed
    on one cannot be invisible to the other. Headers are recorded on *success* as
    well as failure — that is how a bucket gets a real size instead of a
    documented guess.
    """
    if store is None:
        return None
    if headers:
        record_from_headers(store, deploy_id, headers, observed_at=utcnow())
    if error_class == "429":
        return classify_429(headers, error_detail)
    return None


def _deploy_cost(store: Store, deploy_id: str, tin: int, tout: int) -> float | None:
    row = store.deployment_prices(deploy_id)
    if row is None or row["price_in"] is None or row["price_out"] is None:
        return None
    if tin is None or tout is None:
        # An upstream that reports no usage cannot be costed. Returning None
        # rather than 0 keeps an unmeasured call from reading as a free one — and
        # it used to raise TypeError here, which only stayed hidden because the
        # fixtures that reach this path have no prices to look up.
        return None
    return (row["price_in"] * tin + row["price_out"] * tout) / 1_000_000


# --------------------------------------------------------------------------- #
# response shaping
# --------------------------------------------------------------------------- #


def _chat_response(
    deploy_id: str,
    res: CallResult,
    *,
    task: str,
    policy: str,
    reason: dict,
    alternatives: list[str],
    session: dict | None = None,
    decision_id: int | None = None,
) -> JSONResponse:
    raw = res.raw if isinstance(res.raw, dict) else {}
    message: dict = {"role": "assistant", "content": res.text}
    finish = "stop"
    try:
        m = raw["choices"][0]["message"]
        message = {"role": "assistant", "content": m.get("content") or ""}
        # Tool calls (and reasoning side-channels) pass through untouched — pi is
        # an agent and needs its tool_calls back verbatim.
        for k in ("tool_calls", "function_call", "reasoning", "reasoning_content"):
            if m.get(k) is not None:
                message[k] = m[k]
        finish = raw["choices"][0].get("finish_reason") or "stop"
    except (KeyError, IndexError, TypeError):
        pass

    tin, tout = res.tokens_in, res.tokens_out
    meta = {
        "selected_model": deploy_id,
        "task": task,
        "policy": policy,
        "reason": reason,
        "alternatives": alternatives,
        "needs_approval": bool(reason.get("needs_approval")),
        "latency_ms": res.latency_ms,
        "session": session,
        "decision_id": decision_id,
    }
    headers = {
        "X-MI-Deploy": deploy_id,
        "X-MI-Task": task,
        "X-MI-Policy": policy,
        "X-MI-Candidates": ", ".join(alternatives),
        "X-MI-Needs-Approval": "true" if reason.get("needs_approval") else "false",
        "X-MI-Decision-Id": str(decision_id or ""),
    }
    return JSONResponse(content={
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": raw.get("model", deploy_id),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": tin or 0, "completion_tokens": tout or 0,
                  "total_tokens": (tin or 0) + (tout or 0)},
        "mi": meta,
    }, headers=headers)


def _options_response(
    results: list[tuple[str, "CallResult"]],
    *,
    task: str,
    policy: str,
    reason: dict,
    info: dict | None = None,
    decision_id: int | None = None,
) -> JSONResponse:
    """Best-of-N: one choice per successful candidate, for human approval.

    Used when the winning arm is on trial and there is no evidence to prefer it
    on. The caller picks, and the pick is posted to `/v1/approve` — that is what
    turns the choice into an observation. Candidates are already diversified
    across providers, so the two answers are genuinely independent.
    """
    choices: list[dict] = []
    options: list[dict] = []
    tin = tout = 0
    for i, (deploy_id, res) in enumerate(results):
        raw = res.raw if isinstance(res.raw, dict) else {}
        message: dict = {"role": "assistant", "content": res.text}
        finish = "stop"
        try:
            m = raw["choices"][0]["message"]
            message = {"role": "assistant", "content": m.get("content") or ""}
            for k in ("tool_calls", "function_call", "reasoning", "reasoning_content"):
                if m.get(k) is not None:
                    message[k] = m[k]
            finish = raw["choices"][0].get("finish_reason") or "stop"
        except (KeyError, IndexError, TypeError):
            pass
        choices.append({"index": i, "message": message, "finish_reason": finish})
        # Options may come from beyond the diversified top-k, so prefer the
        # caller-supplied map over `reason.selected` (which is only the top-k).
        sel = ((info or {}).get(deploy_id)
               or next((s for s in reason.get("selected", [])
                        if s["deploy_id"] == deploy_id), {}))
        options.append({
            "index": i,
            "deploy_id": deploy_id,
            "cost_per_success": sel.get("cost_per_success"),
            "free_kind": sel.get("free_kind"),
            "p_lb": sel.get("p_lb"),
            # Each option was executed serially, so this is that option's own
            # call time — not the total for the round.
            "latency_ms": res.latency_ms,
        })
        tin += res.tokens_in
        tout += res.tokens_out
    first = options[0]["deploy_id"] if options else ""
    opts_meta = {
        "selected_model": first,
        "task": task,
        "policy": policy,
        "reason": reason,
        "options": options,
        "needs_approval": True,
        "session": None,
        "decision_id": decision_id,
    }
    headers = {
        "X-MI-Decision-Id": str(decision_id or ""),
    }
    return JSONResponse(content={
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": first,
        "choices": choices,
        "usage": {"prompt_tokens": tin, "completion_tokens": tout,
                  "total_tokens": tin + tout},
        "mi": opts_meta,
    }, headers=headers)


def _error(status: int, error_class: str, message: str, decision_id: int | None = None) -> JSONResponse:
    headers = {}
    if decision_id:
        headers["X-MI-Decision-Id"] = str(decision_id)
    return JSONResponse(status_code=status, content={
        "error": {"message": message, "type": error_class, "code": status},
    }, headers=headers if headers else None)


def _no_key_hint(deploy_ids: list[str]) -> str:
    """Say which env var, and where the in-app alternative lives.

    `no_api_key` is the one error a caller can always fix themselves, so the
    message has to name the fix rather than restate the symptom.
    """
    envs = sorted({e for e in (key_env_for(d) for d in deploy_ids) if e})
    parts = []
    if envs:
        parts.append(f"set {envs[0]} in the server environment")
    parts.append("or add the provider key under Settings \u2192 Providers "
                 "(it is sent with each request, so no restart)")
    return "; ".join(parts) + "."


def _all_failed(attempts: list[Attempt], res: CallResult, decision_id: int | None = None) -> JSONResponse:
    last_err = res.error_class or "unknown"
    if last_err == "no_api_key":
        ids = [a.deploy_id for a in attempts] or [res.deploy_id]
        return _error(503, last_err,
                      f"no API key for {ids[0]}: {_no_key_hint(ids)}",
                      decision_id=decision_id)
    return _error(429 if last_err == "429" else 502, last_err,
                  f"all {len(attempts)} candidate(s) failed (last: {last_err}"
                  + (f": {res.error_detail}" if res.error_detail else "")
                  + ")", decision_id=decision_id)


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #


@app.get("/healthz", tags=["operator"])
def healthz() -> dict:
    return {"ok": True, "service": "mininfer"}


@app.get("/v1/models", tags=["catalog"])
def models() -> dict:
    """The routable surface, not the 23k deployments: one virtual model per task
    (so an OpenAI client's model picker is exactly the thing it can ask for)."""
    _, tasks = Policy.load(_policy_path())
    base_ids = ["auto"] + list(tasks)
    seen = set()
    data = []
    for prefix in ("", "mininfer/"):
        for i in base_ids:
            mid = f"{prefix}{i}" if prefix else i
            if mid not in seen:
                seen.add(mid)
                data.append({"id": mid, "object": "model", "owned_by": "mininfer"})
    return {"object": "list", "data": data}


@app.get("/v1/models/explore", tags=["catalog"])
def explore_models(
    q: str | None = None,
    provider: str | None = None,
    capability: str | None = None,
    free_only: bool = False,
    untried_only: bool = False,
    max_price_out: float | None = None,
    min_context: int | None = None,
    sort: str = "name",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Browse the registry itself — every deployment, not just the routable set."""
    store = _store()
    try:
        return store.explore_models(
            q=q, provider=provider, capability=capability, free_only=free_only,
            untried_only=untried_only,
            max_price_out=max_price_out, min_context=min_context, sort=sort,
            limit=limit, offset=offset,
        )
    finally:
        store.close()


PROVIDER_METADATA: dict[str, dict[str, Any]] = {
    "google": {
        "name": "Google Gemini & Vertex AI",
        "description": "Google's multimodal Gemini models with 1M-2M context window.",
        "docs_url": "https://aistudio.google.com/app/apikey",
        "setup_guide": "Get a free Gemini API key from Google AI Studio or Google Cloud Console (APIs & Services > Credentials).",
    },
    "groq": {
        "name": "Groq LPU",
        "description": "Ultra-fast LPU inference hosting Llama, Qwen, and open models.",
        "docs_url": "https://console.groq.com/keys",
        "setup_guide": "Generate an API key in the Groq Console.",
    },
    "openrouter": {
        "name": "OpenRouter Gateway",
        "description": "Unified routing gateway reaching 300+ models from OpenAI, Anthropic, Google, and open-source.",
        "docs_url": "https://openrouter.ai/keys",
        "setup_guide": "Create a key at OpenRouter.ai to access hundreds of free and paid models.",
    },
    "cerebras": {
        "name": "Cerebras CS-3",
        "description": "World record token generation speeds on Cerebras Wafer-Scale clusters.",
        "docs_url": "https://cloud.cerebras.ai",
        "setup_guide": "Get an API key from Cerebras Cloud for 1800+ tok/sec Llama 3 models.",
    },
    "sambanova": {
        "name": "SambaNova Cloud",
        "description": "High-throughput SN40L Reconfigurable Dataflow units running Llama 3.3 70B & Qwen.",
        "docs_url": "https://cloud.sambanova.ai/apis",
        "setup_guide": "Register at SambaNova Cloud for free tier access to 70B parameter models.",
    },
    "mistral": {
        "name": "Mistral AI",
        "description": "Frontier European models including Mistral Large, Pixtral, and Codestral.",
        "docs_url": "https://console.mistral.ai/api-keys",
        "setup_guide": "Get an API key from Mistral La Plateforme. Free experiment tier available.",
    },
    "novita": {
        "name": "Novita AI",
        "description": "Cost-effective GPU serverless endpoints for DeepSeek, Llama, and Mistral.",
        "docs_url": "https://novita.ai/settings/key-management",
        "setup_guide": "Create an API key in Novita AI Console.",
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "description": "NVIDIA Inference Microservices hosting optimized NeMo, Llama, and Qwen.",
        "docs_url": "https://build.nvidia.com",
        "setup_guide": "Generate a personal NIM key at build.nvidia.com with 1000 free inference credits.",
    },
    "ollama": {
        "name": "Ollama (Local Engine)",
        "description": "Run open LLMs locally on your Mac/PC (Llama, DeepSeek, Qwen) with zero marginal cost.",
        "docs_url": "https://ollama.com",
        "setup_guide": "Install Ollama locally and run `ollama serve`. Default URL is http://localhost:11434/v1. 100% private and keyless.",
    },
    "llamacpp": {
        "name": "llama.cpp / llama-server",
        "description": "High-performance C++ LLM inference server with GGUF quantization.",
        "docs_url": "https://github.com/ggerganov/llama.cpp",
        "setup_guide": "Run `llama-server -m model.gguf --port 8080`. Default URL is http://localhost:8080/v1.",
    },
    "deepinfra": {
        "name": "DeepInfra",
        "description": "Fast serverless inference for open-weights models.",
        "docs_url": "https://deepinfra.com/dash/api_keys",
        "setup_guide": "Get an API key from DeepInfra with free starting credits.",
    },
    "github": {
        "name": "GitHub Models",
        "description": "Azure AI inference for GitHub developers.",
        "docs_url": "https://github.com/marketplace/models",
        "setup_guide": "Generate a GitHub Personal Access Token (PAT) with model access.",
    },
}


@app.get("/v1/providers", tags=["catalog"])
def providers() -> dict:
    """Return all supported providers, their configured status, and registered models."""
    store = _store()
    all_deployments = store.deployments()
    store.close()

    deps_by_provider: dict[str, list[dict]] = {}
    for d in all_deployments:
        prov = d.get("provider", "").split("/")[0]
        try:
            caps = json.loads(d.get("caps") or "{}")
        except Exception:
            caps = {}
        try:
            benchmarks = json.loads(d.get("benchmark") or "{}")
        except Exception:
            benchmarks = {}

        item = {
            "deploy_id": d["deploy_id"],
            "model_id": d.get("provider_model_id"),
            "display_name": d.get("display_name") or d.get("provider_model_id"),
            "context_window": d.get("context_window"),
            "max_output": d.get("max_output"),
            "price_in": d.get("price_in"),
            "price_out": d.get("price_out"),
            "is_free": bool(d.get("zero_price") or d.get("free_variant") or d.get("price_in") == 0),
            "caps": caps,
            "benchmarks": benchmarks,
        }
        deps_by_provider.setdefault(prov, []).append(item)

    out = []
    priority_order = ["google", "groq", "openrouter", "cerebras", "sambanova", "mistral", "novita", "nvidia", "ollama", "llamacpp", "deepinfra", "github"]
    remaining_keys = [k for k in ENDPOINTS.keys() if k not in priority_order]

    for p_id in priority_order + remaining_keys:
        if p_id not in ENDPOINTS:
            continue
        base_url, key_env, _ = ENDPOINTS[p_id]
        meta = PROVIDER_METADATA.get(p_id, {})
        has_default_key = bool(key_env and os.environ.get(key_env))
        is_local = p_id in ("ollama", "llamacpp")
        models_list = deps_by_provider.get(p_id, [])
        # Prioritize free models first, then by model_id
        models_list.sort(key=lambda m: (not m["is_free"], (m["model_id"] or "").lower()))

        out.append({
            "id": p_id,
            "name": meta.get("name", p_id.capitalize()),
            "description": meta.get("description", ""),
            "base_url": base_url,
            "key_env": key_env,
            "has_project_key": has_default_key,
            "is_local": is_local,
            "docs_url": meta.get("docs_url", ""),
            "setup_guide": meta.get("setup_guide", ""),
            "models_count": len(models_list),
            "models": models_list,
        })

    return {"providers": out}


@app.get("/v1/local/probe", tags=["operator"])
async def local_probe(engine: str = "ollama", url: str | None = None) -> dict:
    """Check connectivity to a local LLM daemon (Ollama or llama.cpp) and discover installed models."""
    target_url = (url or ("http://localhost:11434" if engine == "ollama" else "http://localhost:8080")).rstrip("/")
    models = []
    connected = False
    error = None
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            if engine == "ollama":
                res = await client.get(f"{target_url}/api/tags")
                if res.status_code == 200:
                    connected = True
                    data = res.json()
                    models = [m.get("name") for m in data.get("models", []) if m.get("name")]
            else:
                res = await client.get(f"{target_url}/v1/models")
                if res.status_code == 200:
                    connected = True
                    data = res.json()
                    models = [m.get("id") for m in data.get("data", []) if m.get("id")]
    except Exception as ex:
        error = str(ex)

    return {
        "engine": engine,
        "url": target_url,
        "connected": connected,
        "models": models,
        "error": error if not connected else None,
    }


@app.post("/v1/local/register", openapi_extra=_api_schema.LOCAL_REGISTER, tags=["operator"])
async def local_register(request: Request) -> dict:
    """Register discovered local models into MinInfer registry."""
    payload = await request.json()
    engine = payload.get("engine") or "ollama"
    models = payload.get("models") or []
    registered = []
    store = _store()
    for m in models:
        did = store.register_local_deployment(engine, m)
        registered.append(did)
    store.close()
    return {"ok": True, "registered": registered}


async def _probe_provider(provider_id: str, custom_key: str, key_source: str = "none") -> dict:
    """Connectivity check for a provider with no model in the registry.

    Calls the provider's own `GET /models`, so it verifies DNS/TLS reachability
    and that the key is accepted, without inventing a chat model. A key that works
    still leaves the registry empty — the reply says so and names the command that
    fixes it, because "connected" with no usable model is the confusing half-state
    this replaces.
    """
    base, key_env, headers = ENDPOINTS[provider_id]
    key = custom_key or (os.environ.get(key_env) if key_env else "")
    out = {
        "ok": False, "provider": provider_id, "deploy_id": None,
        "model": f"{provider_id} (no model ingested)", "is_free": False,
        "latency_ms": None, "reply": None, "error_class": None, "error_detail": None,
        "key_source": key_source,
    }
    if not key:
        out["error_class"] = "no_api_key"
        out["error_detail"] = (
            f"no {provider_id} model is in the registry. Set "
            f"{key_env or provider_id.upper() + '_API_KEY'} and run `mi refresh` to add them"
        )
        return out

    url = base.rstrip("/") + "/models"
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=_tls_verify()) as http_c:
            r = await http_c.get(url, headers={**headers, "Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        kind = _network_class(exc)
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["error_class"] = kind
        out["error_detail"] = _TLS_HINT if kind == "tls_error" else str(exc)
        return out

    out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if r.status_code in (401, 403):
        out["error_class"] = "auth_error"
        out["error_detail"] = f"the provider rejected the key (HTTP {r.status_code})"
        return out
    if r.status_code >= 400:
        out["error_class"] = f"http_{r.status_code}"
        out["error_detail"] = r.text[:200]
        return out

    try:
        n = len((r.json() or {}).get("data") or [])
    except Exception:
        n = 0
    out["ok"] = True
    out["reply"] = (f"key accepted — {n} model(s) available. "
                    f"Run `mi refresh` to add {provider_id} to the registry.")
    return out


@app.get("/v1/keys", tags=["operator"])
def list_provider_keys() -> dict:
    """Which providers are configured server-side, and under which variable.

    Presence only, never the value. `/v1/providers` carries this too, but it also
    carries every model's catalogue, so a client that only needs "is groq
    configured?" should not download hundreds of OpenRouter rows to find out.

    `configured` is the *running process's* view: it reflects the environment it
    started with, plus any key `POST /v1/keys` has since written.
    """
    local = {"ollama", "llamacpp"}
    return {
        "providers": [
            {
                "id": pid,
                "env_var": key_env,
                "configured": bool(key_env and os.environ.get(key_env)),
                "is_local": pid in local,
            }
            for pid, (_, key_env, _) in ENDPOINTS.items()
        ]
    }


@app.post("/v1/keys", openapi_extra=_api_schema.SET_KEY, tags=["operator"])
async def set_provider_key(request: Request) -> dict:
    """Verify a provider key, and only then persist it to the server's `.env`.

    The portal already keeps keys in the browser and forwards them per request;
    this is the opt-in step that also puts one on the server, so `mi ingest` and
    the next boot can see it. A key is written **only after the provider accepts
    it** — an unverified secret in `.env` looks configured, which is worse than
    absent, and the operator can still use it from the portal or paste it in by
    hand once it works.

    An existing non-empty value is never replaced; the reply says `already_set`
    and names the variable, without echoing the key.
    """
    payload = await request.json()
    provider = (payload.get("provider") or "").strip().lower()
    api_key = (payload.get("api_key") or "").strip()
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    if provider not in ENDPOINTS or not key_env_for(f"{provider}:x"):
        raise HTTPException(status_code=400,
                            detail=f"{provider} is unknown or takes no API key")
    env_var = key_env_for(f"{provider}:x")

    probe = await _probe_provider(provider, api_key, key_source="user")
    if not probe.get("ok"):
        return JSONResponse(status_code=400, content={
            "ok": False, "stored": False, "provider": provider, "env_var": env_var,
            "error": {"message": probe.get("error_detail") or "the provider rejected the key",
                      "type": probe.get("error_class") or "provider_unverified",
                      "code": 400},
        })

    outcome = env_mod.set_env_var(ENV_PATH, env_var, api_key, template=ENV_TEMPLATE)
    if outcome != "already_set":
        # The running process reads `os.environ`, not the file, so the key becomes
        # live now rather than after a restart. `already_set` is left alone — the
        # value already in force is the operator's.
        os.environ[env_var] = api_key
    return {"ok": True, "stored": outcome != "already_set", "status": outcome,
            "provider": provider, "env_var": env_var,
            "reply": probe.get("reply") or "key accepted"}


@app.post("/v1/providers/test", openapi_extra=_api_schema.PROVIDERS_TEST, tags=["catalog"])
async def test_provider_connectivity(request: Request) -> dict:
    """Test connectivity to a provider using its free model or cheapest deployment."""
    payload = await request.json()
    provider_id = (payload.get("provider") or "").strip().lower()
    custom_key = (payload.get("api_key") or "").strip()
    if not custom_key:
        # The dashboard keeps keys in the browser and forwards them as
        # `X-User-API-Keys` — the same header the chat path reads. This endpoint
        # read only the body, so a saved key ("Custom Key Active") was invisible to
        # Test: it exercised the *environment* key instead, and with none set sent
        # no credential at all, which a provider reports as "Missing
        # Authentication header".
        raw = request.headers.get("x-user-api-keys")
        if raw:
            try:
                custom_key = str((json.loads(raw) or {}).get(provider_id) or "").strip()
            except Exception:
                custom_key = ""

    if not provider_id:
        raise HTTPException(status_code=400, detail="Missing 'provider' in request payload")

    # Which credential the call will actually use. "Connectivity failed" that does
    # not say *which* key was tried sends the operator hunting; a missing
    # Authorization header means "none", not "a bad one".
    key_env = (ENDPOINTS.get(provider_id) or (None, None, None))[1]
    key_source = ("custom" if custom_key
                  else "env" if (key_env and os.environ.get(key_env))
                  else "none")

    store = _store()
    deps = store.deployments()
    store.close()

    # Find candidate deployments for this provider, preferring live, free/zero_price models first
    candidates = []
    for d in deps:
        if d.get("status") not in ("live", None, ""):
            continue
        prov = d.get("provider", "").split("/")[0].lower()
        if prov == provider_id or d.get("provider", "").lower().startswith(f"{provider_id}/"):
            candidates.append(d)

    if not candidates:
        # Fallback to any deployment for this provider if none are marked 'live'
        for d in deps:
            prov = d.get("provider", "").split("/")[0].lower()
            if prov == provider_id or d.get("provider", "").lower().startswith(f"{provider_id}/"):
                candidates.append(d)

    if not candidates:
        if provider_id not in ENDPOINTS:
            raise HTTPException(status_code=404, detail=f"No deployments found for provider '{provider_id}'")
        # Nothing is ingested for this provider, so there is no model to route a
        # completion to. The old fallback invented `{provider}:test` and called it
        # — a model id that can only ever 404, which is the "Connectivity failed …
        # Model: test / The model `test` does not exist" a user saw. Ask the
        # provider to list its own models instead: that checks exactly what this
        # endpoint promises (reachability, and that the key is accepted) and needs
        # no model id.
        return await _probe_provider(provider_id, custom_key, key_source)
    else:
        # Sort: free models first, then cheapest price_in
        candidates.sort(key=lambda x: (not (x.get("zero_price") or x.get("free_variant") or x.get("price_in") == 0),
                                       float(x.get("price_in") or 0)))
        candidate = candidates[0]

    # If provider is Google and we have an API key, discover live models dynamically
    if provider_id == "google" and custom_key:
        try:
            async with httpx.AsyncClient(timeout=4.0) as http_c:
                g_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={custom_key}"
                g_res = await http_c.get(g_url)
                if g_res.status_code == 200:
                    g_data = g_res.json()
                    live_model_names = {
                        m["name"].removeprefix("models/").lower()
                        for m in g_data.get("models", [])
                        if "generateContent" in (m.get("supportedGenerationMethods") or [])
                    }
                    # Filter candidates to those Google actually currently serves
                    supported = [c for c in candidates if (c.get("provider_model_id") or "").lower() in live_model_names]
                    if supported:
                        candidates = supported
        except Exception:
            pass

    # Build runner with custom key (if provided) or fallback to env
    user_keys = {provider_id: custom_key} if custom_key else {}
    runner = Runner(user_keys=user_keys, timeout=15.0, max_tokens=10)

    # Try a few arms. A shared `:free` model is routinely rate-limited, and a 429
    # *proves* connectivity — the request authenticated and the provider answered —
    # rather than disproving it. Stopping at the first arm made this button report
    # "Connectivity failed" for a provider that was working.
    test_messages = [{"role": "user", "content": "ping"}]
    res = None
    for cand in candidates[:4]:
        res = runner(cand["deploy_id"], test_messages)
        candidate = cand
        if res.ok or res.error_class not in ("429", "http_429"):
            break

    deploy_id = candidate["deploy_id"]

    # Architectural enhancement: If upstream returns 404 ("no longer available" / not found),
    # turn this into real runtime evidence and retire the deployment so the router never calls it again!
    if not res.ok and res.error_class == "http_404":
        try:
            store = _store()
            store.retire_deployment(
                deploy_id,
                reason=res.error_detail or "404 Not Found from upstream provider API",
                source="runtime",
            )
            store.close()
        except Exception:
            pass

    return {
        "ok": res.ok,
        "provider": provider_id,
        "deploy_id": deploy_id,
        "model": candidate.get("provider_model_id") or deploy_id,
        "is_free": bool(candidate.get("zero_price") or candidate.get("free_variant") or candidate.get("price_in") == 0),
        "latency_ms": round(res.latency_ms, 1) if res.latency_ms else None,
        "reply": res.text[:120] if res.text else None,
        "error_class": res.error_class if not res.ok else None,
        "error_detail": res.error_detail if not res.ok else None,
        "key_source": key_source,
    }




# ---------------------------------------------------------------------------
# dashboard — the operator's unit-economics view (PRD §23)
# ---------------------------------------------------------------------------


# Raw string: this is JavaScript, so every `\n`, `\s` and `\uXXXX` must reach the
# browser as an escape rather than being decoded by Python first. Doubling them by
# hand is what broke the markdown regexes; `r"""` is the actual fix.
def _synthesize_why_for_decision(store: Store, chosen: str, task: str) -> list[dict]:
    """Reconstruct explainability tags for decisions that stored only the funnel."""
    if not chosen:
        return []
    out = []
    try:
        cur = store.conn.cursor()
        row = cur.execute(
            "SELECT zero_price, free_variant, subscription, trial_credits, price_in FROM deployments WHERE deploy_id = ?",
            (chosen,)
        ).fetchone()
        if row:
            r = dict(row)
            if r.get("zero_price") or r.get("free_variant"):
                out.append({"key": "free", "label": "free tier", "detail": "$0.00"})
            elif r.get("trial_credits"):
                out.append({"key": "free", "label": "free (trial)", "detail": None})
                out.append({"key": "on-trial", "label": "on trial", "detail": None})
            elif r.get("subscription"):
                out.append({"key": "free", "label": "free (subscription)", "detail": None})
            elif r.get("price_in") == 0:
                out.append({"key": "free", "label": "free tier", "detail": "$0.00"})
    except Exception:
        pass

    out.append({"key": "cheapest", "label": "cheapest capable", "detail": None})
    out.append({"key": "objective", "label": "cost_per_success", "detail": None})
    return out


#: How many quota buckets the Overview page receives in its one call. The card
#: pages through them client-side; `/v1/stats` and the legacy dashboard keep the
#: small default so a caller that only wants counts does not download every row.
_OVERVIEW_QUOTA_ROWS = 500


def _stats_data(store=None, *, quota_limit: int = 10) -> dict:
    own = store is None
    store = store or _store()
    counts = store.counts()
    providers = store.providers_summary(limit=20)
    quota = quota_describe(store, limit=quota_limit)
    decisions = []
    for d in store.recent_decisions(limit=10):
        # `why` is lifted out of the stored blob so every consumer of this
        # endpoint (both dashboards, any client) gets the justification without
        # parsing JSON. If a historical row stored only the funnel, synthesize tags.
        try:
            r = json.loads(d.pop("reason") or "{}")
            why = r.get("why") if isinstance(r, dict) else []
            if not why:
                why = _synthesize_why_for_decision(store, d.get("chosen", ""), d.get("task", ""))
            d["why"] = why
            if isinstance(r, dict):
                if "origin" in r:
                    d["origin"] = r["origin"]
                if "compare_models" in r:
                    d["compare_models"] = r["compare_models"]
                if "preferred" in r:
                    d["preferred"] = r["preferred"]
        except (ValueError, TypeError):
            d["why"] = _synthesize_why_for_decision(store, d.get("chosen", ""), d.get("task", ""))
        decisions.append(d)
    savings = store.spend_savings()
    if own:
        store.close()
    _, tasks = Policy.load(_policy_path())
    return {"counts": counts, "providers": providers, "quota": quota,
            "decisions": decisions, "tasks": list(tasks), "savings": savings}


@app.get("/v1/pushed-models", tags=["catalog"])
def get_pushed_models_endpoint(task: str | None = None):
    """List deployments that the user has pinned/pushed to the top 3 shortlist."""
    store = _store()
    models = store.get_pushed_models(task)
    store.close()
    return {"pushed_models": models}


@app.post("/v1/pushed-models", openapi_extra=_api_schema.PUSHED_MODELS, tags=["catalog"])
async def set_pushed_models_endpoint(request: Request):
    """Push, unpush, or clear pinned models."""
    body = await request.json()
    action = body.get("action", "push")
    deploy_id = body.get("deploy_id")
    task = body.get("task")
    store = _store()
    if action == "push" and deploy_id:
        store.push_model(deploy_id, task)
    elif action == "unpush" and deploy_id:
        store.unpush_model(deploy_id)
    elif action == "clear":
        store.unpush_model(None)
    models = store.get_pushed_models(task)
    store.close()
    return {"ok": True, "pushed_models": models}


@app.get("/v1/plan", tags=["operator"])
def plan_endpoint(task: str, request: Request = None):
    """Routing plan for a task: the funnel and the chosen arms, no model call.

    Distinct from `POST /v1/route`, which also *executes*. A dashboard must be
    able to show what the router would pick without spending a call.
    """
    store = _store()
    policy, tasks = Policy.load(_policy_path())
    if task not in tasks:
        store.close()
        return _error(404, "unknown_task",
                      f"unknown task {task!r}; known: {', '.join(tasks)}")

    user_keys: dict[str, str] = {}
    pushed_models = None
    if request:
        raw_keys = request.headers.get("x-user-api-keys")
        if raw_keys:
            try:
                user_keys = json.loads(raw_keys)
            except Exception:
                pass
        raw_pushed = request.headers.get("x-pushed-models")
        if raw_pushed:
            try:
                pushed_models = json.loads(raw_pushed)
            except Exception:
                pushed_models = [s.strip() for s in raw_pushed.split(",") if s.strip()]

    dec = route(store, tasks[task], policy, mode="auto", user_keys=user_keys, pushed_models=pushed_models)
    active_pushed = store.get_pushed_models(task)
    store.close()
    return {
        "task": task,
        "funnel": dec.funnel,
        "diversity": dec.diversity,
        "strategy": dec.strategy,
        "pushed_models": active_pushed,
        "chosen": [
            {"deploy_id": c.deploy_id, "p_lb": round(c.p_lb, 4),
             "cost_per_success": c.cost_per_success, "free_kind": c.free_kind,
             "availability": round(c.availability, 4), "prior_key": c.prior_key,
             "headroom": c.headroom, "leaderboards": c.prior_tags,
             "pushed": getattr(c, "pushed", False)}
            for c in dec.chosen
        ],
    }


@app.post("/v1/search", openapi_extra=_api_schema.SEARCH, tags=["search"])
async def search_endpoint(request: Request) -> dict:
    """Web search, charged to a session and refused when the session cannot afford it.

    A session is **required** here, unlike the chat path. An unauthenticated,
    uncapped, paid search is the one thing this endpoint could do that nothing
    else in the proxy can, so it is the one place opting in is mandatory.

    The cap is checked against the *worst case* price of the provider that may
    answer (`auto` can reach Tavily), not the price it ends up costing — a budget
    that only counts money already spent cannot stop the spend it is meant to
    bound.
    """
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        return _error(400, "bad_request", "`query` is required")
    provider = body.get("provider") or "auto"
    limit = int(body.get("limit") or 5)

    session_id = _request_session(request, request.headers.get("x-mi-session"), body)
    if not session_id:
        return _error(400, "no_session",
                      "search is billed: pass an X-MI-Session (or X-MI-Session) header")

    policy, _tasks = Policy.load(_policy_path())
    store = _store()
    try:
        blocked = _session_block(policy, store, session_id,
                                 reserve_usd=search_mod.search_cost(provider))
        if blocked is not None:
            return blocked
        rep = search_mod.search(query, provider=provider, limit=limit,
                                force=bool(body.get("force")), store=store)
        store.add_session_usage(session_id, calls=0, searches=1,
                                search_cost_usd=rep.cost_usd,
                                cost_usd=rep.cost_usd,
                                tenant_id=_caller_tenant(request))
        session = _session_payload(policy, store, session_id)
        store.commit()
    finally:
        store.close()
    return {**rep.as_dict(), "session": session}


@app.get("/v1/session", tags=["sessions"])
def session_endpoint(request: Request):
    """Spend so far for one session, so a client can show a running total.

    The cap is enforced server-side (`_session_block`); this is how a client
    displays the number the server is enforcing, rather than counting locally and
    drifting away from it. Returns 400 when no session was named, because an
    absent session is not a zero-spend session — it is a request that opted out.
    """
    session_id = _request_session(request, request.headers.get("x-mi-session"))
    if not session_id:
        return _error(400, "no_session",
                      "pass an X-MI-Session (or X-MI-Session) header to read a session")

    policy, _tasks = Policy.load(_policy_path())
    store = _store()
    out = _session_payload(policy, store, session_id) or {}
    store.close()
    return out


@app.get("/v1/session/messages", tags=["sessions"])
def session_messages_endpoint(request: Request, limit: int | None = None):
    """The session's transcript, oldest first.

    Complements `sessions` (what it cost) with what was actually said, so a
    browser that cleared its cache can replay the conversation. 400 without a
    session for the same reason the spend endpoint is: an absent session is not
    an empty transcript.
    """
    session_id = _request_session(request, request.headers.get("x-mi-session"))
    if not session_id:
        return _error(400, "no_session",
                      "pass an X-MI-Session (or X-MI-Session) header to read a transcript")
    store = _store()
    try:
        messages = store.session_messages(session_id, limit)
    finally:
        store.close()
    return {"session_id": session_id, "messages": messages}


@app.delete("/v1/session/messages", tags=["sessions"])
def clear_session_messages_endpoint(request: Request):
    """Forget a session's transcript. The spend ledger is left alone.

    Content and cost are separate records on purpose: a caller has a right to
    erase what it said, but the tokens it was billed for are the operator's
    accounting, not the caller's to delete.
    """
    session_id = _request_session(request, request.headers.get("x-mi-session"))
    if not session_id:
        return _error(400, "no_session",
                      "pass an X-MI-Session (or X-MI-Session) header to clear a transcript")
    store = _store()
    try:
        cleared = store.clear_messages(session_id)
        store.commit()
    finally:
        store.close()
    return {"session_id": session_id, "cleared": cleared}


@app.get("/v1/stats", tags=["operator"])
def stats() -> dict:
    return _stats_data()


@app.get("/v1/economics/history", tags=["economics"])
def economics_history(deploy_id: str | None = None, as_of: str | None = None,
                      limit: int = 200) -> dict:
    """The price *belief* timeline (P6).

    MinInfer's timeline, not the provider's: `effective_from` is when we adopted
    a belief, which is what a past routing decision was actually made against.
    `as_of` returns the row in effect at that instant rather than the observation
    nearest to it — those differ whenever we learned something late.
    """
    store = _store()
    try:
        rows = store.price_history(deploy_id, as_of=as_of or None, limit=limit)
    finally:
        store.close()
    return {"as_of": as_of, "count": len(rows), "history": rows}


@app.get("/v1/economics/overview", tags=["economics"])
def economics_overview() -> dict:
    """Everything the Overview page's free/cost elements read, in one call (P7).

    A superset of `/v1/stats`: same counts, providers, quota, decisions, tasks and
    savings — now with the provenance this phase added — plus the reviews, the open
    anomalies and the pricing-state distribution. One endpoint rather than five, so
    the page cannot render a price beside a *different* moment's trust state.
    """
    store = _store()
    try:
        data = _stats_data(store, quota_limit=_OVERVIEW_QUOTA_ROWS)
        data["reviews"] = store.reviews()
        data["anomalies"] = store.anomalies(limit=20)
        data["pricing_states"] = store.pricing_state_counts()
    finally:
        store.close()
    data["generated_at"] = utcnow()
    return data


@app.get("/v1/economics/anomalies", tags=["economics"])
def economics_anomalies(status: str = "open", limit: int = 50) -> dict:
    """The anomaly log (P3). `status=` (empty) returns the whole history."""
    store = _store()
    try:
        rows = store.anomalies(status=status or None, limit=limit)
    finally:
        store.close()
    return {"status": status, "count": len(rows), "anomalies": rows}


@app.post("/v1/anomalies/decide", openapi_extra=_api_schema.ANOMALY_DECIDE,
          tags=["operator"])
async def decide_pricing_anomaly(request: Request) -> dict:
    """Acknowledge or resolve a pricing anomaly; the same two decisions `mi anomaly`
    makes, so the dashboard and the CLI cannot drift.

    A body rather than a path segment because an `anomaly_id` contains `|` and `:`
    (`openrouter:x:free|input|2026-10-04T10:00:00+00:00`), which are awkward to put
    in a URL and easy for a client to mangle.
    """
    payload = await request.json()
    anomaly_id = (payload.get("anomaly_id") or "").strip()
    if not anomaly_id:
        raise HTTPException(status_code=400, detail="anomaly_id is required")
    status = payload.get("status")
    if status not in ("acknowledged", "resolved"):
        raise HTTPException(status_code=400,
                            detail="status must be 'acknowledged' or 'resolved'")
    store = _store()
    try:
        if not store.decide_anomaly(anomaly_id, status=status,
                                    note=payload.get("note") or ""):
            raise HTTPException(status_code=404, detail="no such anomaly")
        return {"ok": True, "anomaly_id": anomaly_id, "status": status}
    finally:
        store.close()


@app.post("/v1/quota/seed", openapi_extra=_api_schema.SEED_QUOTAS, tags=["operator"])
async def seed_quota_buckets(request: Request) -> dict:
    """Import the declared free-tier limits from `config/quotas.yaml`.

    The step the Docker entrypoint runs after its bootstrap ingest; a native
    install has nobody to run it, so a fresh registry shows an empty "Quota
    headroom" card and the operator has to know the CLI command. Idempotent:
    `set_limit` rewrites the configured limit for a (deployment, window) pair, so
    seeding twice is not two buckets.
    """
    payload = await request.json()
    config = (payload.get("config") or "").strip() or quota_mod.DEFAULT_CONFIG
    dry_run = bool(payload.get("dry_run"))
    store = _store()
    try:
        report = quota_mod.seed(store, config=config, dry_run=dry_run)
    finally:
        store.close()
    return {"ok": True, "dry_run": dry_run, **report}


@app.get("/v1/economics/quota", tags=["economics"])
def economics_quota(limit: int = 40) -> dict:
    """Bucket headroom with its *source* and a probabilistic exhaustion (P5)."""
    store = _store()
    try:
        rows = quota_describe(store, limit=limit)
    finally:
        store.close()
    return {"count": len(rows), "buckets": rows}


@app.get("/v1/economics/providers", tags=["economics"])
def economics_providers(limit: int = 50) -> dict:
    """Per-provider rollup, with the provenance of each `min_in`."""
    store = _store()
    try:
        rows = store.providers_summary(limit=limit)
    finally:
        store.close()
    return {"count": len(rows), "providers": rows}


@app.get("/v1/economics/deployments/{deploy_id:path}", tags=["economics"])
def economics_deployment(deploy_id: str) -> dict:
    """One deployment's full economics: resolution, history, transitions, anomalies.

    `:path` because a deploy id contains a slash (`openrouter/novita:model:free`).
    """
    store = _store()
    try:
        data = store.deployment_economics(deploy_id)
    finally:
        store.close()
    if data is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deploy_id}")
    return data


@app.get("/v1/savings", tags=["sessions"])
def savings_endpoint(days: int | None = None, session: str | None = None) -> dict:
    """Spend vs. what the cheapest paid sibling would have charged.

    Admin surface: it is registry-wide telemetry, like `/v1/stats`. The
    session-scoped view is the `savings` block on `/v1/session`, which a tenant
    can read for its own session without seeing the operator's totals.

    `days` bounds the window; `session` narrows to one ledger. Both optional, and
    an unbounded query is all-time, which is the honest default for a local
    registry whose whole history is small.
    """
    store = _store()
    try:
        return store.spend_savings(days=days, session_id=session)
    finally:
        store.close()


@app.get("/v1/usage", tags=["sessions"])
def usage_endpoint(request: Request, days: int | None = None,
                   tenant: str | None = None) -> dict:
    """Per-tenant usage: model calls, spend, savings, searches and decisions.

    A tenant key sees **its own** numbers and cannot ask for another's: `tenant`
    is honoured only for an admin token (or when auth is off, where there are no
    tenants to leak between). That is the one rule this endpoint has to get
    right, so it is enforced here rather than left to the caller.
    """
    caller = getattr(getattr(request, "state", None), "tenant", None)
    tenant_id = caller if caller and caller != "admin" else tenant
    store = _store()
    try:
        return store.usage_report(tenant_id=tenant_id, days=days)
    finally:
        store.close()


def dashboard(task: str | None = None) -> str:
    """Gather what the page needs, then render it.

    The templating lives in `mi/web/legacy.py`; what stays here is the part that
    touches the store and the router.
    """
    data = _stats_data()
    policy, tasks = Policy.load(_policy_path())
    store = _store()
    chosen = task if task in tasks else (next(iter(tasks), None))
    routed = None
    if chosen:
        dec = route(store, tasks[chosen], policy, mode="auto")
        routed = {"funnel": dec.funnel, "diversity": dec.diversity,
                  "strategy": dec.strategy, "chosen": dec.chosen}
    store.close()
    return render_dashboard(
        counts=data["counts"], providers=data["providers"], quota=data["quota"],
        decisions=data["decisions"], tasks=list(tasks), task=chosen, routed=routed)


@app.get("/", response_class=HTMLResponse, tags=["dashboard"])
def root(task: str | None = None) -> str:
    """The React dashboard when built, else the server-rendered page."""
    index = WEB_DIST / "index.html"
    if index.exists():
        return index.read_text(encoding="utf-8")
    return dashboard(task)


@app.get("/legacy", response_class=HTMLResponse, tags=["dashboard"])
def legacy_dashboard(task: str | None = None) -> str:
    """The original server-rendered dashboard, always available."""
    return dashboard(task)


def _extract_origin(request: Request | None) -> dict:
    if request is None:
        return {"source": "internal", "is_local": True}
    headers = request.headers
    fwd = headers.get("x-forwarded-for")
    client_ip = (fwd.split(",")[0].strip() if fwd else None) or headers.get("x-real-ip") or (request.client.host if request.client else "127.0.0.1")
    client_tz = headers.get("x-client-timezone") or headers.get("x-timezone") or ""
    country = (headers.get("cf-ipcountry") or headers.get("x-country-code") or headers.get("cloudfront-viewer-country") or "").upper()
    user_agent = headers.get("user-agent", "")
    origin_header = headers.get("origin") or headers.get("referer") or ""
    client_header = headers.get("x-mi-client") or ""

    if client_header == "playground" or "5173" in origin_header or "playground" in origin_header:
        source = "Playground"
    elif "curl" in user_agent.lower():
        source = "cURL"
    elif "python" in user_agent.lower() or "openai" in user_agent.lower():
        source = "Python SDK"
    else:
        source = "API"

    is_local = client_ip in ("127.0.0.1", "::1", "localhost") or client_ip.startswith(("10.", "172.16.", "192.168."))

    return {
        "ip": client_ip,
        "tz": client_tz,
        "country": country,
        "source": source,
        "is_local": is_local,
    }


@app.post("/v1/chat/completions", openapi_extra=_api_schema.CHAT, tags=["inference"])
async def chat_completions(request: Request):
    return await _handle(await request.json(), force_route=False,
                         session_header=request.headers.get("x-mi-session"),
                         request=request)


@app.post("/v1/route", openapi_extra=_api_schema.ROUTE, tags=["inference"])
async def route_endpoint(request: Request):
    return await _handle(await request.json(), force_route=True,
                         session_header=request.headers.get("x-mi-session"),
                         request=request)


@app.post("/v1/approve", openapi_extra=_api_schema.APPROVE, tags=["inference"])
async def approve(request: Request) -> dict:
    """Record a human's choice between the offered answers.

    The approval is what ends a free arm's trial: it lands in `observations` as a
    weak (subjective) signal, so the arm's p(success) moves and it either earns
    its place or is demoted by the quality floor.
    """
    payload = await request.json()
    task = payload.get("task") or "unrouted"
    chosen = payload.get("chosen")
    rejected = [d for d in (payload.get("rejected") or []) if d]
    decision_id = payload.get("decision_id")
    store = _store()
    if chosen:
        store.observe(chosen, task, ok=True, ts=utcnow(),
                      signal_kind="subjective", signal_value=1.0,
                      tenant_id=_caller_tenant(request))
    for did in rejected:
        store.observe(did, task, ok=False, ts=utcnow(),
                      signal_kind="subjective", signal_value=0.0,
                      tenant_id=_caller_tenant(request))
    if chosen:
        store.update_decision_preference(decision_id, chosen, task=task)
    store.commit()
    store.close()
    return {"ok": True, "chosen": chosen, "rejected": rejected, "decision_id": decision_id}


@app.post("/v1/route-verdict", openapi_extra=_api_schema.ROUTE_VERDICT, tags=["inference"])
async def route_verdict(request: Request) -> dict:
    """Record a human judgment specifically on the model routing choice.

    Distinct from response quality (`/v1/approve`), this captures whether the
    router selected an appropriate deployment for the classified task. Stored as
    `signal_kind='routing_approval'` with ok=1 (thumbs up) or ok=0 (thumbs down),
    which adjusts future candidate scoring via Bayesian sentiment updates.
    """
    payload = await request.json()
    task = payload.get("task") or "unrouted"
    deploy_id = payload.get("deploy_id")
    approved = bool(payload.get("approved"))
    reason = payload.get("reason")
    if not deploy_id:
        raise HTTPException(status_code=400, detail="deploy_id is required")

    store = _store()
    meta = {"source": "ui_telemetry"}
    if reason:
        meta["reason"] = reason
    store.observe(
        deploy_id,
        task,
        ok=approved,
        ts=utcnow(),
        signal_kind="routing_approval",
        signal_value=1.0 if approved else 0.0,
        meta=meta,
        tenant_id=_caller_tenant(request),
    )
    store.commit()
    store.close()
    return {"ok": True, "deploy_id": deploy_id, "task": task, "approved": approved}


async def _run_shadow_trial(
    messages: list[dict],
    prompt: str,
    primary_deploy_id: str,
    primary_text: str | None,
    task_name: str,
    *,
    user_keys: dict[str, str] | None = None,
    local_endpoints: dict[str, str] | None = None,
    extra_body: dict | None = None,
    tenant_id: str | None = None,
) -> None:
    """Duplicate request to an untried free candidate in the background.

    Evaluates the trial response via judge and records observations so untried
    arms gain evidence without blocking or charging the primary caller.
    """
    store = _store()
    try:
        policy, _ = Policy.load(_policy_path())
        stats = store.stats(task_name)
        deps = store.deployments()
        untried: list[dict] = []
        for d in deps:
            did = d["deploy_id"]
            if did == primary_deploy_id:
                continue
            if d.get("status") in ("deprecated", "gated", "hibernated"):
                continue
            is_free = bool(d.get("zero_price") or d.get("free_variant") or d.get("trial_credits"))
            if not is_free:
                continue
            if not _keys_available(did, user_keys, local_endpoints):
                continue
            n_obs = int(stats.get(did, {}).get("n", 0) or 0)
            if n_obs < policy.free_trial_obs:
                untried.append({"deploy_id": did, "n_obs": n_obs, "p_prior": float(d.get("price_in") or 0)})

        if not untried:
            return

        import random
        untried.sort(key=lambda x: (x["n_obs"], random.random()))
        # The judge's label space is A..H, so the batch is capped to match. Each
        # candidate is a different provider, so the *calls* cannot be batched —
        # only the judging can, and that is one call for the whole batch.
        batch = [u["deploy_id"] for u in untried[:max(1, min(8, int(policy.judge_batch_size)))]]

        runner = Runner(extra_body=extra_body, user_keys=user_keys, local_endpoints=local_endpoints)

        trial_results: list[tuple[str, object]] = []
        for did in batch:
            trial_res = await asyncio.to_thread(
                runner, did, messages, max_tokens=1000, temperature=0.0, timeout=30.0)
            store.record_usage(did)
            _retire_if_uncallable(store, did, error_class=trial_res.error_class,
                                  error_detail=trial_res.error_detail)
            trial_results.append((did, trial_res))

        # One judging call for the whole batch, labelled A, B, C… and mapped back.
        answered = [(did, r.text) for did, r in trial_results if r.ok and r.text]
        verdicts: dict[str, tuple[bool, str]] = {}
        best_did: str | None = None
        if answered:
            from .judge import evaluate_batch
            by_label = {chr(ord("A") + i): did for i, (did, _) in enumerate(answered)}
            labelled = [(label, answered[i][1]) for i, label in enumerate(by_label)]
            got, best_label = await asyncio.to_thread(
                evaluate_batch, prompt, labelled,
                reference_text=primary_text, runner=runner, judge_model=primary_deploy_id)
            verdicts = {by_label[lbl]: v for lbl, v in got.items() if lbl in by_label}
            if best_label and best_label in by_label:
                best_did = by_label[best_label]

        for did, trial_res in trial_results:
            if did in verdicts:
                judge_ok, judge_reason = verdicts[did]
                store.observe(
                    did,
                    task_name,
                    ok=judge_ok,
                    ts=utcnow(),
                    error_class=None if judge_ok else "judge_rejected",
                    latency_ms=trial_res.latency_ms,
                    tokens_in=trial_res.tokens_in,
                    tokens_out=trial_res.tokens_out,
                    cost_usd=0.0,
                    signal_kind="judge_trial",
                    signal_value=1.0 if judge_ok else 0.0,
                    meta={"shadow": True, "judge_reason": judge_reason,
                          "primary": primary_deploy_id, "batch": len(answered),
                          "best": did == best_did},
                    tenant_id=tenant_id,
                )
            else:
                store.observe(
                    did,
                    task_name,
                    ok=False,
                    ts=utcnow(),
                    error_class=trial_res.error_class or "empty_content",
                    latency_ms=trial_res.latency_ms,
                    cost_usd=0.0,
                    signal_kind="judge_trial",
                    signal_value=0.0,
                    meta={"shadow": True, "error_detail": trial_res.error_detail},
                    tenant_id=tenant_id,
                )
        store.commit()
    except Exception as exc:
        logging.getLogger("mininfer.proxy").warning(f"Shadow trial error: {exc}")
    finally:
        store.close()


def _launch_shadow_trial(
    messages: list[dict],
    prompt: str,
    primary_deploy_id: str,
    primary_text: str | None,
    task_name: str,
    *,
    user_keys: dict[str, str] | None = None,
    local_endpoints: dict[str, str] | None = None,
    extra_body: dict | None = None,
    tenant_id: str | None = None,
) -> None:
    coro = _run_shadow_trial(
        messages, prompt, primary_deploy_id, primary_text, task_name,
        user_keys=user_keys, local_endpoints=local_endpoints,
        extra_body=extra_body, tenant_id=tenant_id,
    )
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        threading.Thread(target=lambda: asyncio.run(coro), daemon=True).start()


@app.post("/v1/models/trial", openapi_extra=_api_schema.TRIAL, tags=["catalog"])
async def trial_model_endpoint(request: Request) -> dict:
    """Run an on-demand trial call against a specific deployment and judge it."""
    payload = await request.json()
    deploy_id = payload.get("deploy_id")
    task_name = payload.get("task") or "general_chat"
    prompt = (payload.get("prompt") or "Explain the purpose of dynamic model routing in 2 concise sentences.").strip()
    if not deploy_id:
        raise HTTPException(status_code=400, detail="deploy_id is required")

    user_keys: dict[str, str] = {}
    local_endpoints: dict[str, str] = {}
    # `x-user-api-keys` is the name the dashboard sends and every other path
    # reads; this one alone asked for `x-user-keys`, so the Models tab's Test
    # button silently fell back to the environment key — the same failure as #21,
    # one endpoint over.
    raw_keys = (request.headers.get("x-user-api-keys")
                or request.headers.get("x-user-keys"))
    if raw_keys:
        try:
            user_keys = json.loads(raw_keys)
        except Exception:
            pass

    runner = Runner(user_keys=user_keys, local_endpoints=local_endpoints)
    messages = [{"role": "user", "content": prompt}]
    res = await asyncio.to_thread(runner, deploy_id, messages, max_tokens=300, temperature=0.0, timeout=25.0)

    store = _store()
    try:
        store.record_usage(deploy_id)
        _retire_if_uncallable(store, deploy_id, error_class=res.error_class, error_detail=res.error_detail)
        from .judge import evaluate_trial_response
        judge_ok = False
        judge_reason = res.error_class or "call_failed"
        if res.ok and res.text:
            judge_ok, judge_reason = await asyncio.to_thread(
                evaluate_trial_response, prompt, res.text, runner=runner, judge_model=deploy_id
            )

        store.observe(
            deploy_id,
            task_name,
            ok=judge_ok,
            ts=utcnow(),
            error_class=None if judge_ok else (res.error_class or "judge_rejected"),
            latency_ms=res.latency_ms,
            tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            cost_usd=0.0,
            signal_kind="judge_trial",
            signal_value=1.0 if judge_ok else 0.0,
            meta={"manual_trial": True, "judge_reason": judge_reason},
            tenant_id=_caller_tenant(request),
        )
        store.commit()
        return {
            "ok": judge_ok,
            "deploy_id": deploy_id,
            "task": task_name,
            "latency_ms": res.latency_ms,
            "judge_reason": judge_reason,
            "reply": res.text[:500] if res.text else None,
            "error_class": res.error_class,
            "error_detail": res.error_detail,
        }
    finally:
        store.close()


@app.get("/v1/reviews", tags=["operator"])
def reviews() -> dict:
    """Deployments hibernated for human review.

    A free arm that started charging lands here instead of silently spending:
    the router drops anything not `live`, and an operator decides whether the
    now-paid model is still worth using.
    """
    store = _store()
    try:
        return {"reviews": store.reviews()}
    finally:
        store.close()


@app.post("/v1/reviews/decide", openapi_extra=_api_schema.REVIEWS_DECIDE, tags=["operator"])
async def decide_review(request: Request) -> dict:
    """Resolve a hibernation: `approve` returns it to `live`, otherwise retire it."""
    payload = await request.json()
    deploy_id = payload.get("deploy_id")
    if not deploy_id:
        raise HTTPException(status_code=400, detail="deploy_id is required")
    approve = bool(payload.get("approve"))
    store = _store()
    try:
        if not store.decide_review(deploy_id, approve=approve, note=payload.get("note") or ""):
            raise HTTPException(status_code=404, detail="no hibernated review for that deployment")
        return {"ok": True, "deploy_id": deploy_id,
                "status": "live" if approve else "deprecated"}
    finally:
        store.close()


_COMPACT_INSTRUCTIONS = (
    "You compress a conversation so another model can continue it without the "
    "full transcript.\n\n"
    "Keep, as terse bullet points and nothing else:\n"
    "- the user's goal, and any change to it\n"
    "- decisions already made, and why\n"
    "- hard constraints: format, length, tone, stack, exact identifiers, numbers\n"
    "- facts the user would expect remembered (names, paths, symbols)\n"
    "- what is still open\n\n"
    "Drop greetings, filler, and anything superseded. No preamble, no closing. "
    "If a previous summary is provided, merge the new information into it "
    "instead of repeating it."
)


@app.post("/v1/compact", openapi_extra=_api_schema.COMPACT, tags=["inference"])
async def compact(request: Request) -> dict:
    """Summarise older turns so later requests resend a brief, not the transcript.

    The saving is in *input* tokens, which is where the latency is; the caller
    caches the returned brief, so the extra call is paid once per fold rather
    than on every turn. Deliberately routed as a throwaway: `store=None` means
    the summarisation itself is not recorded as an observation or a decision.
    """
    payload = await request.json()
    messages = payload.get("messages") or []
    previous = str(payload.get("summary") or "").strip()
    if not isinstance(messages, list) or not messages:
        return {"summary": previous, "model": None, "compacted": 0}

    user_keys: dict[str, str] = {}
    local_endpoints: dict[str, str] = {}
    raw_keys = request.headers.get("x-user-api-keys")
    if raw_keys:
        try:
            user_keys = json.loads(raw_keys)
        except Exception:
            pass
    raw_local = request.headers.get("x-local-endpoints")
    if raw_local:
        try:
            local_endpoints = json.loads(raw_local)
        except Exception:
            pass

    transcript = "\n\n".join(
        f"{m.get('role', 'user')}: {str(m.get('content') or '')[:4000]}"
        for m in messages if isinstance(m, dict)
    )
    ask = _COMPACT_INSTRUCTIONS
    if previous:
        ask += f"\n\nPrevious summary:\n{previous}"
    ask += f"\n\nConversation:\n{transcript}\n\nSummary:"

    store = _store()
    try:
        policy, tasks = Policy.load(_policy_path())
        profile = tasks.get(DEFAULT_TASK) or next(iter(tasks.values()))
        policy.top_k = 2
        decision = route(store, profile, policy, user_keys=user_keys)
        ids = [c.deploy_id for c in decision.chosen
               if _keys_available(c.deploy_id, user_keys, local_endpoints)]
        if not ids:
            return {"summary": previous, "model": None, "compacted": 0}
        runner = Runner(user_keys=user_keys, local_endpoints=local_endpoints)
        selected, res, _attempts = await asyncio.to_thread(
            try_fallbacks, ids, [{"role": "user", "content": ask}], runner,
            task_name="compaction", store=None, max_tokens=500, temperature=0.2)
        summary = (res.text or "").strip()
        return {"summary": summary or previous, "model": selected,
                "compacted": len(messages)}
    finally:
        store.close()


def _extra(payload: dict) -> dict:
    return {k: v for k, v in payload.items()
            if k not in ("model", "task", "policy", "x-mi-task", "x-mi-task", "stream", "mi_options", "mi_options")}


async def _handle(payload: dict, *, force_route: bool,
                  session_header: str | None = None,
                  request: Request | None = None):
    origin_info = _extract_origin(request)
    store = _store()
    policy, tasks = Policy.load(_policy_path())

    user_keys: dict[str, str] = {}
    local_endpoints: dict[str, str] = {}
    if request:
        raw_keys = request.headers.get("x-user-api-keys")
        if raw_keys:
            try:
                user_keys = json.loads(raw_keys)
            except Exception:
                pass
        raw_local = request.headers.get("x-local-endpoints")
        if raw_local:
            try:
                local_endpoints = json.loads(raw_local)
            except Exception:
                pass

    model = payload.get("model", "auto")
    for prefix in ("mi/", "mininfer/", "mininfer/"):
        if model.startswith(prefix):
            model = model[len(prefix):]
            break
    task_field = payload.get("task") or payload.get("x-mi-task")
    policy_name = payload.get("policy", "free_first")
    if policy_name:
        policy.name = policy_name
    stream = bool(payload.get("stream"))

    # The tenant every write from this request is attributed to. `admin` and
    # anonymous local calls get None: the operator is not a tenant, and an
    # unattributed call must stay distinguishable from a tenant's.
    caller_tenant = _caller_tenant(request)

    def _record(*, task_name: str, mode: str, chosen: str,
                candidates: list[str], reason: dict) -> int:
        """Record the request, whatever the outcome.

        The decision log is the request log. A refusal, a session cap and a
        straight `provider/model` call all have to explain why a model was or
        was not chosen, and recording only the successful `auto` path made this
        a highlight reel: every row a mature registry held said `mode=auto`.
        """
        dec_id = store.record_decision(task=task_name, policy=policy.name, mode=mode,
                                       chosen=chosen, candidates=candidates,
                                       reason=json.dumps(reason, default=str),
                                       tenant_id=caller_tenant)
        store.commit()
        return dec_id

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        _record(task_name="unrouted", mode="rejected", chosen="", candidates=[],
                reason={"error": "bad_request", "origin": origin_info,
                        "detail": "`messages` must be a non-empty list"})
        store.close()
        return _error(400, "bad_request", "`messages` must be a non-empty list")

    # Spend is capped per session before anything is called. Checked here rather
    # than per arm: a refused session must not pay for a fallback chain.
    session_id = _request_session(request, session_header, payload)
    blocked = _session_block(policy, store, session_id, messages=messages,
                             max_tokens=payload.get("max_tokens") or 1024)
    if blocked is not None:
        _record(task_name="unrouted", mode="blocked", chosen="", candidates=[],
                reason={"error": "session_limit", "session": session_id,
                        "origin": origin_info})
        store.close()
        return blocked

    TASK_ALIASES = {
        "coding": "code_edit",
        "code": "code_edit",
        "reasoning": "hard_reasoning",
        "math": "hard_reasoning",
        "chat": "general_chat",
        "vision": "shelf_image_audit",
        "sql": "sql_generation",
        "summary": "summarise",
        "summarize": "summarise",
        "tools": "agent_tools",
    }
    if model in TASK_ALIASES:
        model = TASK_ALIASES[model]
    if task_field and task_field in TASK_ALIASES:
        task_field = TASK_ALIASES[task_field]

    # If model is not a known task or auto keyword, and contains no ':', treat it as auto routing
    # to avoid raw invalid deploy IDs crashing with a `bad_deploy_id` endpoint.
    if ":" not in model and model not in tasks and model not in ("auto", "route"):
        model = "auto"

    routed = force_route or model in ("auto", "route") or model in tasks
    intent = None
    merged_from: list[str] | None = None
    if routed:
        explicit = task_field or (model if model in tasks else None)
        # Always classify, even when the caller named a task. The disagreement
        # between what was asked for and what the cues would have said is the
        # only free label this system gets — it is recorded on the decision.
        intent = intent_mod.classify(
            intent_mod.last_user_text(messages), tasks,
            default=DEFAULT_TASK, llm=None if explicit else _intent_llm())
        task_name = explicit or intent.task
        if task_name not in tasks:
            known = ", ".join(["auto"] + list(tasks))
            _record(task_name=task_name, mode="rejected", chosen="", candidates=[],
                    reason={"error": "unknown_task", "requested": task_name,
                            "known": list(tasks), "origin": origin_info})
            store.close()
            return _error(400, "unknown_task",
                          f"unknown task {task_name!r}; known: {known}")

        profile = tasks[task_name]
        if explicit is None and 0 < intent.confidence < INTENT_MERGE_BELOW:
            tied = [n for n, _ in intent.top(3) if n in tasks]
            if len(tied) > 1:
                profile = intent_mod.merge_profiles(
                    "+".join(tied), [tasks[n] for n in tied])
                merged_from = tied

        dec = route(store, profile, policy, mode="auto", user_keys=user_keys)
        if not dec.chosen and merged_from:
            # The join is strictly stricter, so it can exclude everything. When it
            # does, the best single guess beats no answer.
            profile = tasks[task_name]
            merged_from = None
            dec = route(store, profile, policy, mode="auto", user_keys=user_keys)
        ranked = [c.deploy_id for c in dec.chosen]
        deploy_ids = [d for d in ranked if _keys_available(d, user_keys, local_endpoints)]
        skipped = [d for d in ranked if d not in deploy_ids]
        reason = {
            "funnel": dec.funnel,
            "why": dec.why,
            "diversity": dec.diversity,
            "selected": [
                {"deploy_id": c.deploy_id, "p_lb": round(c.p_lb, 4),
                 "cost_per_success": c.cost_per_success, "free_kind": c.free_kind,
                 "availability": round(c.availability, 4),
                 "leaderboards": c.prior_tags}
                for c in dec.chosen
            ],
            "skipped_no_key": skipped,
            "origin": origin_info,
        }
        if intent is not None:
            meta = intent.as_dict()
            if explicit is not None:
                # A label: the caller overrode the classifier.
                meta["explicit"] = explicit
                meta["agreed"] = intent.task == explicit
            reason["intent"] = meta
        reason["profile"] = profile.name
        if merged_from:
            reason["merged_from"] = merged_from
        top_c = next((c for c in dec.chosen if c.deploy_id in deploy_ids), None)
        # A trial arm has no track record — which is exactly when a second
        # opinion is worth the extra call.
        reason["needs_approval"] = bool(
            top_c and top_c.free_kind and top_c.n_obs < policy.free_trial_obs
            and len(deploy_ids) > 1)
        alternatives = deploy_ids[1:]
        ranked_cands = dec.ranked
        if not deploy_ids:
            _record(task_name=task_name, mode="auto",
                    chosen="", candidates=ranked, reason=reason)
            store.close()
            if skipped:
                envs = sorted({e for e in (key_env_for(d) for d in skipped) if e})
                hint = f"; set one of: {', '.join(envs)}" if envs else ""
                return _error(503, "no_api_key",
                              f"no candidate has a configured API key{hint}")
            return _error(503, "no_candidates", "no eligible deployment for this task")
    else:
        task_name = task_field or "unrouted"
        deploy_ids = [model]
        reason = {
            "funnel": {"direct": True},
            "why": [{"key": "direct", "label": "direct deploy", "detail": model}],
            "origin": origin_info,
        }
        alternatives = []
        ranked_cands = []

    # Best-of-N with human approval: run several candidates independently and
    # return them all, rather than a fallback chain that stops at the first
    # success. Opt-in via `mi_options` / `mi_options` so OpenAI clients are unaffected.
    n_options = int(payload.get("mi_options") or 0)

    if n_options > 1:
        # Ask for more candidates than options: one may 429 or time out, and a
        # single answer is not a choice. The pool is the whole ranked list, not
        # the diversified top-k — that list can hold the same model twice behind
        # different gateways and collapse to a single option. Failures are still
        # recorded, so a 429 here demotes that arm for the next call.
        want = max(n_options * 3, n_options)
        if ranked_cands:
            pool = [c.deploy_id for c in ranked_cands if _keys_available(c.deploy_id, user_keys, local_endpoints)]
            compare_ranked = rank_for_compare(ranked_cands)
            opts = _distinct_options(compare_ranked, pool, want)
        else:
            opts = deploy_ids[:want]
    else:
        opts = []

    if len(opts) > 1:
        compare_models = opts[:n_options]
        reason["compare_models"] = compare_models
        reason["why"] = [
            {"key": "compare", "label": f"compare mode ({len(compare_models)} models)", "detail": None},
            {"key": "arm0", "label": f"Arm 0: {compare_models[0]}", "detail": "best score"},
            {"key": "arm1", "label": f"Arm 1: {compare_models[1]}", "detail": "bandit exploration (UCB1)"},
        ]
        dec_id = _record(task_name=task_name, mode="compare",
                         chosen=" vs ".join(compare_models),
                         candidates=compare_models, reason=reason)
        info = {c.deploy_id: {"deploy_id": c.deploy_id, "p_lb": round(c.p_lb, 4),
                              "cost_per_success": c.cost_per_success,
                              "free_kind": c.free_kind,
                              "vendor": c.vendor,
                              "availability": round(c.availability, 4),
                              "leaderboards": c.prior_tags}
                for c in ranked_cands}
        # A comparison has no single answer to store yet, so only the question is
        # recorded; the human's pick at `/v1/approve` ends the turn. Recording a
        # guess here would put an unchosen answer in the transcript.
        if session_id:
            store.record_messages(session_id, _turn(payload.get("messages"), None),
                                  task=task_name)
            store.commit()
        # A comparison that streams runs its arms *concurrently* over one
        # connection. Serial execution meant `mi_options: 2` cost the sum of
        # both calls even when one was fast — and a comparison is exactly the
        # case where the caller is waiting on the slower of two.
        if stream:
            store.close()
            return await _multiplex_stream(
                opts, payload, task_name=task_name, policy=policy.name,
                reason=reason, info=info, extra=_extra(payload),
                session_id=session_id, tenant_id=caller_tenant,
                user_keys=user_keys, local_endpoints=local_endpoints,
                decision_id=dec_id, max_arms=n_options)

        runner = Runner(extra_body=_extra(payload), user_keys=user_keys, local_endpoints=local_endpoints)
        results: list[tuple[str, CallResult]] = []
        last = CallResult("", error_class="no_candidates")
        for did in opts:
            if len(results) >= n_options:
                break
            sel, res, _ = await asyncio.to_thread(
                try_fallbacks, [did], messages, runner,
                task_name=task_name, store=store, session_id=session_id,
                tenant_id=caller_tenant)
            last = res
            if sel:
                results.append((sel, res))
        store.close()
        if not results:
            return _all_failed([Attempt(d, False, last.error_class, last.latency_ms,
                                        0, 0) for d in opts], last, decision_id=dec_id)
        return _options_response(results, task=task_name, policy=policy.name,
                                 reason=reason, info=info, decision_id=dec_id)

    dec_id = _record(task_name=task_name,
                     mode="auto" if routed else "direct",
                     chosen=deploy_ids[0] if deploy_ids else "",
                     candidates=ranked if routed else [model],
                     reason=reason)

    if stream:
        return await _stream(deploy_ids, payload, task_name=task_name, store=store,
                             policy=policy.name, reason=reason, alternatives=alternatives,
                             intent=intent, session_id=session_id, tenant_id=caller_tenant,
                             user_keys=user_keys, local_endpoints=local_endpoints,
                             decision_id=dec_id)

    runner = Runner(extra_body=_extra(payload), user_keys=user_keys, local_endpoints=local_endpoints)
    selected, res, attempts = await asyncio.to_thread(
        try_fallbacks,
        deploy_ids, messages, runner, task_name=task_name, store=store,
        session_id=session_id, tenant_id=caller_tenant)
    session = _session_payload(policy, store, session_id)
    if selected is not None and session_id:
        # The answer, stored with the question, in one transaction that is also
        # the turn's accounting commit — so a transcript never records an answer
        # the caller was not charged for, or vice versa.
        store.record_messages(session_id, _turn(messages, res.text),
                              task=task_name, deploy_id=selected)
        store.commit()
    store.close()
    if selected is not None:
        last_prompt = intent_mod.last_user_text(messages)
        _launch_shadow_trial(
            messages, last_prompt, selected, res.text, task_name,
            user_keys=user_keys, local_endpoints=local_endpoints,
            extra_body=_extra(payload), tenant_id=caller_tenant,
        )
        return _chat_response(selected, res, task=task_name, policy=policy.name,
                              reason=reason, alternatives=alternatives,
                              session=session, decision_id=dec_id)
    return _all_failed(attempts, res, decision_id=dec_id)


# --------------------------------------------------------------------------- #
# streaming
# --------------------------------------------------------------------------- #


def _sse_frame(obj: dict) -> str:
    """One SSE frame. The `mininfer` envelope is additive: an OpenAI client that
    ignores it still finds `choices[].delta` exactly where it expects."""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _sse_deltas(chunk: bytes) -> str:
    """Concatenated `choices[].delta.content` from an SSE chunk.

    Parsed rather than relayed because a multiplexed stream has to interleave two
    arms: the client cannot tell them apart from raw bytes, so the proxy has to
    understand the frames it is demultiplexing.
    """
    out = []
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        body = line[5:].strip()
        if not body or body == b"[DONE]":
            continue
        try:
            j = json.loads(body)
        except ValueError:
            continue
        if not isinstance(j, dict):
            continue
        for choice in j.get("choices") or []:
            text = ((choice.get("delta") or {}).get("content")
                    or (choice.get("message") or {}).get("content"))
            if text:
                out.append(text)
    return "".join(out)


def _sse_error(chunk: bytes) -> str | None:
    """The error class an SSE chunk carries, if any.

    A gateway can report an upstream failure as a **200 with `error` in the
    body**, including mid-stream as a frame with an empty `choices` list
    (OpenRouter's "Upstream error from Nvidia: Service temporarily overloaded").
    Unread, that frame looks like a model that answered nothing: the arm was
    recorded as a success and the comparison rendered a blank pane instead of
    the provider's own error.
    """
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        body = line[5:].strip()
        if not body or body == b"[DONE]":
            continue
        try:
            j = json.loads(body)
        except ValueError:
            continue
        if isinstance(j, dict) and j.get("error"):
            return classify_error_payload(j["error"])
    return None


def _sse_has_tool_calls(chunk: bytes) -> bool:
    """A streamed tool-call turn legitimately has empty text.

    `_sse_deltas` only reads `delta.content`, so without this a tool-call stream
    would look like an empty answer and be failed.
    """
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        body = line[5:].strip()
        if not body or body == b"[DONE]":
            continue
        try:
            j = json.loads(body)
        except ValueError:
            continue
        for choice in (j.get("choices") or []) if isinstance(j, dict) else []:
            if (choice.get("delta") or {}).get("tool_calls"):
                return True
    return False


def _strip_done(chunk: bytes) -> bytes:
    """A chunk with any `data: [DONE]` line removed.

    The single-arm relay emits one terminator of its own, after any
    continuation; the upstream's would end the stream early. Done per line, not
    per chunk, so a chunk that also carries content is not discarded.
    """
    if b"[DONE]" not in chunk:
        return chunk
    kept = [ln for ln in chunk.split(b"\n") if ln.strip() != b"data: [DONE]"]
    return b"\n".join(kept)


def _sse_finish_reason(chunk: bytes) -> str | None:
    """`choices[].finish_reason` from an SSE chunk, when it is set."""
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        body = line[5:].strip()
        if not body or body == b"[DONE]":
            continue
        try:
            j = json.loads(body)
        except ValueError:
            continue
        for choice in (j.get("choices") or []) if isinstance(j, dict) else []:
            fr = choice.get("finish_reason")
            if fr:
                return fr
    return None


def _merge_usage(a: dict | None, b: dict | None) -> dict | None:
    """Sum usage across an auto-continued answer, so billing sees one call."""
    if not a:
        return b
    if not b:
        return a
    out = dict(a)
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if b.get(k) is not None:
            out[k] = (out.get(k) or 0) + b[k]
    if b.get("cost") is not None:
        out["cost"] = (out.get("cost") or 0) + b["cost"]
    return out


# One continuation, never a loop: a model that stops early twice is a property of
# the model, and paying forever for it is not a fix.
_MAX_CONTINUATIONS = 1


def _looks_truncated(text: str, finish_reason: str | None) -> bool:
    """Whether an answer stopped before it was finished.

    `finish_reason == "length"` is the provider telling us. The harder case is a
    model that emits EOS mid-answer and reports `"stop"` — `qwen/qwen3.8-27b` on
    Groq does this, and a cut lands on a markdown boundary: a heading with
    nothing under it, a line that ended in `:`, or an unclosed `**` / backtick.
    Deliberately conservative: a false positive costs one extra (free) call, a
    false negative is the half-answer the user sees. A plain mid-word cut is not
    detected — too many complete answers end in a word for that to be safe.
    """
    if finish_reason == "length":
        return True
    if finish_reason not in (None, "stop"):
        return False
    s = text.rstrip()
    if not s:
        return False
    lines = s.splitlines()
    last = lines[-1].strip() if lines else ""
    if not last:
        return False
    if last.endswith(":") or last.startswith("#"):
        return True
    if last.count("**") % 2 or last.count("`") % 2:
        return True
    return False


def _continuation_messages(messages: list[dict], partial: str) -> list[dict]:
    """Ask the same model to finish a truncated answer without repeating itself."""
    return [
        *messages,
        {"role": "assistant", "content": partial},
        {"role": "user", "content":
         "Your previous message was cut off mid-sentence. Continue exactly from "
         "where it stopped. Do not repeat any text already written and do not "
         "restart the answer."},
    ]


class _ArmFailed(Exception):
    """One arm of a comparison could not be called at all."""


async def _multiplex_stream(opts: list[str], payload: dict, *, task_name: str,
                            policy: str, reason: dict, info: dict,
                            extra: dict,
                            session_id: str | None = None,
                            tenant_id: str | None = None,
                            user_keys: dict[str, str] | None = None,
                            local_endpoints: dict[str, str] | None = None,
                            decision_id: int | None = None,
                            max_arms: int | None = None) -> StreamingResponse:
    """Stream the arms of a comparison over one connection, concurrently.

    The arms are independent requests, so they run in threads (the caller layer is
    synchronous httpx) and hand their frames to the event loop through a queue.
    One arm being slow therefore cannot delay the other — which is the entire
    point of asking for two answers at once.

    `max_arms` is how many answers the caller asked for; the rest of `opts` are
    **spares**, started one at a time as earlier arms fail. Without them a
    comparison was exactly as reliable as its first choice: one arm returning 404
    (or an account with no credit) left the caller with a single answer and an
    error, even though the caller asked for a choice. A spare takes over the
    failed arm's index, so the caller still sees the number it asked for, and the
    failure is still recorded as an observation — only the error frame is
    suppressed, because it has been answered by the replacement.

    Frame shape, gated on `mi_options > 1` *and* `stream`, so no ordinary
    OpenAI client can observe it:

        {"mi":{"event":"arm",   "arm":0, "deploy_id":…, "vendor":…}}
        {"mi":{"event":"delta", "arm":0}, "choices":[{"index":0,…}]}
        {"mi":{"event":"end",   "arm":0, "latency_ms":…, "usage":{…}}}
        {"mi":{"event":"error", "arm":0, "error":"429"}}
        data: [DONE]
    """
    loop = asyncio.get_running_loop()
    q: "asyncio.Queue[tuple[str, int, object]]" = asyncio.Queue()
    stop = threading.Event()
    limit = max_arms if max_arms and max_arms > 0 else len(opts)
    initial = list(opts[:limit])
    spares = list(opts[limit:])

    def arm_frame(i: int, did: str, **extra_fields) -> dict:
        arm = dict(info.get(did) or {"deploy_id": did})
        arm["arm"] = i
        arm["decision_id"] = decision_id
        return {**arm, **extra_fields}

    arms = [arm_frame(i, did) for i, did in enumerate(initial)]
    threads: list[threading.Thread] = []

    def emit(item: tuple[str, int, object]) -> None:
        # Workers are threads; the queue belongs to the loop.
        loop.call_soon_threadsafe(q.put_nowait, item)

    def worker(i: int, did: str) -> None:
        """One arm, start to finish. Records its own observation either way."""
        ok = False
        completed = False
        arm_err: str | None = None
        usage: dict | None = None
        total_usage: dict | None = None
        store = None
        sess = None
        current = None
        started = time.perf_counter()
        try:
            head = did.partition(":")[0].split("/")[0]
            ukey = (user_keys or {}).get(head)
            ubase = (local_endpoints or {}).get(head)
            ep = resolve_endpoint(did, api_key=ukey, base_url=ubase)
            if ep.error or not ep.api_key:
                raise _ArmFailed(ep.error or "no_api_key")
            body = {**extra, "model": ep.model, "messages": payload["messages"]}
            sess = open_stream(ep, body, deploy_id=did)
            if sess.status_code != 200:
                raise _ArmFailed(sess.error_class or "error")
            # Quota is spent per arm: each is a real call.
            store = _store()
            store.record_usage(did)
            store.commit()
            store.close()
            store = None
            emitted = False
            tool_turn = False
            finish: str | None = None
            parts: list[str] = []
            current = sess
            continues = 0
            while True:
                for chunk in current.chunks():
                    if stop.is_set():
                        break
                    err = _sse_error(chunk)
                    if err:
                        # A gateway error inside a 200 stream is a failed arm,
                        # not an empty answer: raise so a spare can take over and
                        # the observation records `ok=False` rather than a win.
                        raise _ArmFailed(err)
                    finish = _sse_finish_reason(chunk) or finish
                    text = _sse_deltas(chunk)
                    seen = _sse_usage(chunk)
                    if seen:
                        usage = seen    # last usage in a segment wins
                    if text:
                        parts.append(text)
                        emit(("delta", i, text))
                        emitted = True
                    elif _sse_has_tool_calls(chunk):
                        tool_turn = True
                if stop.is_set():
                    break
                answer = "".join(parts)
                if (not tool_turn and continues < _MAX_CONTINUATIONS
                        and _looks_truncated(answer, finish)):
                    current.close()
                    continues += 1
                    total_usage = _merge_usage(total_usage, usage)
                    usage = None
                    finish = None
                    body = {**extra, "model": ep.model,
                            "messages": _continuation_messages(payload["messages"], answer)}
                    nxt = open_stream(ep, body, deploy_id=did)
                    if nxt.status_code != 200:
                        nxt.close()
                        break
                    current = nxt
                    continue
                break
            if not stop.is_set() and not emitted and not tool_turn:
                # A 200 that streamed no text is not an answer. Reasoning models
                # do this when the thinking budget swallows the completion, and
                # the caller still got a blank pane — so fail the arm and let a
                # spare answer instead.
                raise _ArmFailed("empty_content")
            ok = not stop.is_set()
            completed = True
        except _ArmFailed as exc:
            arm_err = str(exc)
        except Exception as exc:  # noqa: BLE001 - one arm must not kill the other
            arm_err = getattr(exc, "error_class", None) or "error"
        finally:
            for closeable in (current, sess):
                if closeable is not None:
                    try:
                        closeable.close()
                    except Exception:  # noqa: BLE001
                        pass
            if store is not None:
                store.close()
            # Fresh connection: the request-scoped store is already closed, and
            # two arms write here at once, so neither may share a Store.
            #
            # A stopped arm (`ok` False, loop completed) is recorded as
            # `aborted`: the request was made, so it is a real call the provider
            # may bill, and the frontend already uses that word for a request
            # the user stopped. It used to reach this branch with `arm_err`
            # unbound and lose both the observation and the charge to a
            # swallowed NameError.
            try:
                usage = _merge_usage(total_usage, usage)
                s = _store()
                if ok:
                    tin, tout, cost = _usage_tokens(usage)
                    s.observe(did, task_name, ok=True, ts=utcnow(),
                              signal_kind="provider_reported", signal_value=1.0,
                              tokens_in=tin, tokens_out=tout, cost_usd=cost,
                              session_id=session_id, tenant_id=tenant_id)
                    if session_id:
                        # Both arms are billed, so both are charged to the
                        # session — a comparison is not a discount.
                        s.add_session_usage(session_id, tokens_in=tin,
                                            tokens_out=tout, cost_usd=cost,
                                            tenant_id=tenant_id)
                else:
                    err = arm_err or "aborted"
                    if err == "429":
                        s.exhaust(did, window="minute")
                    s.observe(did, task_name, ok=False, ts=utcnow(),
                              error_class=err,
                              signal_kind="provider_reported", signal_value=0.0,
                              session_id=session_id, tenant_id=tenant_id)
                    if session_id:
                        s.add_session_usage(session_id, tenant_id=tenant_id)
                s.commit()
                s.close()
            except Exception:  # noqa: BLE001 - accounting must not break the stream
                pass
            # Emit last, so the frame a client sees on stream close means the
            # charge is already durable. Emitting `end` from inside the `try`
            # let a client read /v1/session the moment the stream finished and
            # see one arm of a two-arm comparison, because the second charge
            # had not been committed yet — a billing under-count, and a lost
            # charge outright if the process died between the two.
            if completed:
                emit(("end", i, {"latency_ms": (time.perf_counter() - started) * 1000,
                                 "usage": usage}))
            else:
                emit(("error", i, arm_err or "error"))

    async def gen():
        # Announce every arm before the first delta so the UI can render both
        # shells (and their model names) immediately.
        for arm in arms:
            arm_data = {"event": "arm", "needs_approval": True,
                        "task": task_name, "policy": policy, **arm}
            yield _sse_frame({"mi": arm_data})
        threads = [threading.Thread(target=worker, args=(i, did), daemon=True)
                   for i, did in enumerate(initial)]
        for t in threads:
            t.start()
        live = set(range(len(initial)))
        try:
            while live:
                kind, i, value = await q.get()
                if kind == "delta":
                    delta_data = {"event": "delta", "arm": i}
                    yield _sse_frame({"mi": delta_data,
                                      "choices": [{"index": i,
                                                   "delta": {"content": value}}]})
                elif kind == "end":
                    live.discard(i)
                    end_data = {"event": "end", "arm": i, **(value or {})}
                    yield _sse_frame({"mi": end_data})
                else:
                    live.discard(i)
                    if spares:
                        nxt = spares.pop(0)
                        replaced = arm_frame(i, nxt, replaces=True)
                        announced = {"event": "arm", "needs_approval": True,
                                     "task": task_name, "policy": policy, **replaced}
                        yield _sse_frame({"mi": announced})
                        t = threading.Thread(target=worker, args=(i, nxt), daemon=True)
                        threads.append(t)
                        t.start()
                        live.add(i)
                        continue
                    err_data = {"event": "error", "arm": i, "error": value}
                    yield _sse_frame({"mi": err_data})
            yield "data: [DONE]\n\n"
        finally:
            # A disconnected client must not keep paying for two generations.
            stop.set()
            for t in threads:
                t.join(timeout=1.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "X-MI-Multiplex": "true",
            "X-MI-Arms": str(len(opts)),
            "X-MI-Task": task_name,
            "X-MI-Policy": policy,
            "X-MI-Decision-Id": str(decision_id or ""),
            "Cache-Control": "no-cache",
        },
    )



def _sse_usage(chunk: bytes) -> dict | None:
    """Token usage from an SSE chunk, when the upstream reports it.

    Gateways attach usage to the final frame (OpenAI's
    `stream_options.include_usage`, and OpenRouter by default), but the relay
    only ever forwarded bytes — so a streamed call recorded `ok` and nothing
    else. Every streamed request therefore contributed zero tokens and zero cost
    to `routing_stats`, which is why the dashboard's spend was always $0.

    Returns the last usage object in the chunk, or None. Deliberately tolerant:
    a chunk that is not JSON, or has no usage, is simply not a finding.
    """
    found = None
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        body = line[5:].strip()
        if not body or body == b"[DONE]":
            continue
        try:
            j = json.loads(body)
        except ValueError:
            continue
        if isinstance(j, dict) and isinstance(j.get("usage"), dict):
            found = j["usage"]
    return found


def _usage_tokens(usage: dict | None) -> tuple[int | None, int | None, float | None]:
    """(tokens_in, tokens_out, cost_usd) from a usage object, any of them None."""
    if not usage:
        return None, None, None

    def num(*keys):
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                return v
        return None

    cost = num("cost", "total_cost", "cost_usd")
    return num("prompt_tokens", "input_tokens"), \
        num("completion_tokens", "output_tokens"), \
        (float(cost) if cost is not None else None)


async def _stream(deploy_ids, payload, *, task_name, store, policy, reason, alternatives,
                  intent=None, session_id=None, tenant_id=None,
                  user_keys: dict[str, str] | None = None,
                  local_endpoints: dict[str, str] | None = None,
                  decision_id: int | None = None):
    """Relay the first candidate that accepts the request.

    Fallback only runs *before* any byte reaches the caller: once an upstream
    returns 200 we are committed to it, because a half-sent SSE stream cannot be
    un-sent. Failures before that point are recorded and the next arm is tried.
    """
    extra = _extra(payload)
    attempts: list[tuple[str, str]] = []
    last_detail: str | None = None
    for did in deploy_ids:
        head = did.partition(":")[0].split("/")[0]
        ukey = (user_keys or {}).get(head)
        ubase = (local_endpoints or {}).get(head)
        ep = resolve_endpoint(did, api_key=ukey, base_url=ubase)
        if ep.error or not ep.api_key:
            err = ep.error or "no_api_key"
            store.record_usage(did)
            store.observe(did, task_name, ok=False, ts=utcnow(), error_class=err,
                          signal_kind="provider_reported", session_id=session_id,
                          tenant_id=tenant_id)
            store.commit()
            attempts.append((did, err))
            continue

        body = {"max_tokens": 2048, **extra, "model": ep.model, "messages": payload["messages"]}
        sess = await asyncio.to_thread(open_stream, ep, body, deploy_id=did)
        if sess.status_code != 200:
            store.record_usage(did)
            # NOT `reason`: that name is this function's *routing* reason dict, and
            # shadowing it here overwrote it with a string, so the response headers
            # below called `.get()` on a str-or-None and the request 500'd. The two
            # are unrelated — one explains why this arm was chosen, the other why it
            # was rate-limited.
            rl_reason = _record_rate_limits(store, did, headers=sess.headers,
                                            error_class=sess.error_class,
                                            error_detail=sess.error_detail)
            if sess.error_class == "429":
                store.exhaust(did, window="minute")
            _retire_if_uncallable(store, did, error_class=sess.error_class,
                                  error_detail=sess.error_detail)
            store.observe(did, task_name, ok=False, ts=utcnow(),
                          error_class=sess.error_class, signal_kind="provider_reported",
                          session_id=session_id, tenant_id=tenant_id,
                          rate_limit_reason=rl_reason)
            store.commit()
            last_detail = sess.error_detail
            attempts.append((did, sess.error_class or "error"))
            continue

        store.record_usage(did)
        # A stream that *opened* still carries the limit headers (they arrive with
        # the response, before any chunk), so a successful stream is evidence too.
        _record_rate_limits(store, did, headers=sess.headers)
        store.commit()

        def gen(sess=sess, did=did):
            ok = False
            usage: dict | None = None
            total_usage: dict | None = None
            parts: list[str] = []
            err_class: str | None = None
            tool_turn = False
            finish: str | None = None
            current = sess
            continues = 0
            try:
                # Announce the chosen arm and routing metadata in the stream itself
                # so the frontend receives the model identity without relying on headers.
                init_data = {
                    "event": "route",
                    "deploy": did,
                    "deploy_id": did,
                    "model": did,
                    "task": task_name,
                    "policy": policy,
                    "candidates": deploy_ids,
                    "alternatives": [d for d in deploy_ids if d != did],
                    "needs_approval": bool(reason.get("needs_approval")),
                    "decision_id": decision_id,
                }
                yield _sse_frame({"mi": init_data, "model": did})

                while True:
                    for chunk in current.chunks():
                        # Read usage without consuming the frame: it is already
                        # part of the client's byte stream and suppressing it
                        # would be a contract change for no gain.
                        seen = _sse_usage(chunk)
                        if seen:
                            usage = seen    # last usage in a segment wins
                        err = _sse_error(chunk)
                        if err:
                            # Relay the provider's own error frame, then stop: a
                            # 200-with-error stream must not be recorded as a win.
                            err_class = err
                            yield chunk
                            break
                        finish = _sse_finish_reason(chunk) or finish
                        text = _sse_deltas(chunk)
                        if text:
                            parts.append(text)
                        elif _sse_has_tool_calls(chunk):
                            tool_turn = True
                        # The proxy emits the terminator itself once, after any
                        # continuation; relaying this one would end the stream
                        # before the rest of the answer arrived.
                        relay = _strip_done(chunk)
                        if relay.strip():
                            yield relay
                    if err_class:
                        break
                    answer = "".join(parts)
                    if (not tool_turn and continues < _MAX_CONTINUATIONS
                            and _looks_truncated(answer, finish)):
                        current.close()
                        continues += 1
                        total_usage = _merge_usage(total_usage, usage)
                        usage = None
                        finish = None
                        body = {**extra, "model": ep.model,
                                "messages": _continuation_messages(payload["messages"], answer)}
                        try:
                            nxt = open_stream(ep, body, deploy_id=did)
                        except Exception:  # noqa: BLE001 - keep the partial answer
                            break
                        if nxt.status_code != 200:
                            nxt.close()
                            break
                        current = nxt
                        continue
                    break
                if err_class is None and not parts and not tool_turn:
                    # Relayed blank stream: record it as the failure it is.
                    err_class = "empty_content"
                ok = err_class is None
                yield "data: [DONE]\n\n"
            finally:
                current.close()
                tin, tout, cost = _usage_tokens(_merge_usage(total_usage, usage))
                # Fresh connection: the request-scoped store is already closed.
                s = _store()
                s.observe(did, task_name, ok=ok, ts=utcnow(),
                          error_class=err_class,
                          signal_kind="provider_reported",
                          signal_value=1.0 if ok else 0.0,
                          tokens_in=tin, tokens_out=tout, cost_usd=cost,
                          session_id=session_id, tenant_id=tenant_id)
                if session_id:
                    s.add_session_usage(session_id, tokens_in=tin,
                                        tokens_out=tout, cost_usd=cost,
                                        tenant_id=tenant_id)
                    # The answer the caller actually received, assembled from the
                    # deltas that were relayed — not a re-request. An aborted
                    # stream keeps the partial text, matching what was shown.
                    if parts:
                        s.record_messages(session_id, _turn(payload.get("messages"),
                                                           "".join(parts)),
                                          task=task_name, deploy_id=did)
                if parts and ok:
                    last_prompt = intent_mod.last_user_text(payload.get("messages") or [])
                    _launch_shadow_trial(
                        payload.get("messages") or [], last_prompt, did, "".join(parts), task_name,
                        user_keys=user_keys, local_endpoints=local_endpoints,
                        extra_body=_extra(payload), tenant_id=tenant_id,
                    )
                s.commit()
                s.close()

        headers = {
            "X-MI-Deploy": did,
            "X-MI-Task": task_name,
            "X-MI-Policy": policy,
            "X-MI-Candidates": ", ".join(d for d in deploy_ids if d != did),
            "X-MI-Needs-Approval": "true" if reason.get("needs_approval") else "false",
            "X-MI-Decision-Id": str(decision_id or ""),
            "Cache-Control": "no-cache",
        }
        if intent is not None:
            intent_val = f"{intent.task};{intent.confidence};{intent.source}"
            headers["X-MI-Intent"] = intent_val
        store.close()
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


    # Every arm failing on `no_api_key` is the fixable case: name the env var and
    # the in-app alternative instead of a bare "all candidates failed".
    if attempts and all(err == "no_api_key" for _d, err in attempts):
        ids = [d for d, _e in attempts]
        store.close()
        return _error(503, "no_api_key", f"no API key for {ids[0]}: {_no_key_hint(ids)}")
    store.close()
    return _error(502, "all_failed",
                  f"all candidates failed: {attempts}"
                  + (f" — {last_detail}" if last_detail else ""))
