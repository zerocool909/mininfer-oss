"""GLiNER2.5 — an optional, local structured-understanding layer.

This module is a **seam, not a dependency**. Nothing imports the model at module
load; `_load()` does it lazily, once, and returns `None` when the
`[understanding]` extra is not installed. Every public function therefore has
three honest outcomes:

    value       the model answered
    None        the extra is absent, or the call failed — caller uses its own path
    (raise)     never on the optional path; a missing model is not an error

That contract is what lets the proxy and the ingest graph keep their existing
behaviour, and their existing tests, when the extra is not present. It is
deliberately deferred out of the Phase 1.0 image.

The default checkpoint is **`fastino/GLiNER2.5-Decide`** — the 340M English
classification specialist in the GLiNER2.5 family (DeBERTa-v3-large, span
architecture). It takes a label set at call time and returns a label in one
forward pass: no prompt template, no generated tokens, no API call. That is
MinInfer's intent and routing decision, answered locally and for free.

The adapter below targets the **`gliner2` 2.0** API, read from the published
wheel and the model card rather than guessed:

    from gliner2 import AutoExtractor                 # needs gliner2[local]
    model = AutoExtractor.from_pretrained("fastino/GLiNER2.5-Decide")
    model.classify_text(text, {"intent": [...]}, include_confidence=True)
    model.extract(text, model.create_schema().structure("m").field("f", dtype="str"))
    model.extract_entities(text, ["company", "person"])

Two deliberate choices, both from the checkpoint's own guidance:

  * **Scores are returned raw, never re-normalised.** The card warns against
    assuming the confidences form a probability distribution; `classify`
    therefore reports the model's own number, and a single-label answer keeps
    its confidence instead of being flattened to 1.0.
  * **`Decide` is a classifier, not an extractor.** It answers "which of these
    labels", not "pull the price out of this page". The record/entity paths stay
    model-agnostic and fall through when this checkpoint returns nothing; point
    `MI_GLINER_MODEL` at a records-capable checkpoint (`gliner2.5-base-v1`) when
    the ingest node is what needs the model.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import schemas

MODEL_ENV = "MI_GLINER_MODEL"
DEVICE_ENV = "MI_GLINER_DEVICE"
# The routing/decision specialist. `fastino/gliner2.5-small-v1` (74M) is the fast
# CPU alternative for the same call shape; `fastino/gliner2.5-base-v1` (194M) is
# the records-capable checkpoint for extraction.
DEFAULT_MODEL = "fastino/GLiNER2.5-Decide"

_MODEL: Any = None
_LOADED = False
_LOCK = threading.Lock()

_UNSET = object()

_DTYPES = {
    "string": "str", "str": "str",
    "integer": "int", "int": "int",
    "number": "float", "float": "float",
    "boolean": "bool", "bool": "bool",
    "list": "list",
}


class UnderstandingUnavailable(RuntimeError):
    """Raised only by `require()`. The optional path returns None instead."""


class Backend(Protocol):
    """What the rest of MinInfer relies on.

    `classify_heads` is optional: a backend that implements only `classify` and
    `extract` still satisfies the seam (the single-head `decide` path falls back
    to looping `classify`).
    """

    def classify(self, text: str, labels: Sequence[str]) -> Mapping[str, float]: ...
    def extract(self, text: str, schema: Mapping[str, Any]) -> Mapping[str, Any]: ...


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


def _import_library():
    """The `gliner2` package, or None. GLiNER v1 is a different API — not used."""
    try:
        return __import__("gliner2", fromlist=["*"])
    except Exception:
        return None


def _build() -> Backend | None:
    lib = _import_library()
    if lib is None:
        return None
    factory = getattr(lib, "AutoExtractor", None) or getattr(lib, "GLiNER2", None)
    if factory is None or not hasattr(factory, "from_pretrained"):
        return None
    model_id = os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    device = os.environ.get(DEVICE_ENV) or None
    try:
        kwargs = {"map_location": device} if device else {}
        return _Adapter(factory.from_pretrained(model_id, **kwargs))
    except Exception:
        return None


def _load() -> Backend | None:
    global _MODEL, _LOADED
    if _LOADED:
        return _MODEL
    with _LOCK:
        if not _LOADED:
            _MODEL = _build()
            _LOADED = True
    return _MODEL


def _resolve(model: Any) -> Backend | None:
    return _load() if model is _UNSET else model


def available() -> bool:
    """True when a usable local model is loaded."""
    return _load() is not None


def backend_name() -> str:
    if _load() is None:
        return "none"
    return os.environ.get(MODEL_ENV) or DEFAULT_MODEL


def require() -> Backend:
    backend = _load()
    if backend is None:
        raise UnderstandingUnavailable(
            "the [understanding] extra is not installed "
            "(pip install 'mininfer[understanding]')"
        )
    return backend


def reset() -> None:
    """Drop the cached model. For tests and for a model swap without a restart."""
    global _MODEL, _LOADED
    with _LOCK:
        _MODEL = None
        _LOADED = False


# --------------------------------------------------------------------------- #
# translating the library's answers onto this module's contract
# --------------------------------------------------------------------------- #


def _dtype(name: Any) -> str:
    return _DTYPES.get(str(name).lower(), "str")


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _heads_from_classification(raw: Any) -> dict[str, Any]:
    """Read `classify_text`'s answer into `{head: {label, confidence}}`.

    Handles the three shapes the model card documents: a single label as a bare
    string, a single label with confidence, and a multi-label list. Heads the
    model did not answer are simply absent.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    for head, value in raw.items():
        if isinstance(value, Mapping) and "label" in value:
            out[str(head)] = {"label": str(value["label"]),
                              "confidence": _f(value.get("confidence"))}
        elif isinstance(value, str):
            out[str(head)] = {"label": value, "confidence": None}
        elif isinstance(value, (list, tuple)):
            labels = []
            for item in value:
                if isinstance(item, Mapping) and "label" in item:
                    labels.append({"label": str(item["label"]),
                                   "confidence": _f(item.get("confidence"))})
                elif isinstance(item, str):
                    labels.append({"label": item, "confidence": None})
            out[str(head)] = labels
    return out


class _Adapter:
    """Maps the `gliner2` 2.0 API onto the `Backend` protocol."""

    def __init__(self, model: Any) -> None:
        self.model = model

    # -- classification --------------------------------------------------- #

    def classify_heads(self, text: str, heads: Mapping[str, Sequence[str]]) -> Mapping[str, Any]:
        """One `classify_text` call for every head — the model's whole point."""
        fn = getattr(self.model, "classify_text", None)
        if not callable(fn):
            return {}
        payload = {str(h): list(labels) for h, labels in heads.items()}
        for call in (
            lambda: fn(text, payload, include_confidence=True),
            lambda: fn(text, payload),
        ):
            try:
                raw = call()
            except TypeError:
                continue
            except Exception:
                return {}
            return _heads_from_classification(raw)
        return {}

    def classify(self, text: str, labels: Sequence[str]) -> Mapping[str, float]:
        heads = self.classify_heads(text, {"task": list(labels)})
        task = heads.get("task")
        if isinstance(task, Mapping) and "label" in task:
            return {str(task["label"]): task.get("confidence") or 1.0}
        if isinstance(task, list):
            return {str(t["label"]): (t.get("confidence") or 1.0)
                    for t in task if isinstance(t, Mapping) and "label" in t}
        return {}

    # -- extraction ------------------------------------------------------- #

    def extract(self, text: str, schema: Mapping[str, Any]) -> Mapping[str, Any]:
        if isinstance(schema, Mapping) and "entities" in schema:
            got = self._extract_entities(text, schema["entities"])
            if got:
                return got
        fn = getattr(self.model, "extract", None)
        if not callable(fn):
            return {}
        built = self._build_schema(schema)
        arg = built if built is not None else schema
        try:
            raw = fn(text, arg)
        except Exception:
            return {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _extract_entities(self, text: str, labels: Sequence[str]) -> dict[str, Any]:
        fn = getattr(self.model, "extract_entities", None)
        if not callable(fn):
            return {}
        try:
            raw = fn(text, list(labels))
        except Exception:
            return {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _build_schema(self, schema: Mapping[str, Any]):
        """Turn the declarative schema into the library's fluent builder.

        Single-structure by design: every schema this project uses has one
        top-level record type (`models`, `routing_request`). A multi-structure
        schema would need the builder's own composition rules, which is not a
        shape anything here needs yet.
        """
        create = getattr(self.model, "create_schema", None)
        if not callable(create):
            return None
        try:
            builder = create()
        except Exception:
            return None
        for name, spec in schema.items():
            if not isinstance(spec, list) or not spec or not isinstance(spec[0], Mapping):
                continue
            try:
                struct = builder.structure(name)
                for field, dtype in spec[0].items():
                    struct = struct.field(field, dtype=_dtype(dtype))
                return struct
            except Exception:
                return None
        return None


# --------------------------------------------------------------------------- #
# public surface
# --------------------------------------------------------------------------- #


def classify(
    prompt: str, tasks: Sequence[str], *, model: Any = _UNSET
) -> dict[str, float] | None:
    """`{task: confidence}` for the task(s) the model chose, or None.

    Raw confidences, not a normalised share — see the module docstring. A
    single-label answer returns one entry and keeps its own confidence.
    """
    backend = _resolve(model)
    if backend is None or not tasks:
        return None
    try:
        raw = dict(backend.classify(prompt, list(tasks)))
    except Exception:
        return None
    scores: dict[str, float] = {}
    for name in tasks:
        if name in raw:
            value = _f(raw[name])
            if value is not None:
                scores[name] = value
    if not scores or sum(scores.values()) <= 0:
        return None
    return scores


def pick_task(
    prompt: str, tasks: Sequence[str], *, model: Any = _UNSET
) -> tuple[str, float | None] | None:
    scores = classify(prompt, tasks, model=model)
    if not scores:
        return None
    task = max(scores, key=scores.get)
    return task, scores[task]


def decide(
    prompt: str,
    *,
    tasks: Sequence[str],
    providers: Sequence[str] | None = None,
    model: Any = _UNSET,
) -> dict[str, Any] | None:
    """Every routing head in one pass: task, provider, cost, context, style.

    This is the call the Decide checkpoint exists for — several label sets
    scored at once, each independent, no tokens generated. `providers` is passed
    by the caller (the registry knows them; this module does not) and the
    `provider` head is omitted when it is empty, so an empty registry cannot turn
    into a bogus "any" label.
    """
    backend = _resolve(model)
    if backend is None or not tasks:
        return None

    heads: dict[str, list[str]] = {
        "task": list(tasks),
        "cost": list(schemas.COST_LABELS),
        "context": list(schemas.CONTEXT_LABELS),
        "style": list(schemas.STYLE_LABELS),
    }
    if providers:
        heads["provider"] = list(providers)

    fn = getattr(backend, "classify_heads", None)
    if callable(fn):
        try:
            raw = dict(fn(prompt, heads))
        except Exception:
            return None
    else:
        # A backend with only single-head support still answers every head.
        raw = {}
        for head, labels in heads.items():
            got = classify(prompt, labels, model=backend) or {}
            if got:
                best = max(got, key=got.get)
                raw[head] = {"label": best, "confidence": got[best]}
    return raw or None


def extract(
    text: str, entities: Sequence[str], *, model: Any = _UNSET
) -> dict[str, Any] | None:
    """Flat entity spans, or None."""
    backend = _resolve(model)
    if backend is None:
        return None
    try:
        raw = backend.extract(text, {"entities": list(entities)})
    except Exception:
        return None
    return dict(raw) if isinstance(raw, Mapping) else None


def extract_record(
    text: str, schema: Mapping[str, Any], *, model: Any = _UNSET
) -> dict[str, Any] | None:
    """Schema-driven extraction (GLiNER2 structured records), or None."""
    backend = _resolve(model)
    if backend is None:
        return None
    try:
        raw = backend.extract(text, schema)
    except Exception:
        return None
    return dict(raw) if isinstance(raw, Mapping) else None


def extract_models(text: str, *, model: Any = _UNSET) -> dict[str, Any] | None:
    """Structured model records in the shape `agent_ingest.normalize_node` eats.

    Returns None unless at least one record carries a name, so a partial
    extraction falls through to the LLM node instead of committing a nameless
    row. The default `Decide` checkpoint is a classifier and will usually return
    nothing here; that is the intended fall-through, not a failure.
    """
    raw = extract_record(text, schemas.MODEL_RECORD_SCHEMA, model=model)
    if not raw:
        return None
    records = raw.get("models")
    if not isinstance(records, list):
        return None
    cleaned = [
        dict(r) for r in records
        if isinstance(r, Mapping) and str(r.get("model_name") or "").strip()
    ]
    if not cleaned:
        return None
    return {
        "source_type": "model_catalog",
        "models": cleaned,
        "notes": "gliner_local",
    }


def as_intent_backend(*, model: Any = _UNSET) -> Callable[[str, list[str]], str | None] | None:
    """A callable with `intent.classify(llm=...)`'s exact signature, or None.

    Returning None (rather than a callable that always fails) is what lets the
    proxy fall through to its configured LLM tie-break without a special case.
    """
    backend = _resolve(model)
    if backend is None:
        return None

    def ask(prompt: str, candidates: list[str]) -> str | None:
        picked = pick_task(prompt, candidates, model=backend)
        return picked[0] if picked else None

    return ask
