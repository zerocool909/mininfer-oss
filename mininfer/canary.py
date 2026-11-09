"""Daily known-answer canary — prove the free arms can still *answer*.

`probe.py` answers one question: is the provider reachable and does it accept
our key (`GET /models`). That is necessary and not sufficient. A provider can
answer `/models` with 200 while the model behind it 404s, refuses to emit
content, or has quietly started charging.

This module answers the harder question with a handful of known-answer prompts,
graded deterministically. The design is shaped by one constraint: **it must not
compete with the traffic it is protecting.** A user who runs this repo all day on
free tiers has three scarce resources — no budget, no latency, and a shared
free-tier request quota — so a scheduled job that ignores them is a liability.

What that buys, concretely:

* **It cannot spend money.** Only zero-marginal arms are called, and fresh
  pricing is pulled first, so a stale "free" cannot silently become a paid call.
* **It cannot drain the quota the router needs.** It runs only when the app has
  been idle, tests a rotating slice of the prompts (two per arm by default), and
  skips any arm whose free bucket is more than half used. The first `429` stops
  the run: the remaining quota belongs to the user.
* **It cannot delay a request.** The loop runs in a worker thread; the write
  phase is a short batch *after* the calls, never a transaction held across a
  network round trip (the incident this repository already paid for once).
* **It learns the catalogue.** Before grading, it refreshes the keyed gateways
  (OpenRouter by default), then reconciles prices, so new free models appear and
  free→paid transitions are caught the same day.

Result contract: observations are written with `signal_kind='canary'`, so the
router's posteriors learn from them without pretending they were served traffic.

The cadence lives in the same `settings` table as the probe, and the loop that
drives it is the same shape: opt-out, re-read every tick, inert until due.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import time
from typing import Any, Callable

from .execute import CallResult, available_providers, call, resolve_endpoint
from .fetch import utcnow
from .router import output_role, special_role
from .store import Store

# --------------------------------------------------------------- settings keys
ENABLED_KEY = "canary.enabled"
INTERVAL_KEY = "canary.interval"
LAST_RUN_KEY = "canary.last_run"
LAST_REPORT_KEY = "canary.last_report"
HISTORY_KEY = "canary.history"

DEFAULT_INTERVAL = 86400.0      # once a day
MIN_INTERVAL = 3600.0           # an hourly canary is noise, not signal
DEFAULT_STARTUP_DELAY = 600.0   # let the first-run ingest finish first
DEFAULT_MAX_ARMS = 25
DEFAULT_BUDGET_S = 600.0
PER_CALL_TIMEOUT = 90.0
HISTORY_KEEP = 30

#: Shallow by default. Six prompts × seventeen arms is 102 requests — several
#: minutes of a free tier that the user's own traffic also needs. Two per arm,
#: rotated by date, covers the whole set every three days and costs a third.
DEFAULT_PROMPTS_PER_ARM = 2
#: Only run when nobody has used the API for this long. This is the single most
#: effective protection for a shared free tier: the canary yields the bucket.
DEFAULT_IDLE_MINUTES = 15.0
#: A catalogue older than this is not trusted to know which arms are still free.
DEFAULT_MAX_STALE_DAYS = 7.0
#: Refresh before grading. `openrouter` alone is the source of nearly every free
#: arm and is one keyless GET; other keyed gateways are added when configured.
DEFAULT_REFRESH_SOURCES = ("openrouter",)

#: Same set the router treats as "zero marginal cost". A paid arm is never
#: canaried: this job must not be able to produce a bill.
FREE_STATES = ("free_with_quota", "free", "trial")
#: States that are free by *construction* (a $0 list price or a `:free` id), as
#: opposed to a trial balance that depletes. Used when the catalogue is stale.
STRUCTURAL_FREE = ("free_with_quota", "free")

#: Errors that say something about *us* (key, network), not about the model.
#: They are recorded but excluded from the accuracy denominator, exactly as the
#: `routing_stats` view does.
INFRA_ERRORS = frozenset({
    "no_api_key", "auth_error", "network_error", "tls_error", "timeout",
    "http_402", "payment_required", "429",
})
#: A run stops on these rather than retrying: the remaining quota is the user's.
HALT_ERRORS = frozenset({"429", "auth_error", "http_402", "payment_required"})
#: Below this remaining fraction an arm is left alone for the day.
MIN_HEADROOM = 0.5


# --------------------------------------------------------------------- dataset
#: Six prompts, one per failure mode worth catching. Small on purpose: the signal
#: is "can it still answer", not "how good is it" — the offline sweep in
#: `scripts/model_sweep.py` is the wide version.
DATASET: list[dict[str, Any]] = [
    {
        "id": "instruction_exact",
        "task": "general_chat",
        "prompt": "Reply with exactly one word and nothing else: BANANA",
        "max_tokens": 64,
        "check": {"type": "exact", "value": "banana"},
    },
    {
        "id": "math_arithmetic",
        "task": "hard_reasoning",
        "prompt": "What is 17 * 23? Reply with the number only.",
        "max_tokens": 256,
        "check": {"type": "number", "value": 391},
    },
    {
        "id": "extraction_json",
        "task": "extraction",
        "prompt": "Extract the fields into a JSON object with exactly the keys name, "
                  "date, amount. Return JSON only, no prose.\n\nText: On 12 March 1843, "
                  "Ada Lovelace paid 250 pounds for the analytical engine notes.",
        "max_tokens": 200,
        "check": {"type": "json_object",
                  "keys": {"name": "ada lovelace", "date": "1843", "amount": "250"}},
    },
    {
        "id": "summarise",
        "task": "summarise",
        "prompt": "Summarise the passage in one sentence of at most 25 words. It must "
                  "mention evaporation and condensation.\n\nPassage: The water cycle "
                  "begins when the sun heats lakes and oceans, turning liquid water into "
                  "vapour that rises into the atmosphere. As the vapour climbs it cools, "
                  "and tiny droplets form clouds. When those droplets grow heavy enough "
                  "they fall as rain or snow, returning to the ground and eventually to "
                  "the sea.",
        "max_tokens": 220,
        "check": {"type": "contains_all", "values": ["evaporation", "condensation"],
                  "max_words": 35},
    },
    {
        "id": "code_shape",
        "task": "code_edit",
        "prompt": "Write a Python function add(a, b) that returns a + b. Return code only.",
        "max_tokens": 300,
        "check": {"type": "python_shape", "name": "add", "arity": 2},
    },
    {
        "id": "long_context_needle",
        "task": "general_chat",
        "prompt": "Read the text and answer the question with the number only.\n\nText: "
                  "The museum opened in 1892. The east wing was renovated in 1954 and "
                  "again in 1978. The curator's office moved twice. In 2003 a new archive "
                  "was built. The vault code is 7734 and it changes every quarter. "
                  "Visitor numbers peaked in 2011. The cafe closed in 2019.\n\n"
                  "Question: What is the vault code?",
        "max_tokens": 160,
        "check": {"type": "number", "value": 7734},
    },
]


# ---------------------------------------------------------------------- grading
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n\s*```\s*$", re.S)
_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def strip_fences(text: str) -> str:
    text = (text or "").strip()
    m = _FENCE.match(text)
    if m:
        return m.group(1).strip()
    if text.startswith("```"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        return body.rsplit("```", 1)[0].strip()
    return text


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.findall(text or ""):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _first_json(text: str, opener: str, closer: str):
    text = strip_fences(text)
    start, end = text.find(opener), text.rfind(closer)
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _python_shape(code: str, name: str, arity: int) -> tuple[bool, str]:
    """Parse, don't execute. A canary must never run model-written code."""
    try:
        tree = ast.parse(strip_fences(code))
    except SyntaxError as exc:
        return False, f"syntax_error: {exc.msg}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            args = [a for a in node.args.args]
            if len(args) != arity:
                return False, f"{name} takes {len(args)} args"
            if not any(isinstance(n, ast.Return) for n in ast.walk(node)):
                return False, f"{name} has no return"
            return True, ""
    return False, f"no function {name}()"


def grade(item: dict, content: str) -> dict:
    """Deterministic grade. Returns `{"ok": bool, "detail": str}`."""
    check = item["check"]
    kind = check["type"]
    text = content or ""
    norm = _norm(text)
    ok, detail = False, ""

    if kind == "exact":
        ok = norm.strip(" .\"'") == _norm(check["value"]).strip(" .\"'")
        detail = f"got {norm[:60]!r}"
    elif kind == "number":
        want = float(check["value"])
        got = _numbers(text)
        hit = next((n for n in got if abs(n - want) < 1e-6), None)
        ok = hit is not None
        detail = f"got {got[:5]}" if not ok else f"got {hit:g}"
    elif kind == "json_object":
        obj = _first_json(text, "{", "}")
        if not isinstance(obj, dict):
            detail = "not a JSON object"
        else:
            bad = []
            for k, want in check["keys"].items():
                if k not in obj:
                    bad.append(f"missing {k}")
                elif _norm(str(want)) not in _norm(str(obj[k])):
                    bad.append(f"{k}={obj[k]!r}")
            ok = not bad
            detail = "keys ok" if ok else "; ".join(bad)
    elif kind == "contains_all":
        missing = [v for v in check["values"] if _norm(v) not in norm]
        ok = not missing
        detail = f"missing {missing}" if missing else "all present"
        if ok and check.get("max_words"):
            wc = len(text.split())
            ok = wc <= check["max_words"]
            detail = f"{wc} words (max {check['max_words']})"
    elif kind == "python_shape":
        ok, detail = _python_shape(text, check["name"], check.get("arity", 2))
    else:
        detail = f"unknown check {kind}"
    return {"ok": bool(ok), "detail": detail}


# ------------------------------------------------------------------- selection
def free_arms(store: Store, *, max_arms: int | None = None,
              available: set[str] | None = None,
              tiers: tuple[str, ...] = FREE_STATES) -> list[dict]:
    """Callable, zero-marginal, general-purpose text arms.

    Callability is the same predicate the proxy uses (`available_providers`), so
    the canary cannot test an arm the router would refuse to dial — a green
    canary for an unreachable arm is worse than no canary.
    """
    available = available_providers() if available is None else available
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for d in store.deployments():
        if d.get("status") not in ("live", "degraded"):
            continue
        if d.get("pricing_state") not in tiers:
            continue
        head = (d.get("deploy_id") or "").partition(":")[0].split("/")[0]
        if head not in available:
            continue
        outs = set(json.loads(d.get("output_modalities") or "[]"))
        produced = output_role(outs)
        if special_role(d) or (produced and produced != "text"):
            continue
        base = (d.get("provider_model_id") or "").split(":batch")[0]
        if base in seen:
            # Same weights through two paths: prefer the shorter id, then the
            # alphabetically first, so a run is deterministic.
            if (d["deploy_id"].count("/"), d["deploy_id"]) >= \
                    (seen[base]["deploy_id"].count("/"), seen[base]["deploy_id"]):
                continue
        seen[base] = d
        out.append(d)
    out.sort(key=lambda d: d["deploy_id"])
    if max_arms:
        out = out[:max_arms]
    return out


# --------------------------------------------------------------- freshness/state
def _parse_ts(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            value = dt.datetime.fromisoformat(raw.replace(" ", "T"))
        except (TypeError, ValueError):
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def registry_age_days(store: Store, *, now: dt.datetime | None = None) -> float | None:
    """How old the catalogue is, in days. `None` means the registry is empty.

    `last_seen` is stamped by every ingest upsert, so the newest row is the last
    time *any* source refreshed. A stale catalogue is the reason a "free" arm can
    start charging without the canary noticing, so the caller uses this to decide
    whether trial arms may be trusted.
    """
    stamp = _parse_ts(store.latest_deployment_seen())
    if stamp is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return round((now - stamp).total_seconds() / 86400.0, 2)


def is_idle(store: Store, minutes: float, *, now: dt.datetime | None = None) -> bool:
    """True when no *served* traffic has touched the registry recently.

    Canary and shadow-trial observations are excluded: they are background work,
    and counting them would let the canary keep itself awake forever.
    """
    if minutes <= 0:
        return True
    last = _parse_ts(store.latest_served_observation_ts())
    if last is None:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - last).total_seconds() >= minutes * 60.0


# --------------------------------------------------------------- catalogue refresh
def refresh_catalogues(store: Store, sources: tuple[str, ...] = DEFAULT_REFRESH_SOURCES,
                       *, force: bool = True) -> dict:
    """Refresh the keyed gateways, then re-derive prices. Best-effort.

    This is what makes the canary cover *new and changed* free models rather than
    a frozen snapshot. `openrouter` is one keyless `GET /models`; `with_endpoints`
    is off because the per-endpoint fan-out is dozens of requests and adds no new
    free arm. A source that fails is reported, never fatal: a stale catalogue is
    strictly better than no canary.
    """
    from . import ingest as ing  # lazy: the adapters are heavy at import time

    out: dict[str, Any] = {"sources": list(sources), "ok": [], "failed": []}
    for name in sources:
        spec = ing.SOURCES.get(name)
        if spec is None or not spec.available:
            out["failed"].append(f"{name}: unavailable")
            continue
        try:
            kw: dict = {"with_endpoints": False} if name == "openrouter" else {}
            run = ing.run_source(name, force=force, **kw)
            for b in run.bundles:
                store.upsert_weights(b.weights, b.evidence)
                store.upsert_deployment(b.deployment, b.evidence, run.quarantine,
                                        prices=b.prices)
            for snap in run.snapshots:
                store.record_snapshot(snap)
            # Commit per source: the fetch already happened, so this transaction
            # only covers local writes and is never held across a network call.
            store.commit()
            out["ok"].append(name)
        except Exception as exc:  # a bad adapter must not kill the canary
            try:
                store.conn.rollback()
            except Exception:
                pass
            out["failed"].append(f"{name}: {type(exc).__name__}: {exc}")
    try:
        rep = store.reconcile_prices()
        store.commit()
        out["pricing_states"] = rep.get("pricing_states", {}).get("states", {})
    except Exception as exc:
        try:
            store.conn.rollback()
        except Exception:
            pass
        out["failed"].append(f"reconcile: {type(exc).__name__}")
    return out


# ------------------------------------------------------------------- execution
Caller = Callable[[str, str, int, float], CallResult]
Refresher = Callable[[Store], dict]


def _default_caller(deploy_id: str, prompt: str, max_tokens: int,
                    timeout: float) -> CallResult:
    endpoint = resolve_endpoint(deploy_id)
    return call(endpoint, [{"role": "user", "content": prompt}],
                deploy_id=deploy_id, timeout=timeout, max_tokens=max_tokens)


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[len(s) // 2], 1)


def _rotate(items: list[dict], count: int, on: dt.datetime) -> list[dict]:
    """A stable slice of the dataset for this date, so all items get covered.

    Two prompts a day from six is a full cycle every three days; the slice moves
    with the date instead of always testing the same two.
    """
    if count <= 0 or count >= len(items):
        return list(items)
    # Step by `count`, not 1: 2 prompts/day over 6 items then covers all six in
    # three days. Stepping by one overlaps and needs six days.
    offset = (on.date().toordinal() * count) % len(items)
    return [items[(offset + i) % len(items)] for i in range(count)]


def run_canary(store: Store, *, caller: Caller | None = None,
               max_arms: int = DEFAULT_MAX_ARMS,
               prompts_per_arm: int = DEFAULT_PROMPTS_PER_ARM,
               budget_s: float = DEFAULT_BUDGET_S,
               timeout: float = PER_CALL_TIMEOUT,
               now: str | None = None,
               available: set[str] | None = None,
               refresh: bool = False,
               refresh_fn: Refresher | None = None,
               max_stale_days: float = DEFAULT_MAX_STALE_DAYS,
               allow_trial_when_stale: bool = False) -> dict:
    """Call free arms with a rotating slice of the dataset, grade, record, report.

    Never raises for a provider-side failure; the only exceptions that can escape
    are programmer errors, and the caller (`_canary_loop`) swallows those too.
    """
    caller = caller or _default_caller
    # Roll expired quota windows over once, committed, before reading headroom.
    # The private per-arm variant leaves its UPDATE in the caller's transaction,
    # and this method must not carry a write transaction into a provider call.
    try:
        store.reset_expired()
        store.commit()
    except Exception:
        pass
    started = time.perf_counter()
    run_at = now or utcnow()
    run_dt = _parse_ts(run_at) or dt.datetime.now(dt.timezone.utc)

    refreshed = None
    if refresh:
        try:
            refreshed = (refresh_fn or refresh_catalogues)(store)
        except Exception as exc:  # refresh is a bonus, never a prerequisite
            refreshed = {"ok": [], "failed": [f"refresh: {type(exc).__name__}"]}

    age = registry_age_days(store, now=run_dt)
    stale = age is not None and age > max_stale_days
    tiers = FREE_STATES if (not stale or allow_trial_when_stale) else STRUCTURAL_FREE
    items = _rotate(DATASET, prompts_per_arm, run_dt)

    arms = free_arms(store, max_arms=max_arms, available=available, tiers=tiers)
    if not arms:
        report = {
            "ran": False, "reason": "no callable free arms",
            "ran_at": run_at, "arms": 0, "prompts": len(items),
            "items": [i["id"] for i in items],
            "calls": 0, "answered": 0, "correct": 0, "unmeasured": 0,
            "accuracy_pct": 0.0, "verdict": "no_data", "models": [],
            "skipped": [], "halted": None, "refreshed": refreshed,
            "registry_age_days": age, "tiers": list(tiers),
            "duration_s": round(time.perf_counter() - started, 1),
        }
        _persist(store, report, run_at)
        return report

    per_model: dict[str, dict] = {
        d["deploy_id"]: {"model": d["deploy_id"], "provider": d.get("provider"),
                         "pricing_state": d.get("pricing_state"),
                         "calls": 0, "answered": 0, "correct": 0,
                         "unmeasured": 0, "latencies": [], "errors": {},
                         "failed": []}
        for d in arms
    }
    calls = answered = correct = unmeasured = 0
    truncated = False
    # Halting is **per gateway**, not global: an exhausted OpenRouter key says
    # nothing about Groq, and a global halt would hide the healthy half of the
    # fleet exactly when the operator needs to see it.
    halted: dict[str, str] = {}
    skipped: list[dict] = []

    for arm in arms:
        did = arm["deploy_id"]
        m = per_model[did]
        head = did.partition(":")[0].split("/")[0]
        if head in halted:
            skipped.append({"model": did, "reason": "provider_halted",
                            "error": halted[head]})
            continue
        frac, src = store.headroom(did)
        if frac is not None and frac < MIN_HEADROOM:
            skipped.append({"model": did, "reason": "quota_headroom",
                            "headroom": round(frac, 3), "source": src})
            continue

        # Phase 1 — the calls. Nothing is written here, so no SQLite write
        # transaction is open while a provider is being awaited.
        results: list[dict] = []
        for item in items:
            if time.perf_counter() - started >= budget_s:
                truncated = True
                break
            calls += 1
            m["calls"] += 1
            try:
                res = caller(did, item["prompt"], item["max_tokens"], timeout)
            except Exception as exc:  # a caller bug must not abort the run
                res = CallResult(did, ok=False, error_class=type(exc).__name__,
                                 error_detail=str(exc)[:200])
            g = grade(item, res.text) if res.ok else {"ok": False, "detail": ""}
            infra = (not res.ok) and (res.error_class in INFRA_ERRORS)
            ok = bool(res.ok and g["ok"])

            if res.ok:
                answered += 1
                m["answered"] += 1
                if res.latency_ms is not None:
                    m["latencies"].append(res.latency_ms)
            elif infra:
                unmeasured += 1
                m["unmeasured"] += 1
            if ok:
                correct += 1
                m["correct"] += 1
            else:
                key = res.error_class if not res.ok else f"wrong:{item['id']}"
                m["errors"][key] = m["errors"].get(key, 0) + 1
                m["failed"].append({"prompt_id": item["id"],
                                    "detail": (res.error_detail or g["detail"] or "")[:160]})

            results.append({
                "item": item, "ok": ok, "res": res, "grade": g, "error_class": res.error_class,
            })
            if res.error_class in HALT_ERRORS:
                halted[head] = res.error_class
                break

        # Phase 2 — the writes, in one short transaction per arm.
        for row in results:
            store.observe(
                did, row["item"]["task"], row["ok"],
                ts=run_at,
                error_class=None if row["ok"] else (row["error_class"] or "bad_output"),
                latency_ms=row["res"].latency_ms,
                tokens_in=row["res"].tokens_in or None,
                tokens_out=row["res"].tokens_out or None,
                cost_usd=0.0,
                signal_kind="canary",
                signal_value=1.0 if row["ok"] else 0.0,
                meta={"prompt_id": row["item"]["id"], "canary_run": run_at,
                      "detail": row["grade"]["detail"][:200]},
            )
        store.commit()

        if truncated:
            break

    models = []
    for did, m in per_model.items():
        if m["calls"] == 0:
            continue
        measured = m["calls"] - m["unmeasured"]
        acc = _pct(m["correct"], measured)
        if m["answered"] == 0:
            status = "unreachable"
        elif acc >= 80.0:
            status = "ok"
        else:
            status = "degraded"
        models.append({
            "model": did,
            "provider": m["provider"],
            "pricing_state": m["pricing_state"],
            "status": status,
            "calls": m["calls"],
            "measured": measured,
            "answered": m["answered"],
            "correct": m["correct"],
            "accuracy_pct": acc,
            "p50_ms": _median(m["latencies"]),
            "errors": m["errors"],
            "failed": m["failed"],
        })
    models.sort(key=lambda m: (m["status"] != "ok", -m["accuracy_pct"], m["model"]))

    measured_total = calls - unmeasured
    verdict = "ok"
    if not answered:
        verdict = "failed"
    elif any(m["status"] != "ok" for m in models):
        verdict = "degraded"

    report = {
        "ran": True,
        "ran_at": run_at,
        "verdict": verdict,
        "arms": len(models),
        "prompts": len(items),
        "items": [i["id"] for i in items],
        "calls": calls,
        "answered": answered,
        "correct": correct,
        "unmeasured": unmeasured,
        "accuracy_pct": _pct(correct, measured_total),
        "duration_s": round(time.perf_counter() - started, 1),
        "truncated": truncated,
        "halted": halted or None,
        "budget_s": budget_s,
        "skipped": skipped,
        "refreshed": refreshed,
        "registry_age_days": age,
        "tiers": list(tiers),
        "models": models,
    }
    _persist(store, report, run_at)
    return report


def run_canary_db(db_path, **kwargs) -> dict:
    """`run_canary` with its own `Store`, for the background loop and the API.

    The loop and the HTTP handler both run in a worker thread, where sharing the
    request path's open `Store` would be a cross-thread connection. Opening one
    per run is also what makes the job self-contained if the process is killed.
    """
    store = Store(db_path)
    try:
        return run_canary(store, **kwargs)
    finally:
        store.close()


def _persist(store: Store, report: dict, run_at: str) -> None:
    """Last report for the portal, a bounded history for a trend line."""
    try:
        history = json.loads(store.get_setting(HISTORY_KEY) or "[]")
    except json.JSONDecodeError:
        history = []
    history.append({
        "ran_at": run_at,
        "verdict": report.get("verdict"),
        "arms": report.get("arms", 0),
        "calls": report.get("calls", 0),
        "correct": report.get("correct", 0),
        "accuracy_pct": report.get("accuracy_pct", 0.0),
        "halted": report.get("halted"),
        "registry_age_days": report.get("registry_age_days"),
    })
    store.set_setting(HISTORY_KEY, json.dumps(history[-HISTORY_KEEP:]))
    store.set_setting(LAST_REPORT_KEY, json.dumps(report)[:200_000])
    store.set_setting(LAST_RUN_KEY, run_at)
    store.commit()


# ------------------------------------------------------------------- scheduling
def _as_float(raw: str | None, default: float) -> float:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def config(store: Store) -> dict:
    """Current settings.

    Precedence: an explicit `MI_CANARY` env wins, then the portal's setting,
    then the default — **on when at least one provider key is configured**, so
    a user who only runs this repo gets the daily check without configuring
    anything. `MI_CANARY=0` is the documented way out.
    """
    env = os.environ.get("MI_CANARY")
    setting = store.get_setting(ENABLED_KEY)
    if env is not None and env.strip() != "":
        enabled = _truthy(env)
    elif setting is not None:
        enabled = _truthy(setting)
    else:
        enabled = bool(available_providers())

    interval = _as_float(store.get_setting(INTERVAL_KEY), DEFAULT_INTERVAL)
    env_interval = _as_float(os.environ.get("MI_CANARY_INTERVAL"), 0.0)
    if env_interval > 0:
        interval = max(MIN_INTERVAL, env_interval)

    refresh_env = os.environ.get("MI_CANARY_REFRESH")
    return {
        "enabled": enabled,
        "interval_seconds": interval,
        "startup_delay": _as_float(os.environ.get("MI_CANARY_DELAY"),
                                   DEFAULT_STARTUP_DELAY),
        "idle_minutes": _as_float(os.environ.get("MI_CANARY_IDLE_MINUTES"),
                                  DEFAULT_IDLE_MINUTES),
        "prompts_per_arm": int(_as_float(os.environ.get("MI_CANARY_PROMPTS_PER_ARM"),
                                         DEFAULT_PROMPTS_PER_ARM)),
        "refresh": True if refresh_env is None else _truthy(refresh_env),
        "max_stale_days": _as_float(os.environ.get("MI_CANARY_MAX_STALE_DAYS"),
                                    DEFAULT_MAX_STALE_DAYS),
        "last_run": store.get_setting(LAST_RUN_KEY),
        "source": "env" if env else ("setting" if setting is not None else "default"),
    }


def is_due(cfg: dict, *, now: str | None = None) -> bool:
    """Whether a run is due. A missing/unparseable `last_run` counts as due."""
    last = cfg.get("last_run")
    if not last:
        return True
    try:
        prev = dt.datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    now_dt = dt.datetime.fromisoformat(now) if now else dt.datetime.now(dt.timezone.utc)
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=dt.timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=dt.timezone.utc)
    return (now_dt - prev).total_seconds() >= float(
        cfg.get("interval_seconds") or DEFAULT_INTERVAL)


def last_report(store: Store) -> dict | None:
    raw = store.get_setting(LAST_REPORT_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def history(store: Store) -> list[dict]:
    try:
        return json.loads(store.get_setting(HISTORY_KEY) or "[]")
    except json.JSONDecodeError:
        return []
