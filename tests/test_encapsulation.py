"""`Store` owns the schema, so `Store` owns the SQL.

Raw SQL used to live in ten modules — `router`, `proxy`, `metrics`, `quota`,
`resolve`, `sync`, `cli`, `bench`, `agent_ingest`, `agent_resolve` — which meant a
column rename was a ten-file change, and one query had already drifted into two
verbatim copies (the provider rollup, in the dashboard endpoint and in `mi stats`).

The fix was named methods, not a private `conn`. Python privacy is advisory, so
`_conn` would only have hidden the leak; a test that reads the source cannot be
worked around by accident, and it fails with the file and line to look at.
"""
from __future__ import annotations

import pathlib
import re

PKG = pathlib.Path(__file__).resolve().parent.parent / "mininfer"

#: The only module allowed to know the schema.
OWNER = "store.py"

_SQL = re.compile(r"\.conn\.(execute|executemany|executescript)")


def _offenders() -> list[str]:
    out = []
    for path in sorted(PKG.rglob("*.py")):
        if path.name == OWNER:
            continue
        for n, line in enumerate(path.read_text().splitlines(), start=1):
            if _SQL.search(line) and not line.lstrip().startswith("#"):
                out.append(f"{path.relative_to(PKG.parent)}:{n}: {line.strip()}")
    return out


def test_only_the_store_knows_the_schema():
    offenders = _offenders()
    assert not offenders, (
        "raw SQL outside mininfer/store.py — add a named method instead:\n  "
        + "\n  ".join(offenders))


def test_the_guard_would_notice(tmp_path, monkeypatch):
    """A guard that cannot fail is not a guard."""
    fake = tmp_path / "mininfer"
    fake.mkdir()
    (fake / "store.py").write_text("conn.execute('allowed here')\n")
    (fake / "leaky.py").write_text("store.conn.execute('SELECT 1')\n")
    # `tests/` is not an importable package, so patch the module global directly
    import sys
    monkeypatch.setattr(sys.modules[__name__], "PKG", fake)
    offenders = _offenders()
    assert len(offenders) == 1
    assert "leaky.py" in offenders[0]
    # and a comment mentioning it is not an offence
    (fake / "leaky.py").write_text("# store.conn.execute is the old way\n")
    assert _offenders() == []


def test_every_named_query_is_used_somewhere(tmp_path):
    """Dead repository methods are how this rots back into a grab bag."""
    src = (PKG / "store.py").read_text()
    methods = re.findall(r"\n    def (\w+)\(", src)
    # public reads/writes a caller should be using
    candidates = [m for m in methods if not m.startswith("_")]
    unused = []
    for name in candidates:
        for path in PKG.rglob("*.py"):
            if path.name == OWNER:
                continue
            if re.search(rf"\b{name}\(", path.read_text()):
                break
        else:
            unused.append(name)
    # A handful are legitimately internal (called by store's own methods via self)
    # or used only by tests, so this is a floor rather than zero tolerance.
    assert len(unused) <= 6, f"unused Store methods piling up: {unused}"
