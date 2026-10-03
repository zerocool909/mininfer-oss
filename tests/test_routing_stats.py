"""`routing_stats` must train on the model, not on our own infrastructure.

The bug this pins: a container without the corporate root CA recorded 32
`network_error`s as model *losses*, so the router was learning that its best arms
fail. A call that never reached the model (no key, TLS/DNS, a malformed registry
row) is not evidence about the model and must not enter `n` / `wins`.

What must still count is anything the provider said *about this deployment* —
`429`, `timeout`, `5xx`, `http_4xx`, `bad_output` — because that is what the
router ranks. The regression guard is `test_compare.py`'s "a broken free arm is
charged the paid floor", which uses a 404; if the exclusion is ever widened to
cover 4xx, that test fails.
"""
from __future__ import annotations

import os
import sqlite3
import ssl

import httpx
import pytest

import mininfer.execute as execute
import mininfer.store as store_mod
from mininfer.schema import NON_MODEL_ERRORS
from mininfer.store import Store

TS = "2026-01-01T00:00:00+00:00"


def _observe(s: Store, error_class: str | None, *, ok: bool = False,
             deploy_id: str = "prov:m0", task: str = "t") -> None:
    s.observe(deploy_id, task, ok=ok, ts=TS, error_class=error_class)


# --------------------------------------------------------------------------- #
# the view
# --------------------------------------------------------------------------- #

def test_infrastructure_errors_leave_the_denominator_alone(tmp_path):
    s = Store(tmp_path / "r.db")
    _observe(s, None, ok=True)
    _observe(s, "network_error")
    s.commit()
    row = s.stats("t")["prov:m0"]
    assert row["n"] == 1, "a network error is not a trial of the model"
    assert row["wins"] == 1
    assert row["n_infra"] == 1
    assert row["n_all"] == 2
    s.close()


@pytest.mark.parametrize("error_class", sorted(NON_MODEL_ERRORS))
def test_every_declared_non_model_error_is_excluded(tmp_path, error_class):
    """The Python list and the SQL must agree; this is the drift guard."""
    s = Store(tmp_path / f"{error_class}.db")
    _observe(s, error_class)
    s.commit()
    row = s.stats("t")["prov:m0"]
    assert row["n"] == 0, f"{error_class} was counted as a model trial"
    assert row["n_infra"] == 1
    s.close()


@pytest.mark.parametrize("error_class", ["429", "timeout", "5xx", "http_404",
                                         "http_400", "bad_output", "empty_content"])
def test_provider_and_output_failures_still_count(tmp_path, error_class):
    """These are the deployment's own behaviour — the router must see them."""
    s = Store(tmp_path / f"{error_class.replace('.', '_')}.db")
    _observe(s, error_class)
    s.commit()
    row = s.stats("t")["prov:m0"]
    assert row["n"] == 1
    assert row["wins"] == 0
    assert row["n_infra"] == 0
    s.close()


def test_wins_only_counts_counted_rows(tmp_path):
    """A success and an infra error: one trial, one win, one excluded."""
    s = Store(tmp_path / "r.db")
    _observe(s, None, ok=True)
    _observe(s, "tls_error")
    s.commit()
    row = s.stats("t")["prov:m0"]
    assert (row["n"], row["wins"], row["n_infra"], row["n_all"]) == (1, 1, 1, 2)
    s.close()


def test_availability_counters_are_unchanged(tmp_path):
    s = Store(tmp_path / "r.db")
    _observe(s, "429")
    _observe(s, "timeout")
    _observe(s, "network_error")
    s.commit()
    row = s.stats("t")["prov:m0"]
    assert row["n_429"] == 1 and row["n_timeout"] == 1
    # `n` is the model-trial denominator, so the 429/timeout rate stays honest.
    assert row["n"] == 2
    s.close()


# --------------------------------------------------------------------------- #
# an existing registry is repaired, not left on the old view
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(bool(os.environ.get("MI_TEST_PG_DSN")),
                    reason="the migration is SQLite-specific (Postgres uses CREATE OR REPLACE)")
def test_an_existing_registry_gets_the_corrected_view(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    s = Store(db)
    _observe(s, "network_error")
    s.commit()
    s.close()

    # Simulate the pre-fix view an already-deployed registry still holds: the old
    # definition counted every row. `CREATE VIEW IF NOT EXISTS` would leave it.
    conn = sqlite3.connect(db)
    conn.execute("DROP VIEW routing_stats")
    conn.execute("CREATE VIEW routing_stats AS SELECT deploy_id, task, "
                 "COUNT(*) AS n, SUM(ok) AS wins FROM observations GROUP BY deploy_id, task")
    conn.commit()
    conn.close()

    monkeypatch.setattr(store_mod, "_INITIALIZED_DBS", set())   # force re-init
    s2 = Store(db)
    row = s2.stats("t")["prov:m0"]
    assert row["n"] == 0 and row["n_infra"] == 1 and row["n_all"] == 1
    s2.close()


# --------------------------------------------------------------------------- #
# TLS failures are their own class
# --------------------------------------------------------------------------- #

def test_a_certificate_failure_is_a_tls_error():
    """The exact shape httpx raises behind a corporate MITM root we don't trust."""
    inner = ssl.SSLCertVerificationError(
        1, "certificate verify failed: unable to get local issuer certificate")
    exc = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    exc.__cause__ = inner
    assert execute._network_class(exc) == "tls_error"


def test_a_certificate_failure_without_a_cause_chain_is_still_a_tls_error():
    exc = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    assert execute._network_class(exc) == "tls_error"


def test_a_plain_connect_failure_stays_a_network_error():
    """A refused connection / DNS failure is not automatically our TLS config."""
    assert execute._network_class(httpx.ConnectError("connection refused")) == "network_error"


def test_a_tls_error_does_not_look_like_a_model_trial(tmp_path):
    s = Store(tmp_path / "r.db")
    inner = ssl.SSLCertVerificationError(1, "certificate verify failed")
    exc = httpx.ConnectError("certificate verify failed")
    exc.__cause__ = inner
    _observe(s, execute._network_class(exc))
    s.commit()
    assert s.stats("t")["prov:m0"]["n"] == 0
    s.close()


def test_the_tls_hint_is_actionable_not_a_raw_ssl_string():
    """The detail a user sees must name the fix, not echo OpenSSL.

    The dashboard surfaces `error_detail` as "Connectivity failed … Reason:",
    where `unable to get local issuer certificate` is a dead end. The hint points
    at the one command that fixes it, and stays short enough to sit in
    `observations.error_detail`.
    """
    assert "make_ca_bundle.sh" in execute._TLS_HINT
    assert "MI_CA_BUNDLE" in execute._TLS_HINT
    assert "unable to get local issuer certificate" not in execute._TLS_HINT
    assert len(execute._TLS_HINT) < 200
