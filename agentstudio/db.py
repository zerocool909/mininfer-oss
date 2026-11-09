"""Local storage for the agent studio: SQLite, one file, no services.

The registry lives beside the canvas and the traces, on the same engine
mininfer's local profile uses — with the same OneDrive lesson applied: the
caller chooses the path, and nothing here assumes a synced folder.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    canvas     TEXT NOT NULL,             -- nodes + edges + layout, JSON
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One row per run of one agent. `cost_usd` is the sum over node_runs.
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents(id),
    input       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',   -- running|finished|failed
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    cost_usd    REAL,
    tokens      INTEGER,
    error       TEXT
);

-- The payoff table: which arm served each node, what it cost, who lost.
CREATE TABLE IF NOT EXISTS node_runs (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES runs(id),
    node_id    TEXT NOT NULL,
    node_kind  TEXT NOT NULL,           -- llm|branch|recall|tool|...
    task       TEXT,                    -- mininfer task the node declared
    arm        TEXT,                    -- deploy_id from mi.reason.selected
    latency_ms INTEGER,
    tokens     INTEGER,
    cost_usd   REAL,
    error      TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_agent  ON runs(agent_id, started_at);
CREATE INDEX IF NOT EXISTS idx_noderuns_run ON node_runs(run_id);

-- Documents for RAG nodes; chunks point into the vector table by store.
CREATE TABLE IF NOT EXISTS documents (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id),
    content     TEXT NOT NULL,
    embedding   BLOB NOT NULL,          -- packed float32
    dim         INTEGER NOT NULL,
    model       TEXT NOT NULL           -- which embedder produced it
);
"""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


class StudioDB:
    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        # One connection per thread would be Redis-grade plumbing we do not
        # need yet; callers make one StudioDB per task (like Store does).
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- documents -----------------------------------------------------------

    def add_document(self, *, doc_id: str, title: str, content: str,
                     agent_id: str | None = None,
                     embedding: bytes | None = None, dim: int = 0,
                     model: str = "hash-fallback") -> None:
        self.conn.execute(
            "INSERT INTO documents(id, agent_id, title, content, created_at)"
            " VALUES(?,?,?,?,?)",
            (doc_id, agent_id, title, content, _now()))
        if embedding is not None:
            for chunk_text in _chunk(content):
                self.conn.execute(
                    "INSERT INTO chunks(document_id, content, embedding, dim, model)"
                    " VALUES(?,?,?,?,?)",
                    (doc_id, chunk_text, embedding, dim, model))
        self.conn.commit()

    def chunks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT id, document_id, content, embedding, dim, model FROM chunks"))

    # -- runs ----------------------------------------------------------------

    def start_run(self, *, run_id: str, agent_id: str, input_text: str) -> None:
        self.conn.execute(
            "INSERT INTO runs(id, agent_id, input, status, started_at)"
            " VALUES(?,?,?,?,?)", (run_id, agent_id, input_text, "running", _now()))
        self.conn.commit()

    def record_node(self, *, node_run_id: str, run_id: str, node_id: str,
                    node_kind: str, arm: str | None = None, task: str | None = None,
                    latency_ms: int | None = None, tokens: int | None = None,
                    cost_usd: float | None = None, error: str | None = None) -> float:
        """Insert one node_run row. Returns the cost actually recorded (0.0 when
        the gateway did not state one) so callers can accumulate honestly."""
        self.conn.execute(
            "INSERT INTO node_runs(id, run_id, node_id, node_kind, task, arm,"
            " latency_ms, tokens, cost_usd, error, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (node_run_id, run_id, node_id, node_kind, task, arm,
             latency_ms, tokens, cost_usd, error, _now()))
        self.conn.commit()
        return float(cost_usd or 0.0)

    def finish_run(self, *, run_id: str, status: str = "finished",
                   cost_usd: float | None = None, tokens: int | None = None,
                   error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET status=?, finished_at=?, cost_usd=?, tokens=?, error=?"
            " WHERE id=?", (status, _now(), cost_usd, tokens, error, run_id))
        self.conn.commit()

    def run_summary(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT node_id, node_kind, arm, latency_ms, tokens, cost_usd, error"
            " FROM node_runs WHERE run_id=? ORDER BY created_at", (run_id,)))


def _chunk(content: str, *, size: int = 400) -> list[str]:
    """Paragraph-aware word chunks, small enough for one embedding each."""
    words, out, buf = content.split(), [], ""
    for word in words:
        if len(buf) + len(word) + 1 > size and buf:
            out.append(buf)
            buf = word
        else:
            buf = f"{buf} {word}".strip()
    if buf:
        out.append(buf)
    return out or [content]
