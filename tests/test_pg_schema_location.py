"""The Postgres DDL must be findable in every layout the server really runs in.

A source checkout has `supabase/` beside the `mininfer/` package directory;
the installed wheel has the package under site-packages and nothing beside it,
so the container copies `supabase/` into the working directory. Both must
resolve, and neither may strand a Postgres registry half-initialised (the
`/healthz ok, everything 500s` class)."""
from __future__ import annotations

import pathlib

import pytest


def test_the_checkout_layout_resolves() -> None:
    # A source checkout carries the schema beside the package. If this file
    # moves, the constant at store.py must move with it.
    schema = pathlib.Path(
        "supabase/schema.sql")  # resolved via the repo root = parent of tests/
    root = pathlib.Path(__file__).resolve().parents[1]
    assert (root / "supabase" / "schema.sql").is_file()


def test_the_installed_layout_falls_back_to_cwd(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mininfer.store as store

    # Simulate the wheel: the "installed" location does not exist.
    missing = tmp_path / "site-packages-imaginary" / "schema.sql"
    monkeypatch.setattr(store, "_PG_SCHEMA", missing)

    # The container copies the schema beside the process working directory.
    (tmp_path / "supabase").mkdir()
    ddl = tmp_path / "supabase" / "schema.sql"
    ddl.write_text("CREATE SCHEMA mirror_of_supabase;", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    got = store._postgres_ddl()
    assert "mirror_of_supabase" in got


def test_missing_everywhere_raises_with_locations(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mininfer.store as store

    missing = tmp_path / "nowhere" / "schema.sql"
    monkeypatch.setattr(store, "_PG_SCHEMA", missing)
    empty = tmp_path / "empty-cwd"
    empty.mkdir()
    monkeypatch.chdir(empty)

    with pytest.raises(FileNotFoundError) as err:
        store._postgres_ddl()
    # The error must name where it looked, not where it wishes it had.
    assert "schema.sql" in str(err.value)
