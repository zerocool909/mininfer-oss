"""Tests for the dashboard UI, including the interactive Playground tab.

The Playground is a browser client of `/v1/chat/completions`; these tests assert
the page wires it up (tab switch, prompt box, endpoint, task list) without
needing a browser.
"""
from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    return TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)


def test_dashboard_serves_tabbed_playground(tmp_path, monkeypatch):
    page = _client(tmp_path, monkeypatch).get("/legacy").text
    assert "data-tab=overview" in page
    assert "data-tab=playground" in page
    assert "id=pg-prompt" in page
    assert "id=pg-send" in page
    assert "id=pg-result" in page
    assert "id=pg-task" in page


def test_playground_targets_the_chat_endpoint(tmp_path, monkeypatch):
    page = _client(tmp_path, monkeypatch).get("/legacy").text
    # the send handler must POST to the OpenAI-compatible endpoint
    assert "/v1/chat/completions" in page
    assert "window.MI_TASKS=" in page


def test_playground_renders_fallback_note(tmp_path, monkeypatch):
    # the selected-model lookup + fallback notice must be present in the JS
    page = _client(tmp_path, monkeypatch).get("/legacy").text
    assert "fell back to" in page
    assert "cost_per_success" in page


def test_playground_streams_instead_of_blocking(tmp_path, monkeypatch):
    """The chat must ask for a stream and read SSE frames.

    It used to POST without `stream` and call `r.json()`, so an answer only
    appeared once it was finished and the clock was retrospective. The JSON
    fallback in the reader means a missing `stream` flag would still *look*
    correct, which is exactly why this asserts the flag itself.
    """
    page = _client(tmp_path, monkeypatch).get("/legacy").text
    assert "body.stream = true" in page
    assert "getReader()" in page
    assert "data:" in page
    # the trial flag rides a header, so it must be read from one
    assert "X-MI-Needs-Approval" in page


def test_playground_renders_markdown(tmp_path, monkeypatch):
    """Model answers are markdown. Escaping them verbatim is what made a table
    read as a wall of pipes and a heading as '###'."""
    page = _client(tmp_path, monkeypatch).get("/legacy").text
    assert "function md(" in page
    assert "function inline(" in page
    # escape-first: the renderer escapes the source, then adds its own tags
    assert "esc(src)" in page
    assert "answer-body md" in page
    # and the styles the rendered elements need
    assert ".answer-body.md table{" in page


REACT_DIST = pathlib.Path(__file__).resolve().parent.parent / "web" / "dist"


def test_root_serves_react_when_built_else_the_server_page(tmp_path, monkeypatch):
    """`/` is the React app once `npm run build` has run, and the dependency-free
    server-rendered page otherwise — so a fresh clone still has a UI."""
    page = _client(tmp_path, monkeypatch).get("/").text
    assert "<html" in page
    if (REACT_DIST / "index.html").exists():
        assert 'id="root"' in page
    else:
        assert "data-tab=overview" in page


def test_plan_endpoint_returns_the_ranking_without_calling_a_model(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    # empty registry -> a valid plan with nothing eligible, not an execution
    r = client.get("/v1/plan?task=code_edit")
    assert r.status_code == 200
    body = r.json()
    assert body["task"] == "code_edit"
    assert body["chosen"] == []
    assert "funnel" in body


def test_plan_endpoint_rejects_an_unknown_task(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).get("/v1/plan?task=nope")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "unknown_task"
