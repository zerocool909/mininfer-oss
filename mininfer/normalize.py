"""Name normalisation — the input to entity resolution.

Deliberately conservative: we strip *serving and marketing* noise, never
*identity* noise. `-2507` (a release date) and `-thinking` (a different
fine-tune) are identity and must survive normalisation.
"""
from __future__ import annotations

import re

# Noise that describes how a model is served, not what it is: quantization,
# hosting and routing markers. Always safe to strip.
_SERVE_TOKENS = ("fp8", "fp4", "bf16", "fp16", "int8", "int4", "awq", "gptq",
                 "gguf", "nvfp4", "tee", "free", "nitro")
# Tokens that *look* like serving noise but may be a different model: `turbo` is
# a smaller Whisper, `thinking` a different fine-tune, `-online`/`-beta` a
# different behaviour. Stripping one is fine for a display name, but a merge that
# relies on stripping it needs confirmation — see `normalize_identity`.
_VARIANT_TOKENS = ("turbo", "extended", "online", "beta", "thinking")

# Applied repeatedly: `gemma-4-31B-turbo-TEE` carries two such suffixes.
_SERVE_SUFFIX = re.compile(
    r"(?:[:|-])(?:" + "|".join(_SERVE_TOKENS + _VARIANT_TOKENS) + r")$")
# The conservative strip: serving markers only, variant tokens survive.
_SERVE_SUFFIX_SAFE = re.compile(
    r"(?:[:|-])(?:" + "|".join(_SERVE_TOKENS) + r")$")
_PROVIDER_PREFIX = re.compile(r"^(?:[a-z0-9_.-]+)/(?=[^/]+$)")

_ALIASES = {
    "gptoss": "gpt-oss",
    "gpt_oss": "gpt-oss",
    "llama": "llama",
    "qwen": "qwen",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "gemma": "gemma",
    "phi": "phi",
    "glm": "glm",
}


def _normalize_with(name: str, suffix: re.Pattern) -> str:
    s = name.strip().lower()
    if "#" in s:  # HF style "repo#revision"
        s = s.split("#", 1)[0]
    s = _PROVIDER_PREFIX.sub("", s)
    while True:
        stripped = suffix.sub("", s)
        if stripped == s:
            break
        s = stripped
    s = re.sub(r"[\s_.]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-: ")
    return _ALIASES.get(s, s)


def normalize(name: str) -> str:
    """Lowercase, drop provider prefix, collapse punctuation, strip serving noise."""
    return _normalize_with(name, _SERVE_SUFFIX)


def normalize_identity(name: str) -> str:
    """Like `normalize`, but keep tokens that may name a different model.

    Two names that share a `normalize()` key but differ here are only identical
    because `turbo`/`thinking`/`-online`/`-beta`/`-extended` was stripped — the
    Whisper case, where `whisper-large-v3-turbo` is a smaller model than
    `whisper-large-v3`. Entity resolution uses this to send such a merge to review
    instead of auto-merging on name alone.
    """
    return _normalize_with(name, _SERVE_SUFFIX_SAFE)


def normalize_hf_repo(repo: str | None) -> str | None:
    if not repo:
        return None
    return repo.strip().strip("/").lower()


def weights_id_for(hf_repo: str | None, name: str, revision: str | None = None) -> str:
    """Resolution order: hf repo (+revision) > normalised slug."""
    repo = normalize_hf_repo(hf_repo)
    if repo:
        return f"hf:{repo}@{revision}" if revision else f"hf:{repo}"
    return f"slug:{normalize(name)}"


def family_of(name: str) -> str | None:
    """`qwen3-30b-a3b-instruct-2507` -> "qwen". Best-effort, not load-bearing."""
    first = normalize(name).split("-", 1)[0]
    return re.sub(r"\d+(?:\.\d+)?$", "", first) or None


_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[bB](?![a-z0-9])")


def params_b_from_name(name: str) -> float | None:
    """`Qwen3-30B-A3B` -> 30.0 (total params, not active params)."""
    m = _PARAM_RE.search(name)
    return float(m.group(1)) if m else None


def is_moe_name(name: str) -> bool | None:
    n = normalize(name)
    if re.search(r"-a\d+b", n) or "moe" in n:
        return True
    return None
