"""Name normalisation — the input to entity resolution.

Deliberately conservative: we strip *serving and marketing* noise, never
*identity* noise. `-2507` (a release date) and `-thinking` (a different
fine-tune) are identity and must survive normalisation.
"""
from __future__ import annotations

import re

# Noise that describes how a model is served or marketed, not what it is.
# Applied repeatedly: `gemma-4-31B-turbo-TEE` carries two such suffixes.
_SERVE_SUFFIX = re.compile(
    r"(?:[:|-])(?:fp8|fp4|bf16|fp16|int8|int4|awq|gptq|gguf|nvfp4|tee|turbo|free|"
    r"nitro|extended|online|beta|thinking)$"
)
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


def normalize(name: str) -> str:
    """Lowercase, drop provider prefix, collapse punctuation, strip serving noise."""
    s = name.strip().lower()
    if "#" in s:  # HF style "repo#revision"
        s = s.split("#", 1)[0]
    s = _PROVIDER_PREFIX.sub("", s)
    while True:
        stripped = _SERVE_SUFFIX.sub("", s)
        if stripped == s:
            break
        s = stripped
    s = re.sub(r"[\s_.]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-: ")
    return _ALIASES.get(s, s)


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
