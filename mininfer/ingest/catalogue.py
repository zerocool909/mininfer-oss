"""Every upstream `mi ingest` can pull from, in tier order.

The notes are the documentation for this file: each says what the source uniquely
provides, because that is the only reason to keep pulling it. Tier 0 needs no
credential, which is what makes a cold start possible.
"""
from __future__ import annotations

from .types import SourceSpec


SOURCES: dict[str, SourceSpec] = {
    # ---- Tier 0: no credentials -------------------------------------------
    "openrouter": SourceSpec(
        "openrouter", 0, "https://openrouter.ai/api/v1/models", "openrouter",
        "richest single source: hugging_face_id, benchmarks, per-endpoint latency/uptime",
        confidence="aggregator_api",
    ),
    "vercel": SourceSpec(
        "vercel", 0, "https://ai-gateway.vercel.sh/v1/models", "vercel",
        "AI Gateway; free tier encoded as a `-free` model-id suffix",
        confidence="aggregator_api",
    ),
    "huggingface": SourceSpec(
        "huggingface", 0, "https://router.huggingface.co/v1/models", "hf_router",
        "per-provider is_free + first_token_latency_ms + throughput",
        confidence="aggregator_api",
    ),
    "deepinfra": SourceSpec(
        "deepinfra", 0, "https://api.deepinfra.com/v1/openai/models", "deepinfra",
        "USD/Mtok direct, prompt-cache pricing",
        confidence="provider_api",
    ),
    "novita": SourceSpec(
        "novita", 0, "https://api.novita.ai/v3/openai/models", "novita",
        "price fields are 1e-4 USD per Mtok (750 = $0.075)", confidence="provider_api",
    ),
    "deepseek": SourceSpec(
        # Tier 1, not 0: it advertises a key and answers 401 without one. Listing it
        # as keyless made `mi ingest` retry it on every run and report a failure
        # each time, which is noise an operator learns to ignore.
        "deepseek", 1, "https://api.deepseek.com/models", "openai_compat",
        "official provider", key_env="DEEPSEEK_API_KEY",
    ),
    "sambanova": SourceSpec(
        "sambanova", 0, "https://api.sambanova.ai/v1/models", "sambanova",
        "pricing inline per token; free tier on some models", confidence="provider_api",
    ),
    "chutes": SourceSpec(
        "chutes", 0, "https://llm.chutes.ai/v1/models", "chutes",
        "quantization + USD/TAO dual pricing", confidence="provider_api",
    ),
    "nvidia": SourceSpec(
        "nvidia", 0, "https://integrate.api.nvidia.com/v1/models", "nvidia",
        "free credits tier; no pricing in the listing", confidence="provider_api",
    ),
    # ---- Tier 1: free API key = real free quota ---------------------------
    "groq": SourceSpec("groq", 1, "https://api.groq.com/openai/v1/models", "openai_compat",
                       "fastest free tier; rpm/rpd per model", key_env="GROQ_API_KEY"),
    "google": SourceSpec("google", 1, "https://generativelanguage.googleapis.com/v1beta/models",
                         "google", "AI Studio free RPD per model", key_env="GEMINI_API_KEY"),
    "cerebras": SourceSpec("cerebras", 1, "https://api.cerebras.ai/v1/models", "openai_compat",
                           "very fast free tier", key_env="CEREBRAS_API_KEY"),
    "mistral": SourceSpec("mistral", 1, "https://api.mistral.ai/v1/models", "openai_compat",
                          "free experimental tier", key_env="MISTRAL_API_KEY"),
    "github": SourceSpec("github", 1, "https://models.github.ai/catalog/models", "github",
                         "free with a GitHub PAT, rate limited", key_env="GITHUB_TOKEN"),
    "cloudflare": SourceSpec("cloudflare", 1, "", "cloudflare",
                             "10k neurons/day free; needs account id + token",
                             key_env="CLOUDFLARE_API_TOKEN"),
    "cohere": SourceSpec("cohere", 1, "https://api.cohere.com/v1/models", "cohere",
                         "trial keys with monthly calls", key_env="COHERE_API_KEY"),
    "together": SourceSpec("together", 1, "https://api.together.xyz/v1/models", "openai_compat",
                           "$1 signup credit", key_env="TOGETHER_API_KEY"),
    "nebius": SourceSpec("nebius", 1, "https://api.studio.nebius.com/v1/models", "openai_compat",
                         "trial credits", key_env="NEBIUS_API_KEY"),
    "hyperbolic": SourceSpec("hyperbolic", 1, "https://api.hyperbolic.xyz/v1/models",
                             "openai_compat", "trial credits", key_env="HYPERBOLIC_API_KEY"),
    "kluster": SourceSpec("kluster", 1, "https://api.kluster.ai/v1/models", "openai_compat",
                          "trial credits", key_env="KLUSTER_API_KEY"),
    "fireworks": SourceSpec("fireworks", 1, "https://api.fireworks.ai/inference/v1/models",
                            "openai_compat", "$1 credit", key_env="FIREWORKS_API_KEY"),
    "zhipu": SourceSpec("zhipu", 1, "https://open.bigmodel.cn/api/paas/v4/models", "openai_compat",
                        "GLM free flash models", key_env="ZHIPU_API_KEY"),
    "moonshot": SourceSpec("moonshot", 1, "https://api.moonshot.ai/v1/models", "openai_compat",
                           "Kimi free quota", key_env="MOONSHOT_API_KEY"),
    "dashscope": SourceSpec("dashscope", 1,
                            "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
                            "openai_compat", "Qwen free token quota", key_env="DASHSCOPE_API_KEY"),
    # ---- Tier 2: local ----------------------------------------------------
    "ollama": SourceSpec("ollama", 2, "http://localhost:11434/v1/models", "openai_compat",
                         "local; marginal cost 0, capacity 1"),
    "lmstudio": SourceSpec("lmstudio", 2, "http://localhost:1234/v1/models", "openai_compat",
                           "local; marginal cost 0, capacity 1"),
    "vllm": SourceSpec("vllm", 2, "http://localhost:8000/v1/models", "openai_compat",
                       "self-hosted; marginal cost = GPU-time"),
}

