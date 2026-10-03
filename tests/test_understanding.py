"""The local understanding layer is a seam, and a seam has to be proven inert.

Two properties matter and neither is about the model's quality:

1. **Absent, it changes nothing.** No import cost, no error, no behaviour change
   in the proxy or the ingest graph. That is what lets the extra stay out of the
   default image — and what makes Phase 2.0 (CLOUD_ACTIVITY.md §2.0) safe to
   defer while Phase 1.0 ships.
2. **Present, it wires in.** A model is injected and the proxy tie-break, the
   `decide` heads and the ingest node actually use it — proven here with a fake,
   because the real `GLiNER2.5-Decide` checkpoint is ~2 GB and cannot be a test
   dependency (Phase 2.0a.7).

The adapter is additionally pinned against the **real** `gliner2` 2.0 call
shapes (`classify_text` / `create_schema().structure().field()` /
`extract_entities`), read from the published wheel and model card — so a future
API drift fails here rather than on a machine with torch installed.
"""
from __future__ import annotations

import pytest

import mininfer.understanding as und
import mininfer.understanding.gliner as gl


class FakeModel:
    """The `Backend` protocol, in-memory. No torch, no checkpoint."""

    def __init__(self, scores=None, records=None):
        self._scores = scores or {}
        self._records = records or {}

    def classify(self, text, labels=None):
        allowed = set(labels or [])
        return {k: v for k, v in self._scores.items() if not allowed or k in allowed}

    def extract(self, text, schema):
        return self._records


class FakeModelWithHeads(FakeModel):
    def __init__(self, heads=None, **kw):
        super().__init__(**kw)
        self._heads = heads or {}
        self.seen_heads: dict = {}

    def classify_heads(self, text, heads):
        self.seen_heads = dict(heads)
        return self._heads


@pytest.fixture
def loaded(monkeypatch):
    """Install a fake model as the loaded backend, bypassing `_build`."""
    def install(model):
        monkeypatch.setattr(gl, "_MODEL", model, raising=False)
        monkeypatch.setattr(gl, "_LOADED", True, raising=False)
        return model
    yield install
    gl.reset()


def _unavailable(monkeypatch):
    monkeypatch.setattr(gl, "_MODEL", None, raising=False)
    monkeypatch.setattr(gl, "_LOADED", True, raising=False)


# --------------------------------------------------------------------------- #
# absent: inert
# --------------------------------------------------------------------------- #


def test_absent_extra_is_inert(monkeypatch):
    _unavailable(monkeypatch)
    assert gl.available() is False
    assert gl.backend_name() == "none"
    assert gl.classify("hi", ["general_chat"]) is None
    assert gl.pick_task("hi", ["general_chat"]) is None
    assert gl.decide("hi", tasks=["general_chat"]) is None
    assert gl.extract("hi", ["model"]) is None
    assert gl.extract_record("hi", {"m": [{"f": "string"}]}) is None
    assert gl.extract_models("hi") is None
    assert gl.as_intent_backend() is None


def test_require_raises_with_a_usable_message(monkeypatch):
    _unavailable(monkeypatch)
    with pytest.raises(gl.UnderstandingUnavailable) as exc:
        gl.require()
    assert "understanding" in str(exc.value)


# --------------------------------------------------------------------------- #
# present: raw confidences, not a normalised share
# --------------------------------------------------------------------------- #


def test_classify_reports_raw_confidences(loaded):
    """The card warns scores are not a probability distribution — do not touch."""
    loaded(FakeModel(scores={"code_edit": 3.0, "general_chat": 1.0}))
    assert gl.classify("anything", ["code_edit", "general_chat"]) == {
        "code_edit": 3.0, "general_chat": 1.0}


def test_classify_ignores_a_label_the_model_did_not_choose(loaded):
    loaded(FakeModel(scores={"code_edit": 0.9}))
    assert gl.classify("x", ["code_edit", "general_chat"]) == {"code_edit": 0.9}


def test_zero_scores_are_not_a_confident_answer(loaded):
    loaded(FakeModel(scores={"code_edit": 0.0, "general_chat": 0.0}))
    assert gl.classify("anything", ["code_edit", "general_chat"]) is None


def test_pick_task_returns_the_winner_and_its_confidence(loaded):
    loaded(FakeModel(scores={"sql_generation": 0.82, "code_edit": 0.11}))
    assert gl.pick_task("x", ["sql_generation", "code_edit"]) == ("sql_generation", 0.82)


def test_extract_models_drops_a_nameless_record(loaded):
    loaded(FakeModel(records={"models": [
        {"model_name": "Gemini 3.7 Flash", "provider": "google"},
        {"provider": "openrouter"},  # no name: must not become a row
    ]}))
    out = gl.extract_models("text")
    assert [m["model_name"] for m in out["models"]] == ["Gemini 3.7 Flash"]
    assert out["notes"] == "gliner_local"


def test_extract_models_is_none_when_nothing_is_named(loaded):
    loaded(FakeModel(records={"models": [{"provider": "google"}]}))
    assert gl.extract_models("text") is None


def test_the_backend_satisfies_the_intent_llm_signature(loaded):
    """It must drop into `intent.classify(llm=...)` without an adapter."""
    from mininfer.schema import TaskProfile
    from mininfer import intent as intent_mod

    loaded(FakeModel(scores={"sql_generation": 0.9, "general_chat": 0.05}))
    ask = gl.as_intent_backend()
    assert ask is not None

    tasks = {
        "general_chat": TaskProfile(name="general_chat", tokens_in=100, tokens_out=100),
        "sql_generation": TaskProfile(name="sql_generation", tokens_in=100, tokens_out=100),
    }
    # No cues match, so the tie-break is the only thing that can answer.
    res = intent_mod.classify("ambiguous", tasks, default="general_chat", llm=ask)
    assert res.task == "sql_generation"
    assert res.source == "llm"


# --------------------------------------------------------------------------- #
# decide: the multi-head routing call
# --------------------------------------------------------------------------- #


def test_decide_returns_every_head_and_passes_providers(loaded):
    model = FakeModelWithHeads(heads={
        "task": {"label": "sql_generation", "confidence": 0.9},
        "cost": {"label": "free", "confidence": 0.7},
    })
    loaded(model)
    out = gl.decide("fix this query", tasks=["sql_generation", "general_chat"],
                    providers=["groq", "openrouter"])
    assert out["task"]["label"] == "sql_generation"
    assert out["cost"]["label"] == "free"
    # The classifier is told every head at once — that is the model's point.
    assert model.seen_heads["task"] == ["sql_generation", "general_chat"]
    assert model.seen_heads["provider"] == ["groq", "openrouter"]
    assert "cost" in model.seen_heads and "context" in model.seen_heads


def test_decide_omits_the_provider_head_when_none_are_known(loaded):
    model = FakeModelWithHeads(heads={"task": {"label": "general_chat", "confidence": 0.5}})
    loaded(model)
    gl.decide("hi", tasks=["general_chat"])
    assert "provider" not in model.seen_heads


def test_decide_falls_back_to_single_head_classify(loaded):
    """A backend without `classify_heads` still answers every head."""
    loaded(FakeModel(scores={"general_chat": 0.6}))
    out = gl.decide("hi", tasks=["general_chat"])
    assert out["task"] == {"label": "general_chat", "confidence": 0.6}


# --------------------------------------------------------------------------- #
# the adapter against the real gliner2 2.0 call shapes
# --------------------------------------------------------------------------- #


class FakeSchemaBuilder:
    def __init__(self):
        self.chain: list[tuple] = []

    def structure(self, name):
        self.chain.append(("structure", name))
        return self

    def field(self, name, dtype=None):
        self.chain.append(("field", name, dtype))
        return self


class FakeGLiNER:
    """Mimics `gliner2.AutoExtractor` as the model card documents it."""

    def __init__(self, label="sql_generation", confidence=0.82, records=None,
                 entities=None, with_confidence=True):
        self.label = label
        self.confidence = confidence
        self.records = records or {}
        self.entities = entities or {}
        self.with_confidence = with_confidence
        self.schema_builder = FakeSchemaBuilder()

    def classify_text(self, text, schema, include_confidence=False):
        out = {}
        for head in schema:
            if include_confidence and self.with_confidence:
                out[head] = {"label": self.label, "confidence": self.confidence}
            else:
                out[head] = self.label
        return out

    def extract(self, text, schema):
        return self.records

    def extract_entities(self, text, labels):
        return {"entities": self.entities}

    def create_schema(self):
        return self.schema_builder


def test_adapter_reads_a_confident_single_label():
    got = gl._Adapter(FakeGLiNER(label="refund_request", confidence=0.88)).classify(
        "t", ["order_status", "refund_request"])
    assert got == {"refund_request": 0.88}


def test_adapter_reads_a_bare_string_label():
    got = gl._Adapter(FakeGLiNER(label="cancel", with_confidence=False)).classify(
        "t", ["book", "cancel"])
    assert got == {"cancel": 1.0}


def test_adapter_builds_the_fluent_schema():
    model = FakeGLiNER()
    built = gl._Adapter(model)._build_schema({"models": [
        {"model_name": "string", "context_window": "integer", "free": "boolean"}]})
    assert built is model.schema_builder
    assert model.schema_builder.chain == [
        ("structure", "models"),
        ("field", "model_name", "str"),
        ("field", "context_window", "int"),
        ("field", "free", "bool"),
    ]


def test_adapter_uses_the_entity_path_for_a_flat_schema():
    model = FakeGLiNER(entities={"model": ["Gemini 3.7 Flash"]})
    got = gl._Adapter(model).extract("t", {"entities": ["model", "provider"]})
    assert got == {"entities": {"model": ["Gemini 3.7 Flash"]}}


# --------------------------------------------------------------------------- #
# proxy wiring
# --------------------------------------------------------------------------- #


def test_proxy_defaults_to_no_tiebreak(monkeypatch):
    import mininfer.proxy as px
    monkeypatch.setattr(px, "INTENT_BACKEND", "")
    monkeypatch.setattr(px, "INTENT_MODEL", "")
    assert px._intent_llm() is None


def test_proxy_prefers_the_local_backend(monkeypatch, loaded):
    import mininfer.proxy as px
    loaded(FakeModel(scores={"code_edit": 0.9, "general_chat": 0.1}))
    monkeypatch.setattr(px, "INTENT_BACKEND", "gliner")
    monkeypatch.setattr(px, "INTENT_MODEL", "")
    ask = px._intent_llm()
    assert ask is not None
    assert ask("fix this sql", ["code_edit", "general_chat"]) == "code_edit"


def test_proxy_falls_through_when_the_local_backend_is_missing(monkeypatch):
    """`MI_INTENT_BACKEND=gliner` with no model must not break the request."""
    import mininfer.proxy as px
    _unavailable(monkeypatch)
    monkeypatch.setattr(px, "INTENT_BACKEND", "gliner")
    monkeypatch.setattr(px, "INTENT_MODEL", "")
    assert px._intent_llm() is None


# --------------------------------------------------------------------------- #
# ingest wiring
# --------------------------------------------------------------------------- #


def test_ingest_node_is_off_by_default(monkeypatch):
    import mininfer.agent_ingest as ai
    monkeypatch.setattr(ai, "EXTRACT_BACKEND", "")
    assert ai.understanding_node({"text": "anything"}) == {}


def test_ingest_node_extracts_and_makes_the_llm_node_a_no_op(monkeypatch, loaded):
    import mininfer.agent_ingest as ai
    loaded(FakeModel(records={"models": [
        {"model_name": "Gemini 3.7 Flash", "provider": "google"},
    ]}))
    monkeypatch.setattr(ai, "EXTRACT_BACKEND", "gliner")
    out = ai.understanding_node({"text": "Google announces Gemini 3.7 Flash"})
    assert out["extraction"]["models"][0]["model_name"] == "Gemini 3.7 Flash"
    assert out["source_type"] == "model_catalog"
    # The LLM node now has nothing to do: the local extraction is authoritative
    # for this run and the existing normalize/validate path consumes it unchanged.
    assert ai.llm_extract_node({"extraction": out["extraction"]}) == {}


def test_ingest_node_falls_through_when_the_model_finds_nothing(monkeypatch, loaded):
    import mininfer.agent_ingest as ai
    loaded(FakeModel(records={"models": []}))
    monkeypatch.setattr(ai, "EXTRACT_BACKEND", "gliner")
    assert ai.understanding_node({"text": "no models here"}) == {}


def test_ingest_node_never_runs_after_an_error(monkeypatch, loaded):
    import mininfer.agent_ingest as ai
    loaded(FakeModel(records={"models": [{"model_name": "x"}]}))
    monkeypatch.setattr(ai, "EXTRACT_BACKEND", "gliner")
    assert ai.understanding_node({"text": "t", "error": "fetch failed"}) == {}


def test_the_graph_contains_the_understanding_stage():
    import mininfer.agent_ingest as ai
    nodes = set(ai.build_graph().get_graph().nodes)
    assert "understanding" in nodes
    assert "llm_extract" in nodes


# --------------------------------------------------------------------------- #
# the default checkpoint is the one we chose
# --------------------------------------------------------------------------- #


def test_default_model_is_the_decide_classifier():
    assert gl.DEFAULT_MODEL == "fastino/GLiNER2.5-Decide"
    # The heads the CLI relies on are declared, and provider stays dynamic.
    assert "free" in und.schemas.COST_LABELS
    assert "any" in und.schemas.STYLE_LABELS
