"""Request-body schemas for the OpenAPI document.

The handlers parse JSON by hand (`await request.json()`), so FastAPI had nothing
to infer and Swagger UI listed every POST **with no body editor** — the routes
were documented, but "Try it out" had nowhere to put the request.

These fragments are attached with `openapi_extra` rather than as a Pydantic body
on the handler, and that is deliberate. A declared body model makes FastAPI
*validate* the request, and its 422 is `{"detail": [...]}` — not the OpenAI error
envelope this project promises (`docs/api-contract.md`). Documentation should not
change the contract, so the schemas are declarative and the handlers keep
validating.

`tests/test_api_contract.py` asserts every POST declares a request body, so the
schema and the code cannot drift apart.
"""
from __future__ import annotations

from typing import Any


SESSION = {
    "name": "X-MI-Session",
    "in": "header",
    "required": False,
    "schema": {"type": "string"},
    "description": (
        "Groups calls into one budgeted session. With access control on, the "
        "ledger is keyed `tenant/session`, so this is a label, not a boundary."
    ),
}

USER_KEYS = {
    "name": "X-User-API-Keys",
    "in": "header",
    "required": False,
    "schema": {"type": "string"},
    "description": (
        'JSON object mapping provider id to key, e.g. `{"groq": "gsk_..."}`. '
        "The provider is callable for that request only; a supplied key is never "
        "stored server-side."
    ),
}


def _body(properties: dict[str, Any], *, required: list[str] | None = None,
          example: dict[str, Any] | None = None,
          headers: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    if example is not None:
        schema["example"] = example
    extra: dict[str, Any] = {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }
    if headers:
        # Declared so Swagger UI renders an input for them. Without this the
        # header-dependent endpoints are unreachable from "Try it out": the
        # handlers read them, the schema never mentioned them.
        extra["parameters"] = list(headers)
    return extra


_MESSAGES = {
    "type": "array",
    "description": "The conversation so far.",
    "items": {
        "type": "object",
        "required": ["role", "content"],
        "properties": {
            "role": {"type": "string", "enum": ["system", "user", "assistant", "tool"]},
            "content": {"type": "string"},
        },
    },
}

_MODEL = {
    "type": "string",
    "default": "auto",
    "description": (
        "`auto` routes the default task; a task name pins that task; "
        "`provider:model` calls that deployment directly."
    ),
}

_TASK = {"type": "string", "description": "Pin the task instead of classifying the prompt."}


CHAT = _body(
    {
        "model": _MODEL,
        "messages": _MESSAGES,
        "stream": {"type": "boolean", "default": False,
                   "description": "Server-sent events. Each frame carries `choices[].delta`."},
        "temperature": {"type": "number", "default": 0.0},
        "max_tokens": {"type": "integer", "description": "No cap is applied when omitted."},
        "task": _TASK,
        "policy": {"type": "string", "description": "Named policy from the registry."},
        "mi_options": {"type": "integer", "minimum": 1, "maximum": 8,
                       "description": "Compare mode: run this many arms concurrently."},
    },
    required=["messages"],
    example={"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
    headers=(SESSION, USER_KEYS),
)

ROUTE = _body(
    {"model": _MODEL, "messages": _MESSAGES, "task": _TASK,
     "policy": {"type": "string"}, "max_tokens": {"type": "integer"}},
    required=["messages"],
    example={"messages": [{"role": "user", "content": "hello"}]},
    headers=(SESSION, USER_KEYS),
)

SEARCH = _body(
    {
        "query": {"type": "string"},
        "provider": {"type": "string", "default": "auto",
                     "description": "`auto` reaches a free provider first."},
        "limit": {"type": "integer", "default": 5},
        "force": {"type": "boolean", "default": False},
    },
    required=["query"],
    example={"query": "what is a mixture of experts model"},
    # /v1/search is the one path that *requires* a session: an uncapped paid
    # search is the only thing here that could spend without a bound.
    headers=(dict(SESSION, required=True), USER_KEYS),
)

APPROVE = _body(
    {
        "task": {"type": "string", "default": "unrouted"},
        "chosen": {"type": "string", "description": "The deployment the human preferred."},
        "rejected": {"type": "array", "items": {"type": "string"}},
        "decision_id": {"type": "integer"},
    },
    example={"task": "general_chat", "chosen": "groq:qwen/qwen3.8-27b"},
)

ROUTE_VERDICT = _body(
    {
        "deploy_id": {"type": "string"},
        "approved": {"type": "boolean"},
        "task": {"type": "string", "default": "unrouted"},
        "reason": {"type": "string"},
    },
    required=["deploy_id"],
    example={"deploy_id": "groq:qwen/qwen3.8-27b", "approved": True},
)

TRIAL = _body(
    {
        "deploy_id": {"type": "string"},
        "task": {"type": "string", "default": "general_chat"},
        "prompt": {"type": "string", "description": "Defaults to a routing question."},
    },
    required=["deploy_id"],
    example={"deploy_id": "groq:qwen/qwen3.8-27b"},
    headers=(SESSION, USER_KEYS),
)

COMPACT = _body(
    {
        "model": _MODEL,
        "messages": _MESSAGES,
        "summary": {"type": "string", "description": "An existing summary to fold in."},
        "task": _TASK,
        "policy": {"type": "string"},
        "max_tokens": {"type": "integer"},
        "stream": {"type": "boolean", "default": False},
    },
    required=["messages"],
    example={"messages": [{"role": "user", "content": "summarise this thread"}]},
    headers=(SESSION, USER_KEYS),
)

PROVIDERS_TEST = _body(
    {
        "provider": {"type": "string", "description": "A provider id from `/v1/providers`."},
        "api_key": {"type": "string",
                    "description": "Optional. Falls back to `X-User-API-Keys`, then the "
                                   "provider's environment variable."},
    },
    required=["provider"],
    example={"provider": "groq"},
    headers=(USER_KEYS,),
)

REVIEWS_DECIDE = _body(
    {
        "deploy_id": {"type": "string"},
        "approve": {"type": "boolean",
                    "description": "Keep using it at the new price, or stop using it."},
        "note": {"type": "string"},
    },
    required=["deploy_id", "approve"],
    example={"deploy_id": "openrouter:x:free", "approve": False},
)

ANOMALY_DECIDE = _body(
    {
        "anomaly_id": {"type": "string",
                       "description": "From `/v1/economics/anomalies`."},
        "status": {"type": "string", "enum": ["acknowledged", "resolved"],
                   "description": "`acknowledged`: seen, still wrong. "
                                  "`resolved`: closed. Resolving one that is "
                                  "still wrong does not silence it — the next "
                                  "reconcile opens a new row."},
        "note": {"type": "string"},
    },
    required=["anomaly_id", "status"],
    example={"anomaly_id": "openrouter:x:free|input|2026-10-04T10:00:00+00:00",
             "status": "acknowledged", "note": "checked the provider page"},
)

SET_KEY = _body(
    {
        "provider": {"type": "string",
                     "description": "A provider id from `/v1/providers`."},
        "api_key": {"type": "string",
                    "description": "Verified against the provider before it is "
                                   "stored; an unverified key is never written to "
                                   "`.env`."},
    },
    required=["provider", "api_key"],
    example={"provider": "groq", "api_key": "gsk_..."},
)

PROBE_CONFIG = _body(
    {
        "enabled": {"type": "boolean",
                    "description": "Turn the background provider health probe on or off."},
        "interval_seconds": {"type": "number", "minimum": 30,
                             "description": "How often to probe, in seconds (minimum 30)."},
    },
    example={"enabled": True, "interval_seconds": 300},
)

PROBE_RUN = _body(
    {
        "providers": {
            "type": "array", "items": {"type": "string"},
            "description": "Probe only these provider ids. Omit to probe every "
                           "configured provider.",
        },
    },
    example={"providers": ["groq", "openrouter"]},
)

SEED_QUOTAS = _body(
    {
        "config": {"type": "string",
                   "description": "Path to the quota config. Defaults to "
                                  "`config/quotas.yaml`."},
        "dry_run": {"type": "boolean", "default": False,
                    "description": "Report what would be seeded without writing."},
    },
    example={"dry_run": False},
)

PUSHED_MODELS = _body(
    {
        "action": {"type": "string", "enum": ["push", "unpush", "list"], "default": "list"},
        "deploy_id": {"type": "string"},
        "task": {"type": "string"},
    },
    example={"action": "push", "deploy_id": "groq:qwen/qwen3.8-27b"},
)

LOCAL_REGISTER = _body(
    {
        "engine": {"type": "string", "enum": ["ollama", "llamacpp"]},
        "models": {"type": "array", "items": {"type": "string"},
                   "description": "Model ids the local runtime reports."},
    },
    required=["engine"],
    example={"engine": "ollama", "models": ["llama3.2"]},
)
