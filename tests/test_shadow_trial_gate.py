"""The background shadow trial must not dial unassociated models.

`_run_shadow_trial` probes untried *free* arms to gather evidence. It calls an arm
directly, so it bypasses `router._reject` and has to apply the same role gate
itself — otherwise it dials ASR/TTS/music/safety models that can never answer a
chat message. Measured on the live registry before this gate: 36% of the trial's
attempts were non-chat, 91 of them hard 400/402 failures
(`reports/selection-eval/`).
"""
from __future__ import annotations

import asyncio
import types

import pytest

from mininfer import proxy
from mininfer.router import Policy, special_role
from mininfer.schema import TaskProfile

#: Free + live + observed less than the trial threshold, so only the role gate
#: decides. Matched by `router.special_role` on the id.
NON_CHAT = [
    ("groq:whisper-large-v3", "whisper-large-v3", "asr"),
    ("groq:canopylabs/orpheus-v1-english", "canopylabs/orpheus-v1-english", "tts"),
    ("openrouter:google/lyria-3-pro-preview", "google/lyria-3-pro-preview", "music"),
    ("groq:meta-llama/llama-prompt-guard-2-22m", "meta-llama/llama-prompt-guard-2-22m", "safety"),
    ("openrouter:nvidia/nemotron-3.5-content-safety:free",
     "nvidia/nemotron-3.5-content-safety:free", "safety"),
]


def _arm(deploy_id: str, provider_model_id: str, **kw) -> dict:
    return {"deploy_id": deploy_id, "provider_model_id": provider_model_id,
            "weights_id": f"hf:{provider_model_id}", "status": "live",
            "zero_price": 1, "free_variant": 0, "trial_credits": 0,
            "price_in": 0.0, **kw}


def _task(name: str = "general_chat", **kw) -> TaskProfile:
    return TaskProfile(name=name, tokens_in=100, tokens_out=100, **kw)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    # The trial's eligibility has a key check the router shares; stub it so the
    # test is about the role gate, not the test machine's environment.
    monkeypatch.setattr(proxy, "_keys_available", lambda *a, **k: True)


@pytest.mark.parametrize("deploy_id,pmid,role", NON_CHAT)
def test_non_chat_arms_are_never_trialled(deploy_id, pmid, role):
    arm = _arm(deploy_id, pmid)
    assert special_role(arm) == role  # the fixture is really a special-role arm
    assert proxy._eligible_for_trial(arm, {}, _task(), Policy(), None, None) is False


def test_a_normal_chat_arm_is_eligible():
    arm = _arm("groq:qwen/qwen3.8-27b", "qwen/qwen3.8-27b")
    assert special_role(arm) is None
    assert proxy._eligible_for_trial(arm, {}, _task(), Policy(), None, None) is True


def test_allow_roles_re_admits_a_special_arm():
    """A task that genuinely wants a safety model can still trial one."""
    task = _task("content_review", allow_roles=("safety",))
    arm = _arm("groq:meta-llama/llama-prompt-guard-2-22m", "meta-llama/llama-prompt-guard-2-22m")
    assert proxy._eligible_for_trial(arm, {}, task, Policy(), None, None) is True


def test_paid_retired_and_already_seen_arms_are_not_trialled():
    policy = Policy()
    task = _task()
    paid = _arm("groq:x", "x", zero_price=0, free_variant=0, trial_credits=0)
    assert proxy._eligible_for_trial(paid, {}, task, policy, None, None) is False
    retired = _arm("groq:y", "y", status="deprecated")
    assert proxy._eligible_for_trial(retired, {}, task, policy, None, None) is False
    seen = _arm("groq:z", "z")
    stats = {"groq:z": {"n": policy.free_trial_obs}}
    assert proxy._eligible_for_trial(seen, stats, task, policy, None, None) is False


def test_unkeyed_provider_is_not_trialled(monkeypatch):
    monkeypatch.setattr(proxy, "_keys_available", lambda *a, **k: False)
    assert proxy._eligible_for_trial(_arm("groq:a", "a"), {}, _task(), Policy(),
                                     None, None) is False


# --------------------------------------------------------------------------- #
# integration: the worker actually dials only the associated arm
# --------------------------------------------------------------------------- #

class _FakeStore:
    def __init__(self, deps):
        self._deps = deps
        self.observed: list[str] = []

    def stats(self, _task):
        return {}

    def deployments(self):
        return self._deps

    def record_usage(self, _deploy_id):
        pass

    def observe(self, deploy_id, _task, **_kw):
        self.observed.append(deploy_id)

    def commit(self):
        pass

    def close(self):
        pass


class _FakeRunner:
    calls: list[str] = []

    def __init__(self, **_kw):
        pass

    def __call__(self, deploy_id, _messages, **_kw):
        _FakeRunner.calls.append(deploy_id)
        return types.SimpleNamespace(ok=True, text="", error_class=None,
                                     error_detail=None, latency_ms=1.0,
                                     tokens_in=0, tokens_out=0)


class _FakePolicy:
    @staticmethod
    def load(_path):
        return Policy(), {"general_chat": TaskProfile(name="general_chat",
                                                      tokens_in=1, tokens_out=1)}


def test_the_worker_dials_only_the_associated_arm(monkeypatch):
    deps = [
        _arm("groq:whisper-large-v3", "whisper-large-v3"),          # asr -> excluded
        _arm("groq:canopylabs/orpheus-v1-english", "canopylabs/orpheus-v1-english"),  # tts
        _arm("groq:qwen/qwen3.8-27b", "qwen/qwen3.8-27b"),          # chat -> the only one
        _arm("groq:paid", "paid", zero_price=0, free_variant=0, trial_credits=0),
    ]
    store = _FakeStore(deps)
    _FakeRunner.calls = []
    monkeypatch.setattr(proxy, "_store", lambda: store)
    monkeypatch.setattr(proxy, "Runner", _FakeRunner)
    monkeypatch.setattr(proxy, "Policy", _FakePolicy)

    asyncio.run(proxy._run_shadow_trial(
        [{"role": "user", "content": "hello"}], "hello", "primary:x", "text", "general_chat"))

    assert _FakeRunner.calls == ["groq:qwen/qwen3.8-27b"]
