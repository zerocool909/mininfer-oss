"""Search providers: free-first tiering, snapshots, and bot-check honesty.

The fixtures are synthetic — the lite page's markup is not stable enough to check
in, which is the point (see `test_a_bot_check_is_not_an_empty_result`). The one
real payload used here is the shape of a block page, which is short and stable.
"""
from __future__ import annotations

import json

import pytest

from mininfer import search as S

# --- fixtures ----------------------------------------------------------------


def _wiki_payload(*rows: tuple[str, str]) -> bytes:
    return json.dumps({"query": {"search": [
        {"pageid": i, "title": t, "snippet": f"<span class=\"searchmatch\">{t}</span> {s}"}
        for i, (t, s) in enumerate(rows, start=1)]}}).encode()


# One anchor per hit, and the snippet in the following cell. Both attribute
# orders appear because a live probe returned href-then-class while the documented
# shape is class-then-href.
_DDG_PAGE = b"""<html><body><table>
<tr><td><a rel="nofollow" href="https://a.example/x" class="result-link">PostgreSQL</a></td></tr>
<tr><td class="result-snippet">The <b>free</b> database</td></tr>
<tr><td><a class="result-link" href="https://b.example/y">Supabase</a></td></tr>
<tr><td class="result-snippet">A managed platform</td></tr>
</table></body></html>"""

# The shape DuckDuckGo actually served when it decided the caller was a bot:
# a captcha modal and no results at all.
_DDG_BLOCKED = b"""<html><body>
<div class="anomaly-modal__box"><div class="anomaly-modal__check"></div>
<p class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</p>
<input class="anomaly-modal__puzzle"></div>
<p class="feedback-instructions">Select all squares with a duck</p>
</body></html>"""


# --- parsing -----------------------------------------------------------------


def test_wikipedia_results_carry_links():
    rows = S.parse_wikipedia(_wiki_payload(("PostgreSQL", "a free database")), 5)
    assert [r["title"] for r in rows] == ["PostgreSQL"]
    assert rows[0]["url"] == "https://en.wikipedia.org/?curid=1"
    assert rows[0]["snippet"] == "PostgreSQL a free database"  # markup stripped


def test_duckduckgo_reads_both_attribute_orders():
    rows = S.parse_duckduckgo(_DDG_PAGE, 5)
    assert [(r["title"], r["url"]) for r in rows] == [
        ("PostgreSQL", "https://a.example/x"), ("Supabase", "https://b.example/y")]
    assert rows[0]["snippet"] == "The free database"


def test_duckduckgo_honours_the_limit():
    assert len(S.parse_duckduckgo(_DDG_PAGE, 1)) == 1


def test_a_bot_check_is_not_an_empty_result():
    """Returning [] here would report 'nothing found' for a query that was refused."""
    with pytest.raises(S.ProviderBlocked):
        S.parse_duckduckgo(_DDG_BLOCKED, 5)


def test_tavily_results_use_extracted_content():
    payload = json.dumps({"results": [
        {"title": "T", "url": "https://t.example/", "content": "body", "score": 0.9}]}).encode()
    rows = S.parse_tavily(payload, 5)
    assert rows == [{"title": "T", "url": "https://t.example/", "snippet": "body",
                     "score": 0.9}]


@pytest.mark.parametrize("parser", [S.parse_wikipedia, S.parse_tavily])
def test_json_parsers_survive_rubbish(parser):
    assert parser(b"not json at all", 5) == []
    assert parser(b"{}", 5) == []


def test_parse_is_a_pure_function_of_the_bytes():
    """Same bytes, same answer — the property the snapshot lake depends on."""
    assert S.parse_duckduckgo(_DDG_PAGE, 5) == S.parse_duckduckgo(_DDG_PAGE, 5)


# --- tiering -----------------------------------------------------------------


def _stub(monkeypatch, payloads: dict[str, object]):
    """Replace transport with canned payloads; record the order providers were hit."""
    seen: list[str] = []

    def fake(provider, query, limit, *, timeout, force):
        seen.append(provider)
        body = payloads.get(provider, b"{}")
        if isinstance(body, Exception):
            raise body
        return S.Snapshot(provider, f"http://{provider}", "now", "sha", len(body),
                          f"raw/{provider}/x", body)

    monkeypatch.setattr(S, "_call", fake)
    return seen


def test_auto_prefers_free_and_never_reaches_the_paid_tier(monkeypatch):
    seen = _stub(monkeypatch, {"wikipedia": _wiki_payload(("PostgreSQL", "s"))})
    r = S.search("postgres")
    assert r.provider == "wikipedia" and seen == ["wikipedia"]
    assert r.cost_usd == 0.0 and r.ok
    # it also did not probe duckduckgo: the first free tier answered
    assert "tavily" not in seen


def test_auto_falls_through_a_blocked_free_tier(monkeypatch):
    seen = _stub(monkeypatch, {
        "wikipedia": b"{}",  # no hits
        "duckduckgo": _DDG_BLOCKED,
        "tavily": json.dumps({"results": [
            {"title": "T", "url": "https://t.example/", "content": "body"}]}).encode(),
    })
    r = S.search("something obscure")
    assert r.provider == "tavily"
    assert r.tried == ["wikipedia", "duckduckgo", "tavily"]
    assert r.cost_usd == 0.008  # the paid tier is priced into the result
    assert r.results[0]["snippet"] == "body"


def test_auto_reports_why_every_provider_failed(monkeypatch):
    seen = _stub(monkeypatch, {
        "wikipedia": RuntimeError("boom"),
        "duckduckgo": _DDG_BLOCKED,
        "tavily": RuntimeError("TAVILY_API_KEY is not set"),
    })
    r = S.search("q")
    assert not r.ok and r.results == []
    assert "tavily" in (r.error or "")
    assert seen == ["wikipedia", "duckduckgo", "tavily"]


def test_free_tier_answer_costs_nothing(monkeypatch):
    _stub(monkeypatch, {"wikipedia": _wiki_payload(("A", "b"))})
    assert S.search("x").cost_usd == 0.0


def test_explicit_provider_is_not_a_fallback_chain(monkeypatch):
    """Naming a provider means only that provider is tried."""
    seen = _stub(monkeypatch, {"tavily": json.dumps({"results": [
        {"title": "T", "url": "u", "content": "c"}]}).encode()})
    r = S.search("q", provider="tavily")
    assert seen == ["tavily"] and r.provider == "tavily"


def test_empty_and_unknown_queries_are_refused_without_fetching(monkeypatch):
    seen = _stub(monkeypatch, {})
    assert S.search("   ").error == "empty query"
    assert "unknown provider" in (S.search("x", provider="bing").error or "")
    assert seen == []


def test_a_snapshot_is_kept_for_every_answer(monkeypatch):
    """Search results are the untrusted part, so they are replayable."""
    _stub(monkeypatch, {"wikipedia": _wiki_payload(("A", "b"))})
    r = S.search("x")
    assert r.sha256 == "sha" and r.snapshot.startswith("raw/")
    assert r.as_dict()["provider"] == "wikipedia"


def test_wikipedia_sends_a_descriptive_user_agent(monkeypatch):
    """Wikimedia answers a generic agent with zero hits, not an error.

    That is a silent-empty, so it is pinned here rather than left to be
    rediscovered: the same query returned `totalhits: 0` with one agent and 661
    with another.
    """
    import httpx

    seen: dict = {}

    class _Resp:
        content = json.dumps({"query": {"search": []}}).encode()
        status_code = 200

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            seen["post"] = kw
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)

    def fake_get(source, url, headers, **kw):
        seen["headers"] = headers
        seen["source"] = source
        return _Resp.content, False

    monkeypatch.setattr("mininfer.fetch._get_body", fake_get)
    monkeypatch.setattr(S, "_snapshot", lambda *a, **k: S.Snapshot(
        "wikipedia", "u", "now", "sha", 2, "raw/x", b"{}"))

    S.search("q", provider="wikipedia")
    ua = seen["headers"]["User-Agent"]
    assert ua != "MinInfer/0.1 (+model-intelligence-registry)"
    assert "mininfer" in ua.lower()
    assert "(" in ua  # Wikimedia asks for a contact in parentheses


# --------------------------------------------------------------------------- #
# the registry must know what was fetched, not just the filesystem
# --------------------------------------------------------------------------- #


def test_a_store_records_the_snapshot_row(tmp_path, monkeypatch):
    """Search wrote to `raw/` but never to `snapshots`, so the registry could not
    answer "what has this process fetched" — the guarantee `mi metrics` and the
    ingestion agents already keep."""
    from mininfer.store import Store

    _stub(monkeypatch, {"wikipedia": _wiki_payload(("A", "b"))})
    store = Store(tmp_path / "p.db")
    r = S.search("x", provider="wikipedia", store=store)
    store.commit()
    rows = [dict(x) for x in store.conn.execute("SELECT * FROM snapshots")]
    assert len(rows) == 1
    assert rows[0]["source"] == "wikipedia"
    assert rows[0]["sha256"] == r.sha256
    assert rows[0]["storage_uri"] == r.snapshot
    store.close()


def test_recording_is_idempotent_so_a_cache_hit_cannot_duplicate(tmp_path, monkeypatch):
    from mininfer.store import Store

    _stub(monkeypatch, {"wikipedia": _wiki_payload(("A", "b"))})
    store = Store(tmp_path / "p.db")
    for _ in range(3):
        S.search("x", provider="wikipedia", store=store)
        store.commit()
    assert store.conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"] == 1
    store.close()


def test_search_without_a_store_still_works(monkeypatch):
    """The database stays optional, so the module is usable on its own."""
    _stub(monkeypatch, {"wikipedia": _wiki_payload(("A", "b"))})
    assert S.search("x", provider="wikipedia").ok is True


def test_a_refused_response_is_still_recorded_as_evidence(tmp_path, monkeypatch):
    """A bot wall is a fetch, and the bytes are the proof it was served.

    Dropping the row because the parse failed would erase the only evidence that
    the provider refused — which is exactly what you want on disk when a source
    starts blocking.
    """
    from mininfer.store import Store

    _stub(monkeypatch, {"wikipedia": _DDG_BLOCKED})  # parsed, then refused
    store = Store(tmp_path / "p.db")
    r = S.search("x", provider="duckduckgo", store=store)
    store.commit()
    assert not r.ok
    assert store.conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"] == 1
    store.close()
