"""Module dependency direction.

`mininfer.router` and `mininfer.bandit` used to be mutually dependent: `bandit` needed
`Candidate` and `router` needed `bandit_order`, so `bandit` guarded its import
behind `TYPE_CHECKING` and `router` deferred its own inside the function. Both
worked, and both *hid* the cycle — an import graph run over module-level imports
reported no cycles at all, and only reading the source found them.

`Candidate` moved to `schema` (which imports nothing from `mi`), so the dependency
is one-way now. These run in a subprocess on purpose: an import order cannot be
tested in-process, because by the time a test runs, pytest has usually imported
half the package already.
"""
from __future__ import annotations

import subprocess
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _import_and_report(module: str) -> set[str]:
    """Import `module` in a clean interpreter; return the mininfer.* modules it pulled in."""
    code = (
        "import sys, json\n"
        f"import {module}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('mininfer.'))))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True,
                         capture_output=True, text=True)
    import json
    return set(json.loads(out.stdout))


def test_bandit_does_not_depend_on_the_router():
    """The property the `TYPE_CHECKING` guard was hiding."""
    loaded = _import_and_report("mininfer.bandit")
    assert "mininfer.router" not in loaded, (
        f"mininfer.bandit pulled in mininfer.router: {sorted(loaded)} — the cycle is back")
    # and it does still get what it needs, from the leaf
    assert "mininfer.schema" in loaded


def test_schema_is_a_leaf():
    """`Candidate` lives here precisely because this module depends on nothing."""
    # the filter keeps only `mininfer.*`, so the parent package is not in the set
    loaded = _import_and_report("mininfer.schema")
    assert loaded == {"mininfer.schema"}, f"mininfer.schema depends on {sorted(loaded)}"


def test_the_router_may_depend_on_the_bandit():
    """One direction is fine — it is the loop that was the problem."""
    loaded = _import_and_report("mininfer.router")
    assert "mininfer.bandit" in loaded


def test_no_deferred_imports_remain_to_hide_a_cycle():
    """A function-level `import` is how a cycle survives a module-level check."""
    src = (ROOT / "mininfer" / "router.py").read_text()
    assert "        from .bandit import" not in src
    assert "        from .router import" not in (ROOT / "mininfer" / "bandit.py").read_text()
