"""The wheel must contain the subpackages.

`pip install .` used to ship a `mininfer` with no `mininfer/web/` and no
`mininfer/ingest/`, because `pyproject.toml` named the top-level package
explicitly (``packages = ["mininfer"]``) — setuptools then includes the modules
in that directory and *not* its subdirectories. The result installed cleanly and
then failed at import: the CLI could not import `ingest`, the proxy could not
import `web.legacy`.

The environment hid it, which is the point of pinning it here: run from the repo
root, ``import mininfer`` resolves to the source tree, so every import succeeds
against a wheel that is missing half the code. These assertions read the
configuration instead, so they hold wherever pytest is run from.
"""
from __future__ import annotations

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _setuptools_config() -> dict:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]


def test_subpackages_are_discovered_not_named_explicitly():
    cfg = _setuptools_config()
    packages = cfg.get("packages")
    assert isinstance(packages, dict) and "find" in packages, (
        "a bare `packages = [...]` list omits subpackages; use "
        "[tool.setuptools.packages.find]")
    assert "mininfer*" in packages["find"]["include"]


def test_every_subpackage_matches_the_include_globs():
    cfg = _setuptools_config()
    globs = cfg["packages"]["find"]["include"]
    subs = sorted(p.parent.name for p in (ROOT / "mininfer").glob("*/__init__.py"))
    assert subs, "expected at least mininfer/web and mininfer/ingest"
    for sub in subs:
        dotted = f"mininfer.{sub}"
        matched = any(re.fullmatch(p.replace("*", ".*"), dotted) for p in globs)
        assert matched, f"{dotted} would not be packaged by {globs}"
        # Nested (adapters) too: a miss there loses every provider adapter.
        for nested in (ROOT / "mininfer" / sub).glob("*/__init__.py"):
            dotted_nested = f"mininfer.{sub}.{nested.parent.name}"
            assert any(re.fullmatch(p.replace("*", ".*"), dotted_nested) for p in globs), \
                f"{dotted_nested} would not be packaged"


def test_the_lockfile_exists_and_is_pinned():
    lock = ROOT / "requirements.lock"
    assert lock.exists(), "requirements.lock is the reproducible-install contract"
    lines = [ln.strip() for ln in lock.read_text().splitlines()]
    pins = [ln for ln in lines if ln and not ln.startswith("#") and "==" in ln]
    assert len(pins) >= 5, "a lockfile with almost nothing in it is not a lock"
    # Every non-comment, non-continuation line must be an exact pin.
    for ln in lines:
        if not ln or ln.startswith("#") or ln.startswith(" ") or ln.startswith("-"):
            continue
        assert "==" in ln, f"unpinned dependency in the lock: {ln!r}"


def test_the_lock_carries_the_cloud_runtime_dependencies():
    """The image runs `[server, cloud]`; the lock is what the image installs.

    Without `psycopg2` a `postgresql://` MI_DB cannot connect at all, and without
    `redis` the rate limiter falls back to per-process counting — on several
    replicas that is silently *not* a limit. Neither failure stops the container
    from starting, which is why it has to be pinned here rather than noticed in
    production.
    """
    lock = (ROOT / "requirements.lock").read_text()
    assert "psycopg2-binary==" in lock, "the image would have no Postgres driver"
    assert "redis==" in lock, "the image would silently use the in-process limiter"

    cfg = _setuptools_config()
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    cloud = data["project"]["optional-dependencies"]["cloud"]
    assert any(d.startswith("psycopg2") for d in cloud)
    assert any(d.startswith("redis") for d in cloud)


def test_the_understanding_extra_stays_out_of_the_default_image():
    """GLiNER2/torch must not leak into the base install or the universal lock.

    The web image is ~60 MB and `requirements.lock` is `--universal`; a torch pin
    is platform-specific and would break both. The extra is declared so Phase 2.0
    can opt in without a code change, and this guard
    keeps it out of the default install — the failure mode is an image that
    silently grows 40× in a later change.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert "understanding" in project["optional-dependencies"]
    extra = project["optional-dependencies"]["understanding"]
    assert any(d.startswith("gliner") for d in extra)
    assert not any("gliner" in d or d.startswith("torch") for d in project["dependencies"])

    lock = (ROOT / "requirements.lock").read_text().lower()
    assert "gliner" not in lock, "the understanding layer reached the default lock"
    assert "torch" not in lock, "torch in the universal lock would break it"
