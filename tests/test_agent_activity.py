"""The agent activity registry.

The bell's agent section reads this, so the properties that matter are: a run's
timestamps and status never disagree with each other, a finished run stays
readable, and a long-lived server cannot grow the history without bound.
"""
from __future__ import annotations

from mininfer.agent_activity import MAX_RUNS, AgentActivity


def test_a_run_moves_from_running_to_finished():
    activity = AgentActivity()
    run_id = activity.start("scout", "Scout pass", total=3, detail="starting")

    running = activity.running()
    assert [r["run_id"] for r in running] == [run_id]
    assert running[0]["status"] == "running"
    assert running[0]["finished_at"] is None
    assert running[0]["total"] == 3

    activity.update(run_id, done=2, detail="wrote openrouter:x:free")
    activity.finish(run_id, status="ok", detail="2 written, 1 fresh, 0 errors")

    assert activity.running() == []
    done = activity.get(run_id)
    assert done is not None
    assert done["status"] == "ok"
    assert done["done"] == 2
    assert done["finished_at"] is not None
    assert "2 written" in done["detail"]


def test_finishing_is_idempotent_and_error_is_preserved():
    activity = AgentActivity()
    run_id = activity.start("scout", "Scout pass")
    activity.finish(run_id, status="error", detail="boom")

    # A second finish must not overwrite the first verdict with a late "ok".
    activity.finish(run_id, status="ok", detail="late")
    assert activity.get(run_id)["status"] == "error"
    assert activity.get(run_id)["detail"] == "boom"


def test_updating_an_unknown_run_is_a_no_op():
    """An evicted run's worker can still call back; that must not raise."""
    activity = AgentActivity()
    activity.update("scout-nope", done=1, detail="ignored")
    activity.finish("scout-nope", status="ok")
    assert activity.list() == []


def test_running_kind_finds_the_newest_live_pass():
    activity = AgentActivity()
    first = activity.start("scout", "Scout pass")
    activity.finish(first, status="ok")
    second = activity.start("scout", "Scout pass")
    activity.start("ingest", "Ingest url")

    assert activity.running_kind("scout")["run_id"] == second
    assert activity.running_kind("resolve") is None


def test_history_is_bounded_and_never_evicts_a_running_run():
    activity = AgentActivity()
    live = activity.start("scout", "Scout pass")
    for i in range(MAX_RUNS + 10):
        run_id = activity.start("ingest", f"job {i}")
        activity.finish(run_id, status="ok")

    runs = activity.list(limit=MAX_RUNS + 100)
    assert len(runs) == MAX_RUNS
    # The live run survived every eviction, and the newest finished one is here.
    assert live in {r["run_id"] for r in runs}
    assert runs[0]["title"] == f"job {MAX_RUNS + 9}"


def test_snapshot_counts_what_is_running():
    activity = AgentActivity()
    activity.start("scout", "Scout pass")
    done = activity.start("ingest", "Ingest url")
    activity.finish(done, status="ok")

    snap = activity.snapshot()
    assert snap["running"] == 1
    assert snap["count"] == 2
    # Newest first: the finished ingest run was started last, so it leads; the
    # still-running scout follows.
    assert [r["status"] for r in snap["runs"]] == ["ok", "running"]


def test_detail_is_clipped_to_one_short_line():
    activity = AgentActivity()
    run_id = activity.start("scout", "Scout pass",
                            detail="line one\nline two   with   spaces " + "x" * 400)
    detail = activity.get(run_id)["detail"]
    assert "\n" not in detail
    assert "  " not in detail
    assert len(detail) <= 240
