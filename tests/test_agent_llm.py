"""The agent fallback chain: one arm failing must not fail the task.

Free arms 429 constantly, so binding an agent to a single deploy_id is fragile.
These pin the resolution order and the per-arm fallback loop.
"""
from __future__ import annotations

from mininfer.agent_llm import AgentLLM, agent_models


def test_explicit_primary_leads_and_defaults_follow(monkeypatch):
    monkeypatch.delenv("MI_AGENT_MODEL", raising=False)
    monkeypatch.delenv("MI_AGENT_MODELS", raising=False)
    chain = agent_models("openrouter:foo/bar")
    assert chain[0] == "openrouter:foo/bar"
    assert len(chain) > 1                    # defaults appended as fallbacks
    assert len(chain) == len(set(chain))     # no duplicates


def test_configured_chain_is_honoured(monkeypatch):
    monkeypatch.delenv("MI_AGENT_MODEL", raising=False)
    monkeypatch.setenv("MI_AGENT_MODELS", "openrouter:a:free, openrouter:b:free")
    chain = agent_models()
    assert chain[:2] == ["openrouter:a:free", "openrouter:b:free"]


def test_mi_agent_model_beats_mi_agent_models(monkeypatch):
    monkeypatch.setenv("MI_AGENT_MODEL", "groq:qwen/qwen3.8-27b")
    monkeypatch.setenv("MI_AGENT_MODELS", "openrouter:a:free")
    assert agent_models()[0] == "groq:qwen/qwen3.8-27b"


class _Resp:
    def __init__(self, content: str):
        self.content = content


class _Client:
    def __init__(self, content: str, boom: bool = False):
        self.content = content
        self.boom = boom

    def invoke(self, prompt):  # noqa: ANN001
        if self.boom:
            raise RuntimeError("429 rate limited")
        return _Resp(self.content)


def test_it_falls_back_and_cools_down_the_dead_arm(monkeypatch):
    calls: list[str] = []

    def fake_client(self, model):
        calls.append(model)
        return _Client("ok from " + model, boom=(model == "bad:arm"))

    monkeypatch.setattr(AgentLLM, "_client_for", fake_client)
    llm = AgentLLM(["bad:arm", "good:arm"])
    resp = llm.invoke("hi")
    assert calls == ["bad:arm", "good:arm"]            # tried in order
    assert resp.agent_model == "good:arm"
    # The dead arm is not re-probed on the next call (cooldown), so a 429ing arm
    # does not cost a wasted call every time.
    llm.invoke("again")
    assert calls == ["bad:arm", "good:arm", "good:arm"]


def test_it_rotates_across_healthy_arms(monkeypatch):
    """No arm becomes the default: consecutive calls spread across the chain, so
    one free tier's quota is not exhausted while the rest sit idle."""
    def fake_client(self, model):
        return _Client("ok from " + model)

    monkeypatch.setattr(AgentLLM, "_client_for", fake_client)
    llm = AgentLLM(["a", "b", "c"])
    used = [llm.invoke("x").agent_model for _ in range(3)]
    assert used == ["a", "b", "c"]


def test_cooldown_is_ignored_when_every_arm_is_down(monkeypatch):
    """Cooling down must not become "refuse to work" — it retries rather than
    pretending no arm exists."""
    attempted: list[str] = []

    def fake_client(self, model):
        attempted.append(model)
        return _Client("", boom=True)

    monkeypatch.setattr(AgentLLM, "_client_for", fake_client)
    llm = AgentLLM(["a", "b"])
    for _ in range(2):
        try:
            llm.invoke("x")
        except RuntimeError:
            pass
    assert attempted.count("a") == 2 and attempted.count("b") == 2


def test_it_raises_only_when_every_arm_fails(monkeypatch):
    monkeypatch.setattr(AgentLLM, "_client_for",
                        lambda self, model: _Client("", boom=True))
    llm = AgentLLM(["a", "b"])
    try:
        llm.invoke("hi")
    except RuntimeError as exc:
        assert "every agent model failed" in str(exc)
        assert "2 tried" in str(exc)
    else:
        raise AssertionError("expected a failure when all arms fail")
