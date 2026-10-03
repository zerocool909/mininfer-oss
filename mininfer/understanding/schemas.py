"""Schema declarations for the local understanding layer.

These are the *shapes* the router and the ingest pipeline already speak, written
once so a local model (GLiNER2.5) and the schema-driven call sites agree on them.
Nothing here imports the model; it is plain data.
"""
from __future__ import annotations

# `fastino/GLiNER2.5-Decide` answers a label set at call time. These are the
# small closed vocabularies for the routing heads the model is good at; `task`
# comes from the policy and `provider` from the registry, so both stay dynamic.
# Free-form values ("$0", "32k", a provider name that is not in the list) are
# the *record* path's job, not the classifier's.
COST_LABELS: tuple[str, ...] = ("free", "cheap", "balanced", "any")
CONTEXT_LABELS: tuple[str, ...] = ("small", "32k", "128k", "1m", "any")
STYLE_LABELS: tuple[str, ...] = ("concise", "detailed", "any")

# Routing constraints a caller can state in prose. One top-level record type, in
# the same declarative shape as `MODEL_RECORD_SCHEMA` so `_Adapter._build_schema`
# can turn either into the library's fluent builder.
#
# Values stay strings on purpose: the extractor returns the page's own words
# ("32k", "$0", "google") and the caller decides how to parse them. It reports
# evidence; it does not normalise policy.
ROUTING_RECORD_SCHEMA: dict = {
    "routing_request": [
        {
            "task_type": "string",
            "preferred_provider": "string",
            "max_cost": "string",
            "minimum_context": "string",
            "required_capabilities": "list",
            "response_style": "string",
        }
    ],
}

# Model-catalogue facts, aligned with `agent_ingest._SCHEMA_HINT` so a record
# extracted locally flows through the same normalize/validate/quarantine path.
# Prices stay as strings for the same reason as above: the extractor quotes the
# page, `validate_prices` decides whether the quote is trustworthy.
MODEL_RECORD_SCHEMA: dict = {
    "models": [
        {
            "model_name": "string",
            "provider": "string",
            "provider_model_id": "string",
            "context_window": "string",
            "price_in_usd_per_mtok": "string",
            "price_out_usd_per_mtok": "string",
            "free": "string",
            "capabilities": "list",
        }
    ],
}

# Flat entity list for the cheaper `extract` path (span extraction, no records).
EVIDENCE_ENTITIES: tuple[str, ...] = (
    "model",
    "provider",
    "context_window",
    "price",
    "free_tier",
    "release_date",
)

CAPABILITY_ENTITIES: tuple[str, ...] = (
    "vision",
    "tool_calling",
    "structured_output",
    "long_context",
    "reasoning",
)
