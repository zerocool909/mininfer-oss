"""Right model, right job: special-purpose arms must not answer prose.

The failure this guards: `general_chat` requires no capability, so any free,
untested arm passes the hard filter — and exploration then picks exactly the
odd ones. In practice a music model (Lyria), a TTS model (Orpheus) and a
content-safety classifier were all served for prose prompts.
"""
from __future__ import annotations

from mininfer.router import Policy, build_candidates, special_role
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store


def test_special_role_is_detected_from_the_id():
    assert special_role({"deploy_id": "openrouter:google/lyria-3-pro-preview"}) == "music"
    assert special_role({"deploy_id": "groq:canopylabs/orpheus-v1-english"}) == "tts"
    assert special_role({"deploy_id": "x:whisper-large-v3"}) == "asr"
    assert special_role({"deploy_id": "x:llama-3.1-nemoguard-8b-content-safety"}) == "safety"
    assert special_role({"deploy_id": "groq:openai/gpt-oss-safeguard-20b"}) == "safety"
    assert special_role({"deploy_id": "x:bge-m3"}) == "embedding"


def test_a_normal_generator_has_no_special_role():
    for ok in ("groq:openai/gpt-oss-120b", "openrouter:inclusionai/ling-3.1-flash",
               "openrouter:qwen/qwen3.8-27b:free", "deepinfra:meta-llama/llama-4-maverick"):
        assert special_role({"deploy_id": ok}) is None


def _seed(tmp_path) -> Store:
    s = Store(tmp_path / "r.db")
    s.upsert_weights(Weights("w1", "Lyria 3 Pro"))
    s.upsert_weights(Weights("w2", "GPT-OSS 120B"))
    s.upsert_deployment(Deployment("openrouter:google/lyria-3-pro-preview", "w1",
                                   "openrouter", "google/lyria-3-pro-preview",
                                   price_in=0.0, price_out=0.0, zero_price=True,
                                   context_window=32000))
    s.upsert_deployment(Deployment("groq:openai/gpt-oss-120b", "w2", "groq",
                                   "openai/gpt-oss-120b",
                                   price_in=0.0, price_out=0.0, zero_price=True,
                                   context_window=32000))
    s.commit()
    return s


def test_a_special_purpose_arm_is_rejected_for_a_generative_task(tmp_path):
    s = _seed(tmp_path)
    task = TaskProfile("general_chat", tokens_in=100, tokens_out=100)
    cands = {c.deploy_id: c for c in build_candidates(s, task, Policy())}
    assert cands["openrouter:google/lyria-3-pro-preview"].rejected.startswith("unsupported:")
    assert cands["groq:openai/gpt-oss-120b"].rejected is None     # the real generator passes
    s.close()


def test_a_task_can_opt_in_to_a_role(tmp_path):
    s = _seed(tmp_path)
    task = TaskProfile("music_prompt", tokens_in=100, tokens_out=100,
                       allow_roles=("music",))
    cands = {c.deploy_id: c for c in build_candidates(s, task, Policy())}
    assert cands["openrouter:google/lyria-3-pro-preview"].rejected is None
    s.close()
