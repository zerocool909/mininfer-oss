"""Supabase / Postgres sync (Phase 6).

The local SQLite registry is the development source of truth; Postgres is the hot
shared registry and the read-only dashboard's backing store. The schema is the
same shape (see `supabase/schema.sql`), so a sync is a straight copy:

  * **keyed tables** (weights, deployments, snapshots, quota_buckets,
    weight_aliases) are upserted by primary key, preserving `first_seen` so churn
    stays measurable;
  * **append tables** (evidence, observations, decisions, quarantine) are
    replaced wholesale, which makes a re-sync idempotent.

Raw payloads never go to Postgres — they stay in the evidence lake (`raw/` or
R2/S3) and `snapshots` only points at them.

The connection string comes from `SUPABASE_DB_URL` (or `DATABASE_URL`), loaded
from `.env`. Use the **pooler** host: newer Supabase projects only expose the
direct `db.<ref>.supabase.co` host over IPv6. The publishable/anon key is for the
REST API and is not used here.
"""
from __future__ import annotations

import os
import pathlib

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "supabase" / "schema.sql"

_KEYED = ["weights", "weight_aliases", "deployments", "snapshots", "quota_buckets",
          "sessions", "pushed_models", "price_resolution", "price_anomalies"]
# `pricing_evidence` is append-only like `observations`, but it always travels:
# it is the input the stored `deployments.price_*` was resolved from, so a mirror
# without it holds prices whose provenance cannot be checked. At ~3 rows per
# deployment it is far smaller than the opt-in `evidence` log.
_APPEND = ["observations", "decisions", "quarantine", "pricing_evidence",
           "pricing_transitions", "price_history"]
# Evidence is large (~200k rows) and opt-in; when requested it goes after the
# keyed tables so entity ids exist first.
_EVIDENCE = "evidence"
_PK = {
    "weights": ["weights_id"],
    "weight_aliases": ["alias_id"],
    "deployments": ["deploy_id"],
    "snapshots": ["sha256"],
    "quota_buckets": ["deploy_id", "window", "api_key_alias"],
    # `sessions` was in `_KEYED` with no entry here, so a sync of a registry that
    # had any session rows died with `KeyError: 'sessions'`. Empty tables returned
    # early before the lookup, which is the only reason it was not caught.
    "sessions": ["session_id"],
    "pushed_models": ["deploy_id"],
    # The reconciler's decision. Derived, but synced so the Postgres read model
    # carries the same price *and* the same justification without re-running
    # Python over the mirror.
    "price_resolution": ["deploy_id", "kind"],
    # The anomaly log. Keyed on the content-derived id, not a rowid, because sync
    # drops surrogate ids.
    "price_anomalies": ["anomaly_id"],
}# Never overwritten on conflict — first_seen is what makes churn measurable.
_PROTECTED = {"first_seen"}


def ddl() -> str:
    return SCHEMA_PATH.read_text()


def database_url(explicit: str | None = None) -> str | None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # dotenv is optional
        pass
    return (explicit or os.environ.get("SUPABASE_DB_URL")
            or os.environ.get("DATABASE_URL"))


def _rows(store, table: str) -> tuple[list[str], list[tuple]]:
    return store.table_rows(table)


def plan(store, *, tables: list[str] | None = None,
         evidence: bool = False) -> list[tuple[str, list[str], list[tuple]]]:
    order = tables if tables else (_KEYED + ([_EVIDENCE] if evidence else []) + _APPEND)
    out = []
    for t in order:
        cols, rows = _rows(store, t)
        out.append((t, cols, rows))
    return out


def sync(
    store,
    url: str,
    *,
    tables: list[str] | None = None,
    evidence: bool = False,
    dry_run: bool = False,
    apply_schema: bool = True,
    batch: int = 1000,
) -> dict:
    planned = plan(store, tables=tables, evidence=evidence)
    counts = {t: len(rows) for t, _, rows in planned}
    if dry_run:
        return {"dry_run": True, "tables": counts}

    import psycopg2
    from psycopg2.extras import execute_values

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            if apply_schema:
                cur.execute(ddl())
            for table, cols, rows in planned:
                if table in _KEYED:
                    _upsert(cur, execute_values, table, cols, rows, batch)
                else:
                    cur.execute(f"DELETE FROM {table}")
                    _insert(cur, execute_values, table, cols, rows, batch)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"tables": counts}


def _template(cols: list[str]) -> str:
    return "(" + ", ".join(["%s"] * len(cols)) + ")"


def _q(name: str) -> str:
    # Quote identifiers: `window` is a reserved word in Postgres.
    return '"' + name + '"'


def _insert(cur, execute_values, table: str, cols: list[str],
            rows: list[tuple], batch: int) -> None:
    if not rows:
        return
    collist = ", ".join(_q(c) for c in cols)
    sql = f"INSERT INTO {table} ({collist}) VALUES %s"
    execute_values(cur, sql, rows, template=_template(cols), page_size=batch)


def _upsert(cur, execute_values, table: str, cols: list[str],
            rows: list[tuple], batch: int) -> None:
    if not rows:
        return
    pk = _PK[table]
    updates = [c for c in cols if c not in pk and c not in _PROTECTED]
    collist = ", ".join(_q(c) for c in cols)
    conflict = ", ".join(_q(c) for c in pk)
    base = f"INSERT INTO {table} ({collist}) VALUES %s ON CONFLICT ({conflict})"
    if updates:
        setlist = ", ".join(f"{_q(c)}=EXCLUDED.{_q(c)}" for c in updates)
        sql = f"{base} DO UPDATE SET {setlist}"
    else:
        sql = f"{base} DO NOTHING"
    execute_values(cur, sql, rows, template=_template(cols), page_size=batch)
