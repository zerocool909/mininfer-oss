"""Tests for the parts that must not silently drift: normalisation, statistics,
cost arithmetic, and the diversity constraint.

These are the regression surface. A wrong price scale or a bad merge corrupts
every routing decision downstream and produces no error — so they are tested
explicitly rather than discovered in production.
"""
from __future__ import annotations

import pytest

from mininfer.normalize import normalize, params_b_from_name, weights_id_for
from mininfer.router import Policy, _cost_per_call, route, vendor_of, wilson_lb
from mininfer.schema import TaskProfile, make_deploy_id


# --------------------------------------------------------------- normalisation


@pytest.mark.parametrize("raw,expected", [
    ("Qwen/Qwen3-30B-A3B-Instruct-2507", "qwen3-30b-a3b-instruct-2507"),
    ("qwen/qwen3-30b-a3b:free", "qwen3-30b-a3b"),
    ("alibaba/qwen-3-14b", "qwen-3-14b"),
    ("poolside/laguna-s-2.1-free", "laguna-s-2-1"),
    ("nvidia/Gemma-4-31B-IT-NVFP4", "gemma-4-31b-it"),
    ("google/gemma-4-31B-turbo-TEE", "gemma-4-31b"),
])
def test_normalize_strips_serving_noise_only(raw, expected):
    assert normalize(raw) == expected


def test_normalize_keeps_identity_distinguishing_tokens():
    """`thinking` is a different fine-tune, not serving noise."""
    assert "thinking" in normalize("qwen/qwen3-30b-a3b-thinking-2507")
    assert normalize("a/thing-2507") != normalize("a/thing-2508")


def test_weights_id_prefers_hugging_face_repo():
    assert weights_id_for("Qwen/Qwen3-30B-A3B", "whatever") == "hf:qwen/qwen3-30b-a3b"
    assert weights_id_for(None, "Qwen/Qwen3-30B-A3B") == "slug:qwen3-30b-a3b"


def test_params_parsed_from_name():
    assert params_b_from_name("Qwen3-30B-A3B-Instruct") == 30.0
    assert params_b_from_name("llama-3.1-8b") == 8.0
    assert params_b_from_name("gpt-oss-20b") == 20.0
    assert params_b_from_name("no-params-here") is None


# ------------------------------------------------------------------ statistics


def test_wilson_lower_bound_punishes_small_samples():
    """3/3 must not outrank 300/400 — that is the whole point of the bound."""
    assert wilson_lb(3, 3) < wilson_lb(300, 400)


def test_wilson_lower_bound_monotone_in_n():
    assert wilson_lb(5, 10) < wilson_lb(50, 100) < wilson_lb(500, 1000)


def test_wilson_zero_observations_is_zero():
    assert wilson_lb(0, 0) == 0.0


# ------------------------------------------------------------------- economics


def test_unknown_price_is_not_free():
    t = TaskProfile("x", tokens_in=1000, tokens_out=500)
    assert _cost_per_call(None, None, t) == float("inf")
    assert _cost_per_call(0.0, 0.0, t) == 0.0


def test_cost_per_call_uses_per_million_units():
    t = TaskProfile("x", tokens_in=1_000_000, tokens_out=1_000_000)
    assert _cost_per_call(2.0, 8.0, t) == pytest.approx(10.0)


def test_retries_increase_cost_per_success():
    t = TaskProfile("x", tokens_in=1000, tokens_out=100)
    sure = _cost_per_call(1.0, 1.0, t) * t.estimated_calls(0.99)
    flaky = _cost_per_call(1.0, 1.0, t) * t.estimated_calls(0.5)
    assert flaky > sure * 1.5


def test_retries_are_capped():
    """A hopeless arm must not produce an infinite cost that breaks sorting."""
    t = TaskProfile("x", tokens_in=10, tokens_out=10)
    assert t.estimated_calls(0.0) == 4.0
    assert t.estimated_calls(0.01) == 4.0


# --------------------------------------------------------------------- policy


def test_policy_fields_are_explicit_not_magic_numbers():
    p = Policy()
    assert p.objective == "cost_per_success"
    assert p.require_distinct_provider is True
    assert p.allow_unknown_price is False, "unknown price must opt in, never default"


def test_deploy_id_is_provider_scoped():
    assert make_deploy_id("openrouter", "qwen/x") == "openrouter:qwen/x"
    assert make_deploy_id("openrouter/streamlake", "qwen/x") == "openrouter/streamlake:qwen/x"


# -------------------------------------------------------- entity resolution


def test_param_count_is_identity_not_noise():
    """Regression: stripping param tokens merged llama-3.1-8b into llama-3.1-70b."""
    from mininfer.resolve import _key

    assert _key("meta-llama-3.1-8b-instruct", None) != _key("meta-llama-3.1-70b-instruct", None)
    assert _key("llama-4-maverick-17b-128e-instruct", None) != _key("llama-4-scout-17b-16e", None)


def test_quantisation_variants_do_share_a_key():
    """bf16 / fp8 / nvfp4 are serving choices over the same artifact."""
    from mininfer.resolve import _key

    a = _key("nvidia-nemotron-3-ultra-550b-a55b-bf16", None)
    b = _key("nvidia-nemotron-3-ultra-550b-a55b-nvfp4", None)
    assert a == b


def test_moe_active_params_are_identity():
    from mininfer.resolve import _key

    assert _key("qwen3-30b-a3b", None) != _key("qwen3-30b", None)


def test_merge_guard_refuses_on_param_mismatch(tmp_path):
    import sqlite3

    from mininfer.schema import Weights
    from mininfer.store import Store

    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("hf:a/m-8b", "m-8b", params_b=8.0))
    s.upsert_weights(Weights("hf:a/m-70b", "m-70b", params_b=70.0))
    assert s.merge_weights("hf:a/m-8b", "hf:a/m-70b", "test", 1.0) == 0
    assert not s.merged_ok("hf:a/m-8b")
    assert s.conn.execute("SELECT COUNT(*) c FROM quarantine").fetchone()["c"] == 1
    s.close()


def test_merge_guard_allows_matching_params(tmp_path):
    from mininfer.schema import Deployment, Weights
    from mininfer.store import Store

    s = Store(tmp_path / "t.db")
    for wid in ("hf:a/m-bf16", "hf:a/m-nvfp4"):
        s.upsert_weights(Weights(wid, "m-550b", params_b=550.0))
    s.upsert_deployment(Deployment("p:x", "hf:a/m-bf16", "p", "x"))
    moved = s.merge_weights("hf:a/m-bf16", "hf:a/m-nvfp4", "quant variant", 1.0)
    assert moved == 1
    assert s.conn.execute(
        "SELECT weights_id FROM deployments WHERE deploy_id='p:x'").fetchone()[0] == "hf:a/m-nvfp4"
    s.close()


def test_price_sentinel_is_unknown_not_free():
    from mininfer.ingest import per_mtok_from_per_token, price_sentinel

    assert price_sentinel("-1")
    assert per_mtok_from_per_token("-1") is None
    assert per_mtok_from_per_token("0") == 0.0


def test_a_paid_subscription_is_not_free(tmp_path):
    """A monthly plan is a sunk cost, not a free tier.

    Featherless listed 22k models as `subscription=True` alongside real prices
    (`0.1`/`0.2` per Mtok). The router zeroed their cost and counted them
    free-eligible, so a paid plan won on a cost nobody was paying. It is priced
    at list now, and only an operator who confirms the subscription gets the
    zero-marginal-cost shortcut.
    """
    from mininfer.router import build_candidates
    from mininfer.schema import Deployment, TaskProfile, Weights
    from mininfer.store import Store

    store = Store(tmp_path / "sub.db")
    store.upsert_weights(Weights("hf:a/m", "m"))
    store.upsert_deployment(Deployment(
        "featherless:m", "hf:a/m", "featherless", "m",
        price_in=0.1, price_out=0.2, subscription=True))
    task = TaskProfile(name="general_chat", tokens_in=1000, tokens_out=400)

    unowned = build_candidates(store, task, Policy())
    assert unowned, "a subscription arm is still a candidate"
    assert unowned[0].free_kind is None, "a subscription is not free by default"
    assert unowned[0].cost_per_call > 0, "it must be priced at list, not zeroed"

    owned = build_candidates(store, task, Policy(subscription_is_free=True))
    assert owned[0].free_kind == "subscription"
    assert owned[0].cost_per_call == 0.0
    store.close()


# ------------------------------------------------------- fallback diversity


@pytest.mark.parametrize("provider,gateway,upstream", [
    ("openrouter", "openrouter", "openrouter"),
    ("hf", "hf", "hf"),
    ("deepinfra", "deepinfra", "deepinfra"),
    # same upstream reached two ways -> must NOT count as independent fallbacks
    ("hf/deepinfra", "hf", "deepinfra"),
    ("openrouter/azure/us", "openrouter", "azure"),
    ("openrouter/google-vertex/global", "openrouter", "google"),
    ("openrouter/google-ai-studio", "openrouter", "google"),
    ("vercel", "vercel", "vercel"),
])
def test_provider_axes(provider, gateway, upstream):
    from mininfer.router import provider_axes

    assert provider_axes(provider) == (gateway, upstream)


def test_same_upstream_via_gateway_and_direct_are_not_independent():
    from mininfer.router import provider_axes

    assert provider_axes("deepinfra")[1] == provider_axes("hf/deepinfra")[1]


# ------------------------------------------------------------ quota economics


def test_capability_is_three_valued():
    from mininfer.ingest import caps_from_params

    unknown = caps_from_params(None)
    assert unknown["tools"] is None and unknown["structured"] is None, \
        "an unreported capability must not be coerced to False"

    declared = caps_from_params(["tools", "response_format"])
    assert declared == {"tools": True, "structured": True, "vision": None,
                        "reasoning": False, "caching": False, "audio": None}


def test_prior_success_blends_leaderboards():
    """A second leaderboard must move the prior, not be ignored.

    The old first-match behaviour returned as soon as one key was present, so
    adding a Design Arena score changed nothing. A weighted mean means every
    source contributes in proportion to its weight.
    """
    from mininfer.router import prior_success

    norms = {"aa_intelligence": (0.0, 100.0), "design_arena_elo": (0.0, 100.0)}
    bench = {"aa_intelligence": 100.0, "design_arena_elo": 0.0}

    p_both, _lead, keys = prior_success(
        bench, ("aa_intelligence", "design_arena_elo"), norms)
    assert set(keys) == {"aa_intelligence", "design_arena_elo"}
    assert abs(p_both - (0.30 + 0.65 * 0.5)) < 1e-9

    p_top, _, _ = prior_success(bench, ("aa_intelligence",), norms)
    assert p_both < p_top                      # the weak second source pulls it down

    p_weighted, _, _ = prior_success(
        bench, ("aa_intelligence", "design_arena_elo"), norms,
        weights={"aa_intelligence": 3.0})
    assert p_top > p_weighted > p_both         # heavier weight on the strong source

    assert prior_success({}, ("aa_intelligence",), norms)[1] == "none"


# --------------------------------------------------------------------------- #
# vendor — who made the model, as opposed to who serves it
# --------------------------------------------------------------------------- #


def test_vendor_of_reads_the_model_namespace():
    """The model id is the only place the maker is recorded.

    `provider` holds the gateway, so an axis built from the upstream would report
    'openrouter' for all ~400 rows and enforce nothing.
    """
    assert vendor_of("google/gemini-3.8-flash", "openrouter") == "google"
    assert vendor_of("z-ai/glm-5.3-prime", "openrouter") == "z-ai"
    assert vendor_of("qwen/qwen3.8-max-prime", "openrouter") == "qwen"
    # gateway-embedded namespaces canonicalise, so both Google routes are one maker
    assert vendor_of("google-ai-studio/gemini-3.8", "openrouter") == "google"


def test_vendor_of_falls_back_to_the_upstream_without_a_namespace():
    assert vendor_of("space-bunny-alpha", "openrouter") == "openrouter"
    assert vendor_of(None, "novita") == "novita"
    assert vendor_of("", "deepinfra") == "deepinfra"


def test_candidates_carry_their_maker(tmp_path):
    """`provider` is the gateway for every row; `vendor` is the model's maker."""
    from mininfer.router import build_candidates
    from mininfer.schema import Deployment, TaskProfile, Weights
    from mininfer.store import Store

    store = Store(tmp_path / "v.db")
    store.upsert_weights(Weights("hf:g/gem", "gemini-3.8", params_b=7.0))
    store.upsert_weights(Weights("hf:z/glm", "glm-5.3", params_b=7.0))
    store.upsert_deployment(Deployment("openrouter:google/gemini-3.8", "hf:g/gem",
                                       "openrouter", "google/gemini-3.8",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.upsert_deployment(Deployment("openrouter:z-ai/glm-5.3", "hf:z/glm",
                                       "openrouter", "z-ai/glm-5.3",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.commit()

    task = TaskProfile(name="t", tokens_in=10, tokens_out=10)
    pol = Policy(min_context=0, on_unverified_capability="allow")
    got = {c.deploy_id: c.vendor for c in build_candidates(store, task, pol)}
    assert got == {"openrouter:google/gemini-3.8": "google",
                   "openrouter:z-ai/glm-5.3": "z-ai"}
    # upstream is the gateway for both — which is why vendor had to exist
    assert {c.upstream for c in build_candidates(store, task, pol)} == {"openrouter"}
    store.close()


# --------------------------------------------------------------------------- #
# why — the decision has to explain itself
# --------------------------------------------------------------------------- #


def _seeded(tmp_path, **deploys):
    """A store with the given deployments, all benchmark-free unless tagged."""
    from mininfer.schema import Deployment, Weights
    from mininfer.store import Store

    store = Store(tmp_path / "w.db")
    for name, kw in deploys.items():
        w = f"hf:{name}"
        store.upsert_weights(Weights(w, name, params_b=kw.pop("params_b", 7.0),
                                     benchmark=kw.pop("benchmark", None)))
        store.upsert_deployment(Deployment(f"openrouter:{name}", w, "openrouter", name,
                                           context_window=1000, **kw))
    store.commit()
    return store


def test_why_names_the_reason_a_free_arm_won(tmp_path):
    from mininfer.router import Policy, route

    store = _seeded(tmp_path, free={"price_in": 0.0, "price_out": 0.0, "zero_price": True},
                    paid={"price_in": 5.0, "price_out": 5.0})
    dec = route(store, TaskProfile(name="t", tokens_in=10, tokens_out=10),
                Policy(min_context=0, on_unverified_capability="allow",
                       objective="cost_per_success", free_floor_exempt=True,
                       min_success_lb=0.0))
    keys = {w["key"] for w in dec.why}
    assert "cheapest" in keys and "free" in keys
    assert "on-trial" in keys
    # and the claim is checkable, not decorative
    assert dec.chosen[0].deploy_id == "openrouter:free"
    detail = next(w for w in dec.why if w["key"] == "on-trial")["detail"]
    assert "0 of 3" in detail
    store.close()


def test_why_flags_a_model_nothing_has_measured(tmp_path):
    """'Cheapest, and no evidence' is a caveat, so the log must say it."""
    from mininfer.router import Policy, route

    store = _seeded(tmp_path, a={"price_in": 1.0, "price_out": 1.0},
                    b={"price_in": 2.0, "price_out": 2.0})
    dec = route(store, TaskProfile(name="t", tokens_in=10, tokens_out=10),
                Policy(min_context=0, on_unverified_capability="allow",
                       objective="cost_per_success", min_success_lb=0.0))
    assert "unbenchmarked" in {w["key"] for w in dec.why}
    assert "leaderboard" not in {w["key"] for w in dec.why}
    store.close()


def test_why_quotes_the_leaderboard_that_fed_the_prior(tmp_path):
    from mininfer.router import Policy, route

    store = _seeded(tmp_path,
                    bench={"price_in": 1.0, "price_out": 1.0,
                           "benchmark": {"coding": 91.5}})
    task = TaskProfile(name="t", tokens_in=10, tokens_out=10, benchmark_keys=("coding",))
    dec = route(store, task, Policy(min_context=0, on_unverified_capability="allow",
                                    objective="cost_per_success", min_success_lb=0.0))
    tag = next(w for w in dec.why if w["key"] == "leaderboard")
    assert "91.5" in tag["label"]
    assert tag["detail"]  # which source it came from
    store.close()


def test_why_on_an_empty_decision_is_empty(tmp_path):
    from mininfer.router import Policy, route

    store = _seeded(tmp_path, a={"price_in": 1.0, "price_out": 1.0})
    dec = route(store, TaskProfile(name="t", tokens_in=10, tokens_out=10, min_context=99999),
                Policy(min_context=0, on_unverified_capability="allow",
                       objective="cost_per_success", min_success_lb=0.0))
    assert dec.chosen == [] and dec.why == []
    store.close()


# --------------------------------------------------------------------------- #
# unverified capabilities, per task
# --------------------------------------------------------------------------- #
# `caps.tools` is null for 22,655 of 23,799 deployments, so a `require` against it
# does nothing unless unknown is treated as disqualifying. But the cost of that
# setting is not uniform: measured on the shipped registry it costs 5 arms for
# `tools`/`structured` and 108 of 161 for `vision`. Hence per-task.


def _cap_store(tmp_path, caps: dict | None):
    from mininfer.schema import Deployment, Weights
    from mininfer.store import Store

    store = Store(tmp_path / "cap.db")
    store.upsert_weights(Weights("hf:x/m", "m", params_b=7.0))
    store.upsert_deployment(Deployment("openrouter:m", "hf:x/m", "openrouter", "m",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    # `upsert_deployment` takes caps through the Deployment, so set them directly
    import json
    store.conn.execute("UPDATE deployments SET caps=? WHERE deploy_id=?",
                       (json.dumps(caps or {}), "openrouter:m"))
    store.commit()
    return store


def _req_task(**kw):
    from mininfer.schema import TaskProfile
    return TaskProfile(name="t", tokens_in=10, tokens_out=10, **kw)


def test_an_unreported_capability_is_disqualifying_when_the_task_says_so(tmp_path):
    store = _cap_store(tmp_path, {"tools": None})
    task = _req_task(require={"tools": True}, on_unverified_capability="reject")
    pol = Policy(min_context=0, on_unverified_capability="allow")
    dec = route(store, task, pol)
    assert dec.eligible == []
    assert dec.funnel["rejected_unverified"] == 1
    store.close()


def test_the_task_overrides_the_policy(tmp_path):
    """The capability decides, not a house style."""
    store = _cap_store(tmp_path, {"tools": None})
    strict = _req_task(require={"tools": True}, on_unverified_capability="reject")
    permissive = _req_task(require={"tools": True}, on_unverified_capability="allow")

    # policy says allow, task says reject -> reject wins
    assert route(store, strict, Policy(min_context=0,
                                       on_unverified_capability="allow")).eligible == []
    # policy says reject, task says allow -> allow wins
    assert len(route(store, permissive,
                     Policy(min_context=0,
                            on_unverified_capability="reject")).eligible) == 1
    store.close()


def test_a_task_without_an_override_inherits_the_policy(tmp_path):
    store = _cap_store(tmp_path, {"tools": None})
    task = _req_task(require={"tools": True})          # no per-task setting
    assert route(store, task, Policy(min_context=0,
                                     on_unverified_capability="reject")).eligible == []
    assert len(route(store, task, Policy(min_context=0,
                                         on_unverified_capability="allow")).eligible) == 1
    store.close()


def test_a_capability_reported_false_is_rejected_either_way(tmp_path):
    """The stricter setting must not change the meaning of a known `false`."""
    store = _cap_store(tmp_path, {"tools": False})
    for mode in ("allow", "reject"):
        dec = route(store, _req_task(require={"tools": True}),
                    Policy(min_context=0, on_unverified_capability=mode))
        assert dec.eligible == [], mode
        assert dec.funnel["rejected_unsupported"] == 1, mode
    store.close()


def test_the_shipped_policy_gates_the_tasks_where_silence_is_the_failure(tmp_path):
    """A config regression guard, because this reverts invisibly.

    `reject` where a missing capability fails silently (a model that cannot emit a
    tool call just does not call it) and `allow` where it costs two thirds of the
    pool for no change in the chosen model.
    """
    import pathlib
    from mininfer.router import Policy

    root = pathlib.Path(__file__).resolve().parent.parent
    _, tasks = Policy.load(root / "config" / "policy.yaml")

    for name in ("code_edit", "agent_tools", "sql_generation", "extraction"):
        assert tasks[name].on_unverified_capability == "reject", name
    # vision: measured at 108 of 161 arms lost, chosen model unchanged
    assert tasks["shelf_image_audit"].on_unverified_capability is None
    # and tasks that require nothing are untouched either way
    for name in ("general_chat", "summarise", "hard_reasoning"):
        assert not tasks[name].require
