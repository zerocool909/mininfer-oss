#!/usr/bin/env python
"""Known-answer sweep: run every callable chat arm through a curated dataset.

Why a separate script from `bench.py`: `bench` asks the *router* to pick an arm.
This asks a *specific arm* to answer a *fixed, deterministically gradable*
question, which is the only way to compare arms to each other rather than to
the router's own policy.

Discovery is read-only over the registry: every deployment whose provider is
configured in the running environment, that is not a special role (safety,
embedding, rerank, ASR, TTS, image, music) and does not produce non-text
output. Duplicates of the same base weights behind several gateways collapse
to the shortest deploy id (the cheapest path to the same weights).

Results are appended to `runs.jsonl` one row at a time, so a long sweep is
resumable and an interrupted run keeps everything it already paid for.

    python scripts/model_sweep.py --tier free
    python scripts/model_sweep.py --tier all --budget-usd 15 --concurrency 8
    python scripts/model_sweep.py --resume            # same outdir, skips done pairs

Grading is deterministic (no judge call, no extra spend). Every check names the
exact evidence it accepts; see `grade()`.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
import uuid

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mininfer.env import load_env  # noqa: E402
from mininfer.router import output_role, special_role  # noqa: E402

load_env(ROOT / ".env")

DEFAULT_DB = str(ROOT / "mininfer.db")
DEFAULT_DATASET = str(ROOT / "scripts" / "eval_dataset.json")
OUTROOT = ROOT / "reports" / "model-sweep"

# OpenRouter exposes a handful of non-chat utility ids in the same catalogue.
# They are not answerers, so including them would only record noise.
NOT_A_CHAT_MODEL = {"openrouter/bodybuilder"}

# Providers whose key the harness will accept as "callable". `openrouter/*`
# sub-gateway ids resolve through the one OpenRouter key.
KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
}


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def configured_gateways() -> list[str]:
    # `.env` is loaded at import; the running proxy may also have been started
    # with MI_* overrides, in which case the process env wins for those keys.
    return [p for p, env in KEY_ENV.items() if os.environ.get(env)]


def discover(db: str) -> list[dict]:
    gateways = configured_gateways()
    if not gateways:
        raise SystemExit("no provider key in the environment (OPENROUTER_API_KEY / GROQ_API_KEY)")
    con = sqlite3.connect(f"file:{pathlib.Path(db).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select d.deploy_id, d.provider, d.provider_model_id, d.pricing_state,
                  d.price_in, d.price_out, d.caps, d.context_window,
                  w.output_modalities
           from deployments d
           left join weights_resolved w on w.weights_id = d.weights_id
           where d.status = 'live'"""
    ).fetchall()
    con.close()

    seen: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        provider = d["provider"]
        if not any(provider == g or provider.startswith(g + "/") for g in gateways):
            continue
        role = special_role(d)
        outs = set(json.loads(d.get("output_modalities") or "[]"))
        produced = output_role(outs)
        if role or (produced and produced != "text"):
            continue
        base = d["provider_model_id"].split(":batch")[0]
        if base in NOT_A_CHAT_MODEL:
            continue
        d["base"] = base
        # The same weights can be reached through several gateways at different
        # prices. Prefer the zero-marginal path first (a free/trial deployment is
        # a different *deployment*, not a duplicate: `groq:qwen3.6-27b` is trial
        # while `openrouter:qwen3.6-27b` is paid). Only then prefer the shortest
        # deploy id, which picks `openrouter:x` over `openrouter/novita:x`.
        FREE_STATES = ("free_with_quota", "free", "trial")
        def rank(dd):
            return (0 if dd["pricing_state"] in FREE_STATES else 1,
                    dd["deploy_id"].count("/"), dd["deploy_id"])
        if base not in seen or rank(d) < rank(seen[base]):
            seen[base] = d
    out = list(seen.values())
    # Free first, then cheap, so a budget cut still leaves a meaningful sweep.
    def key(d):
        tier = 0 if d["pricing_state"] in ("free_with_quota", "free", "trial") else 1
        price = (d["price_in"] or 0) + (d["price_out"] or 0)
        return (tier, price, d["deploy_id"])
    out.sort(key=key)
    return out


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n\s*```\s*$", re.S)
_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def strip_fences(text: str) -> str:
    text = (text or "").strip()
    m = _FENCE.match(text)
    if m:
        return m.group(1).strip()
    # an unterminated fence (truncated answer) still carries the code
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
    start = text.find(opener)
    end = text.rfind(closer)
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _run_python(code: str, fn: str) -> tuple[bool, str]:
    code = strip_fences(code)
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"syntax_error: {e.msg}"
    if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn
               for n in ast.walk(tree)):
        return False, f"no function {fn}()"
    cases = [[2, 3, 5], [-1, 1, 0], [0, 0, 0]]
    harness = (
        "import json,sys\n"
        "src=sys.stdin.read()\n"
        "ns={}\n"
        "exec(compile(src,'<model>','exec'),ns)\n"
        f"fn=ns.get('{fn}')\n"
        "if fn is None: print(json.dumps([False,'missing']))\n"
        "else:\n"
        "    out=[]\n"
        "    for a,b,exp in json.loads(sys.argv[1]):\n"
        "        try: out.append([fn(a,b)==exp, repr(fn(a,b))])\n"
        "        except Exception as e: out.append([False, type(e).__name__])\n"
        "    print(json.dumps(out))\n"
    )
    try:
        p = subprocess.run([sys.executable, "-c", harness, json.dumps(cases)],
                           input=code, capture_output=True, text=True, timeout=6)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    try:
        res = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return False, (p.stderr or "no output").strip()[:200]
    bad = [r for r in res if not r[0]]
    return (not bad), ("" if not bad else f"failed: {bad}")


def grade(item: dict, content: str) -> dict:
    check = item["check"]
    kind = check["type"]
    text = content or ""
    norm = _norm(text)
    ok = False
    detail = ""

    if kind == "exact":
        ok = norm.strip(" .\"'") == _norm(check["value"]).strip(" .\"'")
        detail = f"got {norm[:60]!r}"
    elif kind == "contains_any":
        hit = next((v for v in check["values"] if _norm(v) in norm), None)
        ok = hit is not None
        detail = f"hit {hit!r}" if ok else "none present"
    elif kind == "contains_all":
        missing = [v for v in check["values"] if _norm(v) not in norm]
        ok = not missing
        detail = f"missing {missing}" if missing else "all present"
        if ok and check.get("any_of"):
            ok = any(_norm(v) in norm for v in check["any_of"])
            detail = "any_of matched" if ok else "any_of missing"
        if ok and check.get("max_words"):
            wc = len(text.split())
            ok = wc <= check["max_words"]
            detail = f"{wc} words (max {check['max_words']})"
    elif kind == "regex_all":
        missing = [p for p in check["values"] if not re.search(p, norm)]
        ok = not missing
        detail = f"missing patterns {missing}" if missing else "all patterns matched"
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
                    bad.append(f"missing key {k}")
                elif _norm(str(want)) not in _norm(str(obj[k])):
                    bad.append(f"{k}={obj[k]!r}")
            ok = not bad
            detail = "keys ok" if ok else "; ".join(bad)
    elif kind == "json_array":
        arr = _first_json(text, "[", "]")
        want = check["value"]
        if not isinstance(arr, list):
            detail = "not a JSON array"
        else:
            try:
                ok = [float(x) for x in arr] == [float(x) for x in want]
            except (TypeError, ValueError):
                ok = arr == want
            detail = f"got {arr!r}" if not ok else "exact array"
    elif kind == "python_function":
        ok, detail = _run_python(text, check["name"])
    else:
        detail = f"unknown check {kind}"

    return {"ok": bool(ok), "detail": detail}


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------
def estimate_cost(model: dict, tin: int, tout: int) -> float | None:
    pi, po = model.get("price_in"), model.get("price_out")
    if pi is None and po is None:
        return None
    return ((pi or 0.0) * tin + (po or 0.0) * tout) / 1_000_000


async def call_one(client: httpx.AsyncClient, base: str, model: dict, item: dict,
                   timeout: float, retries: int, pacer=None) -> dict:
    body = {
        "model": model["deploy_id"],
        "messages": [{"role": "user", "content": item["prompt"]}],
        "max_tokens": item.get("max_tokens", 300),
        "temperature": 0,
        "stream": False,
    }
    headers = {"X-MI-Session": f"sweep-{uuid.uuid4().hex[:12]}"}
    t0 = time.perf_counter()
    last: dict = {}
    for attempt in range(retries + 1):
        if pacer is not None:
            await pacer()
        try:
            r = await client.post(f"{base}/v1/chat/completions", json=body,
                                  headers=headers, timeout=timeout)
            wall = (time.perf_counter() - t0) * 1000
            if r.status_code == 200:
                data = r.json()
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                content = msg.get("content") or ""
                usage = data.get("usage") or {}
                mi = data.get("mi") or {}
                tin = int(usage.get("prompt_tokens") or 0)
                tout = int(usage.get("completion_tokens") or 0)
                return {
                    "ok": True,
                    "status": 200,
                    "content": content,
                    "reasoning_present": bool(msg.get("reasoning")),
                    "finish_reason": choice.get("finish_reason"),
                    "tokens_in": tin,
                    "tokens_out": tout,
                    "cost_usd": estimate_cost(model, tin, tout),
                    "latency_ms": wall,
                    "mi_latency_ms": mi.get("latency_ms"),
                    "decision_id": mi.get("decision_id"),
                }
            body_json = {}
            try:
                body_json = r.json()
            except Exception:
                pass
            err = (body_json.get("error") or {}) if isinstance(body_json, dict) else {}
            last = {
                "ok": False,
                "status": r.status_code,
                "content": "",
                "error": f"{err.get('type') or 'http'}: {err.get('message') or r.text[:200]}",
                "error_type": err.get("type") or f"http_{r.status_code}",
                "latency_ms": wall,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
            }
            if r.status_code in (429, 502, 503, 504) and attempt < retries \
                    and any(k in last["error"].lower() for k in
                            ("rate", "429", "overload", "timeout", "unavailable")):
                await asyncio.sleep(min(30, 4 * (attempt + 1)))
                continue
            return last
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = {
                "ok": False,
                "status": 0,
                "content": "",
                "error": f"{type(e).__name__}: {e}",
                "error_type": type(e).__name__,
                "latency_ms": (time.perf_counter() - t0) * 1000,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
            }
            if attempt < retries:
                await asyncio.sleep(min(30, 4 * (attempt + 1)))
                continue
            return last
    return last


async def sweep(args) -> int:
    items = json.loads(pathlib.Path(args.dataset).read_text(encoding="utf-8"))
    models = discover(args.db)
    if args.tier == "free":
        models = [m for m in models if m["pricing_state"] in ("free_with_quota", "free", "trial")]
    elif args.tier == "paid":
        models = [m for m in models if m["pricing_state"] not in ("free_with_quota", "free", "trial")]
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        models = [m for m in models if m["deploy_id"] in wanted or m["base"] in wanted]
    if args.limit:
        models = models[: args.limit]

    outdir = pathlib.Path(args.outdir or (OUTROOT / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())))
    outdir.mkdir(parents=True, exist_ok=True)
    runs_path = outdir / "runs.jsonl"
    # Resume means "do not pay twice for a settled answer". A 429/5xx/timeout is
    # not settled: it says nothing about the model, so the pair is retried. A
    # 200 (graded either way), a 400 or a 404 is settled and is skipped.
    DEFINITIVE = {"http_400", "http_404", "http_403", "http_401",
                  "not_api_callable", "no_api_key", "unsupported"}
    done: set[tuple[str, str]] = set()
    if runs_path.exists():
        for line in runs_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ok") or r.get("skipped") or (r.get("error_type") in DEFINITIVE):
                done.add((r["model"], r["prompt_id"]))
    (outdir / "meta.json").write_text(json.dumps({
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": args.base_url,
        "tier": args.tier,
        "dataset": args.dataset,
        "models": len(models),
        "items": len(items),
        "concurrency": args.concurrency,
        "budget_usd": args.budget_usd,
        "provider_gateways": configured_gateways(),
    }, indent=2), encoding="utf-8")

    print(f"dataset={len(items)} models={len(models)} pairs={len(models)*len(items)} "
          f"already_done={len(done)} tier={args.tier}", flush=True)

    sem = asyncio.Semaphore(args.concurrency)
    # One global gate for free-tier calls. OpenRouter's free-models-per-minute
    # budget is account-wide, so per-model pacing does not help; this does.
    free_lock = asyncio.Lock()
    last_free = {"t": 0.0}

    async def free_pacer():
        async with free_lock:
            gap = args.free_interval - (time.perf_counter() - last_free["t"])
            if gap > 0:
                await asyncio.sleep(gap)
            last_free["t"] = time.perf_counter()
    fh = runs_path.open("a", encoding="utf-8")
    lock = asyncio.Lock()
    spent = 0.0
    budget_hit = False
    counters = {"ok": 0, "fail": 0, "graded": 0, "graded_ok": 0}
    started = time.perf_counter()

    async with httpx.AsyncClient() as client:
        async def worker(model: dict, item: dict):
            nonlocal spent, budget_hit
            if (model["deploy_id"], item["id"]) in done:
                return
            free = model["pricing_state"] in ("free_with_quota", "free", "trial")
            async with lock:
                if budget_hit or (not free and spent >= args.budget_usd):
                    budget_hit = True
                    row = {"model": model["deploy_id"], "base": model["base"],
                           "provider": model["provider"], "pricing_state": model["pricing_state"],
                           "prompt_id": item["id"], "family": item["family"],
                           "skipped": "budget", "ok": False, "grade_ok": False}
                    fh.write(json.dumps(row) + "\n")
                    fh.flush()
                    return
            async with sem:
                # The per-minute free budget that matters is OpenRouter's.
                # Groq's trial arms are not subject to it, so they run at full
                # concurrency.
                paced = free and model["provider"].startswith("openrouter")
                res = await call_one(client, args.base_url, model, item,
                                     args.timeout, args.retries,
                                     free_pacer if paced else None)
            g = grade(item, res.get("content", "")) if res.get("ok") else {"ok": False, "detail": ""}
            cost = res.get("cost_usd") or 0.0
            async with lock:
                spent += cost
                counters["ok" if res.get("ok") else "fail"] += 1
                counters["graded"] += 1
                counters["graded_ok"] += 1 if g["ok"] else 0
                row = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "model": model["deploy_id"],
                    "base": model["base"],
                    "provider": model["provider"],
                    "pricing_state": model["pricing_state"],
                    "price_in": model.get("price_in"),
                    "price_out": model.get("price_out"),
                    "prompt_id": item["id"],
                    "family": item["family"],
                    "task": item["task"],
                    **{k: v for k, v in res.items() if k != "content"},
                    "content": (res.get("content") or "")[:4000],
                    "grade_ok": g["ok"],
                    "grade_detail": g["detail"],
                }
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                n = counters["graded"]
                if n % 50 == 0:
                    el = time.perf_counter() - started
                    print(f"  {n} calls  ok={counters['ok']} grade_ok={counters['graded_ok']} "
                          f"spent=${spent:.3f} {n/el:.1f}/s", flush=True)

        tasks = [worker(m, it) for m in models for it in items]
        await asyncio.gather(*tasks)

    fh.close()
    if budget_hit:
        print(f"  BUDGET CAP ${args.budget_usd:.2f} reached (spent ${spent:.3f}); remaining paid pairs skipped",
              flush=True)
    print(f"done: calls={counters['graded']} ok={counters['ok']} grade_ok={counters['graded_ok']} "
          f"spent=${spent:.3f} wall={time.perf_counter()-started:.0f}s", flush=True)
    print(f"results: {runs_path}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=os.environ.get("MI_BASE_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--tier", choices=["all", "free", "paid"], default="all")
    ap.add_argument("--only", default=None, help="comma-separated deploy ids or base slugs")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--budget-usd", type=float, default=15.0)
    ap.add_argument("--free-interval", type=float, default=3.2,
                    help="seconds between free-tier calls (OpenRouter: 20/min)")
    args = ap.parse_args()
    return asyncio.run(sweep(args))


if __name__ == "__main__":
    raise SystemExit(main())
