"""Tests for `.env` auto-loading on the `mi` entrypoint.

Without this, keys placed in `.env` never reach the proxy's `os.environ`, so
every route fails with `no_api_key` even though the user did the right thing.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

from mininfer.cli import _load_env


def test_load_env_reads_values(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "MI_TEST_KEY=abc123\n"
        "# a comment\n"
        "\n"
        "MI_TEST_QUOTED='x y'\n"
        'MI_TEST_DQ="z w"\n'
    )
    monkeypatch.chdir(tmp_path)
    for k in ("MI_TEST_KEY", "MI_TEST_QUOTED", "MI_TEST_DQ"):
        monkeypatch.delenv(k, raising=False)

    _load_env()

    assert os.environ["MI_TEST_KEY"] == "abc123"
    assert os.environ["MI_TEST_QUOTED"] == "x y"
    assert os.environ["MI_TEST_DQ"] == "z w"


def test_load_env_does_not_override_real_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_TEST_KEY", "from-shell")
    (tmp_path / ".env").write_text("MI_TEST_KEY=from-file\n")
    monkeypatch.chdir(tmp_path)

    _load_env()

    assert os.environ["MI_TEST_KEY"] == "from-shell"


def test_load_env_is_a_noop_without_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MI_TEST_MISSING", raising=False)

    _load_env()  # must not raise when there is no .env

    assert "MI_TEST_MISSING" not in os.environ


def test_proxy_module_loads_env_on_import(tmp_path):
    """Importing `mininfer.proxy` must merge `.env`.

    uvicorn's `--reload` reloader imports `mininfer.proxy:app` in a child process and
    never runs `mininfer.cli.main`, so the load has to happen at module import or the
    reloaded server sees no provider keys. Run in a subprocess so this import
    does not disturb the test process.
    """
    (tmp_path / ".env").write_text("MI_TEST_PROXY_KEY=from-dotenv\n")
    root = pathlib.Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("MI_TEST_PROXY_KEY", None)
    proc = subprocess.run(
        [sys.executable, "-c",
         "import os, mininfer.proxy; print(os.environ.get('MI_TEST_PROXY_KEY', ''))"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "from-dotenv"


def test_load_env_ignores_a_directory(tmp_path, monkeypatch, capsys):
    """A `.env` directory must not crash import — the Docker bind-mount footgun.

    The old code called `read_text` and raised `IsADirectoryError`, which
    crash-looped the container on a config the operator had to fix from the host.
    Now it is treated as "no file" and the warning names the fix.
    """
    from mininfer.env import load_env

    (tmp_path / ".env").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MI_TEST_DIR_KEY", raising=False)

    assert load_env() is False
    assert "is a directory" in capsys.readouterr().err
