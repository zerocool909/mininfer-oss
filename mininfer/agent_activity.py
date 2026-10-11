"""Live activity for every agent the app runs.

The dashboard's bell used to be pricing only. But the two slowest things a user
can trigger from the UI — a scout pass (an LLM per free arm, plus web searches)
and (via the CLI) an ingest or resolve agent — are agentic jobs that can run for
minutes, and there was nowhere to see that one was running, what it had reached,
or whether it failed.

This is that surface: a small, in-process registry of agent runs with a status,
a progress counter and a rolling detail line. It is deliberately *not* a database
table. An agent run is live telemetry — "running", "arm 7 of 23", "wrote
openrouter:x:free" — not registry data, and the registry's mirror (`mi sync`) has
nothing to say about a process-local job. Keeping it in memory also means a
crashed worker cannot leave a row wedged at `running` forever, which is the
failure mode a persisted job table invites.

Bounded on purpose: the newest `MAX_RUNS` runs are kept and older finished runs
fall off, so a long-lived server cannot grow without limit on notification rows
nobody will read. Running jobs are never evicted.

Thread-safe because the scout runs in a worker thread (`asyncio.to_thread`) while
request handlers read the registry on the event loop.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from typing import Any

from .fetch import utcnow

#: How many runs to remember. Running jobs are always kept; finished ones are
#: evicted oldest-first once the history is full.
MAX_RUNS = 50

#: `detail` is one line rendered in a narrow drawer, not a log. Long messages are
#: clipped here rather than at each call site.
DETAIL_CHARS = 240


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= DETAIL_CHARS else text[: DETAIL_CHARS - 1] + "\u2026"


class AgentActivity:
    """A bounded, thread-safe log of agent runs.

    Every method takes the lock for its whole body: a reader always sees a run
    whose `status` and timestamps agree, and the `on_arm` callback from a worker
    thread cannot interleave with a request handler listing the registry.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    # ------------------------------------------------------------------ write

    def start(self, kind: str, title: str, *, total: int = 0,
              detail: str = "", meta: dict[str, Any] | None = None) -> str:
        """Register a run and return its id. `kind` is the agent family."""
        run_id = f"{kind}-{uuid.uuid4().hex[:10]}"
        now = utcnow()
        with self._lock:
            self._runs[run_id] = {
                "run_id": run_id,
                "kind": kind,
                "title": title,
                "status": "running",       # running | ok | error
                "detail": _clip(detail),
                "done": 0,
                "total": int(total or 0),
                "started_at": now,
                "updated_at": now,
                "finished_at": None,
                "meta": dict(meta or {}),
            }
            self._evict_locked()
        return run_id

    def update(self, run_id: str, *, detail: str | None = None,
               done: int | None = None, total: int | None = None,
               meta: dict[str, Any] | None = None) -> None:
        """Patch a running run. Unknown ids are ignored (the run was evicted)."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            if detail is not None:
                run["detail"] = _clip(detail)
            if done is not None:
                run["done"] = int(done)
            if total is not None:
                run["total"] = int(total)
            if meta:
                run["meta"].update(meta)
            run["updated_at"] = utcnow()

    def finish(self, run_id: str, *, status: str = "ok",
               detail: str | None = None) -> None:
        """Mark a run done. `status` is `ok` or `error`."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run["status"] != "running":
                return
            run["status"] = "error" if status == "error" else "ok"
            if detail is not None:
                run["detail"] = _clip(detail)
            now = utcnow()
            run["updated_at"] = now
            run["finished_at"] = now
            self._evict_locked()

    # ------------------------------------------------------------------- read

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            return dict(run) if run else None

    def running(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._runs.values() if r["status"] == "running"]

    def running_kind(self, kind: str) -> dict[str, Any] | None:
        """The newest running run of a kind, or None. Used to dedupe a start."""
        with self._lock:
            for run in reversed(self._runs.values()):
                if run["status"] == "running" and run["kind"] == kind:
                    return dict(run)
        return None

    def list(self, limit: int = MAX_RUNS) -> list[dict[str, Any]]:
        """Runs newest-first, capped at `limit`."""
        with self._lock:
            runs = [dict(r) for r in self._runs.values()]
        runs.reverse()
        return runs[: max(0, int(limit))]

    def snapshot(self, limit: int = MAX_RUNS) -> dict[str, Any]:
        runs = self.list(limit)
        active = [r for r in runs if r["status"] == "running"]
        return {"count": len(runs), "running": len(active), "runs": runs}

    def reset(self) -> None:
        """Drop every run. For tests, so one test cannot leak into the next."""
        with self._lock:
            self._runs.clear()

    # ------------------------------------------------------------------ inner

    def _evict_locked(self) -> None:
        """Trim finished runs from the front once history overflows.

        `OrderedDict` iteration is oldest-first, so the front is the oldest. A
        running run is skipped rather than evicted: under load, losing a live
        job's progress to make room for a finished one would be exactly backwards.
        """
        while len(self._runs) > MAX_RUNS:
            for run_id, run in self._runs.items():
                if run["status"] != "running":
                    del self._runs[run_id]
                    break
            else:
                return  # every run is running; nothing safe to drop


#: The process-wide registry. The proxy writes and reads this; tests reset it.
ACTIVITY = AgentActivity()
