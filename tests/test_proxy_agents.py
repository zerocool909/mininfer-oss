"""The agent-activity API and the now-background scout pass.

The bug this pins: `/v1/dossiers/refresh` used to run the blocking scout on the
event loop, so a "Scout now" click stalled every other tab's request behind it.
The endpoint must register a run and answer at once, and the worker must report
progress through the registry the bell reads.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from mininfer import agent_activity, proxy


class _FakeStore:
    """Just enough `Store` for the worker: free arms, commit, close."""

    def __init__(self, arms):
        self._arms = arms
        self.committed = False
        self.closed = False

    def free_deployments(self):
        return self._arms

    def new_dossier_arms(self):
        return self._arms

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    return TestClient(proxy.app)


def setup_function():
    agent_activity.ACTIVITY.reset()


def teardown_function():
    agent_activity.ACTIVITY.reset()


def test_refresh_registers_a_run_and_returns_at_once(tmp_path, monkeypatch):
    launched: list[tuple[str, dict]] = []
    monkeypatch.setattr(proxy, "_launch_scout_run",
                        lambda run_id, body, tasks: launched.append((run_id, body)))

    resp = _client(tmp_path, monkeypatch).post("/v1/dossiers/refresh", json={"search": False})
    assert resp.status_code == 200

    body = resp.json()
    assert body["started"] is True and body["already_running"] is False
    # The run is registered (and thus visible to the bell) before the worker ends.
    run = agent_activity.ACTIVITY.get(body["run_id"])
    assert run is not None and run["status"] == "running"
    # The worker was handed the same id and the caller's options.
    assert launched and launched[0][0] == body["run_id"]
    assert launched[0][1]["search"] is False


def test_refresh_does_not_stack_a_second_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "_launch_scout_run", lambda *a: None)
    client = _client(tmp_path, monkeypatch)

    first = client.post("/v1/dossiers/refresh", json={}).json()
    second = client.post("/v1/dossiers/refresh", json={}).json()

    assert second["started"] is False and second["already_running"] is True
    assert second["run_id"] == first["run_id"]


def test_agents_activity_lists_runs(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    agent_activity.ACTIVITY.start("scout", "Scout pass")

    data = client.get("/v1/agents/activity").json()
    assert data["running"] == 1 and data["count"] == 1
    assert data["runs"][0]["kind"] == "scout"


def test_scout_worker_reports_progress_and_finishes(monkeypatch):
    store = _FakeStore([{"deploy_id": "openrouter:a"}, {"deploy_id": "groq:b"}])
    monkeypatch.setattr(proxy, "_store", lambda: store)

    def fake_scout(_store, **kwargs):
        # One arm written, one skipped as fresh: both must move the counter, or
        # the bar stops short on a perfectly healthy pass.
        kwargs["on_arm"]({"deploy_id": "openrouter:a", "display_name": "A"})
        kwargs["on_skip"]({"deploy_id": "groq:b", "reason": "fresh"})
        return {"scanned": 2, "written": 1, "skipped_fresh": 1, "errors": 0,
                "dossiers": []}

    monkeypatch.setattr("mininfer.scout.scout", fake_scout)

    run_id = agent_activity.ACTIVITY.start("scout", "Scout pass")
    proxy._scout_worker(run_id, {}, [])

    run = agent_activity.ACTIVITY.get(run_id)
    assert run["status"] == "ok"
    assert (run["done"], run["total"]) == (2, 2)
    assert "1 written" in run["detail"] and "1 fresh" in run["detail"]
    assert store.committed and store.closed


def test_scout_worker_reports_a_crash_as_a_failed_run(monkeypatch):
    store = _FakeStore([{"deploy_id": "openrouter:a"}])
    monkeypatch.setattr(proxy, "_store", lambda: store)

    def exploding_scout(_store, **_kwargs):
        raise RuntimeError("no agent models configured")

    monkeypatch.setattr("mininfer.scout.scout", exploding_scout)

    run_id = agent_activity.ACTIVITY.start("scout", "Scout pass")
    proxy._scout_worker(run_id, {}, [])

    run = agent_activity.ACTIVITY.get(run_id)
    assert run["status"] == "error"
    assert "no agent models configured" in run["detail"]
    # A failed pass must not be committed, and the connection is still released.
    assert not store.committed and store.closed
