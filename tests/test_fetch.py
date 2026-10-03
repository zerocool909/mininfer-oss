"""The evidence lake's cache.

`_find_recent` decides whether a fetch is served from `raw/` or goes back to the
network. It globbed `*.json` only, so every `fetch_raw(..., ext="html")` snapshot
was written to disk and then never found again: a permanent cache miss that
re-fetched the page on every call and silently degraded the metrics scraper and
the webpage-ingestion agents, both of which fetch HTML.

The property that matters is not "the cache works" but "the extension is not part
of the cache key" — the directory is already keyed by source *and* URL hash.
"""
from __future__ import annotations

import time

import httpx
import pytest

from mininfer import fetch as F


@pytest.fixture(autouse=True)
def _isolated_lake(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "RAW_ROOT", tmp_path)


def _network(monkeypatch, body: bytes = b"<html>fresh</html>") -> list[str]:
    """Stub httpx so a real network call is a test failure, and count attempts."""
    hits: list[str] = []

    class _Resp:
        content = body

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            hits.append(url)
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    return hits


def test_an_html_snapshot_is_found_again():
    """The regression: `rglob("*.json")` could never see this file."""
    F._write_snapshot("demo", "https://x/y", b"<html>x</html>", "abc123", ext="html")
    found = F._find_recent("demo", "https://x/y", ttl_s=3600)
    assert found is not None
    assert found.read_bytes() == b"<html>x</html>"


def test_the_extension_is_not_part_of_the_cache_key():
    for ext in ("html", "json", "txt"):
        F.RAW_ROOT = F.RAW_ROOT / ext          # a fresh lake per extension
        F.RAW_ROOT.mkdir(parents=True, exist_ok=True)
        F._write_snapshot("demo", "https://x/y", b"payload", f"sha-{ext}", ext=ext)
        assert F._find_recent("demo", "https://x/y", 3600) is not None, ext


def test_an_html_fetch_is_served_from_the_cache_the_second_time(monkeypatch):
    hits = _network(monkeypatch)
    first = F.fetch_raw("demo", "https://x/y", ext="html")
    assert first.from_cache is False
    assert hits == ["https://x/y"]

    second = F.fetch_raw("demo", "https://x/y", ext="html")
    assert second.from_cache is True
    assert hits == ["https://x/y"], "the second call went back to the network"
    assert second.payload == first.payload
    assert second.sha256 == first.sha256


def test_force_bypasses_the_cache(monkeypatch):
    hits = _network(monkeypatch)
    F.fetch_raw("demo", "https://x/y", ext="html")
    F.fetch_raw("demo", "https://x/y", ext="html", force=True)
    assert hits == ["https://x/y", "https://x/y"]


def test_a_stale_snapshot_is_not_reused(monkeypatch):
    path = F._write_snapshot("demo", "https://x/y", b"old", "abc", ext="html")
    old = time.time() - 7200
    import os
    os.utime(path, (old, old))
    assert F._find_recent("demo", "https://x/y", ttl_s=3600) is None
    assert F._find_recent("demo", "https://x/y", ttl_s=86400) is not None


def test_the_newest_snapshot_wins_regardless_of_extension(monkeypatch):
    """A source that switched content type must not pin the cache to the old one."""
    import os

    older = F._write_snapshot("demo", "https://x/y", b"old-json", "a", ext="json")
    newer = F._write_snapshot("demo", "https://x/y", b"new-html", "b", ext="html")
    old = time.time() - 600
    os.utime(older, (old, old))
    assert F._find_recent("demo", "https://x/y", 3600) == newer


def test_an_unknown_url_has_no_cache(monkeypatch):
    _network(monkeypatch)
    assert F._find_recent("demo", "https://never/fetched", 3600) is None


def test_persist_false_returns_the_bytes_without_writing(monkeypatch):
    """`mi metrics --dry-run` printed "nothing written" while `fetch_raw` inside
    it persisted the page — so the evidence lake grew during a preview. The
    negative control is the second half: the same call with `persist=True` must
    still write, or this test would pass against a function that never saves."""
    _network(monkeypatch, body=b"<html>preview</html>")

    snap = F.fetch_raw("demo", "https://x/y", ext="html", persist=False)
    assert snap.payload == b"<html>preview</html>"
    assert snap.persisted is False
    assert snap.storage_uri.startswith("s3://mininfer-evidence/demo/")
    assert F._find_recent("demo", "https://x/y", ttl_s=3600) is None
    assert not list(F.RAW_ROOT.rglob("*.html")), "dry run wrote a snapshot"

    written = F.fetch_raw("demo", "https://x/y", ext="html", persist=True)
    assert written.persisted is True
    assert F._find_recent("demo", "https://x/y", ttl_s=3600) is not None
