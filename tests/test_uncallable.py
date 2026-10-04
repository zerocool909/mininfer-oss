"""A provider that serves a model only to allowlisted apps.

OpenRouter publishes some `:free` models to agentic harnesses only and answers a
plain API client with 401 "only available on agentic harnesses". Two things make
this need its own handling rather than the existing `auth_error`:

* **It is evidence about the deployment, not about our key.** `auth_error` is in
  `NON_MODEL_ERRORS` because a bad key says nothing about a model — so leaving this
  classified as one would exclude it from the statistics *and* leave the arm being
  retried on every request, forever.
* **It cannot be filtered at ingest.** `thinkingmachines/inkling-small:free` looks
  exactly like any other `:free` model in `/api/v1/models`: `pricing {0,0}`,
  `is_moderated: false`, no restriction field. The restriction exists only at call
  time, so the retirement has to be discovered by calling and has to *stick*.
"""
from __future__ import annotations

import pytest

from mininfer import execute, proxy, schema
from mininfer.router import Policy, build_candidates
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store

AGENTIC = "openrouter:thinkingmachines/inkling-small:free"
DETAIL = ("thinkingmachines/inkling-small:free is only available on agentic harnesses. "
          "Try plugging it into a coding agent or productivity app listed on "
          "https://openrouter.ai/apps")


def _seed(tmp_path) -> Store:
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w"))
    s.upsert_deployment(Deployment(AGENTIC, "w", "openrouter",
                                   "thinkingmachines/inkling-small:free"))
    s.upsert_deployment(Deployment("openrouter:good", "w", "openrouter", "good"))
    s.commit()
    return s


def _row(s: Store, deploy_id: str) -> dict:
    return dict(s.conn.execute(
        "SELECT status, status_source, status_reason FROM deployments WHERE deploy_id=?",
        (deploy_id,),
    ).fetchone())


# ------------------------------------------------------------- classification


def test_the_agentic_harness_error_gets_its_own_class():
    err = execute._refine_error_class(execute._classify(401), DETAIL)
    assert err == "not_api_callable"


def test_a_plain_auth_failure_stays_an_auth_error():
    """The distinction that matters: a revoked key is ours to fix, and must not
    retire a model from the registry."""
    for detail in ("No auth credentials found", "invalid api key",
                   "your key does not have access to this model"):
        assert execute._refine_error_class(execute._classify(401), detail) == "auth_error"


def test_the_marker_match_is_case_insensitive():
    err = execute._refine_error_class("auth_error", "ONLY AVAILABLE ON AGENTIC HARNESSES")
    assert err == "not_api_callable"


def test_a_non_auth_error_is_left_alone():
    assert execute._refine_error_class("429", DETAIL) == "429"
    assert execute._refine_error_class("5xx", DETAIL) == "5xx"
    assert execute._refine_error_class(None, DETAIL) is None


def test_an_empty_detail_is_left_alone():
    assert execute._refine_error_class("auth_error", None) == "auth_error"
    assert execute._refine_error_class("auth_error", "") == "auth_error"


def test_it_is_excluded_from_the_models_quality_statistics():
    """The arm may be excellent; we simply cannot call it. Counting the failure as
    the model's would train the router against a model that never answered."""
    assert "not_api_callable" in schema.NON_MODEL_ERRORS


def test_the_observation_does_not_count_as_a_model_loss(tmp_path):
    s = _seed(tmp_path)
    s.observe(AGENTIC, "gen", ok=False, ts="2026-10-02T00:00:00+00:00",
              error_class="not_api_callable")
    s.commit()
    row = s.conn.execute(
        "SELECT n, wins FROM routing_stats WHERE deploy_id=?", (AGENTIC,)).fetchone()
    assert row["n"] == 0                       # excluded from the denominator
    s.close()


# ---------------------------------------------------------------- retirement


def test_the_proxy_retires_an_uncallable_arm(tmp_path):
    s = _seed(tmp_path)
    assert proxy._retire_if_uncallable(s, AGENTIC, error_class="not_api_callable",
                                       error_detail=DETAIL) is True
    s.commit()
    assert _row(s, AGENTIC)["status"] == "deprecated"
    s.close()


def test_the_proxy_leaves_other_errors_alone(tmp_path):
    s = _seed(tmp_path)
    for err in ("auth_error", "429", "network_error", None):
        assert proxy._retire_if_uncallable(s, AGENTIC, error_class=err,
                                           error_detail=DETAIL) is False
    s.commit()
    assert _row(s, AGENTIC)["status"] == "live"
    s.close()


def test_the_retirement_records_the_providers_own_message(tmp_path):
    """So an operator reads why, not just that it went away."""
    s = _seed(tmp_path)
    proxy._retire_if_uncallable(s, AGENTIC, error_class="not_api_callable",
                                error_detail=DETAIL)
    s.commit()
    assert "agentic harnesses" in _row(s, AGENTIC)["status_reason"]
    s.close()


def test_a_retired_arm_leaves_the_ranking(tmp_path):
    s = _seed(tmp_path)
    s.retire_deployment(AGENTIC, DETAIL)
    s.commit()
    task = TaskProfile("t", tokens_in=100, tokens_out=100)
    cands = build_candidates(s, task, Policy(require_callable=False))
    retired = next(c for c in cands if c.deploy_id == AGENTIC)
    assert retired.rejected == "status=deprecated"
    s.close()


def test_retiring_twice_is_idempotent(tmp_path):
    s = _seed(tmp_path)
    assert s.retire_deployment(AGENTIC, DETAIL) is True
    s.commit()
    assert s.retire_deployment(AGENTIC, DETAIL) is False   # already deprecated
    s.close()


def test_a_runtime_retirement_covers_the_gateways_other_ids_for_the_same_model(tmp_path):
    """OpenRouter lists one model under several deploy ids.

    `openrouter:m:free` and `openrouter/vendor/nvfp4:m:free` are the same weights
    through the same gateway, and the agentic restriction is a property of the
    model. Retiring only the id that happened to be called first leaves the sibling
    to be rediscovered on the next request — which is how an agentic-only model
    kept coming back, one id at a time, every time it was "removed".
    """
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w"))
    direct, resold = "openrouter:m:free", "openrouter/vendor/nvfp4:m:free"
    s.upsert_deployment(Deployment(direct, "w", "openrouter", "m:free"))
    s.upsert_deployment(Deployment(resold, "w", "openrouter/vendor/nvfp4", "m:free"))
    s.commit()

    assert proxy._retire_if_uncallable(s, direct, error_class="not_api_callable",
                                       error_detail=DETAIL) is True
    s.commit()
    assert _row(s, direct)["status"] == "deprecated"
    assert _row(s, resold)["status"] == "deprecated"
    s.close()


def test_a_runtime_retirement_leaves_other_gateways_serving_the_same_model(tmp_path):
    """`weights_id` is shared across gateways, so a model-level retirement has to
    be scoped to the gateway that refused. An HF route to the same weights is a
    different serving path and may well be callable."""
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w"))
    s.upsert_deployment(Deployment("openrouter:m:free", "w", "openrouter", "m:free"))
    s.upsert_deployment(Deployment("hf/deepinfra:m:free", "w", "hf/deepinfra", "m:free"))
    s.upsert_deployment(Deployment("deepinfra:m:free", "w", "deepinfra", "m:free"))
    s.commit()

    proxy._retire_if_uncallable(s, "openrouter:m:free", error_class="not_api_callable",
                                error_detail=DETAIL)
    s.commit()
    assert _row(s, "openrouter:m:free")["status"] == "deprecated"
    assert _row(s, "hf/deepinfra:m:free")["status"] == "live"
    assert _row(s, "deepinfra:m:free")["status"] == "live"
    s.close()


def test_a_hand_retirement_still_names_one_deployment(tmp_path):
    """`mi retire` is an operator pointing at one id, so it does not fan out —
    the escape hatch stays predictable."""
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w"))
    s.upsert_deployment(Deployment("openrouter:m:free", "w", "openrouter", "m:free"))
    s.upsert_deployment(Deployment("openrouter/vendor/nvfp4:m:free", "w",
                                   "openrouter/vendor/nvfp4", "m:free"))
    s.commit()

    s.retire_deployment("openrouter:m:free", "agentic-only", source="review")
    s.commit()
    assert _row(s, "openrouter:m:free")["status"] == "deprecated"
    assert _row(s, "openrouter/vendor/nvfp4:m:free")["status"] == "live"
    s.close()


# ------------------------------------------------------------- the stickiness


def test_a_retirement_survives_a_reingest(tmp_path):
    """The whole reason `status_source` exists.

    Ingest runs on a schedule and the catalogue reports this model as `live` —
    there is no field that says otherwise. Without a sticky retirement the arm
    comes back on the next ingest and every request pays an attempt to rediscover
    that it cannot be called.
    """
    s = _seed(tmp_path)
    proxy._retire_if_uncallable(s, AGENTIC, error_class="not_api_callable",
                                error_detail=DETAIL)
    s.commit()
    assert _row(s, AGENTIC)["status"] == "deprecated"

    # The next ingest reports it live, exactly as OpenRouter's catalogue does.
    s.upsert_deployment(Deployment(AGENTIC, "w", "openrouter",
                                   "thinkingmachines/inkling-small:free", status="live"))
    s.commit()
    row = _row(s, AGENTIC)
    assert row["status"] == "deprecated"
    assert row["status_source"] == "runtime"
    s.close()


def test_an_operator_can_restore_it(tmp_path):
    """Stickiness must not be permanent — a provider can change which apps it
    serves, and only a human knows that."""
    s = _seed(tmp_path)
    s.retire_deployment(AGENTIC, DETAIL)
    s.commit()
    assert s.enable_deployment(AGENTIC) is True
    s.commit()
    row = _row(s, AGENTIC)
    assert row["status"] == "live"
    assert row["status_source"] == "review"
    s.close()


def test_enabling_something_that_is_not_runtime_retired_is_a_no_op(tmp_path):
    s = _seed(tmp_path)
    assert s.enable_deployment("openrouter:good") is False
    s.close()


def test_only_a_runtime_retirement_is_sticky(tmp_path):
    """`status_source` is what makes a retirement survive, and only a *runtime* one
    gets it. A status the catalogue reported stays the catalogue's to change, so a
    source that later reports the arm healthy is free to do so."""
    s = _seed(tmp_path)
    s.upsert_deployment(Deployment("openrouter:good", "w", "openrouter", "good",
                                   status="degraded"))
    s.commit()
    assert _row(s, "openrouter:good")["status_source"] == "source"

    s.retire_deployment("openrouter:good", DETAIL)
    s.commit()
    assert _row(s, "openrouter:good")["status_source"] == "runtime"

    # A source-reported status does not overwrite a runtime retirement's source,
    # and the retirement is the only thing that is preserved.
    s.upsert_deployment(Deployment("openrouter:good", "w", "openrouter", "good",
                                   status="live"))
    s.commit()
    assert _row(s, "openrouter:good")["status"] == "deprecated"
    s.close()


def test_the_disabled_listing_finds_exactly_the_runtime_retirements(tmp_path):
    s = _seed(tmp_path)
    s.retire_deployment(AGENTIC, DETAIL)
    s.commit()
    retired = [d["deploy_id"] for d in s.deployments() if d.get("status_source") == "runtime"]
    assert retired == [AGENTIC]
    s.close()


# --------------------------------------------------------- status precedence
#
# Regression, found next to the retirement work: the `paid -> paid` branch used
# to return the *existing* status unconditionally, so a provider retiring a model
# — or an endpoint going `degraded` — could never take effect on a row that was
# already paid. Fixing it needed `status_source`, which is the only way to tell an
# operator's decision from the catalogue's.


def test_a_source_reported_deprecation_now_lands(tmp_path):
    s = _seed(tmp_path)
    paid = lambda st: Deployment("openrouter:good", "w", "openrouter", "good",
                                 price_in=1.0, price_out=2.0, status=st)
    s.upsert_deployment(paid("live"))
    s.commit()
    s.upsert_deployment(paid("deprecated"))
    s.commit()
    assert _row(s, "openrouter:good")["status"] == "deprecated"

    # ...and the source can still change its mind, because it owns this status.
    s.upsert_deployment(paid("live"))
    s.commit()
    assert _row(s, "openrouter:good")["status"] == "live"
    s.close()


def test_a_rejection_outranks_the_catalogue(tmp_path):
    """Rejecting a hibernated arm must not be undone by the next ingest, which
    still lists the model and reports it `live`."""
    s = _seed(tmp_path)
    s.upsert_deployment(Deployment("p:m", "w", "p", "m", price_in=0.0, price_out=0.0,
                                   zero_price=True, status="live"))
    s.commit()
    s.upsert_deployment(Deployment("p:m", "w", "p", "m", price_in=1.0, price_out=2.0))
    s.commit()
    assert _row(s, "p:m")["status"] == "hibernated"

    assert s.decide_review("p:m", approve=False) is True
    s.commit()
    assert _row(s, "p:m")["status_source"] == "review"

    s.upsert_deployment(Deployment("p:m", "w", "p", "m", price_in=1.0, price_out=2.0,
                                   status="live"))
    s.commit()
    assert _row(s, "p:m")["status"] == "deprecated"
    s.close()


def test_an_approval_survives_a_later_ingest(tmp_path):
    s = _seed(tmp_path)
    s.upsert_deployment(Deployment("p:m", "w", "p", "m", price_in=0.0, price_out=0.0,
                                   zero_price=True, status="live"))
    s.commit()
    s.upsert_deployment(Deployment("p:m", "w", "p", "m", price_in=1.0, price_out=2.0))
    s.commit()
    assert s.decide_review("p:m", approve=True) is True
    s.commit()

    s.upsert_deployment(Deployment("p:m", "w", "p", "m", price_in=1.0, price_out=2.0,
                                   status="live"))
    s.commit()
    row = _row(s, "p:m")
    assert row["status"] == "live" and row["status_source"] == "review"
    s.close()


def test_enabling_persists_without_a_caller_commit(tmp_path):
    """`mi enable` closes the store without committing, so the decision has to
    commit itself — it reported success while the change rolled back."""
    path = tmp_path / "t.db"
    s = Store(path)
    s.upsert_weights(Weights("w", "w"))
    s.upsert_deployment(Deployment(AGENTIC, "w", "openrouter", "m"))
    s.retire_deployment(AGENTIC, DETAIL)
    s.commit()
    assert s.enable_deployment(AGENTIC) is True
    s.close()                       # deliberately no commit, as the CLI does

    again = Store(path)
    assert _row(again, AGENTIC)["status"] == "live"
    again.close()


# ------------------------------------------- retiring on purpose (the operator's half)


def test_an_operator_can_retire_a_model(tmp_path):
    """`mi retire`: the router *discovers* an agentic-only model by failing on it,
    but a human who already knows should not have to wait for that call."""
    s = _seed(tmp_path)
    assert s.retire_deployment(AGENTIC, "agentic-only", source="review") is True
    s.commit()
    row = _row(s, AGENTIC)
    assert row["status"] == "deprecated"
    assert row["status_source"] == "review"
    assert "agentic-only" in row["status_reason"]
    s.close()


def test_a_hand_retirement_leaves_the_ranking(tmp_path):
    s = _seed(tmp_path)
    s.retire_deployment(AGENTIC, "agentic-only", source="review")
    s.commit()
    task = TaskProfile("t", tokens_in=100, tokens_out=100)
    c = next(c for c in build_candidates(s, task, Policy(require_callable=False))
             if c.deploy_id == AGENTIC)
    assert c.rejected == "status=deprecated"
    s.close()


def test_a_hand_retirement_survives_a_reingest_of_a_free_model(tmp_path):
    """The hole this closed.

    `_spend_transition`'s still-free branch returns "leave the status alone", and
    the incoming `live` then won by default — so a `:free` variant retired by hand
    came straight back on the next ingest. A status *we* decided has to outrank the
    catalogue whatever its provenance, not only when it came from a runtime call.
    """
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w"))
    free = Deployment("openrouter:m:free", "w", "openrouter", "m:free",
                      price_in=0.0, price_out=0.0, free_variant=True, status="live")
    s.upsert_deployment(free)
    s.commit()
    s.retire_deployment("openrouter:m:free", "agentic-only", source="review")
    s.commit()

    s.upsert_deployment(free)          # the catalogue reports it free and live
    s.commit()
    row = _row(s, "openrouter:m:free")
    assert row["status"] == "deprecated", "a hand retirement was resurrected"
    assert row["status_source"] == "review"
    s.close()


def test_enabling_still_undoes_a_hand_retirement(tmp_path):
    """Stickiness must not be permanent — the operator who retired it can undo it."""
    s = _seed(tmp_path)
    s.retire_deployment(AGENTIC, "agentic-only", source="review")
    s.commit()
    assert s.enable_deployment(AGENTIC) is True
    s.commit()
    assert _row(s, AGENTIC)["status"] == "live"
    s.close()


def test_the_disabled_listing_includes_hand_retirements(tmp_path):
    s = _seed(tmp_path)
    s.retire_deployment(AGENTIC, "agentic-only", source="review")
    s.commit()
    listed = [d["deploy_id"] for d in s.deployments()
              if d.get("status_source") in ("runtime", "review") and d.get("status") != "live"]
    assert listed == [AGENTIC]
    s.close()
