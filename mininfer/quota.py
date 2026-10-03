"""Declared quota limits, observed provider limits, and the seeding pass.

A free arm costs zero *while its bucket has headroom* and the price of the
next-best option once it does not (PLAN.md §2). That only works if the router
knows how big the bucket is. This module has two sources for that number, and
they are not interchangeable:

* **configured** — `config/quotas.yaml`, a claim from provider docs, seeded into
  `quota_buckets` with `source: configured`;
* **observed** — what the provider said in a response header
  (`x-ratelimit-remaining-requests: 0`), which is a fact about right now.

Neither alone is enough. A policy limit of 1000/day cannot consume a provider's
observed 50/day free tier; a provider's observed 20/min demotes an arm the policy
would have let run. So the effective headroom is the **minimum of the two**, and
`describe` reports which side set it.

Everything here is a pure function of headers except `seed`/`describe`, which
touch the store: the parsers are the part that has to be right, and they are
testable without a network or a database.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import pathlib
import re
from dataclasses import dataclass, replace

import yaml

from .store import Store, effective_headroom

DEFAULT_CONFIG = pathlib.Path("config/quotas.yaml")

#: How long each window lasts. Used to reconstruct when the current window opened
#: (from `reset_at`) so a usage *rate* can be estimated.
WINDOW_SECONDS = {"minute": 60, "day": 86_400, "month": 30 * 86_400}

#: The 429 reasons. `unknown` is a real answer, not a failure to answer: a 429 is
#: not proof of exhaustion, and pretending to know which limit was hit teaches the
#: operator to trust a guess.
RATE_LIMIT_REASONS = ("rpm", "rpd", "tpm", "concurrency", "account", "unknown")


def load_quotas(path: str | pathlib.Path = DEFAULT_CONFIG) -> list[dict]:
    raw = yaml.safe_load(pathlib.Path(path).read_text()) or {}
    out = []
    for e in raw.get("quotas") or []:
        if not e.get("limit") or not e.get("window"):
            continue
        out.append(dict(e))
    return out


def _targets(store: Store, entry: dict) -> list[str]:
    """Expand an entry to deploy_ids: an explicit id, or every deployment of a
    provider (optionally only the free ones)."""
    if entry.get("deploy_id"):
        return [entry["deploy_id"]]
    provider = entry.get("provider")
    if not provider:
        return []
    return store.deploy_ids_for(provider, free_only=bool(entry.get("free_only")))


def seed(store: Store, entries: list[dict] | None = None, *,
         config: str | pathlib.Path = DEFAULT_CONFIG, dry_run: bool = False) -> dict:
    entries = entries if entries is not None else load_quotas(config)
    buckets = 0
    matched: dict[str, int] = {}
    for e in entries:
        targets = _targets(store, e)
        if not targets:
            continue
        matched[e.get("provider") or e.get("deploy_id")] = \
            matched.get(e.get("provider") or e.get("deploy_id"), 0) + len(targets)
        if dry_run:
            continue
        for did in targets:
            store.set_limit(did, e["window"], int(e["limit"]),
                            api_key_alias=e.get("api_key_alias", "default"),
                            source="configured")
            buckets += 1
    if not dry_run:
        store.commit()
    return {"entries": len(entries), "buckets": buckets, "matched": matched}


# --------------------------------------------------------------------------- #
# Observed rate limits (P5)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class RateLimit:
    """One provider-reported limit, normalized out of whatever header family it
    arrived in. `metric` is the axis (`requests` or `tokens`), because a `429` on
    a token limit and a `429` on a request limit call for different fixes."""

    metric: str                    # "requests" | "tokens"
    limit_n: int | None = None
    remaining_n: int | None = None
    reset_s: float | None = None   # seconds until the window rolls over
    retry_after_s: float | None = None
    source: str = ""               # the header family, for debugging a parser miss


#: Header families, most specific first. A provider that sends both
#: `x-ratelimit-limit-requests` and a bare `x-ratelimit-limit` is reported once,
#: by the first family that answers, so a metric never gets two votes.
_HEADER_FAMILIES: tuple[tuple[str, str, str, str], ...] = (
    ("requests", "x-ratelimit-limit-requests",
     "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests"),
    ("tokens", "x-ratelimit-limit-tokens",
     "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"),
    ("requests", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"),
    ("requests", "anthropic-ratelimit-requests-limit",
     "anthropic-ratelimit-requests-remaining", "anthropic-ratelimit-requests-reset"),
    ("tokens", "anthropic-ratelimit-tokens-limit",
     "anthropic-ratelimit-tokens-remaining", "anthropic-ratelimit-tokens-reset"),
)

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _lower(headers) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def duration_s(value, *, now: dt.datetime | None = None) -> float | None:
    """Seconds from a header value.

    Providers spell this three ways and all three are in the wild: plain seconds
    (`45`, `0.5`), a compound duration (`2m59.56s`, which is Groq's), and an HTTP
    date (`retry-after`). Anything else is `None` — unknown, not zero.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    parts = _DURATION_RE.findall(text.lower())
    if parts:
        return sum(float(n) * _UNIT_S[u] for n, u in parts)
    # `retry-after` may be an HTTP date instead of a delta.
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    return max(0.0, (when - now).total_seconds())


def window_for_reset(reset_s: float | None) -> str:
    """Which bucket a reported limit belongs to, inferred from its reset.

    An *inference*, not a fact: rate-limit headers rarely name their window. The
    thresholds are wide (two minutes, then a day) so only a genuinely unusual
    reset lands in the wrong bucket, and the row records that it was observed
    rather than configured so nobody reads it as documentation.
    """
    if reset_s is None:
        return "minute"
    if reset_s <= 120:
        return "minute"
    if reset_s <= 26 * 3600:
        return "day"
    return "month"


def parse_rate_limit_headers(headers) -> tuple[RateLimit, ...]:
    """Normalize whichever rate-limit headers a provider sent.

    Returns one entry per metric. A family that reports neither a limit nor a
    remaining count is skipped entirely, so an unrelated `retry-after` on an error
    response does not manufacture a rate limit that was never described.
    """
    h = _lower(headers)
    out: list[RateLimit] = []
    seen: set[str] = set()
    for metric, limit_key, remaining_key, reset_key in _HEADER_FAMILIES:
        if metric in seen:
            continue
        limit_n = _int(h.get(limit_key))
        remaining_n = _int(h.get(remaining_key))
        if limit_n is None and remaining_n is None:
            continue
        seen.add(metric)
        out.append(RateLimit(
            metric=metric, limit_n=limit_n, remaining_n=remaining_n,
            reset_s=duration_s(h.get(reset_key)), source=limit_key,
        ))
    retry_after = duration_s(h.get("retry-after"))
    if retry_after is not None:
        if out:
            out = [replace(o, retry_after_s=retry_after) for o in out]
        else:
            # A bare `retry-after` with no limit family still says "back off", and
            # that is the whole reason the classification below exists.
            out.append(RateLimit(metric="requests", retry_after_s=retry_after,
                                 source="retry-after"))
    return tuple(out)


def classify_429(headers, body_text: str | None = None) -> str:
    """Which limit a `429` hit, so the operator can fix the right thing.

    A `429` is *not* proof of exhaustion — it can be a request rate, a token rate,
    an account-level ceiling, concurrency, or the provider shedding load. Anything
    the evidence does not name is `unknown`, which is a usable answer; guessing
    `rpm` for all of them is how a token limit gets "fixed" by lowering RPM.
    """
    h = _lower(headers)
    text = (body_text or "").lower()
    if "concurren" in text:
        return "concurrency"
    for obs in parse_rate_limit_headers(h):
        if obs.remaining_n != 0:
            continue
        if obs.metric == "tokens":
            return "tpm"
        return "rpm" if window_for_reset(obs.reset_s) == "minute" else "rpd"
    for keyword in ("quota", "billing", "insufficient", "credit", "payment",
                    "exceeded your current"):
        if keyword in text:
            return "account"
    return "unknown"


#: How much a provider's own rate-limit header is trusted. High but not 1.0:
#: headers describe the key's view of the limit, and a proxy or gateway in front of
#: the provider can report a different one than the model serves.
OBSERVED_LIMIT_CONFIDENCE = 0.9


def record_from_headers(store: Store, deploy_id: str, headers, *,
                        observed_at: str | None = None) -> int:
    """Parse a response's rate-limit headers and persist them. Returns the count.

    The window is *inferred* from the reported reset (`window_for_reset`) because
    headers rarely name it, and the reset is turned into an absolute time here so
    the projection has something to measure rate against.
    """
    observations = parse_rate_limit_headers(headers)
    base = _parse_ts(observed_at) or dt.datetime.now(dt.timezone.utc)
    for obs in observations:
        reset_at = None
        if obs.reset_s is not None:
            reset_at = (base + dt.timedelta(seconds=obs.reset_s)).isoformat(timespec="seconds")
        store.record_rate_limit(
            deploy_id,
            window=window_for_reset(obs.reset_s),
            limit_n=obs.limit_n,
            remaining_n=obs.remaining_n,
            reset_at=reset_at,
            observed_at=observed_at,
            confidence=OBSERVED_LIMIT_CONFIDENCE,
        )
    return len(observations)


# --------------------------------------------------------------------------- #
# The operator's view
# --------------------------------------------------------------------------- #

def _ratio(part: float, whole: float | None) -> float | None:
    if not whole:
        return None
    return max(0.0, min(1.0, part / whole))


def bucket_headroom(row) -> tuple[float | None, str]:
    """The *effective* headroom for one bucket, and which side set it.

    Delegates to `store.effective_headroom` so what an operator reads here and
    what the router ranks on cannot disagree about whether an arm is running out.
    """
    return effective_headroom(
        limit_n=row["limit_n"], used_n=row["used_n"],
        observed_limit_n=row["observed_limit_n"],
        observed_remaining_n=row["observed_remaining_n"])


def _parse_ts(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        ts = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def project_exhaustion(row, *, now: dt.datetime | None = None) -> dict:
    """A *probabilistic* exhaustion estimate, never a bare timestamp.

    Usage is stochastic, so the estimate is returned with the basis it came from
    and a confidence that rises with the number of observations. Someone will
    eventually ask why a bucket "lasted until 16:32" but emptied at 15:51, and the
    only honest answer is that it was an estimate with a basis.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    window = row["window"]
    limit = row["observed_limit_n"] or row["limit_n"]
    remaining = row["observed_remaining_n"]
    if remaining is None:
        remaining = None if limit is None else max(0, limit - (row["used_n"] or 0))
    used_n = row["used_n"] or 0
    reset_at = row["observed_reset_at"] or row["reset_at"]
    basis = {"window": window, "limit": limit, "remaining": remaining,
             "used": used_n, "reset_at": reset_at}

    duration = WINDOW_SECONDS.get(window)
    reset_dt = _parse_ts(reset_at)
    if not limit or remaining is None or duration is None or reset_dt is None:
        return {"estimated_exhaustion_at": None, "confidence": 0.0, "basis": basis}

    start = reset_dt - dt.timedelta(seconds=duration)
    elapsed = max(0.0, min(float(duration), (now - start).total_seconds()))
    basis["elapsed_s"] = elapsed
    # Confidence is *samples*, not elapsed time: a rate estimated from two calls is
    # a coincidence, and saying so is the point of returning it.
    confidence = min(1.0, used_n / 10.0)
    if remaining <= 0:
        return {"estimated_exhaustion_at": now.isoformat(timespec="seconds"),
                "confidence": confidence, "basis": basis}
    if elapsed <= 0 or used_n <= 0:
        return {"estimated_exhaustion_at": None, "confidence": confidence, "basis": basis}

    rate = used_n / elapsed
    basis["observed_rate_per_s"] = rate
    eta = now + dt.timedelta(seconds=remaining / rate)
    if eta >= reset_dt:
        # It refills before it would empty, so there is no exhaustion to predict.
        return {"estimated_exhaustion_at": None, "confidence": confidence, "basis": basis}
    basis["eta_s"] = (eta - now).total_seconds()
    return {"estimated_exhaustion_at": eta.isoformat(timespec="seconds"),
            "confidence": confidence, "basis": basis}


def describe(store: Store, limit: int = 40) -> list[dict]:
    """Current bucket state, tightest effective headroom first.

    Over-fetches before sorting: `quota_rows` orders by an indexable
    approximation (observed ratio when there is one, else configured), and the
    real order is by whichever of the two is tighter across the row.
    """
    store.reset_expired()
    out = []
    for r in store.quota_rows(max(1, limit) * 5):
        headroom, source = bucket_headroom(r)
        out.append({**r, "headroom": headroom, "headroom_source": source,
                    "exhaustion": project_exhaustion(r)})
    # Unknown headroom sorts last: it is not "plenty of room", but it is also not
    # the tightest bucket, and putting it first would bury a real near-empty one.
    out.sort(key=lambda r: (r["headroom"] is None, r["headroom"] or 0.0))
    return out[:limit]
