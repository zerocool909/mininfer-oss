"""`mi doctor` — the first-run checklist, with the fix for each failure.

A fresh clone fails in a handful of specific ways (old Python, missing extra, no
`.env`, no key, unbuilt dashboard, empty registry). The command exists so those are
reported once, in one place, rather than discovered one traceback at a time.
"""
from __future__ import annotations

import types

import pytest

from mininfer.cli import cmd_doctor, main


def test_doctor_is_a_registered_command():
    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--help"])
    assert exc.value.code == 0


def test_a_missing_registry_is_a_required_failure(tmp_path, capsys):
    # A db path whose parent directory does not exist: opening it raises.
    args = types.SimpleNamespace(db=str(tmp_path / "nope" / "registry.db"),
                                 policy="config/policy.yaml")
    rc = cmd_doctor(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "required check(s) failed" in out
    assert "mi ingest" in out                      # the fix is named, not just the fault


def test_a_readable_registry_passes_that_check(tmp_path, capsys):
    from mininfer.store import Store
    db = tmp_path / "r.db"
    Store(db).close()
    args = types.SimpleNamespace(db=str(db), policy="config/policy.yaml")
    cmd_doctor(args)
    out = capsys.readouterr().out
    assert "[FAIL] registry readable" not in out
    # An empty-but-readable registry is a warning ("populated"), not a read error.
    assert "registry populated" in out
