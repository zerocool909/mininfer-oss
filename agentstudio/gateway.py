"""min(Infer) gateway client for the agent studio.

Phase 0 (docs/AGENT_APP_PLAN.md): streaming chat with arm attribution.
The client only ever talks to the gateway's OpenAI-compatible surface —
never to a provider directly — so every node run inherits the router's
cost/quality decisions and names its arm via `mi.reason.selected`.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _env(key: str) -> str:
    """Read MI_* from the environment, or from the repo's .env as a fallback."""
    import os

    raw = os.environ.get(key)
    if raw:
        return raw.strip()
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.partition("=")[2].strip()
    return ""


def default_base() -> str:
    if os_env := _env("MI_GATEWAY_BASE"):
        return os_env.rstrip("/")
    return "http://127.0.0.1:8765"


def default_key() -> str:
    raw = _env("MI_API_KEYS")
    # `key:tenant,key2:tenant2` — the smoke path uses the first tenant.
    return raw.split(",")[0].split(":")[0].strip() if raw else ""


@dataclass
class Arm:
    """One deployment the router named, selected or rejected."""

    deploy_id: str = ""
    why: str = ""

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> "Arm":
        return cls(deploy_id=str(row.get("deploy_id") or row.get("id") or ""),
                   why=str(row.get("why") or ""))


@dataclass
class Reason:
    """The router's account of a call: who won, why, and what it cost."""

    selected: list[Arm] = field(default_factory=list)
    rejected: list[Arm] = field(default_factory=list)
    task: str = ""

    @classmethod
    def from_json(cls, mi: dict[str, Any]) -> "Reason":
        reason = (mi.get("reason") or {}) if isinstance(mi, dict) else {}
        return cls(
            task=str(reason.get("task") or ""),
            selected=[Arm.from_json(r) for r in reason.get("selected", []) or []],
            rejected=[Arm.from_json(r) for r in reason.get("rejected", []) or []],
        )


@dataclass
class Turn:
    """A completed chat turn: the text, plus where it came from."""

    text: str = ""
    task: str = ""
    arm: str = ""
    cost_usd: float | None = None
    tokens_out: int = 0

    @classmethod
    def finished(cls, chunk_mi: dict[str, Any] | None) -> "Turn":
        reason = Reason.from_json(chunk_mi or {})
        if reason.selected:
            arm = reason.selected[0].deploy_id
            return cls(task=reason.task, arm=arm)
        # Streamed calls announce the arm in a *flat* route event:
        # `{"mi": {"event": "route", "deploy_id": ...}}` — no `reason` block.
        # Same account, different frame; treat them as aliases.
        flat_id = str((chunk_mi or {}).get("deploy_id") or "")
        return cls(task=str((chunk_mi or {}).get("task") or ""), arm=flat_id)


class GatewayError(RuntimeError):
    pass


class Gateway:
    """One tenant's connection to the proxy. Stateless; reuse one per app."""

    def __init__(self, base: str | None = None, key: str | None = None,
                 timeout: float = 120.0):
        self.base = (base or default_base()).rstrip("/")
        self.key = key or default_key()
        if not self.key:
            raise GatewayError("no tenant key: set MI_API_KEYS in .env or MI_GATEWAY_BASE+key")
        self.headers = {"Authorization": f"Bearer {self.key}"}
        self.timeout = timeout

    async def chat(self, messages: list[dict[str, str]], *, model: str = "auto",
                   max_tokens: int = 256, temperature: float = 0.2,
                   **extra: Any) -> Turn:
        """One non-streaming turn. Cheap; used for classify/extract style nodes."""
        body = {"model": model, "messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, **extra}
        async with httpx.AsyncClient(base_url=self.base, timeout=self.timeout) as c:
            r = await c.post("/v1/chat/completions", json=body, headers=self.headers)
        if r.status_code != 200:
            raise GatewayError(f"gateway {r.status_code}: {r.text[:400]}")
        data = r.json()
        turn = Turn.finished(data.get("mi"))
        turn.text = "\n".join(
            choice.get("message", {}).get("content") or ""
            for choice in data.get("choices", []))
        usage = data.get("usage") or {}
        turn.tokens_out = int(usage.get("completion_tokens") or 0)
        return turn

    async def stream(self, messages: list[dict[str, str]], *, model: str = "auto",
                     max_tokens: int = 256, temperature: float = 0.2,
                     **extra: Any) -> AsyncIterator[tuple[str, Turn | None]]:
        """Server-sent events, skipped into `(token|done, payload)` items.

        Token items are the deltas to forward to a canvas. The final item is
        `(done, Turn)` carrying the `mi` block — after the text there is a
        decision: which arm answered, what it cost, who lost.
        """
        body = {"model": model, "messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, "stream": True, **extra}
        async with httpx.AsyncClient(base_url=self.base, timeout=self.timeout) as c:
            async with c.stream("POST", "/v1/chat/completions", json=body,
                                headers=self.headers) as resp:
                if resp.status_code != 200:
                    chunk = await resp.aread()
                    raise GatewayError(f"gateway {resp.status_code}: {chunk[:400]!r}")
                text_parts: list[str] = []
                mi: dict[str, Any] | None = None
                usage_tokens = 0
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    # The `mi` route event is announced at the *start* of the
                    # stream (`Arm = did` before any byte of answer), and later
                    # mi events may refine it. Last one with an arm wins.
                    if isinstance(chunk.get("mi"), dict):
                        mi = chunk["mi"] or mi
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = (choice.get("delta") or {}).get("content") or ""
                    if delta:
                        text_parts.append(delta)
                        yield "token", delta
                    if choice.get("finish_reason"):
                        usage_tokens = int((chunk.get("usage") or {}).get(
                            "completion_tokens") or 0) or usage_tokens
                if not text_parts:
                    raise GatewayError(
                        "the arm answered reasoning only — every token of the "
                        "budget was trace; raise max_tokens or pin a "
                        "non-reasoning arm")
                turn = Turn.finished(mi)
                turn.text = "".join(text_parts)
                turn.tokens_out = usage_tokens
                yield "done", turn


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env
