"""Model-calling layer.

One OpenAI-compatible caller, shared by the bench harness (Phase 2) and the
future HTTP proxy (Phase 4). We do not hand-write provider SDKs: every listed
endpoint speaks the OpenAI chat-completions shape, and anything else is deferred
to LiteLLM later (PLAN.md §6).

Rules enforced here:

  * **Refuse to guess.** An unresolved endpoint, a missing API key or an
    unparseable response is returned as an `error_class`, never a silent fallback
    to a wrong host or an empty string.
  * **Error classification is part of the contract.** `429` / `timeout` / `5xx` /
    `bad_output` feed the router's observation model directly (PLAN.md §3), which
    is what turns a failed call into a future routing signal instead of noise.
"""
from __future__ import annotations

import json
import os
import ssl
import time
from dataclasses import dataclass, field

import httpx

from .fetch import _verify as _tls_verify

# base_url -> (api_key_env, extra headers). The `/chat/completions` path is
# appended at call time. key_env=None means the endpoint is genuinely keyless.
ENDPOINTS: dict[str, tuple[str, str | None, dict[str, str]]] = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", {}),
    "vercel": ("https://ai-gateway.vercel.sh/v1", "AI_GATEWAY_API_KEY", {}),
    "huggingface": ("https://router.huggingface.co/v1", "HF_TOKEN", {}),
    "hf": ("https://router.huggingface.co/v1", "HF_TOKEN", {}),
    "deepinfra": ("https://api.deepinfra.com/v1/openai", "DEEPINFRA_API_KEY", {}),
    "chutes": ("https://llm.chutes.ai/v1", "CHUTES_API_KEY", {}),
    "novita": ("https://api.novita.ai/v3/openai", "NOVITA_API_KEY", {}),
    "sambanova": ("https://api.sambanova.ai/v1", "SAMBANOVA_API_KEY", {}),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", {}),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", {}),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", {}),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", {}),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", {}),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", {}),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY", {}),
    "cohere": ("https://api.cohere.com/v1", "COHERE_API_KEY", {}),
    "fireworks": ("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", {}),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY", {}),
    "moonshot": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY", {}),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", {}),
    "github": ("https://models.inference.ai.azure.com", "GITHUB_TOKEN", {}),
    "nebius": ("https://api.studio.nebius.ai/v1", "NEBIUS_API_KEY", {}),
    "hyperbolic": ("https://api.hyperbolic.xyz/v1", "HYPERBOLIC_API_KEY", {}),
    "kluster": ("https://api.kluster.ai/v1", "KLUSTER_API_KEY", {}),
    "ollama": (os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"), "OLLAMA_API_KEY", {}),
    "llamacpp": (os.environ.get("LLAMACPP_BASE_URL", "http://localhost:8080/v1"), "LLAMACPP_API_KEY", {}),
    "vertex": (os.environ.get("VERTEX_BASE_URL", "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/{PROJECT_ID}/locations/us-central1/endpoints/openapi"), "VERTEX_API_KEY", {}),
}

# Providers whose deploy_id embeds a gateway prefix. `hf/deepinfra:x` means "the
# HF router reaches deepinfra's upstream", so the *callable* host is the HF
# router, not deepinfra's API directly.
_GATEWAY_HEADS = ("openrouter", "hf", "huggingface", "vercel")


@dataclass(slots=True)
class Endpoint:
    base_url: str
    model: str
    api_key: str | None
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class CallResult:
    deploy_id: str
    text: str = ""
    ok: bool = False
    error_class: str | None = None
    latency_ms: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    # `stop` | `length` | `content_filter` | `tool_calls` | None. Kept because a
    # `length` finish is a truncated answer the caller must be able to see; it
    # used to be discarded, so a half-answer looked complete.
    finish_reason: str | None = None
    # The provider's own error message. A bare `http_404` tells a caller nothing:
    # "model not found", "API not enabled" and "wrong region" are all 404s, and
    # only the body separates them.
    error_detail: str | None = None
    raw: dict = field(default_factory=dict)
    # The upstream's rate-limit headers, kept **raw**. Parsing them is the quota
    # layer's job (`quota.parse_rate_limit_headers`), so this module stays a
    # transport and does not grow provider-specific knowledge.
    rate_limit_headers: dict = field(default_factory=dict)


@dataclass(slots=True)
class StreamSession:
    """An open upstream SSE stream. Caller relaying bytes must `close()` it."""
    status_code: int
    error_class: str | None
    headers: dict = field(default_factory=dict)
    response: object | None = None
    client: object | None = None
    error_detail: str | None = None

    def chunks(self):
        if self.response is None:
            return iter(())
        return self.response.iter_bytes()

    def close(self) -> None:
        try:
            if self.response is not None:
                self.response.close()
        finally:
            if self.client is not None:
                self.client.close()


def _url(endpoint: Endpoint) -> str:
    return f"{endpoint.base_url}/chat/completions"


def _headers(endpoint: Endpoint) -> dict[str, str]:
    return {"Content-Type": "application/json", **endpoint.headers}


#: Response headers worth keeping for the quota layer. A prefix filter rather than
#: a whitelist: providers keep inventing header names, and a missed one is a limit
#: we cannot observe. `retry-after` is included because a bare back-off signal is
#: still evidence about which limit was hit.
_RATE_LIMIT_PREFIXES = ("x-ratelimit", "ratelimit", "anthropic-ratelimit", "retry-after")


def _rate_limit_headers(resp) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items()
            if k.lower().startswith(_RATE_LIMIT_PREFIXES)}


def _classify(status: int) -> str | None:
    if status == 429:
        return "429"
    if status in (401, 403):
        return "auth_error"
    if status >= 500:
        return "5xx"
    if status != 200:
        return f"http_{status}"
    return None


#: Wording a provider uses when it serves a deployment only to allowlisted
#: applications — OpenRouter's agentic harnesses. Detected from the response
#: *body*, because the catalogue does not say so: in `/api/v1/models`,
#: `thinkingmachines/inkling-small:free` looks exactly like any other `:free`
#: model (`pricing {0,0}`, `is_moderated: false`, no restriction field). The
#: restriction exists only at call time, which is why this cannot be an ingest
#: filter.
_UNCALLABLE_MARKERS = (
    "only available on agentic harnesses",
    "agentic harness",
    "coding agent or productivity app",
)


def _refine_error_class(err: str | None, detail: str | None) -> str | None:
    """Reclassify an auth failure that is really "this deployment is not callable".

    A 401 is normally about *our* key, which is why `auth_error` is excluded from
    the model's statistics. This one is about the deployment, so it gets its own
    class — otherwise the arm is excluded from the numbers *and* retried forever.
    """
    if err != "auth_error" or not detail:
        return err
    text = detail.lower()
    return "not_api_callable" if any(m in text for m in _UNCALLABLE_MARKERS) else err


#: A TLS failure is almost always *our* trust store, not the provider: the root
#: CA is in the OS keychain (which `curl` reads) and not in `certifi`. The raw
#: `SSLCertVerificationError` text ("unable to get local issuer certificate")
#: tells an operator nothing actionable; this does. It replaces the raw detail
#: rather than wrapping it, so it also fits `observations.error_detail`.
_TLS_HINT = ("certificate verify failed — the root CA is missing from Python's "
             "trust store. Run scripts/make_ca_bundle.sh and set MI_CA_BUNDLE "
             "(README: TLS / corporate root CAs)")


def _network_class(exc: BaseException) -> str:
    """Tell "we could not verify the provider" from "the provider is unreachable".

    A corporate/MITM root missing from our trust store raises an `ssl.SSLError`
    underneath the httpx error. That is *our* configuration, so it is classified
    `tls_error` and kept out of the router's success statistics
    (`schema.NON_MODEL_ERRORS`) — otherwise a bad CA bundle teaches the router
    that its best arms fail. A genuine DNS/connect failure stays `network_error`.
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, ssl.SSLError):
            return "tls_error"
        stack.extend((e.__cause__, e.__context__))
    if "certificate verify failed" in str(exc).lower():
        return "tls_error"
    return "network_error"


def classify_error_payload(err: object) -> str:
    """Map an OpenAI-shaped `error` object to one of the router's error classes.

    Some gateways report an upstream failure as a **200 with `error` in the
    body** rather than an HTTP error status (OpenRouter does, including
    mid-stream). Left unread it looks like a model that answered nothing.

    Gateways also disagree on `code`: OpenRouter sends the HTTP status as an int
    (503); others send strings like `insufficient_quota` or
    `rate_limit_exceeded`. A numeric code reuses `_classify`; a string is matched
    on its text, so a quota error still lands on `429` and an overload on `5xx` —
    both of which the router counts as a trial of that deployment.
    """
    if not isinstance(err, dict):
        return "bad_output"
    code = err.get("code")
    if isinstance(code, int):
        return _classify(code) or f"http_{code}"
    if isinstance(code, str) and code.isdigit():
        return _classify(int(code)) or f"http_{code}"
    blob = " ".join(str(err.get(k) or "") for k in ("code", "type", "message")).lower()
    if "rate" in blob or "quota" in blob or "429" in blob:
        return "429"
    if any(w in blob for w in ("overload", "unavailable", "timeout", "503", "502", "500")):
        return "5xx"
    return "bad_output"


def _detail_from_text(text: str) -> str | None:
    """The provider's own `error.message`, or a short excerpt of the body.

    A streamed failure arrives as an SSE frame (`data: {...}`), not a bare JSON
    body, and some gateways wrap it in a one-element array — both are unwrapped
    here so the reader gets the sentence, not the envelope.
    """
    s = (text or "").strip()
    if not s:
        return None
    if s.startswith("data:"):
        s = s[5:].strip()
    try:
        j = json.loads(s)
    except ValueError:
        return s[:280]
    if isinstance(j, list) and j:
        j = j[0]
    if isinstance(j, dict):
        err = j.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:280]
        if isinstance(err, str):
            return err[:280]
        if j.get("message"):
            return str(j["message"])[:280]
    return s[:280]


def resolve_endpoint(
    deploy_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> Endpoint:
    """Turn `provider:model` into a callable endpoint, or an `error` we refuse on.

    Explicit overrides (base_url / api_key / model) take precedence over the
    built-in map, which is how the operator routes around a provider we do not
    know about yet without editing code.
    """
    provider, _, pid = deploy_id.partition(":")
    if not pid:
        return Endpoint("", "", None, error="bad_deploy_id")

    head = provider.split("/")[0]
    entry = ENDPOINTS.get(head)

    if base_url:
        key = api_key if api_key is not None else ("local" if head in ("ollama", "llamacpp") else _any_key())
        return Endpoint(base_url.rstrip("/"), model or pid, key,
                        _auth_headers(key))

    if entry is None:
        return Endpoint("", "", None,
                        error=f"no endpoint for provider {provider!r} (override with --base-url)")
    base, key_env, extra = entry
    key = api_key if api_key is not None else (os.environ.get(key_env) if key_env else None)
    if head in ("ollama", "llamacpp") and not key:
        key = "local"
    headers = dict(extra)
    headers.update(_auth_headers(key))
    return Endpoint(base.rstrip("/"), pid, key, headers)


def key_env_for(deploy_id: str) -> str | None:
    """Env var name that holds the API key for a deployment's provider."""
    provider = deploy_id.partition(":")[0]
    entry = ENDPOINTS.get(provider.split("/")[0])
    return entry[1] if entry else None


def available_providers(user_keys: dict[str, str] | None = None) -> set[str]:
    """Provider heads (the part before `/` and `:`) we can actually call.

    A key in the environment, a key the caller supplied, or a local engine. A
    deployment behind a provider we hold no key for cannot be called, so it is
    not a real candidate — a free arm we cannot reach is not "free", it is noise.

    Google used to be admitted unconditionally ("first-class on leaderboard"),
    which made the *router* believe `google:*` was reachable while the *proxy*
    skipped every one of them with `no_api_key` — so the ranking could put an arm
    first that it could never dial, and burn the request before falling through.
    Reachability is now the same question on both sides; use
    `--include-uncredentialed` (which sets `require_callable=False`) to see the
    theoretical ranking instead.
    """
    avail = set()
    for name, (_, key_env, _) in ENDPOINTS.items():
        if user_keys and user_keys.get(name):
            avail.add(name)
        elif key_env and os.environ.get(key_env):
            avail.add(name)
        elif name in ("ollama", "llamacpp"):
            avail.add(name)
    return avail


def _any_key() -> str | None:
    """First configured API key, used when the operator supplies --base-url
    without --api-key (e.g. a gateway with a single account key)."""
    for _, key_env, _ in ENDPOINTS.values():
        if key_env and os.environ.get(key_env):
            return os.environ[key_env]
    return None


def _auth_headers(key: str | None) -> dict[str, str]:
    if key:
        return {"Authorization": f"Bearer {key}"}
    return {}


def call(
    endpoint: Endpoint,
    messages: list[dict],
    *,
    deploy_id: str,
    timeout: float = 120.0,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> CallResult:
    body = {
        "model": endpoint.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    return call_body(endpoint, body, deploy_id=deploy_id, timeout=timeout)


def call_body(
    endpoint: Endpoint,
    body: dict,
    *,
    deploy_id: str,
    timeout: float = 120.0,
) -> CallResult:
    """POST a complete OpenAI request body. The proxy uses this to forward the
    caller's own `tools`, `tool_choice`, `response_format`, ... unchanged."""
    if endpoint.error:
        return CallResult(deploy_id, error_class=endpoint.error)
    if not endpoint.api_key:
        return CallResult(deploy_id, error_class="no_api_key")

    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, verify=_tls_verify()) as c:
            r = c.post(_url(endpoint), headers=_headers(endpoint), json=body)
    except httpx.TimeoutException:
        return CallResult(deploy_id, error_class="timeout",
                          latency_ms=(time.perf_counter() - t0) * 1000,
                          error_detail=f"Request timed out after {timeout}s")
    except httpx.HTTPError as exc:
        kind = _network_class(exc)
        return CallResult(deploy_id, error_class=kind,
                          latency_ms=(time.perf_counter() - t0) * 1000,
                          error_detail=_TLS_HINT if kind == "tls_error" else str(exc),
                          raw={"exc": str(exc)})
    latency_ms = (time.perf_counter() - t0) * 1000

    err = _classify(r.status_code)
    # Captured before the early returns, so a 429 carries the headers that explain
    # *which* limit it hit. On the error path those headers are the whole story.
    rate_limit_headers = _rate_limit_headers(r)
    if err:
        detail = _detail_from_text(r.text)
        return CallResult(deploy_id, error_class=_refine_error_class(err, detail),
                          latency_ms=latency_ms, error_detail=detail,
                          rate_limit_headers=rate_limit_headers)

    try:
        data = r.json()
    except ValueError:
        return CallResult(deploy_id, error_class="bad_output", latency_ms=latency_ms,
                          rate_limit_headers=rate_limit_headers)
    # A gateway can return 200 with `error` in the body. Read it before `choices`,
    # or an upstream 503 becomes an empty answer the router counts as a success.
    if isinstance(data, dict) and data.get("error"):
        detail = _detail_from_text(json.dumps(data["error"]))
        return CallResult(deploy_id,
                          error_class=_refine_error_class(
                              classify_error_payload(data["error"]), detail),
                          latency_ms=latency_ms, error_detail=detail,
                          raw=data, rate_limit_headers=rate_limit_headers)
    try:
        # A tool-call turn may omit `content` entirely, so read it defensively.
        choice = data["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        finish_reason = choice.get("finish_reason")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return CallResult(deploy_id, error_class="bad_output", latency_ms=latency_ms,
                          rate_limit_headers=rate_limit_headers)

    usage = data.get("usage") or {}
    return CallResult(
        deploy_id,
        text=text,
        ok=True,
        latency_ms=latency_ms,
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        finish_reason=finish_reason,
        raw=data,
        rate_limit_headers=rate_limit_headers,
    )


def open_stream(
    endpoint: Endpoint,
    body: dict,
    *,
    deploy_id: str,
    timeout: float = 120.0,
) -> StreamSession:
    """Open a streaming completion. Returns a session whose `status_code` is 200
    only when the upstream accepted the request; failures never stream to the
    caller, so the proxy can fall back to the next candidate."""
    if endpoint.error:
        return StreamSession(0, endpoint.error)
    if not endpoint.api_key:
        return StreamSession(0, "no_api_key")
    client = httpx.Client(timeout=timeout, follow_redirects=True, verify=_tls_verify())
    req = client.build_request("POST", _url(endpoint), headers=_headers(endpoint),
                               json={**body, "stream": True})
    try:
        resp = client.send(req, stream=True)
    except httpx.TimeoutException:
        client.close()
        return StreamSession(0, "timeout")
    except httpx.HTTPError as exc:
        client.close()
        kind = _network_class(exc)
        return StreamSession(0, kind,
                             error_detail=_TLS_HINT if kind == "tls_error" else str(exc))

    err = _classify(resp.status_code)
    if err:
        # Read the reason before closing: this is the only place it exists.
        try:
            detail = _detail_from_text(resp.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - a missing reason must not mask the error
            detail = None
        resp.close()
        client.close()
        return StreamSession(resp.status_code, _refine_error_class(err, detail),
                             error_detail=detail)
    return StreamSession(resp.status_code, None, dict(resp.headers), resp, client)


class Runner:
    """Callable used by the bench harness and the proxy.

    `extra_body` lets the proxy forward the caller's own request fields (`tools`,
    `tool_choice`, `response_format`, ...) unchanged, adding only the model id.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        extra_body: dict | None = None,
        user_keys: dict[str, str] | None = None,
        local_endpoints: dict[str, str] | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.extra_body = dict(extra_body or {})
        self.user_keys = user_keys or {}
        self.local_endpoints = local_endpoints or {}

    def __call__(self, deploy_id: str, messages: list[dict], **kw) -> CallResult:
        head = deploy_id.partition(":")[0].split("/")[0]
        effective_key = self.api_key or self.user_keys.get(head)
        effective_base = self.base_url or self.local_endpoints.get(head)
        ep = resolve_endpoint(deploy_id, api_key=effective_key,
                              base_url=effective_base, model=self.model)
        if ep.error:
            return CallResult(deploy_id, error_class=ep.error)
        body = dict(self.extra_body)
        body["model"] = ep.model
        body["messages"] = messages
        body.setdefault("temperature", kw.get("temperature", self.temperature))
        # No implicit cap. A default of 1024 silently truncated every long
        # non-streamed answer; the streaming path never set one, so neither does
        # this. A caller that wants a cap still gets it.
        effective_max = kw.get("max_tokens", self.max_tokens)
        if effective_max is not None:
            body.setdefault("max_tokens", effective_max)
        return call_body(ep, body, deploy_id=deploy_id,
                         timeout=kw.get("timeout", self.timeout))
