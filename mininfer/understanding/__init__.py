"""The local understanding layer.

One tiny, local, schema-driven model that answers two questions MinInfer asks
constantly — *what is this prompt asking for* and *what does this page say a
model is* — without spending a routed call to answer them. See
`PRODUCTIZATION.md` and `CLOUD_ACTIVITY.md` §2.0 (Phase 2.0, deferred).

Importing this package is free: the model loads on first use, and every entry
point returns `None` when the `[understanding]` extra is not installed. Callers
keep their existing behaviour in that case, which is why nothing on the default
request path depends on the model being present.
"""
from __future__ import annotations

from . import schemas
from .gliner import (
    DEFAULT_MODEL,
    MODEL_ENV,
    UnderstandingUnavailable,
    as_intent_backend,
    available,
    backend_name,
    classify,
    decide,
    extract,
    extract_models,
    extract_record,
    pick_task,
    require,
    reset,
)

__all__ = [
    "schemas",
    "available",
    "backend_name",
    "require",
    "reset",
    "classify",
    "pick_task",
    "decide",
    "extract",
    "extract_record",
    "extract_models",
    "as_intent_backend",
    "UnderstandingUnavailable",
    "MODEL_ENV",
    "DEFAULT_MODEL",
]
