"""The registry. One schema, two engines.

SQLite is the local backend and Postgres is the cloud one; which you get is
decided by the target string ("postgresql://..." versus a file path) and nowhere
else. `db.py` owns the dialect differences, and because this module is the only
place raw SQL is allowed to live (`tests/test_encapsulation.py` enforces that),
that is the entire distance between the two.

Rules enforced here:
  * benchmark data attaches to `weights`, economics attaches to `deployments`
  * `first_seen` is never overwritten, so churn is measurable
  * every write from ingest also records evidence
  * observations are append-only; all statistics are derived views
"""
from __future__ import annotations

import datetime as dt
import dataclasses as dc
import json
import os
import pathlib
import threading
from typing import Any, Iterable

from . import db
from .fetch import utcnow
from .schema import (CAP_KEYS, NON_MODEL_ERRORS, Deployment, Evidence, Weights)


_stamp_lock = threading.Lock()
_last_stamp: dt.datetime | None = None


def _unique_stamp() -> str:
    """Microsecond ISO timestamp that is strictly increasing within the process.

    Row identity here is content-derived and includes the stamp (see
    `_record_price_history`/`_open_anomaly`/`_transition_row`), so two writes in
    the same tick must not share one. `datetime.now()` is only as precise as the
    OS clock, and on Windows that advances in ~15.6 ms steps -- several rapid
    reconciles return the *same* microsecond string, the ids collide, and
    `ON CONFLICT DO NOTHING` silently drops the later belief. Nudging past the
    previous stamp keeps each value wall-clock accurate to within microseconds
    while guaranteeing uniqueness within the process.
    """
    global _last_stamp
    with _stamp_lock:
        now = dt.datetime.now(dt.timezone.utc)
        if _last_stamp is not None and now <= _last_stamp:
            now = _last_stamp + dt.timedelta(microseconds=1)
        _last_stamp = now
    return now.isoformat(timespec="microseconds")


def _per_mtok(stored: float | None) -> str:
    """Format a stored price. The column is already dollars per Mtok.

    `ingest.shared.per_mtok_from_per_token` is what converts a provider's
    per-token figure on the way in, so anything formatted here must *not* scale
    again — doing so was a 1e6 error in the hibernation reason.
    """
    return f"${(stored or 0.0):.4f}"


def _since(days: int) -> str:
    """ISO timestamp `days` ago, in the same shape `utcnow()` writes.

    String comparison is enough because every `ts` in the registry is written by
    `utcnow()` in one format with a `+00:00` offset; mixing formats would make a
    `>=` comparison silently wrong, so this mirrors it exactly.
    """
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=max(0, int(days)))).isoformat(timespec="seconds")


def _since_sql(days: int) -> str:
    """The same instant in the shape `CURRENT_TIMESTAMP` writes.

    Session rows are stamped by the database (`first_seen`/`last_seen`), which
    uses `YYYY-MM-DD HH:MM:SS` with no offset; observations are stamped by
    `utcnow()`, which uses `T…+00:00`. Comparing one against the other lexically
    is silently wrong — `' '` sorts before `'T'` — so the window each table needs
    is produced in that table's own format.
    """
    return _since(days).replace("T", " ")[:19]


def _next_reset(window: str, now: dt.datetime) -> str:
    """UTC timestamp at which a quota window rolls over."""
    now = now.astimezone(dt.timezone.utc)
    if window == "minute":
        nxt = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    elif window == "day":
        nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
    elif window == "month":
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = (first + dt.timedelta(days=32)).replace(day=1)
    else:
        nxt = now + dt.timedelta(minutes=1)
    return nxt.isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Price reconciliation (P2)
#
# The rule, stated once because everything below implements it:
#
#     A peer's price is an anomaly SIGNAL. It is never a price.
#
# The resolved value is always one of the deployment's *own* observations. A peer
# comparison may refuse the newest observation (quarantine), and the deployment
# then keeps its previous trusted value — it never adopts the peer's number. A
# median over four honestly-different deployments is a figure no provider
# publishes, so canonicalising it would replace four correct prices with one
# invented one.
# --------------------------------------------------------------------------- #

def _env_number(name: str, default, *, cast=float):
    """A threshold from the environment, so a *hosted* deployment can retune the
    reconciler without a redeploy.

    That is the point of a re-derivable decision: changing one of these should be
    `mi reconcile` away, not a release. A malformed value falls back to the default
    rather than raising — a typo in a secret must not take the reconciler offline.
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


#: An observation older than this is kept but marked `expired`: a stale price is
#: better than none for ranking, but it must not read as fresh.
PRICE_FRESHNESS_DAYS = _env_number("PQS_FRESHNESS_DAYS", 30)

#: A jump of this factor from the deployment's own previous observation is a
#: `suspect` change unless independent peers confirm it (then it is a repricing).
PRICE_HISTORY_FACTOR = _env_number("PQS_HISTORY_FACTOR", 10.0)

#: Deviation from the peer median, as a factor. >= suspect is flagged, >=
#: quarantine is refused. 10x is the Novita unit-error scale, so it must quarantine.
PRICE_PEER_SUSPECT_FACTOR = _env_number("PQS_SPREAD_FACTOR", 5.0)
PRICE_PEER_QUARANTINE_FACTOR = _env_number("PQS_SCALE_FACTOR", 10.0)
#: The top band, for severity rather than for the decision — a 20x deviation is
#: two orders of magnitude and reads differently from a 10x one.
PRICE_PEER_CRITICAL_FACTOR = _env_number("PQS_CRITICAL_FACTOR", 20.0)

#: Independent sources needed before a market median exists at all. Three, not
#: two: with two values the median is their mean, which a single outlier moves —
#: exactly the case this check exists to catch. With three, one bad source cannot
#: shift it.
PRICE_MIN_SOURCES = _env_number("PQS_MIN_SOURCES", 3, cast=int)

# The price states, spelled out so a typo is a NameError rather than a row that
# no query matches. `SUPERSEDED` is deliberately absent: it describes an
# observation (every non-winner is superseded by the winner), not a deployment's
# current price. Modelling it as a deployment state would say nothing.
PRICE_CANONICAL = "canonical"
PRICE_SUSPECT = "suspect"
PRICE_QUARANTINED = "quarantined"
PRICE_EXPIRED = "expired"
PRICE_STATES = (PRICE_CANONICAL, PRICE_SUSPECT, PRICE_QUARANTINED, PRICE_EXPIRED)

# The anomaly kinds. `stale` is deliberately *not* one: a stale price is a
# freshness fact (already a `price_resolution` state, and a re-ingest fixes it),
# not a disagreement a human has to adjudicate. Opening one anomaly per
# deployment whose evidence aged out would bury the real ones.
ANOMALY_UNIT_SCALE = "unit_scale"   # refused: >= 10x the market
ANOMALY_SPREAD = "spread"           # flagged: 5-10x the market
ANOMALY_HISTORY_JUMP = "history_jump"  # flagged: a jump with no market to check
def _anomaly_kind(r: dict) -> str:
    """Which failure this looks like, from the decision that was already made."""
    if r["state"] == PRICE_QUARANTINED:
        return ANOMALY_UNIT_SCALE
    if r["factor"] is not None:
        return ANOMALY_SPREAD
    return ANOMALY_HISTORY_JUMP


def _anomaly_severity(r: dict) -> str:
    """The banded factor. A history jump has no factor to band, so it warns."""
    factor = r["factor"]
    if factor is None:
        return "warning"
    if factor >= PRICE_PEER_CRITICAL_FACTOR:
        return "critical"
    if factor >= PRICE_PEER_QUARANTINE_FACTOR:
        return "high"
    if factor >= PRICE_PEER_SUSPECT_FACTOR:
        return "warning"
    return "info"


# --------------------------------------------------------------------------- #
# Pricing state machine (P4)
#
# `Deployment.price_*` says what a call costs. This says what *kind* of thing the
# deployment is economically, which is the question a "was free, now $0.075"
# string was being made to answer badly. The state is derived, never hand-set:
# `_sync_pricing_states` is the only writer, and every change is logged.
# --------------------------------------------------------------------------- #

PRICING_UNKNOWN = "unknown"
PRICING_FREE = "free"
PRICING_FREE_WITH_QUOTA = "free_with_quota"
PRICING_PAID_AFTER_QUOTA = "paid_after_quota"
PRICING_TRIAL = "trial"
PRICING_SUBSCRIPTION = "subscription"
PRICING_PAID = "paid"
PRICING_HIBERNATED = "hibernated"
PRICING_DEPRECATED = "deprecated"
PRICING_STATES = (
    PRICING_UNKNOWN, PRICING_FREE, PRICING_FREE_WITH_QUOTA, PRICING_PAID_AFTER_QUOTA,
    PRICING_TRIAL, PRICING_SUBSCRIPTION, PRICING_PAID, PRICING_HIBERNATED,
    PRICING_DEPRECATED,
)

#: Headroom at or below this reads as "the free bucket is spent". Shares the
#: router's threshold on purpose — a state that disagreed with the ranking about
#: whether a free arm is still free would be worse than no state at all.
QUOTA_EXHAUSTED_HEADROOM = _env_number("PQS_EXHAUSTED_HEADROOM", 0.05)


def _pricing_state(
    *, status: str, zero_price, free_variant, subscription, trial_credits,
    price_in: float | None, price_out: float | None, headroom: float | None,
) -> str:
    """The deployment's economic state.

    The priority order *is* the content of this function, and two of its steps are
    the ones that matter economically:

    * a retired or withdrawn id is not "free" whatever its price says — the router
      drops it, so describing it as free would be a lie the UI repeats;
    * a trial balance is not free either. It is a balance that *depletes*, and
      conflating the two is how a router spends money it believed was zero. Same
      for a subscription, which is prepaid rather than free.
    """
    if status == "deprecated":
        return PRICING_DEPRECATED
    if status == "hibernated":
        return PRICING_HIBERNATED
    if trial_credits:
        return PRICING_TRIAL
    if subscription:
        return PRICING_SUBSCRIPTION
    if not (zero_price or free_variant):
        if price_in is None and price_out is None:
            return PRICING_UNKNOWN
        return PRICING_PAID
    if headroom is None:
        # Free with no declared bucket: nothing to run out of.
        return PRICING_FREE
    if headroom <= QUOTA_EXHAUSTED_HEADROOM:
        return PRICING_PAID_AFTER_QUOTA
    return PRICING_FREE_WITH_QUOTA


def _deviation(a: float, b: float) -> float:
    """How many times apart two prices are, always >= 1.

    Handles zero without dividing by it: a free price against a paid one is an
    infinite deviation, which is the honest reading — not `1.0` (equal) and not a
    crash.
    """
    if a == b:
        return 1.0
    if a <= 0 or b <= 0:
        return float("inf")
    return max(a / b, b / a)


def _source_key(source: str | None) -> str:
    """The adapter that produced an observation, used to collapse it to one vote.

    One value per source, so a gateway cannot confirm itself: `openrouter/a`
    producing seventeen endpoints must be one data point in the market median, not
    seventeen. Keyed on the *source* rather than the provider name because
    `hf/novita` and `hf/hyperbolic` share the `huggingface` source but are
    different upstreams — they stay separate data points.
    """
    return source or "unknown"


def _factor_label(factor: float) -> str:
    """A deviation as a readable factor. `free -> paid` reads better than `infx`,
    and it is the same fact: the previous observation was zero."""
    if factor == float("inf"):
        return "free -> paid"
    return f"{factor:.0f}x"


def _median(values: list[float]) -> float:
    xs = sorted(values)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def effective_headroom(*, limit_n, used_n, observed_limit_n, observed_remaining_n
                       ) -> tuple[float | None, str]:
    """The fraction of a bucket still available, and which side set it (P5).

    `min(configured, observed)`, and one implementation of it: `headroom` (what
    the router ranks on) and `quota.describe` (what an operator reads) must not be
    able to disagree about whether an arm is running out.

    Returns `(None, 'unknown')` when neither side is known — which the router
    treats conservatively, NOT as "plenty of room".
    """
    parts: list[tuple[str, float]] = []
    if limit_n:
        parts.append(("configured",
                      max(0.0, min(1.0, (limit_n - (used_n or 0)) / limit_n))))
    if observed_limit_n and observed_remaining_n is not None:
        parts.append(("observed",
                      max(0.0, min(1.0, observed_remaining_n / observed_limit_n))))
    if not parts:
        return None, "unknown"
    value = min(v for _, v in parts)
    return value, "hybrid" if len(parts) == 2 else parts[0][0]


def _age_days(ts: str | None, *, now: dt.datetime | None = None) -> float | None:
    """How old an ISO timestamp is, or None when it cannot be parsed."""
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - t).total_seconds() / 86400


def _spend_transition(
    *, incoming_free: bool, was_free: bool, existing_status: str,
    existing_source: str | None, incoming_status: str,
    price_in: float | None, price_out: float | None,
) -> tuple[str | None, str | None]:
    """The free -> paid spend event, as one pure decision.

    Extracted because two writers need it — `upsert_deployment` (a caller supplied
    the price) and `reconcile_prices` (the evidence did). Two copies is how a free
    arm silently starts charging: one path hibernates, the other does not, and the
    difference is invisible until the invoice.

    Returns `(status, reason)`. `status is None` means "leave the row's status
    alone" (a free arm that is still free keeps whatever the source said, e.g. a
    deprecated id).
    """
    if incoming_free:
        # Free again: the cost risk is gone, so a hibernated arm may come back.
        if existing_status == "hibernated":
            return "live", "free again"
        return None, None
    if was_free or existing_status == "hibernated":
        return "hibernated", (
            f"was free, now {_per_mtok(price_in)}/{_per_mtok(price_out)} per Mtok")
    # paid -> paid. A decision a human made, or one the runtime made from calling
    # the thing, outranks the catalogue. Otherwise the catalogue *is* the news — an
    # endpoint that went `degraded`, a model the provider retired — and the old
    # unconditional "keep existing" meant those statuses could never land on a row
    # that was already paid.
    if existing_source in ("review", "runtime"):
        return existing_status, None
    return incoming_status, None

DDL = """
CREATE TABLE IF NOT EXISTS weights (
  weights_id       TEXT PRIMARY KEY,
  display_name     TEXT NOT NULL,
  hf_repo          TEXT,
  hf_revision      TEXT,
  family           TEXT,
  params_b         REAL,
  arch             TEXT,
  license          TEXT,
  released_at      TEXT,
  modalities       TEXT DEFAULT '[]',
  aliases          TEXT DEFAULT '[]',
  benchmark        TEXT DEFAULT '{}',
  benchmark_source TEXT,
  first_seen       TEXT DEFAULT CURRENT_TIMESTAMP,
  last_seen        TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS deployments (
  deploy_id         TEXT PRIMARY KEY,
  weights_id        TEXT NOT NULL,
  provider          TEXT NOT NULL,
  provider_model_id TEXT NOT NULL,
  context_window    INTEGER,
  max_output        INTEGER,
  quantization      TEXT,
  price_in          REAL,
  price_out         REAL,
  price_cached_in   REAL,
  zero_price        INTEGER DEFAULT 0,
  free_variant      INTEGER DEFAULT 0,
  subscription      INTEGER DEFAULT 0,
  trial_credits     INTEGER DEFAULT 0,
  discount          REAL,
  caps              TEXT DEFAULT '{}',
  limits            TEXT DEFAULT '{}',
  limits_confirmed  INTEGER DEFAULT 0,
  uptime_1d         REAL,
  first_token_ms    REAL,
  throughput        REAL,
  status            TEXT DEFAULT 'live',
  status_reason     TEXT,
  status_changed_at TEXT,
  -- Where the status came from. `source` is what the catalogue said, `runtime`
  -- is what we learned by calling it, `review` is an operator's decision. A
  -- `runtime` retirement is sticky: a catalogue that reports the model as `live`
  -- has no idea what happened when we tried to call it, and resurrecting it just
  -- fails again on the next request.
  status_source     TEXT DEFAULT 'source',
  -- Derived by `_sync_pricing_states` (P4). Never hand-set: it is a function of
  -- the price, the free markers, the routing status and quota headroom.
  pricing_state     TEXT,
  source            TEXT,
  source_url        TEXT,
  first_seen        TEXT DEFAULT CURRENT_TIMESTAMP,
  last_seen         TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (weights_id) REFERENCES weights(weights_id)
);
CREATE INDEX IF NOT EXISTS idx_dep_weights  ON deployments(weights_id);
CREATE INDEX IF NOT EXISTS idx_dep_provider ON deployments(provider);
CREATE INDEX IF NOT EXISTS idx_dep_price    ON deployments(price_in, price_out);

CREATE TABLE IF NOT EXISTS evidence (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_kind  TEXT NOT NULL,
  entity_id    TEXT NOT NULL,
  field        TEXT NOT NULL,
  value        TEXT,
  source       TEXT,
  url          TEXT,
  fetched_at   TEXT,
  confidence   REAL,
  observed_via TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_entity ON evidence(entity_kind, entity_id, field);

-- Append-only price *observations*. An adapter emits these; `upsert_deployment`
-- resolves the deployment's stored economics from them. The typed counterpart to
-- `evidence`: `raw_value` keeps exactly what the provider published, `raw_unit`
-- names the unit it was published in, and `usd_per_mtok` is the one normalized
-- value two sources may be compared on. Never UPDATEd — a corrected price is a
-- new row, so the history that a reconciliation decision is made from survives.
CREATE TABLE IF NOT EXISTS pricing_evidence (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  deploy_id    TEXT NOT NULL,
  weights_id   TEXT NOT NULL,
  provider     TEXT NOT NULL,
  kind         TEXT NOT NULL,          -- input | output | cached_input | ...
  raw_value    TEXT,                   -- the published token, verbatim
  raw_unit     TEXT NOT NULL,          -- pricing.units.UnitSpec.name
  usd_per_mtok REAL,                   -- normalized; NULL for a sentinel
  pricing_type TEXT NOT NULL,          -- free | paid | trial | subscription | unknown
  is_sentinel  INTEGER NOT NULL DEFAULT 0,
  source       TEXT,
  url          TEXT,
  observed_at  TEXT NOT NULL,
  confidence   REAL
);
CREATE INDEX IF NOT EXISTS idx_pe_deploy  ON pricing_evidence(deploy_id, kind, observed_at);
CREATE INDEX IF NOT EXISTS idx_pe_weights ON pricing_evidence(weights_id, kind, observed_at);

-- The reconciler's decision, per deployment and billing kind (P2). Derived from
-- `pricing_evidence` and re-derivable at will (`mi reconcile`), but stored rather
-- than computed by a view because the algorithm is not expressible in SQL: it
-- compares a deployment against its own history *and* against independent peers,
-- and the peer comparison must never be allowed to become a price.
--
-- `usd_per_mtok` is the resolved value. It is always one of the deployment's own
-- observations, or NULL when the newest was refused and there is no earlier
-- trusted value — never a peer statistic. `state` is one of
-- canonical | suspect | quarantined | expired.
CREATE TABLE IF NOT EXISTS price_resolution (
  deploy_id     TEXT NOT NULL,
  kind          TEXT NOT NULL,
  usd_per_mtok  REAL,
  state         TEXT NOT NULL,
  reason        TEXT,
  source        TEXT,
  observed_at   TEXT,
  confidence    REAL,
  reference_median REAL,                 -- the market median the deviation is against
  source_count  INTEGER NOT NULL DEFAULT 0,
  factor        REAL,                    -- the deviation that drove the state
  reconciled_at TEXT NOT NULL,
  PRIMARY KEY (deploy_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_pr_state ON price_resolution(state);

-- The anomaly log (P3): one row per *incident*, open while the reconciler
-- disagrees and closed when the price returns to canonical. Re-running the
-- reconciler refreshes the open row rather than appending, so `mi reconcile`
-- every 30 minutes does not produce 48 rows a day for one bad source.
--
-- `anomaly_id` is text and content-derived rather than an autoincrement rowid:
-- `sync` drops surrogate `id` columns (they are local row order, not data), so a
-- surrogate key here would make two mirrors disagree about which anomaly a
-- reference points at. There is nothing to point at, so the id is the identity.
--
-- `evidence` stores the *coordinates* of the observations that produced the
-- decision (source, observed_at, value) rather than `pricing_evidence.id`, for
-- the same reason: the rowid does not survive a sync.
CREATE TABLE IF NOT EXISTS price_anomalies (
  anomaly_id     TEXT PRIMARY KEY,
  deploy_id      TEXT NOT NULL,
  weights_id     TEXT,
  dimension      TEXT NOT NULL,   -- input | output | cached_input (the price)
  kind           TEXT NOT NULL,   -- unit_scale | spread | history_jump (the fault)
  severity       TEXT NOT NULL,   -- info | warning | high | critical
  state          TEXT NOT NULL,   -- the price_resolution state that raised it
  expected       REAL,            -- the market median, or the previous value
  observed       REAL,            -- the value that was refused or flagged
  factor         REAL,
  source_count   INTEGER NOT NULL DEFAULT 0,
  resolved_value REAL,            -- what the deployment kept
  detail         TEXT,            -- the reconciler's own reason string
  evidence       TEXT DEFAULT '{}',
  status         TEXT NOT NULL DEFAULT 'open',  -- open | acknowledged | resolved
  opened_at      TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  resolved_at    TEXT,
  resolution     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pa_status ON price_anomalies(status, severity);
CREATE INDEX IF NOT EXISTS idx_pa_deploy ON price_anomalies(deploy_id, kind);

-- The pricing state machine's history (P4). Append-only: one row per change, so
-- "Free tier disappeared on Sep 29" is a query rather than a string that was
-- built for a human once and then overwritten. `transition_id` is content-derived
-- because `sync` drops surrogate ids.
CREATE TABLE IF NOT EXISTS pricing_transitions (
  transition_id TEXT PRIMARY KEY,
  deploy_id     TEXT NOT NULL,
  from_state    TEXT,               -- NULL for the first state ever recorded
  to_state      TEXT NOT NULL,
  detected_at   TEXT NOT NULL,
  price_in      REAL,
  price_out     REAL,
  evidence      TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_pt_deploy ON pricing_transitions(deploy_id, detected_at);

-- Effective-dated price beliefs (P6). Append-only, and a row is written only when
-- the belief *changes*, so "what did we believe on <date>" is a query.
--
-- `effective_to` is deliberately NOT stored: it is the next row's `effective_from`,
-- exposed by the `price_history_dated` view with a window function. Storing it
-- would mean an UPDATE on every change, and an append-only log that is sometimes
-- updated is no longer a log.
--
-- `effective_from` is when the reconciler adopted the belief, which is *our*
-- timeline. The provider's timeline is `pricing_evidence.observed_at`: a price can
-- have been true upstream for a week before we first saw it, and conflating the
-- two would make the history claim knowledge we did not have at the time.
CREATE TABLE IF NOT EXISTS price_history (
  history_id     TEXT PRIMARY KEY,
  deploy_id      TEXT NOT NULL,
  kind           TEXT NOT NULL,
  usd_per_mtok   REAL,
  state          TEXT NOT NULL,
  reason         TEXT,
  source         TEXT,
  observed_at    TEXT,
  confidence     REAL,
  effective_from TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ph_deploy ON price_history(deploy_id, kind, effective_from);

CREATE TABLE IF NOT EXISTS snapshots (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT, url TEXT, fetched_at TEXT,
  sha256      TEXT UNIQUE, nbytes INTEGER, storage_uri TEXT
);

-- Our view of remaining headroom per free bucket. `used_n` is only ever
-- incremented by our own calls, so an empty table means "unknown", not "empty".
CREATE TABLE IF NOT EXISTS quota_buckets (
  deploy_id    TEXT NOT NULL,
  window       TEXT NOT NULL,        -- minute | day | month
  api_key_alias TEXT NOT NULL DEFAULT 'default',
  limit_n      INTEGER,              -- the CONFIGURED policy limit
  used_n       INTEGER DEFAULT 0,
  reset_at     TEXT,
  source       TEXT DEFAULT 'configured',
  -- What the provider reported in a response header (P5). The effective limit is
  -- the *minimum* of this and `limit_n`, so a policy cannot spend a provider's
  -- whole free tier and a provider's observed limit is not ignored because the
  -- policy happens to be more generous.
  observed_limit_n     INTEGER,
  observed_remaining_n INTEGER,
  observed_reset_at    TEXT,
  observed_at          TEXT,
  observed_confidence  REAL,
  updated_at   TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (deploy_id, window, api_key_alias)
);

-- Append-only. Never UPDATE. Statistics are derived.
CREATE TABLE IF NOT EXISTS observations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  deploy_id    TEXT NOT NULL,
  task         TEXT NOT NULL,
  ts           TEXT NOT NULL,
  ok           INTEGER NOT NULL,
  error_class  TEXT,
  latency_ms   REAL,
  tokens_in    INTEGER,
  tokens_out   INTEGER,
  cost_usd     REAL,
  signal_kind  TEXT,          -- verified | subjective | provider_reported
  signal_value REAL,
  meta         TEXT DEFAULT '{}',
  -- Which limit a 429 hit (P5): rpm | rpd | tpm | concurrency | account |
  -- unknown. Nullable because most calls are not rate-limited, and `unknown` is
  -- a recorded answer rather than a missing one.
  rate_limit_reason TEXT,
  -- Which session and which tenant the call belonged to. Both nullable: a
  -- CLI/benchmark call belongs to neither, and reporting must tell "no tenant"
  -- from "tenant with zero". `tenant_id` is a label on shared learning, not a
  -- partition — see `Store.usage_report`.
  session_id   TEXT,
  tenant_id    TEXT,
  -- The difficulty the routing decision was made under. In the base table as well
  -- as `_ADDED_COLUMNS`, like every other column added after the fact: the ALTER is
  -- for registries that already exist, not for new ones.
  effort       TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs ON observations(deploy_id, task);

CREATE TABLE IF NOT EXISTS decisions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  task       TEXT NOT NULL,
  policy     TEXT,
  mode       TEXT,            -- auto | direct | rejected | blocked
  chosen     TEXT,
  candidates TEXT,
  reason     TEXT,
  tenant_id  TEXT
);

CREATE TABLE IF NOT EXISTS quarantine (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT, entity_id TEXT, field TEXT,
  value       TEXT, reason TEXT, seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_q_uniq
  ON quarantine(source, entity_id, field, value, reason);

-- Entity resolution output. A weights row that is discovered to be the same
-- artifact as another is aliased, not deleted, so the merge stays auditable.
CREATE TABLE IF NOT EXISTS weight_aliases (
  alias_id     TEXT PRIMARY KEY,
  canonical_id TEXT NOT NULL,
  reason       TEXT,
  confidence   REAL,
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE VIEW IF NOT EXISTS weights_resolved AS
SELECT w.*, COALESCE(a.canonical_id, w.weights_id) AS resolved_id
FROM weights w LEFT JOIN weight_aliases a ON a.alias_id = w.weights_id;

-- Per-session spend. A session is whatever the caller says it is (an
-- `X-MI-Session` header, or the OpenAI `user` field); a request without one is
-- a one-shot and is not counted here, so the proxy stays stateless for callers
-- that do not opt in.
--
-- This is a ledger, not a chat store: it answers "what has this session spent",
-- so the cap can be enforced server-side. A client-side counter cannot enforce
-- anything and drifts on refresh, retry and regenerate.
CREATE TABLE IF NOT EXISTS sessions (
  session_id  TEXT PRIMARY KEY,
  calls       INTEGER NOT NULL DEFAULT 0,
  tokens_in   INTEGER NOT NULL DEFAULT 0,
  tokens_out  INTEGER NOT NULL DEFAULT 0,
  cost_usd    REAL    NOT NULL DEFAULT 0,
  -- Tool spend, split out from model spend. A search costs ~$0.008 and a routed
  -- call on a free arm costs $0, so folding them into one number would hide the
  -- only line item that actually costs money.
  searches    INTEGER NOT NULL DEFAULT 0,
  search_cost_usd REAL NOT NULL DEFAULT 0,
  -- Which tenant owns the session. A session id is already namespaced
  -- `tenant/session` under auth, but that is a string convention; this column
  -- makes "this tenant's spend" a query rather than a prefix scan.
  tenant_id   TEXT,
  first_seen  TEXT DEFAULT CURRENT_TIMESTAMP,
  last_seen   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- A session transcript: the *content*, as opposed to the spend in `sessions`.
-- `sessions` answers "what did this cost"; this answers "what was said". Two
-- bounds keep it from becoming an unbounded liability: it is only written for a
-- *named* session (a one-shot request stores nothing — the same opt-in rule
-- spend follows), and each session keeps at most `Store.TRANSCRIPT_LIMIT`
-- messages (the oldest are pruned). Not mirrored by `mi sync` by default: chat
-- content stays in the local store of record unless Postgres is the primary.
CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role       TEXT NOT NULL,     -- user | assistant | system | tool
  content    TEXT NOT NULL,
  task       TEXT,
  deploy_id  TEXT,
  ts         TEXT NOT NULL,
  meta       TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS pushed_models (
  deploy_id  TEXT PRIMARY KEY,
  task       TEXT,
  ts         TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Warm-tier provider health — the last result of the optional background probe
-- (`mininfer.probe`). Kept out of `observations` on purpose: this is about the
-- *provider* (key still valid? reachable?), not a model's answer quality, and
-- the router reads it *before* a request instead of deriving it after one.
-- `checked_at` is what makes a stale failure expire.
CREATE TABLE IF NOT EXISTS provider_health (
  provider   TEXT PRIMARY KEY,
  status     TEXT NOT NULL,          -- ok | auth_error | network_error | http_NNN
  detail     TEXT,
  latency_ms REAL,
  n_models   INTEGER,
  checked_at TEXT NOT NULL
);

-- Small key/value bag for operator-toggled runtime settings (the warm tier's
-- on/off and cadence). In the registry, not a separate file, so a portal toggle
-- survives a restart and every process sharing the database agrees.
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _counted_expr() -> str:
    """SQL for "this observation is evidence about the model".

    Built from `schema.NON_MODEL_ERRORS`, so the Python list and the SQL cannot
    drift. `error_class IS NULL` is a success and is counted explicitly:
    `NULL IN (...)` is `NULL`, not `FALSE`, and `NOT NULL` would silently *drop*
    every good observation.
    """
    classes = ", ".join(f"'{e}'" for e in sorted(NON_MODEL_ERRORS))
    return (
        "error_class IS NULL OR NOT ("
        f"error_class IN ({classes})"
        ")"
    )


# `routing_stats` is the *derived* view the router reads. Its denominator `n`
# counts only calls that reached the model: a `network_error` (our own TLS/DNS),
# `no_api_key`, `auth_error` or a 4xx we caused is not a model failure, and
# counting it as one trains the router against its best arms. `n_all` keeps the
# raw count and `n_infra` exposes the excluded rows, so nothing is hidden.
#
# This is a view on purpose: fixing it re-derives every statistic from history,
# so there is no backfill and observations stay append-only.
_ROUTING_STATS_VIEW = f"""
CREATE VIEW routing_stats AS
SELECT deploy_id, task,
       COUNT(*)                                             AS n_all,
       SUM(CASE WHEN counted THEN 1 ELSE 0 END)             AS n,
       SUM(CASE WHEN counted THEN ok ELSE 0 END)            AS wins,
       SUM(CASE WHEN counted THEN 0 ELSE 1 END)             AS n_infra,
       AVG(CASE WHEN ok=1 THEN latency_ms END)              AS mean_latency_ms,
       SUM(CASE WHEN error_class='429' THEN 1 ELSE 0 END)   AS n_429,
       SUM(CASE WHEN error_class='timeout' THEN 1 ELSE 0 END) AS n_timeout,
       SUM(COALESCE(cost_usd,0))                            AS total_cost_usd
FROM (
    SELECT *, ({_counted_expr()}) AS counted
    FROM observations
) AS obs GROUP BY deploy_id, task;
"""

# Postgres replaces a view atomically; SQLite has no `CREATE OR REPLACE VIEW`,
# so `_migrate` drops it first. Two replicas booting at once therefore cannot
# race on Postgres, and a single SQLite writer cannot race at all.
_ROUTING_STATS_VIEW_PG = _ROUTING_STATS_VIEW.replace(
    "CREATE VIEW", "CREATE OR REPLACE VIEW", 1)


# The price read-model: every deployment, plus the reconciled state and
# provenance of its price. The price itself (*) is `deployments.price_*`, which
# `reconcile_prices` materializes; this view is how a consumer sees *why* — which
# source, when, with what confidence, and whether the reconciler trusts it.
_DEPLOYMENTS_PRICED_VIEW = """
CREATE VIEW deployments_priced AS
SELECT d.*,
       pin.usd_per_mtok  AS price_in_canonical,
       pin.source        AS price_in_source,
       pin.observed_at   AS price_in_observed_at,
       pin.confidence    AS price_in_confidence,
       pin.state         AS price_in_state,
       pin.reason        AS price_in_reason,
       pout.usd_per_mtok AS price_out_canonical,
       pout.source       AS price_out_source,
       pout.observed_at  AS price_out_observed_at,
       pout.confidence   AS price_out_confidence,
       pout.state        AS price_out_state,
       pout.reason       AS price_out_reason
FROM deployments d
LEFT JOIN price_resolution pin  ON pin.deploy_id = d.deploy_id AND pin.kind = 'input'
LEFT JOIN price_resolution pout ON pout.deploy_id = d.deploy_id AND pout.kind = 'output'
"""
_DEPLOYMENTS_PRICED_VIEW_PG = _DEPLOYMENTS_PRICED_VIEW.replace(
    "CREATE VIEW", "CREATE OR REPLACE VIEW", 1)


# The belief timeline with its end derived. `effective_to IS NULL` is the current
# belief, so the same view answers "now" and "then" without a second table.
_PRICE_HISTORY_VIEW = """
CREATE VIEW price_history_dated AS
SELECT h.*,
       LEAD(h.effective_from) OVER (
         PARTITION BY h.deploy_id, h.kind
         ORDER BY h.effective_from, h.history_id
       ) AS effective_to
FROM price_history h
"""
_PRICE_HISTORY_VIEW_PG = _PRICE_HISTORY_VIEW.replace(
    "CREATE VIEW", "CREATE OR REPLACE VIEW", 1)


# Surrogate `id` columns are reassigned by any target database and nothing
# references them, so they are never copied or mirrored. A schema fact, which is
# why it lives here rather than in the module that happens to consume it.
_SKIP_COLS = {"id"}


_INITIALIZED_DBS: set[str] = set()
# One-time schema creation is not thread-safe: `_store()` is called per
# request and a streamed comparison runs one store per arm *concurrently*,
# so two arms could both decide the database was new and both run the DDL —
# which fails with "database is locked" and surfaces as an arm that "could
# not be called". Double-checked so the common path never takes the lock.
_INIT_GUARD = threading.Lock()

# The Postgres schema is not a second copy of `DDL`: it is `supabase/schema.sql`,
# which `mi sync` already treats as the canonical mirror. Reading it here means a
# schema change is made once (plus the SQLite DDL above, which cannot be derived
# mechanically because the two differ on purpose — identity columns versus
# AUTOINCREMENT).
_PG_SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "supabase" / "schema.sql"


class Store:
    def __init__(self, path: str | pathlib.Path = "mininfer.db"):
        self.target = str(path)
        self.conn = db.connect(path)
        if not db.is_postgres(path):
            # SQLite-only: the router keys its benchmark-norms cache on this.
            # Postgres callers use `target`, which is the DSN.
            self.path = pathlib.Path(path)
        key = str(self.path.resolve()) if hasattr(self, "path") else self.target
        if key not in _INITIALIZED_DBS:
            with _INIT_GUARD:
                if key not in _INITIALIZED_DBS:
                    ddl = (DDL if not db.is_postgres(path)
                           else _PG_SCHEMA.read_text(encoding="utf-8"))
                    self.conn.executescript(ddl)
                    self._migrate()
                    self.conn.commit()
                    _INITIALIZED_DBS.add(key)

    # Columns added after a table shipped. `CREATE TABLE IF NOT EXISTS` is a
    # no-op on an existing table, so a new column needs saying out loud or every
    # existing registry silently loses the feature.
    _ADDED_COLUMNS = {
        "sessions": {
            "searches": "INTEGER NOT NULL DEFAULT 0",
            "search_cost_usd": "REAL NOT NULL DEFAULT 0",
            "tenant_id": "TEXT",
        },
        # Which session and tenant a call belonged to. Nullable because a call
        # outside any session (the CLI, a benchmark, a one-shot request) has no
        # session or tenant to be attributed to — and reporting must be able to
        # tell "no tenant" from "tenant with zero".
        "observations": {"session_id": "TEXT", "tenant_id": "TEXT",
                          "rate_limit_reason": "TEXT",
                          # The difficulty the routing decision was made under. Without it
                          # every outcome is averaged into one `(deploy, task)` number, and a
                          # model that is good at easy prompts and bad at hard ones looks
                          # uniformly mediocre — so the difficulty signal can never be
                          # recalibrated from outcomes.
                          "effort": "TEXT"},
        # Provider-reported limits (P5). `limit_n` stays the configured policy;
        # these carry what the provider said, and `headroom` takes the minimum.
        "quota_buckets": {
            "observed_limit_n": "INTEGER",
            "observed_remaining_n": "INTEGER",
            "observed_reset_at": "TEXT",
            "observed_at": "TEXT",
            "observed_confidence": "REAL",
        },
        "decisions": {"tenant_id": "TEXT"},
        # Why a deployment left `live` (`hibernated` = a free arm that started
        # charging, awaiting a human) and when, so the review has context.
        "deployments": {"status_reason": "TEXT", "status_changed_at": "TEXT",
                        "pricing_state": "TEXT", "status_source": "TEXT DEFAULT 'source'"},
    }

    def _migrate(self) -> None:
        for table, columns in self._ADDED_COLUMNS.items():
            have = self.conn.columns(table)
            if not have:
                continue
            for name, decl in columns.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # An index on an *added* column has to be created here, after the ALTER,
        # not in the base DDL: `CREATE INDEX IF NOT EXISTS … (session_id)` would
        # run against an old registry that does not have the column yet and fail
        # the whole initialisation. Idempotent, and cheap enough to run once per
        # process.
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id, ts)",
            "CREATE INDEX IF NOT EXISTS idx_obs_tenant ON observations(tenant_id, ts)",
            # The difficulty-aware read path (#3): `(deploy, task, effort)`.
            "CREATE INDEX IF NOT EXISTS idx_obs_effort ON observations(deploy_id, task, effort)",
            "CREATE INDEX IF NOT EXISTS idx_dec_tenant ON decisions(tenant_id, ts)",
            "CREATE INDEX IF NOT EXISTS idx_sess_tenant ON sessions(tenant_id)",
            # Index on a column `_ADDED_COLUMNS` may have just added, so it cannot
            # live in the base DDL.
            "CREATE INDEX IF NOT EXISTS idx_dep_pricing_state ON deployments(pricing_state)",
        ):
            self.conn.execute(stmt)
        # `routing_stats` is derived, so redefining it is safe — and necessary:
        # `CREATE VIEW IF NOT EXISTS` leaves an existing registry on the old
        # definition, which silently counts infrastructure errors as model
        # losses. Postgres replaces atomically; SQLite drops first.
        if db.is_postgres(self.target):
            self.conn.execute(_ROUTING_STATS_VIEW_PG)
        else:
            self.conn.execute("DROP VIEW IF EXISTS routing_stats")
            self.conn.execute(_ROUTING_STATS_VIEW)
        # Same reasoning as `routing_stats`: derived, so redefining it is safe and
        # necessary. It reads `pricing_evidence`, which the DDL above has already
        # created (a `CREATE TABLE IF NOT EXISTS` earlier in this same boot).
        if db.is_postgres(self.target):
            self.conn.execute(_DEPLOYMENTS_PRICED_VIEW_PG)
        else:
            self.conn.execute("DROP VIEW IF EXISTS deployments_priced")
            self.conn.execute(_DEPLOYMENTS_PRICED_VIEW)
        # Same again: the belief timeline's `effective_to` is derived, so the view
        # is redefined on every boot rather than frozen at first creation.
        if db.is_postgres(self.target):
            self.conn.execute(_PRICE_HISTORY_VIEW_PG)
        else:
            self.conn.execute("DROP VIEW IF EXISTS price_history_dated")
            self.conn.execute(_PRICE_HISTORY_VIEW)
        # Google retired the 1.5 generation. A seeded row that 404s is worse than
        # no row: the router offers it, the caller dials it, and it fails. Mark
        # them so `_reject` drops them from the ranking. `gemini-2.0-flash` (the
        # current-generation row already in the registry) is what to use instead;
        # `seed_google_and_local_deployments` adds the 2.5 ids on demand rather
        # than on every open, which would rewrite every registry.
        self.conn.execute(
            "UPDATE deployments SET status='deprecated'"
            " WHERE provider='google' AND provider_model_id IN"
            " ('gemini-1.5-flash','gemini-1.5-pro')")
        # The old seed also labelled every direct Gemini row `zero_price`, which
        # made the router treat a per-token API as free — it chose them on price
        # and then spent money. A free *quota* is a rate limit, not a price.
        self.conn.execute(
            "UPDATE deployments SET zero_price=0, free_variant=0"
            " WHERE provider='google' AND source='seed'"
            " AND (zero_price!=0 OR free_variant!=0)")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ ingest

    def upsert_weights(self, w: Weights, evidence: Iterable[Evidence] = ()) -> None:
        row = w.to_row()
        self.conn.execute(
            """
            INSERT INTO weights (weights_id, display_name, hf_repo, hf_revision, family,
                params_b, arch, license, released_at, modalities, aliases, benchmark,
                benchmark_source, last_seen)
            VALUES (:weights_id,:display_name,:hf_repo,:hf_revision,:family,:params_b,:arch,
                :license,:released_at,:modalities,:aliases,:benchmark,:benchmark_source,
                CURRENT_TIMESTAMP)
            ON CONFLICT(weights_id) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, weights.display_name),
                hf_repo      = COALESCE(excluded.hf_repo, weights.hf_repo),
                hf_revision  = COALESCE(excluded.hf_revision, weights.hf_revision),
                family       = COALESCE(excluded.family, weights.family),
                params_b     = COALESCE(excluded.params_b, weights.params_b),
                arch         = COALESCE(excluded.arch, weights.arch),
                license      = COALESCE(excluded.license, weights.license),
                released_at  = COALESCE(excluded.released_at, weights.released_at),
                modalities   = CASE WHEN excluded.modalities != '[]'
                                    THEN excluded.modalities ELSE weights.modalities END,
                aliases      = CASE WHEN excluded.aliases != '[]'
                                    THEN excluded.aliases ELSE weights.aliases END,
                benchmark    = CASE WHEN excluded.benchmark NOT IN ('{}','null')
                                    THEN excluded.benchmark ELSE weights.benchmark END,
                benchmark_source = COALESCE(excluded.benchmark_source, weights.benchmark_source),
                last_seen    = CURRENT_TIMESTAMP
            """,
            row,
        )
        self._add_evidence(evidence)

    def upsert_deployment(
        self, d: Deployment, evidence: Iterable[Evidence] = (), quarantine: Iterable[tuple] = (),
        prices=None,
    ) -> None:
        # The adapter emits price *observations*; the store resolves the stored
        # economics from them. This is the seam P1 exists to create: an adapter can
        # no longer decide what a deployment costs, only what was observed. The
        # rule here is deliberately trivial (its own newest observation per kind) —
        # P2 replaces the rule, not the seam.
        if prices is not None:
            self._add_pricing_evidence(d, prices)
            resolved = self._resolve_deployment_price(d.deploy_id)
            if resolved["price_in"] is not None or resolved["price_out"] is not None:
                d = dc.replace(
                    d,
                    price_in=resolved["price_in"],
                    price_out=resolved["price_out"],
                    price_cached_in=resolved["price_cached_in"],
                    # Both sides known and zero is the only free case. An unknown
                    # side is *not* free: the whole point of the flag is to stop the
                    # router choosing an arm on a price nobody published.
                    zero_price=(resolved["price_in"] == 0 and resolved["price_out"] == 0),
                )
        row = d.to_row()
        row["zero_price"] = int(d.zero_price)
        row["free_variant"] = int(d.free_variant)
        row["subscription"] = int(d.subscription)
        row["trial_credits"] = int(d.trial_credits)
        row["limits_confirmed"] = int(d.limits_confirmed)
        # A free arm that starts charging is a *spend* event, not a price update.
        # Left alone, the router keeps choosing it on the price it used to have
        # and every request quietly costs money — so it is hibernated and a human
        # is asked. Nothing downstream has to know: `_reject` already drops a
        # deployment whose status is not `live`.
        row["status_reason"] = None
        row["status_source"] = "source"
        # Bound from Python rather than `CURRENT_TIMESTAMP`: on Postgres the column
        # is `text` and `CURRENT_TIMESTAMP` is `timestamptz`, and a `CASE` cannot
        # reconcile the two ("CASE types text and timestamp with time zone cannot be
        # matched"). A bare value assignment would cast, a CASE will not. Binding
        # `utcnow()` also keeps one timestamp format across both engines.
        from .fetch import utcnow

        row["now"] = utcnow()
        existing = self.conn.execute(
            "SELECT zero_price, free_variant, status, status_reason, status_source"
            " FROM deployments WHERE deploy_id=?",
            (d.deploy_id,),
        ).fetchone()
        if existing is not None:
            if existing["status_source"] in ("runtime", "review") and existing["status"] != "live":
                # A status *we* decided outranks the catalogue, whatever its
                # provenance: a runtime discovery that the arm cannot be called, or a
                # human's `mi retire` / rejected hibernation. The catalogue has no idea
                # either happened, so re-ingesting would resurrect it — and for a
                # `:free` variant it did exactly that, because `_spend_transition`'s
                # still-free branch returns "leave it alone" and the incoming `live`
                # then won by default. `mi enable` is the way back.
                row["status"] = existing["status"]
                row["status_reason"] = existing["status_reason"]
                row["status_source"] = existing["status_source"]
            else:
                # One rule, shared with `reconcile_prices`. See `_spend_transition`.
                status, reason = _spend_transition(
                    incoming_free=bool(d.zero_price or d.free_variant),
                    was_free=bool(existing["zero_price"] or existing["free_variant"]),
                    existing_status=existing["status"],
                    existing_source=existing["status_source"],
                    incoming_status=d.status,
                    price_in=d.price_in, price_out=d.price_out,
                )
                if status is not None:
                    row["status"] = status
                    # The status a row already carried — an operator's approval,
                    # say — keeps its provenance, or the next ingest would relabel
                    # the human's decision as the catalogue's and lose it.
                    if status == existing["status"]:
                        row["status_source"] = existing["status_source"] or "source"
                if reason is not None:
                    row["status_reason"] = reason
        self.conn.execute(
            """
            INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,
                context_window, max_output, quantization, price_in, price_out, price_cached_in,
                zero_price, free_variant, subscription, trial_credits, discount, caps, limits,
                limits_confirmed, uptime_1d, first_token_ms, throughput, status, status_reason,
                status_source, status_changed_at, source, source_url, last_seen)
            VALUES (:deploy_id,:weights_id,:provider,:provider_model_id,:context_window,
                :max_output,:quantization,:price_in,:price_out,:price_cached_in,:zero_price,
                :free_variant,:subscription,:trial_credits,:discount,:caps,:limits,
                :limits_confirmed,:uptime_1d,:first_token_ms,:throughput,:status,:status_reason,
                :status_source, :now, :source, :source_url, :now)
            ON CONFLICT(deploy_id) DO UPDATE SET
                weights_id     = excluded.weights_id,
                context_window = COALESCE(excluded.context_window, deployments.context_window),
                max_output     = COALESCE(excluded.max_output, deployments.max_output),
                quantization   = COALESCE(excluded.quantization, deployments.quantization),
                price_in       = COALESCE(excluded.price_in, deployments.price_in),
                price_out      = COALESCE(excluded.price_out, deployments.price_out),
                price_cached_in= COALESCE(excluded.price_cached_in, deployments.price_cached_in),
                zero_price     = excluded.zero_price,
                free_variant   = excluded.free_variant,
                subscription   = excluded.subscription,
                trial_credits  = excluded.trial_credits,
                discount       = COALESCE(excluded.discount, deployments.discount),
                caps           = CASE WHEN excluded.caps != '{}' THEN excluded.caps ELSE deployments.caps END,
                limits         = CASE WHEN excluded.limits != '{}' THEN excluded.limits ELSE deployments.limits END,
                limits_confirmed = MAX(excluded.limits_confirmed, deployments.limits_confirmed),
                uptime_1d      = COALESCE(excluded.uptime_1d, deployments.uptime_1d),
                first_token_ms = COALESCE(excluded.first_token_ms, deployments.first_token_ms),
                throughput     = COALESCE(excluded.throughput, deployments.throughput),
                status         = excluded.status,
                status_source  = excluded.status_source,
                status_reason  = CASE WHEN excluded.status = 'hibernated'
                                      THEN excluded.status_reason
                                      WHEN excluded.status != deployments.status
                                      THEN excluded.status_reason
                                      ELSE deployments.status_reason END,
                status_changed_at = CASE WHEN excluded.status != deployments.status
                                         THEN :now
                                         ELSE deployments.status_changed_at END,
                source         = excluded.source,
                source_url     = excluded.source_url,
                last_seen      = :now
            """,
            row,
        )
        self._add_evidence(evidence)
        for source, entity_id, field, value, reason in quarantine:
            self.conn.execute(
                "INSERT INTO quarantine (source, entity_id, field, value, reason) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                (source, entity_id, field, json.dumps(value, default=str), reason),
            )

    def retire_deployment(self, deploy_id: str, reason: str, *,
                          source: str = "runtime") -> bool:
        """Take a deployment out of routing on purpose.

        `source='runtime'` (the default) records something *we* discovered by
        calling it — most concretely: OpenRouter serves some `:free` models only to
        allowlisted apps and answers a plain API client with 401 "only available on
        agentic harnesses". That is not a bad key, so it must not be swallowed as
        one; it means the arm cannot be called, and the router should stop trying.

        A runtime discovery retires the whole *family* under the same gateway. One
        catalogue model is listed under several deploy ids — `openrouter:m:free` and
        `openrouter/vendor/nvfp4:m:free` are the same weights — the restriction is a
        property of the model, and retiring only the id that happened to be called
        first leaves the sibling to be rediscovered on the next request, which is
        exactly how an agentic-only model kept coming back one id at a time. Routes
        through *another* gateway are deliberately left alone: `weights_id` is
        shared across all of them, so scoping by weights would over-retire.

        `source='review'` is a human deciding the same thing — `mi retire` — and it
        names one deployment, so it retires only that one. Both are sticky, so the
        next ingest does not put the arm back.

        `status_source` is what makes it stick: the catalogue reports these models
        `live` (there is no field for the restriction), and under
        `source='source'` the next ingest would restore it.
        """
        from .fetch import utcnow

        targets = [deploy_id]
        if source == "runtime":
            targets = self._gateway_family(deploy_id) or targets
        now = utcnow()
        changed = 0
        for did in targets:
            cur = self.conn.execute(
                "UPDATE deployments SET status='deprecated', status_reason=?,"
                " status_source=?, status_changed_at=?"
                " WHERE deploy_id=? AND status != 'deprecated'",
                (reason, source, now, did),
            )
            changed += int(getattr(cur, "rowcount", 0) or 0)
        # No commit: the call path that discovers this (`try_fallbacks`) commits
        # once per attempt, like every other batch writer here. The CLI commits.
        return changed > 0

    def _gateway_family(self, deploy_id: str) -> list[str]:
        """Every deploy id for the same model served through the same gateway.

        `provider` is the gateway, plus — for a gateway that resells named
        upstreams — the upstream: `openrouter` and `openrouter/thinkingmachines/nvfp4`
        both route through OpenRouter, so the head identifies the gateway, and
        `provider_model_id` identifies the model within it. Returning `[]` when the
        row is unknown lets the caller fall back to the single id it was given.
        """
        row = self.conn.execute(
            "SELECT provider, provider_model_id FROM deployments WHERE deploy_id=?",
            (deploy_id,),
        ).fetchone()
        if row is None or not row["provider_model_id"]:
            return []
        head = (row["provider"] or "").split("/")[0]
        return [
            r["deploy_id"]
            for r in self.conn.execute(
                "SELECT deploy_id, provider FROM deployments WHERE provider_model_id=?",
                (row["provider_model_id"],),
            )
            if (r["provider"] or "").split("/")[0] == head
        ]

    def enable_deployment(self, deploy_id: str) -> bool:
        """Clear a retirement, so an operator can override it.

        The escape hatch that keeps stickiness from being permanent: a provider can
        change which apps it serves, and only a human knows that.

        Accepts either provenance — a runtime discovery or a hand-made retirement —
        because both are things an operator may need to undo.

        Commits, like the other `decide_*` methods: an operator's action has no
        batch caller to commit for it, and the CLI closing the store without one
        left the change rolled back.
        """
        from .fetch import utcnow

        cur = self.conn.execute(
            "UPDATE deployments SET status='live', status_source='review',"
            " status_reason='re-enabled by operator', status_changed_at=?"
            " WHERE deploy_id=? AND status_source IN ('runtime','review')",
            (utcnow(), deploy_id),
        )
        self.conn.commit()
        return bool(getattr(cur, "rowcount", 0))

    def record_snapshot(self, snap) -> None:
        self.conn.execute(
            """INSERT INTO snapshots (source,url,fetched_at,sha256,nbytes,storage_uri)
               VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (snap.source, snap.url, snap.fetched_at, snap.sha256, snap.nbytes, snap.storage_uri),
        )

    def _add_evidence(self, items: Iterable[Evidence]) -> None:
        rows = [e.to_row() for e in items]
        if rows:
            self.conn.executemany(
                """INSERT INTO evidence (entity_kind,entity_id,field,value,source,url,
                   fetched_at,confidence,observed_via) VALUES (?,?,?,?,?,?,?,?,?)""",
                rows,
            )

    # ------------------------------------------------------------- pricing

    def _add_pricing_evidence(self, d: Deployment, prices) -> int:
        """Append this deployment's price observations. Never updates.

        Only *recordable* observations are written — a known price, or an explicit
        sentinel. A field the provider simply did not publish is not evidence, and
        storing it as `unknown` would make "DeepInfra did not list a cache price"
        look like a claim about the cache price.
        """
        from .fetch import utcnow

        rows = [
            (
                d.deploy_id, d.weights_id, o.provider, o.kind,
                None if o.raw_value is None else str(o.raw_value),
                o.raw_unit,
                None if o.usd_per_mtok is None else float(o.usd_per_mtok),
                o.pricing_type, int(o.is_sentinel),
                o.source, o.url, o.observed_at or utcnow(), o.confidence,
            )
            for o in prices.recordable
        ]
        if rows:
            self.conn.executemany(
                """INSERT INTO pricing_evidence (deploy_id,weights_id,provider,kind,
                   raw_value,raw_unit,usd_per_mtok,pricing_type,is_sentinel,source,url,
                   observed_at,confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    def _resolve_deployment_price(self, deploy_id: str) -> dict[str, float | None]:
        """The deployment's own economics, from its newest observation per kind.

        Deployment-level by construction: only rows with this `deploy_id` are read,
        so a peer's price can never appear here. "Newest" is
        `observed_at DESC, id DESC` — a replayed snapshot carries its original
        `observed_at`, so it does not silently win by being inserted later.
        """
        rows = self.conn.execute(
            """SELECT pe.kind, pe.usd_per_mtok FROM pricing_evidence pe
                WHERE pe.deploy_id = ?
                  AND pe.id = (SELECT id FROM pricing_evidence
                                WHERE deploy_id = pe.deploy_id AND kind = pe.kind
                                ORDER BY observed_at DESC, id DESC LIMIT 1)""",
            (deploy_id,),
        ).fetchall()
        by_kind = {r["kind"]: r["usd_per_mtok"] for r in rows}
        return {
            "price_in": by_kind.get("input"),
            "price_out": by_kind.get("output"),
            "price_cached_in": by_kind.get("cached_input"),
        }

    # ------------------------------------------------------- reconciliation (P2)

    def reconcile_prices(self, deploy_ids: Iterable[str] | None = None) -> dict[str, Any]:
        """Re-derive every deployment's canonical price from its evidence.

        The rule, and the reason it is not a median:

        * the resolved value is always one of the deployment's **own** observations
          — authority and freshness decide which;
        * an independent-peer comparison may **refuse** an observation
          (`quarantined`), and the deployment then keeps its previous trusted
          value. A peer never supplies a price.

        Re-derivable: nothing in `pricing_evidence` is mutated, so a changed
        threshold is a re-run of this method, not a migration. `deploy_ids`
        narrows which deployments are *written*; the market is always computed
        from all of the evidence, or a narrowed pass would silently lose the peer
        check.
        """
        from .fetch import utcnow

        resolutions = self._compute_resolutions(deploy_ids)
        stamp = utcnow()
        rows = [
            (r["deploy_id"], r["kind"], r["usd_per_mtok"], r["state"], r["reason"],
             r["source"], r["observed_at"], r["confidence"], r["reference_median"],
             r["source_count"], r["factor"], stamp)
            for r in resolutions
        ]
        if rows:
            self.conn.executemany(
                """INSERT INTO price_resolution (deploy_id,kind,usd_per_mtok,state,reason,
                   source,observed_at,confidence,reference_median,source_count,factor,
                   reconciled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(deploy_id,kind) DO UPDATE SET
                     usd_per_mtok=excluded.usd_per_mtok, state=excluded.state,
                     reason=excluded.reason, source=excluded.source,
                     observed_at=excluded.observed_at, confidence=excluded.confidence,
                     reference_median=excluded.reference_median,
                     source_count=excluded.source_count,
                     factor=excluded.factor, reconciled_at=excluded.reconciled_at""",
                rows,
            )

        by_deploy: dict[str, dict[str, dict]] = {}
        for r in resolutions:
            by_deploy.setdefault(r["deploy_id"], {})[r["kind"]] = r

        states: dict[str, int] = {}
        changed = 0
        for deploy_id, kinds in by_deploy.items():
            for r in kinds.values():
                states[r["state"]] = states.get(r["state"], 0) + 1
            changed += self._apply_deployment_price(deploy_id, kinds, stamp)
        return {
            "deployments": len(by_deploy),
            "kinds": len(resolutions),
            "changed": changed,
            "states": states,
            "anomalies": self._reconcile_anomalies(resolutions, stamp),
            "pricing_states": self._sync_pricing_states(stamp),
            "history": self._record_price_history(resolutions),
        }

    def _compute_resolutions(self, deploy_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Apply the rule to every (deployment, kind) with evidence. Writes nothing.

        Kept separate from the write so a decision can be inspected before it is
        applied — and so the market median is built once for the whole pass rather
        than re-derived per deployment.
        """
        sql = ("SELECT id, deploy_id, weights_id, provider, kind, usd_per_mtok,"
               " source, observed_at, confidence FROM pricing_evidence"
               " WHERE usd_per_mtok IS NOT NULL")
        ids = list(deploy_ids) if deploy_ids else []
        rows = self.conn.execute(sql, ()).fetchall()

        by_deploy: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            by_deploy.setdefault((r["deploy_id"], r["kind"]), []).append(dict(r))
        for obs in by_deploy.values():
            # `id` breaks a tie between two observations stamped the same second,
            # so ordering is total and the result is reproducible.
            obs.sort(key=lambda o: (o["observed_at"] or "", o["id"]))

        # The market reference per artifact: the newest value from each distinct
        # source. One vote per source, so a gateway with fifty endpoints is one data
        # point rather than fifty. This *includes* the deployment being judged — it
        # is a member of the market — and that is what makes the median robust in a
        # three-source market with one bad source: the good two outvote it.
        # Excluding self instead leaves the two good arms comparing against a
        # median dragged halfway to the bad one, and flags *them* as deviant.
        #
        # **Zero-priced sources are excluded.** The deviation is a *scale*
        # comparison, and scale is undefined relative to zero: `paid / 0` is
        # infinity, so a single free peer would quarantine every paid deployment of
        # the same artifact. Free is a category, not a scale — whether an arm is
        # still free is a *state* question (the free->paid transition), not a
        # unit-error one, and a unit error cannot turn a free price into a nonzero
        # one anyway.
        #
        # Built from ALL evidence even when `deploy_ids` narrows the pass: a market
        # computed only from the deployed ids would silently disable the peer check
        # (`test_reconcile_can_narrow_to_given_deployments`). Narrowing limits which
        # deployments are *written*, never what the market is.
        market: dict[tuple[str, str], dict[str, dict]] = {}
        for obs in by_deploy.values():
            newest = obs[-1]
            if not newest["usd_per_mtok"]:
                continue
            per_source = market.setdefault((newest["weights_id"], newest["kind"]), {})
            src = _source_key(newest["source"])
            prev = per_source.get(src)
            if prev is None or (newest["observed_at"] or "", newest["id"]) >= (
                    prev["observed_at"] or "", prev["id"]):
                per_source[src] = newest

        selected = set(ids) if ids else None
        return [
            self._reconcile_one(deploy_id, kind, obs, market)
            for (deploy_id, kind), obs in by_deploy.items()
            if selected is None or deploy_id in selected
        ]

    def _reconcile_one(self, deploy_id: str, kind: str, obs: list[dict],
                       market: dict) -> dict[str, Any]:
        """One (deployment, kind): pick the winner and name the state.

        The winner is always one of `obs` — this deployment's own evidence. The
        market median below may *refuse* the newest observation, and the deployment
        then falls back to its own previous one; it never adopts a peer's number.
        """
        newest = obs[-1]
        value = newest["usd_per_mtok"]
        winner: dict | None = newest
        state, reason = PRICE_CANONICAL, "newest observation"

        # The market for this artifact, one vote per source, as of each source's
        # newest observation. Includes this deployment's own source.
        reference = list(market.get((newest["weights_id"], kind), {}).values())
        reference_values = [o["usd_per_mtok"] for o in reference]
        reference_median = (_median(reference_values)
                            if len(reference_values) >= PRICE_MIN_SOURCES else None)
        source_count = len(reference_values)
        factor: float | None = None

        previous = obs[-2]["usd_per_mtok"] if len(obs) >= 2 else None
        history = _deviation(value, previous) if previous is not None else None

        if reference_median is not None and value:
            # `value` is checked too: a free deployment among paid peers is a free
            # tier, not an anomaly, and `_deviation(0, paid)` is infinity.
            factor = _deviation(value, reference_median)
            if factor >= PRICE_PEER_QUARANTINE_FACTOR:
                state = PRICE_QUARANTINED
                reason = (f"{factor:.0f}x the {source_count}-source market median "
                          f"({_per_mtok(reference_median)}) — refused")
                # Fall back to the deployment's OWN previous value. Never the market
                # median: that number belongs to other deployments, and adopting it
                # would invent a price no provider publishes.
                if len(obs) >= 2:
                    winner = obs[-2]
                    value = winner["usd_per_mtok"]
                else:
                    winner, value = None, None
            elif factor >= PRICE_PEER_SUSPECT_FACTOR:
                state = PRICE_SUSPECT
                reason = (f"{factor:.1f}x the {source_count}-source market median "
                          "— flagged")
            elif history is not None and history >= PRICE_HISTORY_FACTOR:
                # A large change the market agrees with is a repricing, not a bug.
                reason = (f"repricing confirmed across {source_count} sources "
                          f"({_factor_label(history)} change)")
        elif history is not None and history >= PRICE_HISTORY_FACTOR:
            state = PRICE_SUSPECT
            reason = (f"{_factor_label(history)} jump from the previous observation, "
                      f"only {source_count} source(s) to compare against")

        if state in (PRICE_CANONICAL, PRICE_SUSPECT) and winner is not None:
            age = _age_days(winner["observed_at"])
            if age is not None and age > PRICE_FRESHNESS_DAYS:
                state = PRICE_EXPIRED
                reason = f"newest observation is {age:.0f}d old"

        return {
            "deploy_id": deploy_id,
            "kind": kind,
            "weights_id": newest["weights_id"],
            "usd_per_mtok": value,
            "state": state,
            "reason": reason,
            "source": None if winner is None else winner["source"],
            "observed_at": None if winner is None else winner["observed_at"],
            "confidence": None if winner is None else winner["confidence"],
            "reference_median": reference_median,
            "source_count": source_count,
            "factor": factor,
            # For the anomaly log: what the newest observation said, what it was
            # measured against, and the *coordinates* of both — never the evidence
            # rowid, which does not survive a sync.
            "observed": newest["usd_per_mtok"],
            "expected": (reference_median if reference_median is not None else previous),
            "evidence": {
                "newest": {"source": newest["source"],
                           "observed_at": newest["observed_at"],
                           "usd_per_mtok": newest["usd_per_mtok"]},
                "kept": None if winner is None else {
                    "source": winner["source"],
                    "observed_at": winner["observed_at"],
                    "usd_per_mtok": winner["usd_per_mtok"],
                },
            },
        }

    def _apply_deployment_price(self, deploy_id: str, kinds: dict[str, dict],
                                stamp: str) -> int:
        """Materialize a reconciled price and re-run the free/paid transition.

        Returns 1 when the stored economics changed — a count of real changes, not
        of rows visited.
        """
        existing = self.conn.execute(
            "SELECT price_in, price_out, price_cached_in, zero_price, free_variant,"
            " status, status_source FROM deployments WHERE deploy_id=?", (deploy_id,),
        ).fetchone()
        if existing is None:
            return 0

        sets: dict[str, Any] = {}
        for kind, column in (("input", "price_in"), ("output", "price_out"),
                             ("cached_input", "price_cached_in")):
            if kind in kinds:
                sets[column] = kinds[kind]["usd_per_mtok"]

        if "input" in kinds or "output" in kinds:
            pin = sets.get("price_in", existing["price_in"])
            pout = sets.get("price_out", existing["price_out"])
            # Free is both sides *known* and zero. A quarantined side resolved to
            # NULL, which is unknown, not free — the distinction the flag exists for.
            sets["zero_price"] = int(pin is not None and pout is not None
                                     and pin == 0 and pout == 0)

        status, reason = _spend_transition(
            incoming_free=bool(sets.get("zero_price", existing["zero_price"])
                               or existing["free_variant"]),
            was_free=bool(existing["zero_price"] or existing["free_variant"]),
            existing_status=existing["status"],
            existing_source=existing["status_source"],
            # Reconciling reports no catalogue status of its own, so the row's own
            # status is the input: this path can preserve or hibernate, never
            # invent a `degraded`.
            incoming_status=existing["status"],
            price_in=sets.get("price_in", existing["price_in"]),
            price_out=sets.get("price_out", existing["price_out"]),
        )
        if status is not None:
            sets["status"] = status
            if status != existing["status"]:
                sets["status_changed_at"] = stamp
        if reason is not None:
            sets["status_reason"] = reason

        changed = any(
            column in sets and sets[column] != existing[column]
            for column in ("price_in", "price_out", "price_cached_in", "zero_price", "status")
        )
        if not sets:
            return 0
        assignments = ", ".join(f"{column}=?" for column in sets)
        self.conn.execute(
            f"UPDATE deployments SET {assignments} WHERE deploy_id=?",
            (*sets.values(), deploy_id),
        )
        return 1 if changed else 0

    # ------------------------------------------------------------ anomalies (P3)

    def _reconcile_anomalies(self, resolutions: list[dict], stamp: str) -> dict[str, int]:
        """Open, refresh or close the anomaly log from this pass.

        An anomaly is a *disagreement that needs a human*: the reconciler refused a
        price or flagged one. Entering `suspect`/`quarantined` opens one; returning
        to `canonical` closes it, because that needs no judgement. The row is
        refreshed rather than re-inserted, so the log holds one row per ongoing
        incident rather than one per reconcile run.
        """
        flagged = {
            (r["deploy_id"], r["kind"]): r for r in resolutions
            if r["state"] in (PRICE_QUARANTINED, PRICE_SUSPECT)
        }
        open_rows = {
            (r["deploy_id"], r["dimension"]): dict(r)
            for r in self.conn.execute(
                "SELECT anomaly_id, deploy_id, dimension FROM price_anomalies"
                " WHERE status IN ('open','acknowledged')")
        }
        opened = refreshed = closed = 0
        for key, r in flagged.items():
            row = open_rows.get(key)
            if row is None:
                self._open_anomaly(r, stamp)
                opened += 1
            else:
                self._refresh_anomaly(row["anomaly_id"], r, stamp)
                refreshed += 1
        for key, row in open_rows.items():
            if key not in flagged:
                self._close_anomaly(row["anomaly_id"], stamp, "price returned to canonical")
                closed += 1
        return {"opened": opened, "refreshed": refreshed, "closed": closed}

    def _open_anomaly(self, r: dict, stamp: str) -> None:
        # Content-derived id: the deploy, the dimension, and the microsecond it was
        # first seen. Not a rowid — `sync` drops surrogate ids, so a rowid would
        # make two mirrors disagree about which incident a reference points at.
        # Microseconds rather than the second-precision `stamp`, because resolving
        # a still-wrong anomaly and re-reconciling must open a genuinely new row
        # instead of colliding with the one just closed.
        opened = _unique_stamp()
        anomaly_id = f"{r['deploy_id']}|{r['kind']}|{opened}"
        self.conn.execute(
            """INSERT INTO price_anomalies (anomaly_id,deploy_id,weights_id,dimension,kind,
               severity,state,expected,observed,factor,source_count,resolved_value,detail,
               evidence,status,opened_at,last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)
               ON CONFLICT(anomaly_id) DO NOTHING""",
            (anomaly_id, r["deploy_id"], r["weights_id"], r["kind"], _anomaly_kind(r),
             _anomaly_severity(r), r["state"], r["expected"], r["observed"], r["factor"],
             r["source_count"], r["usd_per_mtok"], r["reason"],
             json.dumps(r["evidence"], default=str), stamp, stamp),
        )

    def _refresh_anomaly(self, anomaly_id: str, r: dict, stamp: str) -> None:
        """Update an open incident in place.

        `status` and `opened_at` are deliberately untouched, so an operator's
        acknowledgement survives every subsequent reconcile.
        """
        self.conn.execute(
            """UPDATE price_anomalies SET severity=?, state=?, expected=?, observed=?,
               factor=?, source_count=?, resolved_value=?, detail=?, evidence=?, last_seen_at=?
               WHERE anomaly_id=?""",
            (_anomaly_severity(r), r["state"], r["expected"], r["observed"], r["factor"],
             r["source_count"], r["usd_per_mtok"], r["reason"],
             json.dumps(r["evidence"], default=str), stamp, anomaly_id),
        )

    def _close_anomaly(self, anomaly_id: str, stamp: str, note: str) -> None:
        self.conn.execute(
            "UPDATE price_anomalies SET status='resolved', resolved_at=?, resolution=?"
            " WHERE anomaly_id=? AND status IN ('open','acknowledged')",
            (stamp, note, anomaly_id),
        )

    def anomalies(self, *, status: str | None = "open", limit: int = 50) -> list[dict[str, Any]]:
        """The anomaly log, worst first. `status=None` returns everything."""
        q = "SELECT * FROM price_anomalies"
        args: list[Any] = []
        if status:
            q += " WHERE status = ?"
            args.append(status)
        # Severity, then recency: a review queue should lead with a 20x.
        q += (" ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1"
              " WHEN 'warning' THEN 2 ELSE 3 END, last_seen_at DESC LIMIT ?")
        args.append(max(1, int(limit)))
        return [dict(r) for r in self.conn.execute(q, tuple(args))]

    def decide_anomaly(self, anomaly_id: str, *, status: str, note: str = "") -> bool:
        """Acknowledge or resolve an incident by hand.

        `acknowledged` records that a human has seen it, and stops it being
        re-flagged as new on every reconcile; it does not change a price. Closing
        something that is genuinely still wrong does not silence it: the next
        reconcile opens a *new* row, so the disagreement stays visible.
        """
        if status not in ("open", "acknowledged", "resolved"):
            raise ValueError(f"unknown anomaly status {status!r}")
        from .fetch import utcnow

        stamp = utcnow()
        cur = self.conn.execute(
            "UPDATE price_anomalies SET status=?,"
            " resolution = CASE WHEN ? != '' THEN ? ELSE resolution END,"
            " resolved_at = CASE WHEN ? = 'resolved' THEN ? ELSE resolved_at END"
            " WHERE anomaly_id=?",
            (status, note, note, status, stamp, anomaly_id),
        )
        self.conn.commit()
        return bool(getattr(cur, "rowcount", 0))

    # ------------------------------------------------- price history (P6)

    def _record_price_history(self, resolutions: list[dict]) -> int:
        """Append a belief row when a price we hold *changes* (P6).

        Append-only and change-only. An ingest that re-confirms $0.075 writes
        nothing, which is what keeps this small enough to be the answer to "what
        did we believe on <date>" rather than a second copy of `pricing_evidence`.

        The belief is the price **and** whether we trust it: the same value moving
        from `canonical` to `quarantined` is a different belief, and a later ingest
        re-confirming the same value is not a new one.

        `effective_from` is microsecond-precise, unlike the other timestamps here,
        because two reconciles can land in the same second and "the belief in effect
        at T" has to be able to tell them apart. It still compares correctly against
        a second-precision `as_of`, since `"."` sorts after `"+"`.
        """
        if not resolutions:
            return 0
        current = {
            (r["deploy_id"], r["kind"]): r
            for r in self.conn.execute(
                "SELECT deploy_id, kind, usd_per_mtok, state FROM ("
                "  SELECT deploy_id, kind, usd_per_mtok, state,"
                "         ROW_NUMBER() OVER (PARTITION BY deploy_id, kind"
                "           ORDER BY effective_from DESC, history_id DESC) rn"
                # Postgres requires an alias on a subquery in FROM; SQLite does not.
                "  FROM price_history) AS latest WHERE rn = 1")
        }
        # One stamp for the whole pass: every row written together shares an
        # `effective_from`, because "we now believe this" is one event, while the id
        # stays unique per (deployment, kind).
        effective_from = _unique_stamp()
        rows = []
        for r in resolutions:
            before = current.get((r["deploy_id"], r["kind"]))
            if (before is not None and before["usd_per_mtok"] == r["usd_per_mtok"]
                    and before["state"] == r["state"]):
                continue
            rows.append((
                f"{r['deploy_id']}|{r['kind']}|{effective_from}", r["deploy_id"], r["kind"],
                r["usd_per_mtok"], r["state"], r["reason"], r["source"],
                r["observed_at"], r["confidence"], effective_from,
            ))
        if rows:
            self.conn.executemany(
                """INSERT INTO price_history (history_id,deploy_id,kind,usd_per_mtok,state,
                   reason,source,observed_at,confidence,effective_from)
                   VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(history_id) DO NOTHING""",
                rows,
            )
        return len(rows)

    #: The belief columns, spelled out rather than `SELECT *` so the `rn` that the
    #: as-of query ranks by does not leak into the result.
    _HISTORY_COLS = ("history_id, deploy_id, kind, usd_per_mtok, state, reason,"
                     " source, observed_at, confidence, effective_from")

    def price_history(self, deploy_id: str | None = None, *, as_of: str | None = None,
                      limit: int = 200) -> list[dict[str, Any]]:
        """The belief timeline, newest first; with `as_of`, the belief in effect then.

        `as_of` answers "what did we believe at T" — the row in effect at that
        instant, not the observation nearest to it. The two differ whenever we
        learned something late, which is exactly when the distinction matters.
        """
        limit = max(1, int(limit))
        if as_of:
            q = (f"SELECT {self._HISTORY_COLS} FROM ("
                 "  SELECT *, ROW_NUMBER() OVER (PARTITION BY deploy_id, kind"
                 "         ORDER BY effective_from DESC, history_id DESC) rn"
                 "  FROM price_history WHERE effective_from <= ?")
            args: list[Any] = [as_of]
            if deploy_id:
                q += " AND deploy_id = ?"
                args.append(deploy_id)
            q += ") AS ranked WHERE rn = 1 ORDER BY deploy_id, kind LIMIT ?"
            args.append(limit)
            return [dict(r) for r in self.conn.execute(q, tuple(args))]

        q = f"SELECT {self._HISTORY_COLS} FROM price_history"
        args = []
        if deploy_id:
            q += " WHERE deploy_id = ?"
            args.append(deploy_id)
        q += " ORDER BY effective_from DESC, history_id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(q, tuple(args))]

    # -------------------------------------------------- pricing state (P4)

    def _sync_pricing_states(self, stamp: str) -> dict[str, Any]:
        """Derive every deployment's pricing state, and log what changed.

        The only writer of `deployments.pricing_state`. It covers **every**
        deployment, not just the ones with price evidence: a seeded or
        agent-ingested row has a price and a status but no observations, and a
        state machine that skipped those would leave blank exactly the rows a
        human is most likely to inspect.

        A transition is detected *between reconciles*, and `detected_at` is when we
        noticed, not when the provider changed. That is honest: a single ingest run
        that reads a free observation and a paid one before reconciling records only
        the resulting state, because there is only one current price to derive from.
        Replaying the evidence history to reconstruct what we believed on an
        arbitrary past date is P6 (`GET /v1/economics/history`); this is the log of
        what the reconciler actually observed.
        """
        # Roll every expired quota window over once, in bulk, so the headroom map
        # below is not read from a bucket that has already reset.
        self.reset_expired()
        headroom = {
            r["deploy_id"]: r["headroom"]
            for r in self.conn.execute(
                "SELECT deploy_id, MIN(h) AS headroom FROM ("
                "  SELECT deploy_id, (limit_n - COALESCE(used_n, 0)) * 1.0 / limit_n AS h"
                "  FROM quota_buckets WHERE limit_n IS NOT NULL AND limit_n > 0"
                # Aliased for Postgres; SQLite tolerates its absence, so an
                # unaliased subquery is a bug that only shows up on the hosted engine.
                ") AS buckets GROUP BY deploy_id")
        }
        rows = self.conn.execute(
            "SELECT deploy_id, pricing_state, status, zero_price, free_variant,"
            " subscription, trial_credits, price_in, price_out FROM deployments").fetchall()

        transitions: list[tuple] = []
        counts: dict[str, int] = {}
        for row in rows:
            state = _pricing_state(
                status=row["status"], zero_price=row["zero_price"],
                free_variant=row["free_variant"], subscription=row["subscription"],
                trial_credits=row["trial_credits"], price_in=row["price_in"],
                price_out=row["price_out"], headroom=headroom.get(row["deploy_id"]),
            )
            counts[state] = counts.get(state, 0) + 1
            if row["pricing_state"] == state:
                continue
            self.conn.execute(
                "UPDATE deployments SET pricing_state=? WHERE deploy_id=?",
                (state, row["deploy_id"]),
            )
            transitions.append(
                self._transition_row(row, state, headroom.get(row["deploy_id"]), stamp))
        if transitions:
            self.conn.executemany(
                """INSERT INTO pricing_transitions (transition_id,deploy_id,from_state,to_state,
                   detected_at,price_in,price_out,evidence) VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(transition_id) DO NOTHING""",
                transitions,
            )
        return {"changed": len(transitions), "states": counts}

    def _transition_row(self, row, to_state: str, headroom, stamp: str) -> tuple:
        # Content-derived id (a rowid would not survive `sync`), microsecond-precise
        # so two changes in the same second are still two rows.
        opened = _unique_stamp()
        transition_id = f"{row['deploy_id']}|{row['pricing_state'] or '-'}|{to_state}|{opened}"
        evidence = {
            "status": row["status"],
            "price_in": row["price_in"],
            "price_out": row["price_out"],
            "zero_price": bool(row["zero_price"]),
            "free_variant": bool(row["free_variant"]),
            "trial_credits": bool(row["trial_credits"]),
            "subscription": bool(row["subscription"]),
            "headroom": headroom,
        }
        return (transition_id, row["deploy_id"], row["pricing_state"], to_state, stamp,
                row["price_in"], row["price_out"], json.dumps(evidence, default=str))

    def pricing_transitions(self, deploy_id: str | None = None, *,
                            limit: int = 50) -> list[dict[str, Any]]:
        """The pricing-state history, newest first; optionally one deployment's."""
        q = "SELECT * FROM pricing_transitions"
        args: list[Any] = []
        if deploy_id:
            q += " WHERE deploy_id = ?"
            args.append(deploy_id)
        q += " ORDER BY detected_at DESC, transition_id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return [dict(r) for r in self.conn.execute(q, tuple(args))]

    # ------------------------------------------------------------------ quotas

    def set_bucket(
        self, deploy_id: str, window: str, limit_n: int | None,
        *, used_n: int = 0, api_key_alias: str = "default", source: str = "configured",
    ) -> None:
        self.conn.execute(
            """INSERT INTO quota_buckets (deploy_id,"window",api_key_alias,limit_n,used_n,source,updated_at)
               VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(deploy_id,"window",api_key_alias) DO UPDATE SET
                 limit_n=COALESCE(excluded.limit_n,quota_buckets.limit_n),
                 used_n=excluded.used_n, source=excluded.source, updated_at=CURRENT_TIMESTAMP""",
            (deploy_id, window, api_key_alias, limit_n, used_n, source),
        )

    def set_limit(
        self, deploy_id: str, window: str, limit_n: int,
        *, api_key_alias: str = "default", source: str = "configured",
        reset_at: str | None = None,
    ) -> None:
        """Declare a quota limit. Unlike `set_bucket`, this PRESERVES `used_n`,
        so re-seeding limits never silently refills a drained bucket."""
        reset_at = reset_at or _next_reset(window, dt.datetime.now(dt.timezone.utc))
        self.conn.execute(
            """INSERT INTO quota_buckets
                 (deploy_id,"window",api_key_alias,limit_n,used_n,reset_at,source,updated_at)
               VALUES (?,?,?,?,0,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(deploy_id,"window",api_key_alias) DO UPDATE SET
                 limit_n=excluded.limit_n,
                 reset_at=COALESCE(excluded.reset_at,quota_buckets.reset_at),
                 source=excluded.source, updated_at=CURRENT_TIMESTAMP""",
            (deploy_id, window, api_key_alias, limit_n, reset_at, source),
        )

    def record_rate_limit(
        self, deploy_id: str, *, window: str, limit_n: int | None = None,
        remaining_n: int | None = None, reset_at: str | None = None,
        observed_at: str | None = None, confidence: float | None = None,
        api_key_alias: str = "default",
    ) -> None:
        """Record what the *provider* reported about a bucket (P5).

        Deliberately not `set_limit`: that declares a policy, this records an
        observation. It never writes `limit_n`, so a provider's number cannot
        overwrite a configured ceiling — `headroom` takes the minimum of the two,
        which is the only rule that cannot spend a provider's whole free tier.

        `remaining_n` is stored exactly as reported, even when it is negative or
        larger than the limit. Clamping it would hide a misconfigured bucket behind
        a plausible-looking number.
        """
        from .fetch import utcnow

        stamp = observed_at or utcnow()
        self.conn.execute(
            """INSERT INTO quota_buckets (deploy_id,"window",api_key_alias,limit_n,used_n,
                 reset_at,observed_limit_n,observed_remaining_n,observed_reset_at,
                 observed_at,observed_confidence,updated_at)
               VALUES (?,?,?,NULL,0,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(deploy_id,"window",api_key_alias) DO UPDATE SET
                 observed_limit_n=COALESCE(excluded.observed_limit_n,
                                           quota_buckets.observed_limit_n),
                 observed_remaining_n=excluded.observed_remaining_n,
                 observed_reset_at=COALESCE(excluded.observed_reset_at,
                                            quota_buckets.observed_reset_at),
                 observed_at=excluded.observed_at,
                 observed_confidence=COALESCE(excluded.observed_confidence,
                                              quota_buckets.observed_confidence),
                 updated_at=CURRENT_TIMESTAMP""",
            (deploy_id, window, api_key_alias, reset_at, limit_n, remaining_n, reset_at,
             stamp, confidence),
        )

    def _reset_expired_for(self, deploy_id: str, now: dt.datetime | None = None) -> int:
        now = now or dt.datetime.now(dt.timezone.utc)
        stamp = now.isoformat(timespec="seconds")
        rows = self.conn.execute(
            'SELECT "window", api_key_alias FROM quota_buckets'
            " WHERE deploy_id=? AND reset_at IS NOT NULL AND reset_at<=?",
            (deploy_id, stamp),
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "UPDATE quota_buckets SET used_n=0, reset_at=?, updated_at=CURRENT_TIMESTAMP"
                ' WHERE deploy_id=? AND "window"=? AND api_key_alias=?',
                (_next_reset(r["window"], now), deploy_id, r["window"], r["api_key_alias"]),
            )
        return len(rows)

    def reset_expired(self, now: dt.datetime | None = None) -> int:
        """Roll over every expired window in the registry. Returns rows reset."""
        now = now or dt.datetime.now(dt.timezone.utc)
        stamp = now.isoformat(timespec="seconds")
        rows = self.conn.execute(
            'SELECT deploy_id, "window", api_key_alias FROM quota_buckets'
            " WHERE reset_at IS NOT NULL AND reset_at<=?", (stamp,)
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "UPDATE quota_buckets SET used_n=0, reset_at=?, updated_at=CURRENT_TIMESTAMP"
                ' WHERE deploy_id=? AND "window"=? AND api_key_alias=?',
                (_next_reset(r["window"], now), r["deploy_id"], r["window"],
                 r["api_key_alias"]),
            )
        return len(rows)

    def record_usage(
        self, deploy_id: str, *, window: str | None = None, n: int = 1,
        api_key_alias: str = "default",
    ) -> int:
        """Charge `n` calls to a deployment's buckets. Called on every real call,
        success or failure — a 429 still consumed an attempt."""
        self._reset_expired_for(deploy_id)
        q = ("UPDATE quota_buckets SET used_n=used_n+?, updated_at=CURRENT_TIMESTAMP"
             " WHERE deploy_id=? AND api_key_alias=?")
        args: list[Any] = [n, deploy_id, api_key_alias]
        if window:
            q += ' AND "window"=?'
            args.append(window)
        return self.conn.execute(q, args).rowcount

    def exhaust(
        self, deploy_id: str, *, window: str | None = None,
        api_key_alias: str = "default",
    ) -> int:
        """Mark bucket(s) drained — e.g. on an observed 429. Only affects buckets
        with a known limit; an unknown limit cannot be 'exhausted'."""
        q = ("UPDATE quota_buckets SET used_n=limit_n, updated_at=CURRENT_TIMESTAMP"
             " WHERE deploy_id=? AND api_key_alias=? AND limit_n IS NOT NULL")
        args: list[Any] = [deploy_id, api_key_alias]
        if window:
            q += ' AND "window"=?'
            args.append(window)
        return self.conn.execute(q, args).rowcount

    def headroom(self, deploy_id: str) -> tuple[float | None, str]:
        """Fraction of the tightest bucket still available, and its source.

        The **effective** fraction — the minimum of the configured policy limit and
        whatever the provider last reported — because either side alone gets one
        case wrong: a policy would spend a provider's whole free tier, and a
        provider limit would be ignored whenever the policy was more generous.

        Returns `(None, 'unknown')` when neither side is known, which the router
        treats conservatively. Expired windows are rolled over first, so a drained
        bucket refills on its own.
        """
        self._reset_expired_for(deploy_id)
        rows = self.conn.execute(
            "SELECT limit_n, used_n, observed_limit_n, observed_remaining_n"
            " FROM quota_buckets WHERE deploy_id=?", (deploy_id,)
        ).fetchall()
        best: float | None = None
        src = "unknown"
        for r in rows:
            frac, part = effective_headroom(
                limit_n=r["limit_n"], used_n=r["used_n"],
                observed_limit_n=r["observed_limit_n"],
                observed_remaining_n=r["observed_remaining_n"])
            if frac is None:
                continue
            if best is None or frac < best:
                best, src = frac, part
        return best, src

    # ------------------------------------------------------------ observations

    def observe(
        self, deploy_id: str, task: str, ok: bool, *, ts: str, error_class: str | None = None,
        latency_ms: float | None = None, tokens_in: int | None = None,
        tokens_out: int | None = None, cost_usd: float | None = None,
        signal_kind: str = "verified", signal_value: float | None = None,
        meta: dict | None = None, session_id: str | None = None,
        tenant_id: str | None = None, rate_limit_reason: str | None = None,
        effort: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO observations (deploy_id,task,ts,ok,error_class,latency_ms,tokens_in,
               tokens_out,cost_usd,signal_kind,signal_value,meta,session_id,tenant_id,
               rate_limit_reason,effort)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (deploy_id, task, ts, int(ok), error_class, latency_ms, tokens_in, tokens_out,
             cost_usd, signal_kind, signal_value, json.dumps(meta or {}), session_id,
             tenant_id, rate_limit_reason, effort),
        )

    def spend_savings(self, *, session_id: str | None = None,
                      tenant_id: str | None = None,
                      days: int | None = None) -> dict[str, Any]:
        """Actual spend vs. the cheapest *paid* price for the same artifact.

        The counterfactual, stated because the number is only as honest as its
        definition: for a successful call, `saved_usd` is what the *same tokens*
        would have cost at the cheapest paid deployment of the same `weights_id`,
        counted only for calls that themselves cost nothing. A paid call saves
        nothing by this measure, and a model with no paid sibling saves nothing
        either — the registry cannot claim you avoided a price nobody published.
        `unpriced_free_calls` counts exactly those, so the estimate never reads as
        complete when it is not.

        Prices are the registry's *current* ones, not the ones at call time, so
        this is an estimate and is labelled as one. Rows without token counts are
        excluded: savings arithmetic on unknown usage would be invention.
        """
        where = ["o.ok = 1", "o.tokens_in IS NOT NULL", "o.tokens_out IS NOT NULL"]
        args: list[Any] = []
        if session_id:
            where.append("o.session_id = ?")
            args.append(session_id)
        if tenant_id:
            where.append("o.tenant_id = ?")
            args.append(tenant_id)
        if days:
            where.append("o.ts >= ?")
            args.append(_since(days))
        clause = " AND ".join(where)

        # The correlated subquery is a `MIN` over a computed expression, not over
        # `price_in` and `price_out` separately: the cheapest input price and the
        # cheapest output price can belong to two different siblings, and picking
        # them independently would price a deployment that does not exist.
        row = self.conn.execute(
            f"""WITH priced AS (
                  SELECT COALESCE(o.cost_usd, 0.0) AS actual,
                         (SELECT MIN((p.price_in * o.tokens_in
                                      + p.price_out * o.tokens_out) / 1000000.0)
                            FROM deployments p
                           WHERE p.weights_id = d.weights_id
                             AND p.deploy_id <> o.deploy_id
                             AND p.price_in IS NOT NULL
                             AND p.price_out IS NOT NULL
                             AND p.zero_price = 0
                             AND p.free_variant = 0
                             AND p.subscription = 0
                             AND p.trial_credits = 0) AS shadow
                    FROM observations o
                    JOIN deployments d ON d.deploy_id = o.deploy_id
                   WHERE {clause})
                SELECT COUNT(*) AS calls,
                       SUM(CASE WHEN actual = 0 THEN 1 ELSE 0 END) AS free_calls,
                       COALESCE(SUM(actual), 0.0) AS actual_cost_usd,
                       COALESCE(SUM(CASE WHEN actual = 0 AND shadow IS NOT NULL
                                         THEN shadow ELSE 0.0 END), 0.0) AS saved_usd,
                       SUM(CASE WHEN actual = 0 AND shadow IS NULL
                                THEN 1 ELSE 0 END) AS unpriced_free_calls
                  FROM priced""",
            tuple(args),
        ).fetchone()
        out = dict(row)
        out["calls"] = int(out["calls"] or 0)
        out["free_calls"] = int(out["free_calls"] or 0)
        out["unpriced_free_calls"] = int(out["unpriced_free_calls"] or 0)
        for k in ("actual_cost_usd", "saved_usd"):
            out[k] = round(float(out[k] or 0.0), 6)
        out["session_id"] = session_id
        out["tenant_id"] = tenant_id
        out["days"] = days
        # How the counterfactual was priced, so a caller can label the number rather
        # than trusting it (P7). `priced_free_share` is the fraction of free calls
        # that had a paid sibling to price against — a *coverage* measure, not a
        # statistical confidence, which is why it is not called one.
        out["price_basis"] = "cheapest paid sibling, reconciled prices"
        out["priced_free_share"] = (
            round((out["free_calls"] - out["unpriced_free_calls"]) / out["free_calls"], 4)
            if out["free_calls"] else None
        )
        return out

    def usage_report(self, *, tenant_id: str | None = None,
                     days: int | None = None, top: int = 5) -> dict[str, Any]:
        """Per-tenant usage, for reporting.

        `tenant_id=None` is the operator's whole-registry view; a tenant gets its
        own numbers and nothing else (the endpoint, not this method, enforces
        that). The report joins three tables written on three paths —
        `observations` (model calls), `decisions` (routing) and `sessions` (spend,
        which is the only place search cost exists) — rather than pretending one
        is authoritative for all of it.

        The decision this does *not* make: the router still learns from every
        tenant's observations. `tenant_id` is an attribution label, not a
        partition. Isolating the learned signal would give every new tenant a cold
        start and discard the accumulation that makes free-first work; what is
        genuinely per-tenant is spend, headroom and transcripts, and those are
        scoped.
        """
        since, since_sql = (_since(days), _since_sql(days)) if days else (None, None)

        obs_where, obs_args = ["1=1"], []
        if tenant_id:
            obs_where.append("tenant_id = ?")
            obs_args.append(tenant_id)
        if since:
            obs_where.append("ts >= ?")
            obs_args.append(since)
        clause = " AND ".join(obs_where)

        totals = self.conn.execute(
            f"""SELECT COUNT(*) AS calls,
                       SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok_calls,
                       COALESCE(SUM(tokens_in),0) AS tokens_in,
                       COALESCE(SUM(tokens_out),0) AS tokens_out,
                       COALESCE(SUM(cost_usd),0.0) AS cost_usd
                  FROM observations WHERE {clause}""",
            tuple(obs_args)).fetchone()
        by_task = self.conn.execute(
            f"""SELECT task, COUNT(*) AS calls,
                       COALESCE(SUM(cost_usd),0.0) AS cost_usd
                  FROM observations WHERE {clause}
                 GROUP BY task ORDER BY calls DESC, task ASC LIMIT ?""",
            tuple(obs_args) + (top,)).fetchall()
        by_deploy = self.conn.execute(
            f"""SELECT deploy_id, COUNT(*) AS calls,
                       SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS wins,
                       COALESCE(SUM(cost_usd),0.0) AS cost_usd
                  FROM observations WHERE {clause}
                 GROUP BY deploy_id ORDER BY calls DESC, deploy_id ASC LIMIT ?""",
            tuple(obs_args) + (top,)).fetchall()

        def _count(table: str, ts_col: str) -> int:
            where, args = ["1=1"], []
            if tenant_id:
                where.append("tenant_id = ?")
                args.append(tenant_id)
            if since_sql:
                where.append(f"{ts_col} >= ?")
                args.append(since_sql)
            q = f"SELECT COUNT(*) AS c FROM {table} WHERE {' AND '.join(where)}"
            return int(self.conn.execute(q, tuple(args)).fetchone()["c"] or 0)

        sess_args: list[Any] = []
        sess_where = ["1=1"]
        if tenant_id:
            sess_where.append("tenant_id = ?")
            sess_args.append(tenant_id)
        if since_sql:
            sess_where.append("last_seen >= ?")
            sess_args.append(since_sql)
        search = self.conn.execute(
            f"""SELECT COALESCE(SUM(searches),0) AS searches,
                       COALESCE(SUM(search_cost_usd),0.0) AS search_cost_usd
                  FROM sessions WHERE {' AND '.join(sess_where)}""",
            tuple(sess_args)).fetchone()

        tin = int(totals["tokens_in"] or 0)
        tout = int(totals["tokens_out"] or 0)
        out = {
            "tenant_id": tenant_id,
            "days": days,
            "calls": int(totals["calls"] or 0),
            "ok_calls": int(totals["ok_calls"] or 0),
            "tokens_in": tin, "tokens_out": tout, "tokens": tin + tout,
            "cost_usd": round(float(totals["cost_usd"] or 0.0), 6),
            "searches": int(search["searches"] or 0),
            "search_cost_usd": round(float(search["search_cost_usd"] or 0.0), 6),
            "sessions": _count("sessions", "last_seen"),
            "decisions": _count("decisions", "ts"),
            "by_task": [{**dict(r), "cost_usd": round(float(r["cost_usd"] or 0.0), 6)}
                        for r in by_task],
            "by_deploy": [{**dict(r),
                           "cost_usd": round(float(r["cost_usd"] or 0.0), 6)}
                          for r in by_deploy],
        }
        out["saved_usd"] = self.spend_savings(tenant_id=tenant_id, days=days)["saved_usd"]
        return out

    def stats(self, task: str | None = None) -> dict[str, dict]:
        q = "SELECT * FROM routing_stats"
        args: tuple = ()
        if task:
            q += " WHERE task=?"
            args = (task,)
        return {r["deploy_id"]: dict(r) for r in self.conn.execute(q, args)}

    def routing_stats_by_effort(self, task: str | None = None,
                                effort: str | None = None) -> dict[tuple, dict]:
        """`routing_stats` at the `(deploy_id, task, effort)` grain.

        The router still reads the task-level view; this is the difficulty-aware
        cut, so a model that is strong on easy prompts and weak on hard ones can be
        told apart instead of averaged into one mediocre number. Grouping by a
        nullable `effort` keeps pre-existing rows (NULL) in their own bucket rather
        than silently folding them into "medium".
        """
        q = f"""
            SELECT deploy_id, task, effort,
                   COUNT(*) AS n_all,
                   SUM(CASE WHEN counted THEN 1 ELSE 0 END) AS n,
                   SUM(CASE WHEN counted THEN ok ELSE 0 END) AS wins
            FROM (SELECT *, ({_counted_expr()}) AS counted FROM observations) AS obs
        """
        where: list[str] = []
        args: list = []
        if task:
            where.append("task=?")
            args.append(task)
        if effort:
            where.append("effort=?")
            args.append(effort)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " GROUP BY deploy_id, task, effort"
        return {(r["deploy_id"], r["task"], r["effort"]): dict(r)
                for r in self.conn.execute(q, tuple(args))}

    def routing_approval_stats(self, task: str | None = None) -> dict[str, dict[str, int]]:
        q = """
            SELECT deploy_id,
                   SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS approvals,
                   SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS disapprovals
            FROM observations
            WHERE signal_kind = 'routing_approval'
        """
        args: tuple = ()
        if task:
            q += " AND task=?"
            args = (task,)
        q += " GROUP BY deploy_id"
        return {r["deploy_id"]: {"approvals": int(r["approvals"] or 0),
                                  "disapprovals": int(r["disapprovals"] or 0)}
                for r in self.conn.execute(q, args)}

    def record_decision(self, *, task: str, policy: str, mode: str, chosen: str,
                        candidates: list[str], reason: str | dict,
                        tenant_id: str | None = None) -> int:
        from .fetch import utcnow

        reason_str = json.dumps(reason, default=str) if isinstance(reason, (dict, list)) else str(reason)
        self.conn.execute(
            """INSERT INTO decisions (ts,task,policy,mode,chosen,candidates,reason,tenant_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (utcnow(), task, policy, mode, chosen, json.dumps(candidates), reason_str,
             tenant_id),
        )
        # Via the dialect seam, not `cursor.lastrowid`: that is always 0 on
        # psycopg2, which made every decision id here wrong on Postgres.
        return self.conn.last_insert_id()

    def update_decision_reason(self, decision_id: int | None, reason: dict) -> None:
        """Replace a decision's `reason` after the fact.

        The row is written before execution — the funnel is known then, the
        outcome is not — so anything learned during execution has to be written
        back explicitly. The escalation path is the case that forces it: without
        this, `complexity_escalated` is set on an in-memory dict after the INSERT
        and never persisted, so `mi complexity --report` reports an escalation
        rate that is structurally always zero.
        """
        if decision_id is None:
            return
        self.conn.execute("UPDATE decisions SET reason = ? WHERE id = ?",
                          (json.dumps(reason, default=str), decision_id))
        self.conn.commit()

    def update_decision_preference(self, decision_id: int | None, preferred: str,
                                   task: str | None = None) -> None:
        """Mark the winning model chosen by the user in a comparison decision."""
        target_id = decision_id
        if target_id is None:
            row = self.conn.execute(
                "SELECT id FROM decisions WHERE mode = 'compare' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                target_id = row["id"]
        if target_id is not None:
            row = self.conn.execute("SELECT reason FROM decisions WHERE id = ?", (target_id,)).fetchone()
            if row:
                try:
                    r = json.loads(row["reason"])
                except Exception:
                    r = {}
                r["preferred"] = preferred
                self.conn.execute("UPDATE decisions SET reason = ? WHERE id = ?", (json.dumps(r), target_id))
                self.conn.commit()

    def get_pushed_models(self, task: str | None = None, ttl_hours: float = 24.0) -> list[str]:
        """Return list of deploy_ids that are pushed by user to be selected in top 3 and within TTL."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pushed_models (
              deploy_id  TEXT PRIMARY KEY,
              task       TEXT,
              ts         TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Purge entries older than `ttl_hours`. Portable on purpose: the previous
        # `datetime(ts) < datetime('now', ?)` is a SQLite-only function, and on
        # Postgres the failed DELETE *aborts the transaction* — the SELECT below
        # then failed with `InFailedSqlTransaction`, so every routing request on
        # the Postgres backend 500'd while the `except: pass` here hid the cause.
        # `ts` is written by `push_model` through `utcnow()`, the same
        # `YYYY-MM-DDTHH:MM:SS+00:00` shape as `_since`, so a lexical comparison
        # against a cutoff in that shape is exact (see `_since`).
        try:
            cutoff = (dt.datetime.now(dt.timezone.utc)
                      - dt.timedelta(hours=float(ttl_hours))).isoformat(timespec="seconds")
            self.conn.execute(
                "DELETE FROM pushed_models WHERE ts IS NOT NULL AND ts < ?", (cutoff,))
            self.conn.commit()
        except Exception:
            pass

        if task:
            q = "SELECT deploy_id FROM pushed_models WHERE task = ? OR task = '' OR task IS NULL ORDER BY ts DESC"
            return [r["deploy_id"] for r in self.conn.execute(q, (task,)).fetchall()]
        q = "SELECT deploy_id FROM pushed_models ORDER BY ts DESC"
        return [r["deploy_id"] for r in self.conn.execute(q).fetchall()]

    def push_model(self, deploy_id: str, task: str | None = None) -> None:
        """Mark a deployment as priority pushed to top 3."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pushed_models (
              deploy_id  TEXT PRIMARY KEY,
              task       TEXT,
              ts         TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute(
            "INSERT INTO pushed_models (deploy_id, task, ts) VALUES (?, ?, ?)"
            " ON CONFLICT (deploy_id) DO UPDATE SET task=excluded.task, ts=excluded.ts",
            (deploy_id, task or "", utcnow()),
        )
        self.conn.commit()

    def unpush_model(self, deploy_id: str | None = None) -> None:
        """Clear a pushed model or all pushed models."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pushed_models (
              deploy_id  TEXT PRIMARY KEY,
              task       TEXT,
              ts         TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        if deploy_id:
            self.conn.execute("DELETE FROM pushed_models WHERE deploy_id = ?", (deploy_id,))
        else:
            self.conn.execute("DELETE FROM pushed_models")
        self.conn.commit()

    # ------------------------------------------------------ runtime settings

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (key, str(value), utcnow()))

    # ------------------------------------------------------- provider health

    def set_provider_health(self, provider: str, status: str, *,
                            detail: str | None = None,
                            latency_ms: float | None = None,
                            n_models: int | None = None,
                            checked_at: str | None = None) -> None:
        """Upsert one provider's probe result. Called by `mininfer.probe`."""
        self.conn.execute(
            "INSERT INTO provider_health"
            " (provider, status, detail, latency_ms, n_models, checked_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (provider) DO UPDATE SET status=excluded.status,"
            " detail=excluded.detail, latency_ms=excluded.latency_ms,"
            " n_models=excluded.n_models, checked_at=excluded.checked_at",
            (provider, status, detail, latency_ms, n_models,
             checked_at or utcnow()))

    def provider_health(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT provider, status, detail, latency_ms, n_models, checked_at"
            " FROM provider_health").fetchall()
        return {r["provider"]: dict(r) for r in rows}

    def unhealthy_providers(self, *, ttl_seconds: float = 900.0) -> set[str]:
        """Providers whose last probe failed *recently*.

        Staleness is the whole point: with the probe switched off, an old failure
        must not exclude a provider forever, so anything older than
        `ttl_seconds` is treated as unknown and admitted. Timestamps are the
        `utcnow()` shape on both sides, so the lexical `>=` is exact (see
        `_since`).
        """
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(seconds=float(ttl_seconds))).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT provider FROM provider_health"
            " WHERE status != 'ok' AND checked_at >= ?", (cutoff,)).fetchall()
        return {r["provider"] for r in rows}

    # ------------------------------------------------------------------- reads

    def deployments(self, task: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT d.*, w.display_name, w.benchmark, w.family, w.params_b, w.arch,
                      w.resolved_id AS weights_id_resolved,
                      (SELECT COUNT(*) FROM deployments d2
                        WHERE d2.weights_id = d.weights_id) AS same_weights_count
               FROM deployments d JOIN weights_resolved w ON w.weights_id = d.weights_id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def reviews(self) -> list[dict[str, Any]]:
        """Deployments waiting on a human: hibernated, with the reason.

        Carries the pricing-state transition that produced the hibernation (P7), so
        the UI can say *free -> paid* as a fact rather than re-deriving it from the
        reason string.
        """
        rows = self.conn.execute(
            """SELECT d.deploy_id, d.provider, d.provider_model_id, d.price_in, d.price_out,
                      d.status, d.status_reason, d.status_changed_at, w.display_name,
                      (SELECT t.from_state FROM pricing_transitions t
                        WHERE t.deploy_id = d.deploy_id AND t.to_state = 'hibernated'
                        ORDER BY t.detected_at DESC, t.transition_id DESC
                        LIMIT 1) AS pricing_type_before
               FROM deployments d LEFT JOIN weights w ON w.weights_id = d.weights_id
               WHERE d.status = 'hibernated'
               ORDER BY d.status_changed_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def pricing_state_counts(self) -> dict[str, int]:
        """How many deployments are in each derived pricing state (P7).

        A read, where `_sync_pricing_states` is the write: the Overview needs the
        distribution without re-deriving it as a side effect of rendering.
        """
        rows = self.conn.execute(
            "SELECT pricing_state, COUNT(*) n FROM deployments GROUP BY pricing_state"
        ).fetchall()
        return {r["pricing_state"] or "unknown": int(r["n"]) for r in rows}

    def deployment_economics(self, deploy_id: str) -> dict[str, Any] | None:
        """Everything known about one deployment's economics (P7).

        Assembled here rather than by the endpoint so the shape is one definition,
        and so a caller cannot forget that a price has a *state* and a history as
        well as a value.
        """
        row = self.conn.execute(
            "SELECT d.*, w.display_name FROM deployments d"
            " LEFT JOIN weights w ON w.weights_id = d.weights_id"
            " WHERE d.deploy_id = ?", (deploy_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "deployment": dict(row),
            "resolution": [dict(r) for r in self.conn.execute(
                "SELECT * FROM price_resolution WHERE deploy_id = ? ORDER BY kind",
                (deploy_id,))],
            "history": self.price_history(deploy_id, limit=50),
            "transitions": self.pricing_transitions(deploy_id, limit=50),
            "anomalies": [a for a in self.anomalies(status=None, limit=50)
                          if a["deploy_id"] == deploy_id],
            "quota": [dict(r) for r in self.conn.execute(
                "SELECT * FROM quota_buckets WHERE deploy_id = ?", (deploy_id,))],
        }

    def decide_review(self, deploy_id: str, *, approve: bool, note: str = "") -> bool:
        """Resolve a hibernation: `approve` returns it to `live`, otherwise retire it.

        The decision is written to the row, not discarded — a later ingest sees
        `live`/`deprecated` and preserves it, so an operator is asked once rather
        than every six hours.
        """
        status = "live" if approve else "deprecated"
        reason = note or ("approved by admin" if approve else "rejected by admin")
        # `status_source='review'` is what makes the decision outrank the catalogue.
        # Without it the next ingest reports the model `live` and resurrects one a
        # human just rejected.
        cur = self.conn.execute(
            "UPDATE deployments SET status=?, status_source='review', status_reason=?,"
            " status_changed_at=CURRENT_TIMESTAMP"
            " WHERE deploy_id=? AND status='hibernated'",
            (status, reason, deploy_id),
        )
        self.conn.commit()
        return bool(getattr(cur, "rowcount", 0))

    def seed_google_and_local_deployments(self) -> None:
        """Seed the direct Google Gemini deployments the endpoint actually serves.

        The 1.5 generation was retired upstream and 2.0-flash is not served to
        every key, so both are left out; these are the current ids. They are
        seeded **paid** — the Gemini API bills per token, and a free *quota* is a
        rate limit, not a zero price. Labelling them free would make the router
        choose them on price and then spend money, which is the exact mistake the
        README's "free is a quota constraint" section warns about.
        """
        # Retire older generations (1.5 and 2.0)
        self.conn.execute(
            "UPDATE deployments SET status='deprecated', status_reason='retired upstream by Google'"
            " WHERE provider='google' AND provider_model_id IN"
            " ('gemini-1.5-flash','gemini-1.5-pro','gemini-2.0-flash','gemini-2.5-flash-lite')")
        google_models = [
            # (deploy_id, provider_model_id, context, max_out, $/Mtok in, out)
            # Prices are dollars per Mtok
            ("google:gemini-3.8-flash", "gemini-3.8-flash", 1_048_576, 65_536, 0.15, 0.60),
            ("google:gemini-3.5-flash-lite", "gemini-3.5-flash-lite", 1_048_576, 65_536, 0.075, 0.30),
            ("google:gemini-3.5-flash", "gemini-3.5-flash", 1_048_576, 65_536, 0.15, 0.60),
            ("google:gemini-3.1-flash-lite", "gemini-3.1-flash-lite", 1_048_576, 65_536, 0.075, 0.30),
            ("google:gemini-flash-latest", "gemini-flash-latest", 1_048_576, 65_536, 0.15, 0.60),
            ("google:gemini-flash-lite-latest", "gemini-flash-lite-latest", 1_048_576, 65_536, 0.075, 0.30),
            ("google:gemini-2.5-flash", "gemini-2.5-flash", 1_048_576, 65_536, 0.30, 2.50),
            ("google:gemini-2.5-pro", "gemini-2.5-pro", 1_048_576, 65_536, 1.25, 10.00),
            ("google:gemini-pro-latest", "gemini-pro-latest", 1_048_576, 65_536, 1.25, 10.00),
        ]
        caps = json.dumps({"structured": True, "tools": True, "vision": True})
        for did, pmid, ctx, max_out, pin, pout in google_models:
            wid = f"slug:google:{pmid}"
            self.conn.execute(
                "INSERT INTO weights (weights_id, display_name) VALUES (?, ?)"
                " ON CONFLICT DO NOTHING", (wid, pmid))
            self.conn.execute("""
                INSERT INTO deployments (
                    deploy_id, weights_id, provider, provider_model_id,
                    context_window, max_output, price_in, price_out,
                    zero_price, free_variant, caps, status, source
                ) VALUES (?, ?, 'google', ?, ?, ?, ?, ?, 0, 0, ?, 'live', 'seed')
                ON CONFLICT(deploy_id) DO UPDATE SET
                    price_in       = excluded.price_in,
                    price_out      = excluded.price_out,
                    context_window = excluded.context_window,
                    max_output     = excluded.max_output,
                    caps           = excluded.caps
            """, (did, wid, pmid, ctx, max_out, pin, pout, caps))
        self.conn.commit()

    def register_local_deployment(self, engine: str, model_name: str) -> str:
        """Register a locally discovered model from Ollama or llama.cpp."""
        clean_name = model_name.strip()
        deploy_id = f"{engine}:{clean_name}"
        weights_id = f"slug:local:-{clean_name.replace(':', '-').replace('/', '-')}"
        self.conn.execute("""
            INSERT INTO weights (weights_id, display_name, family)
            VALUES (?, ?, ?) ON CONFLICT DO NOTHING
        """, (weights_id, f"Local ({engine}): {clean_name}", "local"))
        caps = json.dumps({"structured": True, "tools": True, "vision": False})
        self.conn.execute("""
            INSERT INTO deployments (
                deploy_id, weights_id, provider, provider_model_id,
                context_window, max_output, price_in, price_out,
                zero_price, free_variant, caps, status, source
            ) VALUES (?, ?, ?, ?, 32768, 4096, 0.0, 0.0, 1, 1, ?, 'live', 'local_probe')
            ON CONFLICT (deploy_id) DO UPDATE SET
                weights_id       = excluded.weights_id,
                provider         = excluded.provider,
                provider_model_id= excluded.provider_model_id,
                context_window   = excluded.context_window,
                max_output       = excluded.max_output,
                price_in         = excluded.price_in,
                price_out        = excluded.price_out,
                zero_price       = excluded.zero_price,
                free_variant     = excluded.free_variant,
                caps             = excluded.caps,
                status           = excluded.status,
                source           = excluded.source
        """, (deploy_id, weights_id, engine, clean_name, caps))
        self.conn.commit()
        return deploy_id


    # ------------------------------------------------------------- one row
    # Named queries rather than `conn.execute` at the call site. A schema belongs
    # with the SQL that reads it: a column rename used to need edits in ten
    # modules, and the provider rollup below had drifted into two verbatim copies.

    def deployment_prices(self, deploy_id: str) -> dict[str, Any] | None:
        """Prices and the free-tier flags for one deployment, or None.

        Both halves are returned together because every price check in the tree is
        really a free-tier check: a zero price is only meaningful alongside *why*
        it is zero (see `router._free_kind`).
        """
        row = self.conn.execute(
            "SELECT price_in, price_out, zero_price, free_variant, subscription,"
            " trial_credits FROM deployments WHERE deploy_id=?", (deploy_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def deployment_perf(self, deploy_id: str) -> dict[str, Any] | None:
        """Scraped performance for one deployment, or None."""
        row = self.conn.execute(
            "SELECT first_token_ms, throughput FROM deployments WHERE deploy_id=?",
            (deploy_id,)).fetchone()
        return dict(row) if row is not None else None

    def set_deployment_perf(self, deploy_id: str, *, first_token_ms: float | None,
                            throughput: float | None) -> None:
        """Write scraped performance, keeping whatever is already there.

        COALESCE rather than assignment: the source page carries throughput and
        TTFT independently, so a `None` on one used to blank the other column —
        including the latency the router ranks on.
        """
        self.conn.execute(
            """UPDATE deployments SET
                 first_token_ms = COALESCE(:ft, first_token_ms),
                 throughput     = COALESCE(:tp, throughput)
               WHERE deploy_id = :id""",
            {"ft": first_token_ms, "tp": throughput, "id": deploy_id},
        )

    def weights_exists(self, weights_id: str) -> bool:
        """False once a weights row has been merged away."""
        return self.conn.execute(
            "SELECT 1 FROM weights WHERE weights_id=?", (weights_id,)).fetchone() is not None

    def weights_meta(self, weights_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT display_name, params_b, hf_repo, family FROM weights"
            " WHERE weights_id=?", (weights_id,)).fetchone()
        return dict(row) if row is not None else None

    def weights_with_benchmarks(self) -> list[dict[str, Any]]:
        """Every weights row that carries benchmark scores, with its source."""
        return [dict(r) for r in self.conn.execute(
            "SELECT weights_id, display_name, benchmark, benchmark_source FROM weights"
            " WHERE benchmark IS NOT NULL AND benchmark != '{}'")]

    def all_benchmarks(self) -> list[dict[str, Any]]:
        """The parsed benchmark blob of every weights row that has one.

        Unparseable blobs are skipped rather than raising: a bad row must not stop
        the router from ranking, and `mi ingest` will overwrite it next run.
        """
        out: list[dict[str, Any]] = []
        for row in self.conn.execute("SELECT benchmark FROM weights"):
            try:
                out.append(json.loads(row["benchmark"] or "{}"))
            except json.JSONDecodeError:
                continue
        return out

    def providers_summary(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Deployments per provider, with the free-ish count and cheapest input.

        One definition, because this query had been copied verbatim into both the
        dashboard endpoint and `mi stats` — two places to update for one schema.

        `min_in` carries provenance (P7): which source the cheapest price came
        from and whether the reconciler trusts it. A cheapest price with no
        attribution is a number an operator has to take on faith.
        """
        q = (
            "SELECT p.provider, p.n, p.free_n, p.min_in,\n"
            "       (SELECT dp.price_in_source FROM deployments_priced dp\n"
            "         WHERE dp.provider = p.provider AND dp.price_in = p.min_in\n"
            "         ORDER BY dp.deploy_id LIMIT 1) AS min_in_source,\n"
            "       (SELECT dp.price_in_state FROM deployments_priced dp\n"
            "         WHERE dp.provider = p.provider AND dp.price_in = p.min_in\n"
            "         ORDER BY dp.deploy_id LIMIT 1) AS min_in_state\n"
            "FROM (SELECT provider, COUNT(*) n,\n"
            "             SUM(CASE WHEN zero_price=1 OR free_variant=1 OR subscription=1\n"
            "                      OR trial_credits=1 THEN 1 ELSE 0 END) free_n,\n"
            "             MIN(price_in) min_in\n"
            "      FROM deployments GROUP BY provider) p\n"
            "ORDER BY p.n DESC")
        args: tuple = ()
        if limit:
            q += " LIMIT ?"
            args = (limit,)
        return [dict(r) for r in self.conn.execute(q, args)]

    # The capability keys the registry actually carries (`caps` is a JSON blob of
    # `{key: true|false|null}`). The vocabulary itself lives in `schema.CAP_KEYS`,
    # shared with the extraction agents so the two cannot drift.

    def _capability_facets(self) -> dict[str, int]:
        """How many deployments confirm each capability, for the filter chips.

        `true` only: a null capability is *unknown*, not supported, and counting
        it here would offer a filter that promises more than the registry knows.
        """
        cols = ", ".join(
            f"SUM(CASE WHEN caps LIKE ? THEN 1 ELSE 0 END) AS {k}"
            for k in CAP_KEYS)
        args = tuple(f'%"{k}": true%' for k in CAP_KEYS)
        row = self.conn.execute(f"SELECT {cols} FROM deployments", args).fetchone()
        return {k: int(row[k] or 0) for k in CAP_KEYS}

    def explore_models(
        self, *, q: str | None = None, provider: str | None = None,
        capability: str | None = None, free_only: bool = False,
        untried_only: bool = False,
        max_price_out: float | None = None, min_context: int | None = None,
        sort: str = "name", limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        """Browse the registry: filter, sort, page. Read-only."""
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))

        where: list[str] = []
        args: list[Any] = []
        if provider:
            where.append("d.provider = ?")
            args.append(provider)
        if q:
            like = f"%{q}%"
            where.append("(d.deploy_id LIKE ? OR d.provider_model_id LIKE ?"
                         " OR w.display_name LIKE ? OR w.weights_id LIKE ?)")
            args += [like, like, like, like]
        if capability and capability in CAP_KEYS:
            where.append("d.caps LIKE ?")
            args.append(f'%"{capability}": true%')
        if untried_only:
            where.append("(d.zero_price=1 OR d.free_variant=1 OR d.trial_credits=1) AND COALESCE(s.n, 0) = 0")
        elif free_only:
            # Subscription is excluded on purpose: it is not free, it is prepaid,
            # which is the same distinction `router._free_kind` makes.
            where.append("(d.zero_price=1 OR d.free_variant=1 OR d.trial_credits=1)")
        if max_price_out is not None:
            where.append("d.price_out IS NOT NULL AND d.price_out <= ?")
            args.append(max_price_out)
        if min_context is not None:
            where.append("d.context_window IS NOT NULL AND d.context_window >= ?")
            args.append(min_context)
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        order = {
            "name": "w.display_name ASC, d.provider ASC",
            "price": "COALESCE(d.price_in, 9e9) ASC, COALESCE(d.price_out, 9e9) ASC",
            "context": "COALESCE(d.context_window, 0) DESC",
            "success": ("CASE WHEN COALESCE(s.n,0)=0 THEN -1.0"
                        " ELSE CAST(s.wins AS REAL)/s.n END DESC, COALESCE(s.n,0) DESC"),
            "calls": "COALESCE(s.n,0) DESC",
        }.get(sort, "w.display_name ASC, d.provider ASC")

        base = (
            " FROM deployments d"
            " LEFT JOIN weights w ON w.weights_id = d.weights_id"
            " LEFT JOIN (SELECT deploy_id, SUM(n) AS n, SUM(wins) AS wins,"
            "                   AVG(mean_latency_ms) AS mean_latency_ms"
            "            FROM routing_stats GROUP BY deploy_id) s"
            "        ON s.deploy_id = d.deploy_id"
            " LEFT JOIN pushed_models p ON p.deploy_id = d.deploy_id"
            + clause
        )
        total = self.conn.execute("SELECT COUNT(*) AS c" + base, tuple(args)).fetchone()["c"]
        rows = self.conn.execute(
            "SELECT d.deploy_id, d.weights_id, d.provider, d.provider_model_id,"
            " d.context_window, d.max_output, d.quantization, d.price_in, d.price_out,"
            " d.price_cached_in, d.zero_price, d.free_variant, d.subscription,"
            " d.trial_credits, d.caps, d.status, w.display_name, w.family, w.params_b,"
            " w.modalities, w.benchmark, w.benchmark_source,"
            " COALESCE(s.n,0) AS n, COALESCE(s.wins,0) AS wins,"
            " s.mean_latency_ms,"
            " CASE WHEN p.deploy_id IS NULL THEN 0 ELSE 1 END AS pushed"
            + base + " ORDER BY " + order + " LIMIT ? OFFSET ?",
            tuple(args) + (limit, offset),
        ).fetchall()

        models: list[dict[str, Any]] = []
        for row in rows:
            m = dict(row)
            try:
                m["caps"] = json.loads(m.get("caps") or "{}")
            except (ValueError, TypeError):
                m["caps"] = {}
            try:
                m["modalities"] = json.loads(m.get("modalities") or "[]")
            except (ValueError, TypeError):
                m["modalities"] = []
            try:
                m["benchmark"] = json.loads(m.get("benchmark") or "{}")
            except (ValueError, TypeError):
                m["benchmark"] = {}
            if m.get("zero_price"):
                kind = "zero_price"
            elif m.get("free_variant"):
                kind = "free_variant"
            elif m.get("trial_credits"):
                kind = "trial_credits"
            elif m.get("subscription"):
                kind = "subscription"
            else:
                kind = None
            m["free_kind"] = kind
            # `free` is the cheap-now facts, not the prepaid one — the same line
            # `router._free_kind` draws.
            m["free"] = kind in ("zero_price", "free_variant", "trial_credits")
            m["pushed"] = bool(m["pushed"])
            n = int(m.get("n") or 0)
            m["success_rate"] = (round(int(m["wins"]) / n, 4) if n else None)
            m["trial_status"] = "untried" if n == 0 else (f"trialing ({n}/3)" if n < 3 else "proven")
            models.append(m)

        providers = [r["provider"] for r in self.conn.execute(
            "SELECT DISTINCT provider FROM deployments ORDER BY provider")]
        return {"total": int(total), "limit": limit, "offset": offset,
                "count": len(models), "models": models,
                "providers": providers,
                "capabilities": self._capability_facets(),
                "sort": sort if sort in order else "name"}

    def recent_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        """Newest decisions first, with the stored reason blob attached."""
        return [dict(r) for r in self.conn.execute(
            "SELECT id, ts, task, policy, mode, chosen, reason FROM decisions"
            " ORDER BY id DESC LIMIT ?", (limit,))]

    def decisions_with_intent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Decisions that recorded an intent block, i.e. where `auto` was used."""
        return [dict(r) for r in self.conn.execute(
            "SELECT task, reason FROM decisions WHERE reason LIKE '%\"intent\"%'"
            " ORDER BY id DESC LIMIT ?", (limit,))]

    def decisions_with_complexity(self, limit: int = 50) -> list[dict[str, Any]]:
        """Decisions that recorded a complexity block."""
        return [dict(r) for r in self.conn.execute(
            "SELECT id, ts, task, chosen, reason FROM decisions WHERE reason LIKE '%\"complexity\"%'"
            " ORDER BY id DESC LIMIT ?", (limit,))]

    def quota_rows(self, limit: int = 40) -> list[dict[str, Any]]:
        """Bucket state, tightest first, with both limit sources.

        Ordered by the *observed* ratio when there is one, else the configured
        one: `COALESCE` rather than a scalar `MIN(a, b)`, because SQLite's `MIN`
        returns NULL if either argument is NULL where Postgres' `LEAST` skips it —
        the same query would then order differently on the two engines.
        `quota.describe` re-sorts the returned rows by the true effective headroom.
        """
        return [dict(r) for r in self.conn.execute(
            """SELECT deploy_id, "window", api_key_alias, limit_n, used_n, reset_at, source,
                      observed_limit_n, observed_remaining_n, observed_reset_at,
                      observed_at, observed_confidence
               FROM quota_buckets
               ORDER BY COALESCE(
                 CAST(observed_remaining_n AS REAL) / NULLIF(observed_limit_n, 0),
                 CAST(limit_n - used_n AS REAL) / NULLIF(limit_n, 0)
               ) ASC
               LIMIT ?""", (limit,))]

    def deploy_ids_for(self, provider: str, *, free_only: bool = False) -> list[str]:
        """Every deployment of a provider, optionally only the free-tier ones.

        `provider` matches the head of `provider/...` too, so `openrouter` finds
        `openrouter/novita:...` as well as `openrouter:...`.
        """
        q = "SELECT deploy_id FROM deployments WHERE (provider=? OR provider LIKE ?)"
        args: list[Any] = [provider, f"{provider}/%"]
        if free_only:
            q += (" AND (zero_price=1 OR free_variant=1 OR subscription=1"
                  " OR trial_credits=1)")
        return [r["deploy_id"] for r in self.conn.execute(q, args)]

    def table_rows(self, table: str) -> tuple[list[str], list[tuple]]:
        """Every row of one table, for the sync mirror.

        `table` is interpolated because SQLite cannot parameterise an identifier,
        so the caller must pass a name from a fixed list — never anything derived
        from input. The surrogate `id` is dropped: it is local row order, not data.
        """
        cur = self.conn.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description if d[0] not in _SKIP_COLS]
        return cols, [tuple(r[c] for c in cols) for r in cur.fetchall()]

    # -------------------------------------------------- entity resolution

    def unstaged_weights(self) -> list[dict[str, Any]]:
        """Weights rows that resolve to themselves (i.e. not yet aliased)."""
        rows = self.conn.execute(
            """SELECT w.weights_id, w.display_name, w.hf_repo FROM weights w
               LEFT JOIN weight_aliases a ON a.alias_id = w.weights_id
               WHERE a.alias_id IS NULL"""
        ).fetchall()
        return [dict(r) for r in rows]

    def merge_weights(self, alias_id: str, canonical_id: str, reason: str,
                      confidence: float) -> int:
        """Repoint deployments from alias to canonical. Returns rows moved.

        Guarded. A bad merge is silent and permanent from the consumer's point of
        view — it silently attaches one model's benchmarks and outcomes to
        another — so a disagreement on parameter count refuses the merge and
        records why, instead of trusting the caller.
        """
        if alias_id == canonical_id:
            return 0
        a = self.conn.execute(
            "SELECT display_name, params_b FROM weights WHERE weights_id=?", (alias_id,)
        ).fetchone()
        b = self.conn.execute(
            "SELECT display_name, params_b FROM weights WHERE weights_id=?", (canonical_id,)
        ).fetchone()
        if a is None or b is None:
            return 0
        pa, pb = a["params_b"], b["params_b"]
        if pa is not None and pb is not None and abs(pa - pb) > 0.01:
            self.conn.execute(
                "INSERT INTO quarantine (source,entity_id,field,value,reason)"
                " VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                ("resolver", alias_id, "merge",
                 json.dumps({"canonical": canonical_id, "params": [pa, pb]}),
                 f"REFUSED: param count differs ({pa}B vs {pb}B) — {a['display_name']!r} "
                 f"vs {b['display_name']!r}"),
            )
            return 0
        cur = self.conn.execute(
            "UPDATE deployments SET weights_id=? WHERE weights_id=?", (canonical_id, alias_id))
        moved = cur.rowcount
        self.conn.execute(
            """INSERT INTO weight_aliases (alias_id,canonical_id,reason,confidence)
               VALUES (?,?,?,?) ON CONFLICT (alias_id) DO UPDATE SET
                 canonical_id = excluded.canonical_id,
                 reason       = excluded.reason,
                 confidence   = excluded.confidence""",
            (alias_id, canonical_id, reason, confidence),
        )
        self.conn.execute("DELETE FROM weights WHERE weights_id=?", (alias_id,))
        return moved

    def merged_ok(self, alias_id: str) -> bool:
        """True if `alias_id` was merged (or no longer exists as a live row)."""
        row = self.conn.execute(
            "SELECT 1 FROM weight_aliases WHERE alias_id=?", (alias_id,)
        ).fetchone()
        return row is not None

    def add_quarantine(self, source: str, entity_id: str, field: str,
                       value: Any, reason: str) -> None:
        """Record a claim we refused to merge into the registry.

        Companion to `quarantine_rows`. Idempotent: the unique index means
        re-seeing the same bad value refreshes nothing rather than piling up
        duplicates on every scrape.
        """
        self.conn.execute(
            "INSERT INTO quarantine (source,entity_id,field,value,reason)"
            " VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            (source, entity_id, field, json.dumps(value, default=str), reason),
        )

    def session_usage(self, session_id: str) -> dict[str, Any]:
        """Spend so far for a session. Zeros for a session never seen."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return {"session_id": session_id, "calls": 0, "tokens_in": 0,
                    "tokens_out": 0, "cost_usd": 0.0, "tokens": 0,
                    "searches": 0, "search_cost_usd": 0.0,
                    "model_cost_usd": 0.0,
                    "first_seen": None, "last_seen": None}
        out = dict(row)
        out["tokens"] = (out["tokens_in"] or 0) + (out["tokens_out"] or 0)
        out["searches"] = out.get("searches") or 0
        out["search_cost_usd"] = out.get("search_cost_usd") or 0.0
        # `cost_usd` is the total; this is the part of it that was model calls.
        out["model_cost_usd"] = round(out["cost_usd"] - out["search_cost_usd"], 8)
        return out

    def add_session_usage(self, session_id: str, *, tokens_in: int | None = None,
                          tokens_out: int | None = None,
                          cost_usd: float | None = None,
                          calls: int = 1,
                          searches: int = 0,
                          search_cost_usd: float = 0.0,
                          tenant_id: str | None = None) -> None:
        """Add one operation's spend to a session.

        `calls` counts *model* calls and `searches` counts searches, because they
        are different units: a search has no tokens and a model call usually has
        no dollar price. A search passes `calls=0` — folding it into `calls`
        would make "how many model calls has this session made" unanswerable.

        A call that reports no usage still counts as a call: the count is what
        tells an operator the session is real when tokens are unknown, and it is
        the only signal available for providers that do not report usage.

        `cost_usd` is always the *total* for this operation, so a search adds its
        price to it and to `search_cost_usd`. That keeps one number to compare
        against a spend budget and still lets the dashboard separate the two.
        """
        self.conn.execute(
            """INSERT INTO sessions (session_id, calls, tokens_in, tokens_out,
                                     cost_usd, searches, search_cost_usd,
                                     tenant_id, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
               ON CONFLICT(session_id) DO UPDATE SET
                 calls           = sessions.calls + excluded.calls,
                 tokens_in       = sessions.tokens_in + excluded.tokens_in,
                 tokens_out      = sessions.tokens_out + excluded.tokens_out,
                 cost_usd        = sessions.cost_usd + excluded.cost_usd,
                 searches        = sessions.searches + excluded.searches,
                 search_cost_usd = sessions.search_cost_usd + excluded.search_cost_usd,
                 tenant_id       = COALESCE(sessions.tenant_id, excluded.tenant_id),
                 last_seen       = CURRENT_TIMESTAMP""",
            (session_id, calls, tokens_in or 0, tokens_out or 0, cost_usd or 0.0,
             searches, search_cost_usd, tenant_id),
        )

    # ---------------------------------------------------------- transcripts
    #
    # A session's spend is a ledger; its messages are content. Content is the
    # part that grows without bound and cannot be re-derived, so it is bounded
    # twice: only a *named* session is recorded (a one-shot stores nothing), and
    # each session keeps at most `TRANSCRIPT_LIMIT` messages. The cap matches the
    # client's own localStorage cap, so a browser that clears its cache still
    # finds its conversation in the store of record.

    TRANSCRIPT_LIMIT = 200
    MESSAGE_MAX_CHARS = 64_000
    _MESSAGE_ROLES = ("user", "assistant", "system", "tool")

    def record_messages(
        self, session_id: str | None, messages: list[dict] | None, *,
        task: str | None = None, deploy_id: str | None = None,
        ts: str | None = None, meta: dict | None = None,
    ) -> int:
        """Append a turn's messages to a session transcript. Returns rows added.

        `messages` is the OpenAI-shaped list (`{role, content}`). Only the roles
        that carry a conversation are kept and blank content is dropped, because
        a tool-call turn legitimately has no text and storing it would make the
        transcript unreadable. A message is truncated to `MESSAGE_MAX_CHARS`: one
        runaway paste must not become the whole table.
        """
        if not session_id:
            return 0
        from .fetch import utcnow

        stamp = ts or utcnow()
        rows: list[tuple] = []
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "").strip()
            content = m.get("content")
            if role not in self._MESSAGE_ROLES:
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            rows.append((session_id, role, content[: self.MESSAGE_MAX_CHARS],
                         task, deploy_id, stamp, json.dumps(meta or {})))
        if not rows:
            return 0
        self.conn.executemany(
            """INSERT INTO messages (session_id, role, content, task, deploy_id, ts, meta)
               VALUES (?,?,?,?,?,?,?)""",
            rows,
        )
        self._prune_messages(session_id)
        return len(rows)

    def _prune_messages(self, session_id: str) -> None:
        """Keep the newest `TRANSCRIPT_LIMIT` messages for a session."""
        self.conn.execute(
            "DELETE FROM messages WHERE session_id=? AND id NOT IN"
            " (SELECT id FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?)",
            (session_id, session_id, self.TRANSCRIPT_LIMIT),
        )

    def session_messages(self, session_id: str,
                         limit: int | None = None) -> list[dict[str, Any]]:
        """The newest `limit` messages, oldest-first, for replay."""
        limit = max(1, min(int(limit or self.TRANSCRIPT_LIMIT), self.TRANSCRIPT_LIMIT))
        rows = self.conn.execute(
            """SELECT id, role, content, task, deploy_id, ts, meta FROM messages
               WHERE session_id=? ORDER BY id DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        out = []
        for r in reversed(rows):
            m = dict(r)
            try:
                m["meta"] = json.loads(m.get("meta") or "{}")
            except (ValueError, TypeError):
                m["meta"] = {}
            out.append(m)
        return out

    def clear_messages(self, session_id: str) -> int:
        """Forget a session's transcript. Returns the number of rows removed."""
        return self.conn.execute(
            "DELETE FROM messages WHERE session_id=?", (session_id,)).rowcount

    def quarantine_rows(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM quarantine ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("weights", "deployments", "evidence", "snapshots", "observations",
                  "quarantine", "decisions"):
            out[t] = self.conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        return out

    def commit(self) -> None:
        self.conn.commit()
