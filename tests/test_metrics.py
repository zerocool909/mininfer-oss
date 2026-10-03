"""Tests for the performance-metric enrichment (Vercel model-browser scrape).

The page payload is a Next.js RSC flight string, so the fixtures below build the
same `self.__next_f.push([1,"..."])` shape the real page uses — escaped JSON
inside a JS string literal — rather than checking in a 2 MB HTML blob.
"""
from __future__ import annotations

import json

from mininfer.metrics import MAX_CHANGE_FACTOR, apply, parse_metrics, preview, validate_row
from mininfer.store import Store


def _rsc_page(*metrics: dict) -> bytes:
    """Build a synthetic Vercel-style page carrying the given metrics objects.

    The real page embeds one `"metrics":{...}` object per model, so the fixture
    wraps each in its own object rather than collecting them into an array.
    """
    payload = json.dumps([{"metrics": m} for m in metrics])
    chunk = payload.replace("\\", "\\\\").replace('"', '\\"')
    return f'<script>self.__next_f.push([1,"{chunk}"])</script>'.encode()


def _metric(model: str, *, tput=None, avg_tput=None, ttft=None, avg_ttft=None) -> dict:
    tp = {}
    if tput is not None:
        tp["p50TokensPerSecond"] = tput
    if avg_tput is not None:
        tp["averageTokensPerSecond"] = avg_tput
    lat = {}
    if ttft is not None:
        lat["p50TimeToFirstTokenMs"] = ttft
    if avg_ttft is not None:
        lat["averageTimeToFirstTokenMs"] = avg_ttft
    return {"model": model, "provider": "deepseek", "throughput": tp, "latency": lat}


def test_parse_prefers_p50_over_average():
    html = _rsc_page(_metric("deepseek/v4-flash", tput=251, avg_tput=253.9,
                             ttft=350, avg_ttft=405.5))
    rows = parse_metrics(html)
    assert len(rows) == 1
    assert rows[0]["model"] == "deepseek/v4-flash"
    assert rows[0]["throughput"] == 251.0
    assert rows[0]["first_token_ms"] == 350.0


def test_parse_falls_back_to_average_when_p50_absent():
    html = _rsc_page(_metric("zai/glm", avg_tput=202.9, avg_ttft=2147.0))
    rows = parse_metrics(html)
    assert rows[0]["throughput"] == 202.9
    assert rows[0]["first_token_ms"] == 2147.0


def test_parse_skips_entries_without_a_model_id():
    html = _rsc_page({"provider": "x", "throughput": {}, "latency": {}},
                     _metric("good/model", tput=10, ttft=20))
    rows = parse_metrics(html)
    assert [r["model"] for r in rows] == ["good/model"]


def test_parse_handles_multiple_models():
    html = _rsc_page(_metric("a/one", tput=1, ttft=2), _metric("b/two", tput=3, ttft=4))
    rows = parse_metrics(html)
    assert {r["model"] for r in rows} == {"a/one", "b/two"}


def test_parse_returns_empty_on_unrelated_page():
    assert parse_metrics(b"<html><body>nothing here</body></html>") == []


def _seed_deployment(store: Store, deploy_id: str, *, first_token_ms=None,
                     throughput=None) -> None:
    provider, _, mid = deploy_id.partition(":")
    store.conn.execute(
        "INSERT INTO weights (weights_id, display_name) VALUES (?, ?)",
        (f"slug:{mid}", mid),
    )
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " first_token_ms, throughput) VALUES (?, ?, ?, ?, ?, ?)",
        (deploy_id, f"slug:{mid}", provider, mid, first_token_ms, throughput),
    )
    store.commit()


def _current(store: Store, deploy_id: str) -> dict:
    return dict(store.conn.execute(
        "SELECT first_token_ms, throughput FROM deployments WHERE deploy_id=?",
        (deploy_id,)).fetchone())


def test_apply_patches_only_existing_deployments(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:deepseek/v4-flash")
    rep = apply(store, [
        {"model": "deepseek/v4-flash", "throughput": 251.0, "first_token_ms": 350.0},
        {"model": "ghost/model", "throughput": 1.0, "first_token_ms": 1.0},
    ])
    assert rep == {"parsed": 2, "updated": 1, "missing": 1, "empty": 0,
                   "quarantined": 0, "quarantine_fields": []}
    assert _current(store, "vercel:deepseek/v4-flash") == {
        "first_token_ms": 350.0, "throughput": 251.0}
    # the ghost model must not have been created
    assert store.conn.execute(
        "SELECT COUNT(*) c FROM deployments WHERE deploy_id='vercel:ghost/model'"
    ).fetchone()["c"] == 0
    store.close()


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
# The page carries throughput and TTFT independently, so a row can arrive with
# one and not the other. Writing the absent one through is how a good latency
# silently becomes NULL — and latency is a router sort key, so the damage lands
# on ranking, not on the dashboard.


def test_apply_never_blanks_the_metric_the_page_omitted(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:m", first_token_ms=350.0, throughput=251.0)
    rep = apply(store, [{"model": "m", "throughput": None, "first_token_ms": 900.0}])
    assert rep["updated"] == 1
    assert _current(store, "vercel:m") == {
        "first_token_ms": 900.0, "throughput": 251.0}  # throughput survived
    store.close()


def test_apply_counts_a_row_with_no_values_and_touches_nothing(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:m", first_token_ms=350.0, throughput=251.0)
    rep = apply(store, [{"model": "m", "throughput": None, "first_token_ms": None}])
    assert rep["empty"] == 1
    assert rep["updated"] == 0
    assert _current(store, "vercel:m") == {
        "first_token_ms": 350.0, "throughput": 251.0}
    store.close()


def test_first_sighting_accepts_anything_plausible(tmp_path):
    """Nothing to disagree with yet, so only the physical bounds apply."""
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:m")
    rep = apply(store, [{"model": "m", "throughput": 251.0, "first_token_ms": 350.0}])
    assert rep["updated"] == 1
    assert rep["quarantined"] == 0
    assert _current(store, "vercel:m") == {
        "first_token_ms": 350.0, "throughput": 251.0}
    store.close()


def test_apply_quarantines_a_unit_change(tmp_path):
    """milliseconds republished as microseconds: 100x on the router's sort key."""
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:m", first_token_ms=350.0, throughput=251.0)
    rep = apply(store, [{"model": "m", "throughput": None, "first_token_ms": 35000.0}])
    assert rep["quarantined"] == 1
    assert rep["updated"] == 0
    assert _current(store, "vercel:m")["first_token_ms"] == 350.0  # unchanged
    row = store.quarantine_rows()[0]
    assert (row["source"], row["entity_id"], row["field"]) == (
        "metrics", "vercel:m", "first_token_ms")
    assert "moved 100x" in row["reason"]
    store.close()


def test_apply_accepts_a_plausible_improvement(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:m", first_token_ms=350.0, throughput=251.0)
    rep = apply(store, [{"model": "m", "throughput": 400.0, "first_token_ms": 300.0}])
    assert rep["quarantined"] == 0
    assert rep["updated"] == 1
    assert _current(store, "vercel:m") == {
        "first_token_ms": 300.0, "throughput": 400.0}
    store.close()


def test_the_change_ceiling_is_the_documented_factor(tmp_path):
    """The bound is a named constant, not a literal buried in a comparison."""
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:inside", first_token_ms=100.0)
    _seed_deployment(store, "vercel:outside", first_token_ms=100.0)
    inside = 100.0 * (MAX_CHANGE_FACTOR - 1)
    assert apply(store, [{"model": "inside", "first_token_ms": inside}])["quarantined"] == 0
    outside = 100.0 * (MAX_CHANGE_FACTOR + 1)
    assert apply(store, [{"model": "outside", "first_token_ms": outside}])["quarantined"] == 1
    store.close()


def test_apply_quarantines_out_of_range_and_nonfinite_values(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:m")
    rep = apply(store, [{"model": "m", "throughput": -5.0,
                         "first_token_ms": float("nan")}])
    assert rep["quarantined"] == 2
    assert rep["updated"] == 0
    assert _current(store, "vercel:m") == {
        "first_token_ms": None, "throughput": None}
    store.close()


def test_a_refused_field_does_not_discard_the_other(tmp_path):
    """The two metrics are independent claims, so they are judged independently."""
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:m", first_token_ms=350.0, throughput=251.0)
    rep = apply(store, [{"model": "m", "throughput": 5_000_000.0,
                         "first_token_ms": 300.0}])
    assert rep["quarantined"] == 1
    assert rep["updated"] == 1
    assert _current(store, "vercel:m") == {
        "first_token_ms": 300.0, "throughput": 251.0}
    store.close()


def test_quarantine_keeps_the_refused_value_for_review(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:m", throughput=251.0)
    apply(store, [{"model": "m", "throughput": 999_999_999.0}])
    row = store.quarantine_rows()[0]
    assert row["value"] == "999999999.0"
    assert row["reason"].startswith("REFUSED:")
    store.close()


def test_quarantine_is_idempotent_across_scrapes(tmp_path):
    """Re-scraping the same bad number must not pile up duplicate rows."""
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:m", throughput=251.0)
    for _ in range(3):
        apply(store, [{"model": "m", "throughput": 999_999_999.0}])
    assert len(store.quarantine_rows()) == 1
    store.close()


def test_preview_writes_nothing_but_agrees_with_apply(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed_deployment(store, "vercel:m", first_token_ms=350.0, throughput=251.0)
    rows = [
        {"model": "m", "throughput": 5_000_000.0, "first_token_ms": 300.0},
        {"model": "ghost", "throughput": 1.0, "first_token_ms": 1.0},
    ]
    seen = preview(store, rows)
    assert store.quarantine_rows() == []
    assert _current(store, "vercel:m") == {
        "first_token_ms": 350.0, "throughput": 251.0}
    real = apply(store, rows)
    for k in ("parsed", "missing", "empty", "quarantined", "quarantine_fields"):
        assert seen[k] == real[k], k
    store.close()


def test_validate_row_is_pure_and_reports_only_refusals():
    assert validate_row({"throughput": 251.0, "first_token_ms": 350.0}, {}) == {}
    assert validate_row({"throughput": None, "first_token_ms": None}, {}) == {}
    assert set(validate_row({"throughput": 0.0, "first_token_ms": 350.0},
                            {})) == {"throughput"}
    # a first sighting has no prior value to contradict, so bounds alone decide
    assert validate_row({"first_token_ms": 350.0}, {"first_token_ms": None}) == {}
    assert set(validate_row({"first_token_ms": 350.0},
                            {"first_token_ms": 3.0})) == {"first_token_ms"}
