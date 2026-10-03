"""P8 — the hosted configuration.

Two things a deployment gets wrong without anything failing locally:

* **A threshold that only exists as a constant.** The reconciler is re-derivable,
  which is exactly what makes retuning a `mi reconcile` rather than a release — but
  only if the thresholds can be set from the environment at all.
* **A serverless host that reimplements the engine.** The Modal app must serve
  `mininfer.proxy:app` and run the real CLI. Otherwise there are two reconcilers
  to keep in step, and the second one is wrong.

The second is checked structurally, in the style `test_deploy_manifests.py` uses: a
manifest (or a scheduler entrypoint) is easy to edit back into a divergent shape,
and nothing fails until production.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODAL_APP = ROOT / "deploy" / "modal" / "app.py"


def _thresholds(env: dict[str, str]) -> dict[str, str]:
    """Read the reconciler's thresholds in a clean interpreter.

    A subprocess because they are module-level and read from the environment at
    import: that is the property under test.
    """
    code = (
        "import json; from mininfer import store;"
        "print(json.dumps({k: getattr(store, k) for k in ("
        "'PRICE_FRESHNESS_DAYS','PRICE_HISTORY_FACTOR','PRICE_PEER_SUSPECT_FACTOR',"
        "'PRICE_PEER_QUARANTINE_FACTOR','PRICE_PEER_CRITICAL_FACTOR',"
        "'PRICE_MIN_SOURCES','QUOTA_EXHAUSTED_HEADROOM')}))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True,
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env})
    import json
    return json.loads(out.stdout)


# ----------------------------------------------------- retunable thresholds


def test_the_thresholds_have_their_documented_defaults():
    got = _thresholds({})
    assert got["PRICE_PEER_QUARANTINE_FACTOR"] == 10.0   # the Novita unit-error scale
    assert got["PRICE_PEER_SUSPECT_FACTOR"] == 5.0
    assert got["PRICE_PEER_CRITICAL_FACTOR"] == 20.0
    assert got["PRICE_MIN_SOURCES"] == 3                 # two is a mean, not a median
    assert got["PRICE_FRESHNESS_DAYS"] == 30


def test_a_hosted_deployment_can_retune_the_reconciler():
    """Without this, changing a threshold is a release — which defeats the point of
    a decision that is supposed to be re-derivable."""
    got = _thresholds({
        "PQS_SCALE_FACTOR": "20",
        "PQS_SPREAD_FACTOR": "2.5",
        "PQS_MIN_SOURCES": "2",
        "PQS_FRESHNESS_DAYS": "7",
        "PQS_EXHAUSTED_HEADROOM": "0.2",
    })
    assert got["PRICE_PEER_QUARANTINE_FACTOR"] == 20.0
    assert got["PRICE_PEER_SUSPECT_FACTOR"] == 2.5
    assert got["PRICE_MIN_SOURCES"] == 2
    assert got["PRICE_FRESHNESS_DAYS"] == 7
    assert got["QUOTA_EXHAUSTED_HEADROOM"] == 0.2


def test_a_malformed_threshold_is_ignored_not_fatal():
    """A typo in a secret must not take the reconciler offline."""
    got = _thresholds({"PQS_SCALE_FACTOR": "ten", "PQS_MIN_SOURCES": "", "PQS_SPREAD_FACTOR": " "})
    assert got["PRICE_PEER_QUARANTINE_FACTOR"] == 10.0
    assert got["PRICE_MIN_SOURCES"] == 3
    assert got["PRICE_PEER_SUSPECT_FACTOR"] == 5.0


def test_min_sources_stays_an_int():
    """It indexes a list and compares to `len()`, so a float would be a type error
    at the wrong moment rather than at boot."""
    assert isinstance(_thresholds({"PQS_MIN_SOURCES": "4"})["PRICE_MIN_SOURCES"], int)


# --------------------------------------------------------- the Modal host


def _modal_app():
    pytest.importorskip("modal", reason="modal is a deployment dependency, not a runtime one")
    spec = importlib.util.spec_from_file_location("mininfer_modal_app", MODAL_APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_modal_app_registers_a_cron_and_an_api():
    app = _modal_app()
    assert sorted(app.app.registered_functions or {}) == ["api", "maintain"]


def test_the_api_serves_the_real_proxy_app():
    """The whole argument for a serverless host: it must not be a second API."""
    src = MODAL_APP.read_text(encoding="utf-8")
    assert "from mininfer.proxy import app as proxy_app" in src
    assert "return proxy_app" in src


def test_the_cron_runs_the_real_cli():
    src = MODAL_APP.read_text(encoding="utf-8")
    assert "from mininfer import cli" in src
    assert '"ingest"' in src and '"reconcile"' in src


def test_the_cron_does_not_reimplement_the_engine():
    """The failure this prevents: a copy of the reconciler that drifts.

    A scheduler entrypoint is the easiest place to paste logic "just for this
    platform"; the moment it does, the two hosts disagree about prices and only one
    of them is right.
    """
    src = MODAL_APP.read_text(encoding="utf-8")
    for internal in ("_compute_resolutions", "_reconcile_one", "_record_price_history",
                     "PRICE_PEER_QUARANTINE_FACTOR", "price_resolution"):
        assert internal not in src, f"the Modal app reaches into the reconciler: {internal}"


def test_the_modal_image_ships_what_the_code_reads_at_runtime():
    """The policy is read by `Policy.load`, the schema DDL by `Store` when the target
    is Postgres. Missing either fails at the first request, not at deploy."""
    src = MODAL_APP.read_text(encoding="utf-8")
    assert '"config"' in src
    assert '"supabase"' in src
    assert '"mininfer"' in src


def test_the_modal_cron_matches_the_container_worker_cadence():
    """Two hosts, one cadence. A tighter loop on one of them mostly re-reads
    identical bytes; a looser one leaves the other's prices stale."""
    import tomllib

    app = _modal_app()
    fly = tomllib.loads((ROOT / "deploy" / "cloud" / "fly.toml").read_text(encoding="utf-8"))
    worker = fly["processes"]["worker"]
    assert "sleep 21600" in worker, "the Fly worker's cadence changed; update SCHEDULE"
    assert app.SCHEDULE == "0 */6 * * *"
