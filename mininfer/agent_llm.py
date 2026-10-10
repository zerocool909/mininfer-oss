"""Agent model resolution with a free-first fallback chain.

Every agentic step (`mi add-url`, `mi resolve --adjudicate`, `mi scout`) used to
bind to **one** deploy_id. A free arm makes that cheap but fragile — free tiers
429 constantly — so a single-arm agent fails the whole task on the first hiccup.

This resolves an ordered list of *same-capability* arms and tries them in turn,
promoting whichever answered so the next call starts from a warm arm. It is a
fallback loop, not a router: no ranking, no cost model — the caller names the
chain (or accepts the free-first default) and the first arm that answers wins.

Why not LiteLLM: the repo already has the provider seam (`execute.resolve_endpoint`
turns `provider:model` into a base_url + key + model, and classifies errors), and
the image is deliberately lean (`requirements.lock`, ~60 MB). LiteLLM would add a
large dependency to duplicate that seam and would still not express "another arm
of the same *task*" — which a list of deploy_ids does directly. The behaviour
wanted here is ~60 lines, so it lives here.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .execute import resolve_endpoint

#: Free-first default chain. Used only when nothing is configured; `MI_AGENT_MODEL`
#: (single, back-compat) or `MI_AGENT_MODELS` (comma-separated chain) override it.
#:
#: The ids carry the OpenRouter *upstream* prefix (`openrouter/<upstream>:<model>`)
#: because the bare form (`openrouter:<model>`) routes to whichever upstream is
#: cheap and was observed answering 429 — the gateway-embedded id is the arm that
#: actually served. The last entry is a paid arm on purpose: a fallback chain that
#: cannot reach a paid model is not a fallback, it is a longer outage.
_DEFAULT_CHAIN = (
    "openrouter/novita:inclusionai/ling-3.1-flash",
    "openrouter/nvidia:nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/cohere:cohere/north-mini-code:free",
    "groq:qwen/qwen3.8-27b",
)


def agent_models(primary: str | None = None) -> list[str]:
    """The ordered arms an agent call may use. `primary` (an explicit deploy_id)
    leads; `MI_AGENT_MODEL` leads when it is set; otherwise `MI_AGENT_MODELS`,
    otherwise the free-first default chain."""
    chain: list[str] = []
    if primary:
        chain.append(primary)
    elif os.environ.get("MI_AGENT_MODEL", "").strip():
        chain.append(os.environ["MI_AGENT_MODEL"].strip())
    else:
        configured = os.environ.get("MI_AGENT_MODELS", "").strip()
        if configured:
            chain.extend(m.strip() for m in configured.split(",") if m.strip())
        else:
            chain.extend(_DEFAULT_CHAIN)
    # Union with the default chain so an explicit primary still has somewhere to
    # fall back to, without duplicating arms already listed.
    for m in _DEFAULT_CHAIN:
        if m not in chain:
            chain.append(m)
    return chain


@dataclass(slots=True)
class AgentResponse:
    """Minimal langchain-shaped reply: `.content`, plus which arm answered."""

    content: str
    agent_model: str = ""


class AgentLLM:
    """A langchain-shaped LLM (`llm.invoke(prompt)`) that spreads across arms.

    Two behaviours, both deliberate:

    * **round-robin**, not "promote the winner". Pinning to whichever arm answered
      last makes one model the default for every call, which burns its free-tier
      quota (and invites the provider's rate limiter) while the other arms sit
      idle. The cursor advances past each success, so consecutive calls rotate.
    * **per-arm cooldown**, so a 429 does not get re-probed on every call. A failed
      arm is skipped for `cooldown_s`; if every arm is cooling down the cooldown is
      ignored rather than refusing to work.

    Errors are surfaced only when every arm has failed: the last few, and how many
    were tried.
    """

    def __init__(self, models: list[str] | None = None, *, temperature: float = 0.0,
                 timeout: float = 120.0, cooldown_s: float = 60.0):
        self.order = list(models) if models else agent_models()
        self.temperature = temperature
        self.timeout = timeout
        self.cooldown_s = cooldown_s
        self.used: str = ""
        self._cursor = 0
        self._down: dict[str, float] = {}

    def _client_for(self, model: str):
        from langchain_openai import ChatOpenAI

        ep = resolve_endpoint(model)
        if ep.error or not ep.api_key:
            raise RuntimeError(f"{model} not callable: {ep.error or 'no_api_key'}")
        return ChatOpenAI(model=ep.model, base_url=ep.base_url, api_key=ep.api_key,
                          temperature=self.temperature, timeout=self.timeout)

    def invoke(self, prompt):  # noqa: ANN001 - mirrors `ChatOpenAI.invoke`
        n = len(self.order)
        if not n:
            raise RuntimeError("no agent models configured")
        errors: list[str] = []
        for respect_cooldown in (True, False):
            now = time.monotonic()
            for offset in range(n):
                i = (self._cursor + offset) % n
                model = self.order[i]
                if respect_cooldown and self._down.get(model, 0.0) > now:
                    continue
                try:
                    resp = self._client_for(model).invoke(prompt)
                except Exception as exc:  # noqa: BLE001 - try the next arm
                    errors.append(f"{model}: {type(exc).__name__}: {exc}")
                    self._down[model] = now + self.cooldown_s
                    continue
                content = resp.content if hasattr(resp, "content") else str(resp)
                self.used = model
                self._cursor = (i + 1) % n     # rotate: next call starts elsewhere
                self._down.pop(model, None)
                return AgentResponse(content=content, agent_model=model)
            if errors:
                break
        raise RuntimeError(
            "every agent model failed (" + str(len(errors)) + " tried): "
            + " | ".join(errors[-3:]))
