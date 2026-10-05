"""Fetch layer + raw evidence lake.

Every fetch is persisted to `raw/<source>/<date>/<sha256>.json` before parsing.
The registry is therefore *always* re-derivable from history, which is the only
thing that makes it safe to change a parser later.

This is a local stand-in for the S3/R2 bucket. `storage_uri` records the logical
object key so migrating to R2 is a copy, not a redesign.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import time
from dataclasses import dataclass

import httpx

RAW_ROOT = pathlib.Path(os.environ.get("MI_RAW", "raw"))
_UA = "MinInfer/0.1 (+model-intelligence-registry)"


#: Environment variables that point at a CA bundle, most specific first; the
#: first whose path exists wins. `SSL_CERT_FILE` is honoured by OpenSSL/httpx
#: anyway; `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` are **not**, and they are
#: exactly the names a `requests`/`curl` user already has exported — honouring
#: them here is what makes "it worked with curl" carry over.
_CA_BUNDLE_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
                   "CURL_CA_BUNDLE")


def _verify() -> str | bool:
    """TLS trust store: a bundle path, or `True` for httpx's own default.

    **A normal network needs no configuration.** With nothing set this returns
    `True`, so httpx verifies against `certifi` (the Mozilla roots), which already
    trusts every public provider. Nothing here is machine- or device-specific.

    A bundle is only needed when Python's trust store lacks a root that another
    client already trusts. Point one of `_CA_BUNDLE_VARS` at a PEM bundle to add
    those anchors; never disable verification instead. `.certs/` is gitignored
    because such a bundle is machine-specific.
    """
    for name in _CA_BUNDLE_VARS:
        value = os.environ.get(name)
        if value and pathlib.Path(value).exists():
            return value
    return True



@dataclass(slots=True)
class Snapshot:
    source: str
    url: str
    fetched_at: str
    sha256: str
    nbytes: int
    storage_uri: str
    payload: object
    from_cache: bool = False
    observed_via: str = "aggregator_api"
    # False when the caller asked not to write (e.g. `mi metrics --dry-run`).
    # `storage_uri` is still the path the snapshot *would* occupy, so a report
    # can name it without the bytes existing.
    persisted: bool = True


def _url_key(url: str) -> str:
    """Cache directory is keyed by URL, not by source.

    A single source fetches many URLs (OpenRouter's model list and 400+ endpoint
    documents). Keying only by source means one fetch returns another URL's
    cached payload — a silent, very confusing corruption.
    """
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _local_path(source: str, url: str, day: str, digest: str) -> pathlib.Path:
    return RAW_ROOT / source / _url_key(url) / day / f"{digest}.json"


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _get_body(
    source: str,
    url: str,
    headers: dict[str, str],
    *,
    timeout: float,
    cache_ttl_s: int,
    force: bool,
) -> tuple[bytes, bool]:
    if not force:
        cached = _find_recent(source, url, cache_ttl_s)
        if cached is not None:
            return cached.read_bytes(), True
    with httpx.Client(timeout=timeout, follow_redirects=True, verify=_verify()) as c:
        r = c.get(url, headers=headers)
        r.raise_for_status()
        return r.content, False


def _snapshot_path(source: str, url: str, digest: str, ext: str = "json") -> pathlib.Path:
    """Where a snapshot of this body belongs. Pure — computes, never writes."""
    day = dt.date.today().isoformat()
    path = _local_path(source, url, day, digest)
    if ext != "json":
        path = path.with_suffix(f".{ext}")
    return path


def _write_snapshot(source: str, url: str, body: bytes, digest: str, ext: str = "json") -> pathlib.Path:
    path = _snapshot_path(source, url, digest, ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _snapshot_uri(source: str, path: pathlib.Path) -> str:
    # path = raw/<source>/<urlkey>/<date>/<digest>.<ext>
    return (f"s3://mininfer-evidence/{source}/{path.parent.parent.name}/"
            f"{path.parent.name}/{path.name}")


def fetch(
    source: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    cache_ttl_s: int = 6 * 3600,
    force: bool = False,
) -> Snapshot:
    """Fetch JSON, persisting an immutable raw snapshot keyed by content hash."""
    hdrs = {"User-Agent": _UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body, from_cache = _get_body(source, url, hdrs, timeout=timeout,
                                 cache_ttl_s=cache_ttl_s, force=force)
    digest = hashlib.sha256(body).hexdigest()
    if from_cache:
        cached = _find_recent(source, url, cache_ttl_s)
        return Snapshot(source, url, utcnow(), digest, len(body),
                        _snapshot_uri(source, cached), json.loads(body),
                        from_cache=True)
    path = _write_snapshot(source, url, body, digest)
    return Snapshot(source, url, utcnow(), digest, len(body),
                    _snapshot_uri(source, path), json.loads(body))


def fetch_raw(
    source: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    cache_ttl_s: int = 6 * 3600,
    force: bool = False,
    ext: str = "html",
    persist: bool = True,
) -> Snapshot:
    """Fetch a raw (non-JSON) page, snapshot it, and carry the bytes as payload.

    `persist=False` still returns the bytes and the intended `storage_uri`, but
    writes nothing. A dry run must not mutate the evidence lake while claiming
    to write nothing; the alternative — recording the snapshot row too — is
    reasonable, but a preview should be pure (#review).
    """
    hdrs = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml,text/plain,*/*"}
    if headers:
        hdrs.update(headers)
    body, from_cache = _get_body(source, url, hdrs, timeout=timeout,
                                 cache_ttl_s=cache_ttl_s, force=force)
    digest = hashlib.sha256(body).hexdigest()
    if from_cache:
        cached = _find_recent(source, url, cache_ttl_s)
        return Snapshot(source, url, utcnow(), digest, len(body),
                        _snapshot_uri(source, cached), body, from_cache=True)
    path = _snapshot_path(source, url, digest, ext)
    if persist:
        _write_snapshot(source, url, body, digest, ext=ext)
    return Snapshot(source, url, utcnow(), digest, len(body),
                    _snapshot_uri(source, path), body, persisted=persist)


def _find_recent(source: str, url: str, ttl_s: int) -> pathlib.Path | None:
    root = RAW_ROOT / source / _url_key(url)
    if not root.exists():
        return None
    newest, newest_mtime = None, 0.0
    # Any extension, not just `.json`. This glob used to be `*.json`, so every
    # `fetch_raw(..., ext="html")` snapshot was written to disk and then never
    # found again — a permanent cache miss that re-fetched the page on every call
    # and silently degraded the metrics scraper and the ingestion agents. The
    # directory is already keyed by source *and* URL hash, so everything under it
    # is a snapshot of the same thing and the extension is not part of the key.
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > newest_mtime:
            newest, newest_mtime = p, m
    if newest is None or (time.time() - newest_mtime) > ttl_s:
        return None
    return newest
