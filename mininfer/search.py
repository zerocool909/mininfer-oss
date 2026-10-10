"""Web search — a free-first tier, snapshotted like every other source.

Three providers behind one call, chosen for cost in that order:

* ``wikipedia`` — the Wikimedia API. No key, no quota, no scraping: it is the
  only genuinely free option that is also *supported* rather than tolerated.
  It answers factual queries and nothing else, which is most of what an agent
  asks for.
* ``duckduckgo`` — the lite HTML endpoint. No key, no quota, but unofficial:
  it is markup scraping, so it is treated exactly like the Vercel metrics page —
  the raw bytes are snapshotted before parsing and the parser is a pure function
  of that snapshot.
* ``tinyfish`` — the structured free tier for agents. One GET with an
  ``X-API-Key`` header (``TINYFISH_API_KEY``), returns JSON with URLs, titles,
  snippets and metadata. Free up to 12,000 requests/day (resets 00:00 UTC);
  beyond that it answers ``402 INSUFFICIENT_CREDITS`` until a wallet is funded.
* ``tavily`` — the paid tier, and the only one built for this job (it returns
  extracted page content rather than links). Server-owned key
  (``TAVILY_API_KEY``), because the point of putting search behind the proxy is
  that callers never hold the credential.

``provider="auto"`` tries the free tiers first and only reaches for the paid one
when they come back empty. That is the same rule the router already applies to
models — free first, pay only when free isn't good enough — applied to the
search that feeds them.

Every response is snapshotted to ``raw/<provider>/…`` before it is parsed, so a
search is auditable and replayable after the fact. That matters more here than
anywhere else: the content being fetched is the *untrusted* part. A result is
evidence to show a caller, never a fact to merge into the registry.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field

import httpx

from .fetch import (
    Snapshot,
    _UA,
    _snapshot_uri,
    _verify,
    _write_snapshot,
    utcnow,
)

PROVIDERS = ("wikipedia", "duckduckgo", "tinyfish", "tavily")
FREE_PROVIDERS = ("wikipedia", "duckduckgo", "tinyfish")

WIKI_API = "https://en.wikipedia.org/w/api.php"
DDG_LITE = "https://lite.duckduckgo.com/lite/"
TAVILY_API = "https://api.tavily.com/search"
TINYFISH_API = "https://api.search.tinyfish.ai"

#: List price for one basic Tavily search. Verified against their pricing page:
#: 1,000 credits/month free, then $0.008 per credit.
TAVILY_COST_USD = 0.008

# Wikimedia's User-Agent policy asks for a product name plus a contact, and it
# enforces it by returning *zero hits* for a generic agent rather than an error —
# `MinInfer/0.1 (+model-intelligence-registry)` gets `totalhits: 0`, and a
# descriptive agent gets 661 for the same query. That is a silent-empty, so the
# fix belongs here and the symptom is worth naming.
#
# Override with MI_USER_AGENT when deploying this for real: the address below
# is a placeholder, and Wikimedia would rather have a reachable one.
WIKI_UA = os.environ.get(
    "MI_USER_AGENT"
) or os.environ.get(
    "MI_USER_AGENT",
    "mininfer/0.1 (model-intelligence-registry; +https://github.com/mininfer-ai)")



@dataclass
class SearchResult:
    """Results plus where they came from, so a caller can judge them."""

    query: str
    provider: str
    results: list[dict] = field(default_factory=list)
    #: Which providers were tried before this one answered, in order.
    tried: list[str] = field(default_factory=list)
    url: str = ""
    sha256: str = ""
    snapshot: str = ""
    from_cache: bool = False
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.results)

    def as_dict(self) -> dict:
        return {
            "query": self.query, "provider": self.provider, "results": self.results,
            "tried": self.tried, "url": self.url, "sha256": self.sha256,
            "snapshot": self.snapshot, "from_cache": self.from_cache,
            "cost_usd": self.cost_usd, "error": self.error,
        }


# --------------------------------------------------------------------------- #
# parsing — pure functions of a snapshot, so a markup change is a parser fix
# --------------------------------------------------------------------------- #

_TAG = re.compile(r"<[^>]+>")
# The lite page is a table with one `result-link` anchor per hit. Attribute order
# is not stable (a probe returned href-then-class, the documented shape is
# class-then-href), so the anchor tag is matched first and its attributes are
# read out of it rather than matched in a fixed order.
_ANCHOR = re.compile(r"(<a\b[^>]*\bresult-link\b[^>]*>)(.*?)</a>",
                     re.IGNORECASE | re.DOTALL)
_HREF = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_SNIPPET = re.compile(r'class="result-snippet"[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)
# A block page looks like results-free markup: DDG serves a captcha modal instead
# of hits when it decides the caller is a bot. Detecting it turns "no results"
# into "refused", which is the difference between an empty search and a lie.
_BLOCKED = re.compile(r"anomaly-modal|feedback-instructions", re.IGNORECASE)


def _text(html: str) -> str:
    return _TAG.sub("", html).replace("&amp;", "&").replace("&quot;", '"') \
        .replace("&#x27;", "'").replace("&lt;", "<").replace("&gt;", ">").strip()


def parse_wikipedia(payload: bytes, limit: int) -> list[dict]:
    """Wikimedia search JSON -> results. No scraping involved."""
    try:
        j = json.loads(payload)
    except ValueError:
        return []
    out = []
    for hit in ((j.get("query") or {}).get("search") or [])[:limit]:
        pageid = hit.get("pageid")
        out.append({
            "title": hit.get("title", ""),
            "url": (f"https://en.wikipedia.org/?curid={pageid}" if pageid
                    else "https://en.wikipedia.org/"),
            "snippet": _text(hit.get("snippet") or ""),
        })
    return out


def parse_duckduckgo(payload: bytes, limit: int) -> list[dict]:
    """DDG lite results -> results.

    Snippets are matched positionally because the markup carries no ids, which is
    precisely why the raw bytes are kept — a change here is a parser fix plus a
    replay, not lost data.

    Raises `ProviderBlocked` when DDG answers with its bot check instead of
    results. That is a routine outcome for an unofficial endpoint, and it must not
    be reported as "this query has no answers".
    """
    html = payload.decode("utf-8", errors="replace")
    if _BLOCKED.search(html):
        raise ProviderBlocked("duckduckgo served its bot check instead of results")
    snippets = [_text(s) for s in _SNIPPET.findall(html)]
    out = []
    for i, (tag, label) in enumerate(_ANCHOR.findall(html)):
        if len(out) >= limit:
            break
        href = _HREF.search(tag)
        out.append({
            "title": _text(label),
            "url": href.group(1) if href else "",
            "snippet": snippets[i] if i < len(snippets) else "",
        })
    return out


def parse_tinyfish(payload: bytes, limit: int) -> list[dict]:
    """TinyFish Search JSON -> results.

    `results[]` carries `title`, `url`, `snippet` and, depending on
    `domain_type`, extra metadata (publisher, authors, citation count). The
    shared trio is what every consumer of this module reads; the extras are kept
    so a news/academic search loses nothing.
    """
    try:
        j = json.loads(payload)
    except ValueError:
        return []
    out = []
    for hit in (j.get("results") or [])[:limit]:
        row = {
            "title": hit.get("title") or "",
            "url": hit.get("url") or "",
            "snippet": (hit.get("snippet") or "")[:2000],
        }
        # Preserve the structured extras when present, absent when not.
        for field in ("site_name", "date", "publisher", "authors", "venue",
                      "year", "cited_by_count", "pdf_url"):
            if hit.get(field) is not None:
                row[field] = hit[field]
        out.append(row)
    return out


def parse_tavily(payload: bytes, limit: int) -> list[dict]:
    try:
        j = json.loads(payload)
    except ValueError:
        return []
    out = []
    for hit in (j.get("results") or [])[:limit]:
        out.append({
            "title": hit.get("title") or "",
            "url": hit.get("url") or "",
            # Tavily returns extracted content, which is the reason to pay for it.
            "snippet": (hit.get("content") or "")[:2000],
            "score": hit.get("score"),
        })
    return out


PARSERS = {
    "wikipedia": parse_wikipedia,
    "duckduckgo": parse_duckduckgo,
    "tinyfish": parse_tinyfish,
    "tavily": parse_tavily,
}


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #


class ProviderBlocked(RuntimeError):
    """A provider answered, but with a bot check rather than results."""


def _snapshot(source: str, url: str, body: bytes, ext: str) -> Snapshot:
    """Persist bytes the same way `fetch_raw` does, for callers that POST.

    `fetch_raw` is GET-only, and Tavily's API is a POST with the key in the body,
    so this is the one place a search response is written without it.
    """
    digest = hashlib.sha256(body).hexdigest()
    path = _write_snapshot(source, url, body, digest, ext=ext)
    return Snapshot(source, url, utcnow(), digest, len(body),
                    _snapshot_uri(source, path), body)


def _url_for(provider: str, query: str, limit: int) -> str:
    q = httpx.QueryParams({"q": query})
    if provider == "wikipedia":
        return (f"{WIKI_API}?action=query&list=search&format=json&srlimit={limit}"
                f"&srsearch={q['q']}")
    return f"{DDG_LITE}?{q}"


def _call(provider: str, query: str, limit: int, *, timeout: float,
          force: bool) -> Snapshot:
    """One provider, one snapshot. Raises on transport failure."""
    if provider == "tinyfish":
        key = os.environ.get("TINYFISH_API_KEY", "").strip()
        if not key:
            raise RuntimeError("TINYFISH_API_KEY is not set")
        with httpx.Client(timeout=timeout, verify=_verify(),
                          follow_redirects=True) as c:
            r = c.get(TINYFISH_API, params={"query": query},
                      headers={"X-API-Key": key, "User-Agent": _UA})
            if r.status_code == 402:
                # The provider's own daily free allowance is the ceiling here:
                # 12,000 requests/day, reset at 00:00 UTC. Surface it distinctly
                # rather than as a generic 4xx.
                raise RuntimeError(
                    "tinyfish: daily free allowance exhausted (402 INSUFFICIENT_CREDITS)")
            r.raise_for_status()
        return _snapshot("tinyfish", str(r.url), r.content, "json")

    if provider == "tavily":
        key = os.environ.get("TAVILY_API_KEY", "").strip()
        if not key:
            raise RuntimeError("TAVILY_API_KEY is not set")
        body = {"api_key": key, "query": query, "max_results": limit,
                "search_depth": "basic"}
        with httpx.Client(timeout=timeout, verify=_verify(),
                          follow_redirects=True) as c:
            r = c.post(TAVILY_API, json=body,
                       headers={"User-Agent": _UA, "Content-Type": "application/json"})
            r.raise_for_status()
        return _snapshot("tavily", TAVILY_API, r.content, "json")

    url = _url_for(provider, query, limit)
    # Reads through the raw lake, so a repeat query inside the TTL costs nothing
    # and the bytes behind any answer stay on disk.
    from .fetch import _get_body, _find_recent

    headers = {"User-Agent": WIKI_UA if provider == "wikipedia" else _UA,
               "Accept": "application/json,text/html,*/*"}
    payload, from_cache = _get_body(provider, url, headers, timeout=timeout,
                                    cache_ttl_s=3600, force=force)
    if from_cache:
        cached = _find_recent(provider, url, 3600)
        digest = hashlib.sha256(payload).hexdigest()
        return Snapshot(provider, url, utcnow(), digest, len(payload),
                        _snapshot_uri(provider, cached), payload, from_cache=True)
    return _snapshot(provider, url, payload,
                     "json" if provider == "wikipedia" else "html")


#: The env var each keyed search provider reads. Exposed so the portal can
#: configure a search key through the same verified `/v1/keys` flow as a model
#: provider — otherwise TinyFish is `.env`-only and cannot be set from the UI.
PROVIDER_ENV = {
    "tinyfish": "TINYFISH_API_KEY",
    "tavily": "TAVILY_API_KEY",
}


def verify_key(provider: str, api_key: str, *, timeout: float = 15.0
               ) -> tuple[bool, str]:
    """Check a search provider key with one minimal query. `(ok, detail)`.

    Called by the portal's "To server" step, so a key the provider rejects is
    never written to `.env`. A provider that is reachable but over its free
    allowance (TinyFish 402) or rate-limited (429) still proves the key is valid,
    so those count as accepted with a note.
    """
    provider = (provider or "").strip().lower()
    if provider not in PROVIDER_ENV:
        return False, f"{provider!r} is not a search provider"
    api_key = (api_key or "").strip()
    if not api_key:
        return False, "empty key"
    try:
        with httpx.Client(timeout=timeout, verify=_verify(),
                          follow_redirects=True) as c:
            if provider == "tinyfish":
                r = c.get(TINYFISH_API, params={"query": "ping"},
                          headers={"X-API-Key": api_key, "User-Agent": _UA})
                if r.status_code == 401:
                    return False, "TinyFish rejected the key (401)"
                if r.status_code == 402:
                    return True, "TinyFish key accepted (daily free allowance used)"
                if r.status_code in (403, 429):
                    return True, f"TinyFish key accepted ({r.status_code})"
                r.raise_for_status()
                return True, "TinyFish key accepted"
            r = c.post(TAVILY_API,
                       json={"api_key": api_key, "query": "ping",
                             "max_results": 1, "search_depth": "basic"},
                       headers={"User-Agent": _UA, "Content-Type": "application/json"})
        if r.status_code in (401, 403):
            return False, f"Tavily rejected the key ({r.status_code})"
        r.raise_for_status()
        return True, "Tavily key accepted"
    except Exception as exc:  # noqa: BLE001 - a probe failure is a returned verdict
        return False, f"{type(exc).__name__}: {exc}"


def allow_paid() -> bool:
    """Whether `auto` may fall through to the paid Tavily tier.

    Default on, so behaviour is unchanged. Set `MI_SEARCH_ALLOW_PAID=0` for a
    free-only "birthright" search: an empty free tier is then an empty result,
    never a silent $0.008 charge.
    """
    raw = os.environ.get("MI_SEARCH_ALLOW_PAID", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def search_cost(provider: str = "auto") -> float:
    """Worst-case price of a search, before running it.

    `auto` reserves the paid tier's rate because it can fall through to it, which
    is the number a budget has to check against. Zero when the paid tier is
    disabled (`MI_SEARCH_ALLOW_PAID=0`), because then it genuinely cannot be
    reached. Env-overridable so a deployment on a different plan is not stuck
    with a hardcoded rate.
    """
    if provider == "auto" and not allow_paid():
        return 0.0
    if provider not in ("auto", "tavily"):
        return 0.0
    try:
        env_cost = os.environ.get("MI_SEARCH_COST_USD")
        return max(0.0, float(env_cost or TAVILY_COST_USD))
    except ValueError:
        return TAVILY_COST_USD



def search(
    query: str,
    *,
    provider: str = "auto",
    limit: int = 5,
    timeout: float = 20.0,
    force: bool = False,
    tavily_cost_usd: float | None = None,
    store=None,
) -> SearchResult:
    """Search the web, free tiers first.

    `provider="auto"` walks the free providers and stops at the first one with
    results; it only reaches Tavily when nothing free answered, so the paid tier
    stays a fallback rather than a default.

    Pass a `Store` to record the snapshot row, so every fetched byte is in the
    registry and not merely on disk — the same guarantee `mi metrics` and the
    ingestion agents already keep. Optional so `search()` stays callable with no
    database at all.
    """
    if tavily_cost_usd is None:
        tavily_cost_usd = search_cost("tavily")
    query = (query or "").strip()
    if not query:
        return SearchResult(query, provider, error="empty query")
    if provider != "auto" and provider not in PARSERS:
        return SearchResult(query, provider, error=f"unknown provider {provider!r}")

    if provider == "auto":
        order = FREE_PROVIDERS + (("tavily",) if allow_paid() else ())
    else:
        order = (provider,)
    tried: list[str] = []
    last_error: str | None = None
    for name in order:
        tried.append(name)
        try:
            snap = _call(name, query, limit, timeout=timeout, force=force)
        except Exception as exc:  # noqa: BLE001 - a provider being down is not fatal
            last_error = f"{name}: {type(exc).__name__}: {exc}"
            continue
        if store is not None:
            # INSERT OR IGNORE on sha256, so a cache hit re-recording its own row
            # is a no-op rather than a duplicate.
            store.record_snapshot(snap)
        try:
            rows = PARSERS[name](snap.payload, limit)
        except ProviderBlocked as exc:
            last_error = f"{name}: {exc}"
            continue
        if not rows and name != order[-1]:
            last_error = f"{name}: no results"
            continue
        return SearchResult(
            query=query, provider=name, results=rows, tried=tried,
            url=snap.url, sha256=snap.sha256, snapshot=snap.storage_uri,
            from_cache=snap.from_cache,
            cost_usd=tavily_cost_usd if name == "tavily" else 0.0,
            error=None if rows else last_error or "no results",
        )
    return SearchResult(query=query, provider=provider, tried=tried,
                        error=last_error or "no provider returned results")
