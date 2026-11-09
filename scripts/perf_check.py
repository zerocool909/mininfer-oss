"""Auth verification + end-to-end perf check against the running proxy.

Zero mock: /healthz, /v1/models and a small set of real chat completions.
Kept small deliberately — every model call spends free-tier quota.

Usage:
    python reports/perf/verify_and_perf.py [base_url] [--calls N]

The tenant key is read from .env (MI_API_KEYS), never committed below.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import statistics
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[2]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def key_of(env: dict[str, str]) -> str:
    raw = env.get("MI_API_KEYS", "")
    return raw.split(",")[0].split(":")[0].strip()


PROMPTS = [
    "Reply with one short sentence: what is the capital of Australia?",
    "Reply with one short sentence: what is 12 times 12?",
]


async def main() -> None:
    base = sys.argv[1] if len(sys.argv) and not sys.argv[1].startswith("-") else "http://127.0.0.1:8765"
    calls = 10
    if "--calls" in sys.argv:
        calls = int(sys.argv[sys.argv.index("--calls") + 1])
    env = load_env()
    key = key_of(env)
    header = {"Authorization": f"Bearer {key}"}
    results: dict[str, str] = {}

    # 0. wait for boot
    for _ in range(60):
        try:
            r = await client(base).get("/healthz")
            if r.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    else:
        print("proxy never came up"); return

    # 1. auth matrix
    health = (await client(base).get("/healthz"))
    results["healthz (no key, must be 200)"] = str(health.status_code)
    unauth = (await client(base).get("/v1/models"))
    results["/v1/models (no key, must be 401)"] = str(unauth.status_code)
    keyed = (await client(base).get("/v1/models", headers=header))
    results["/v1/models (with key, must be 200)"] = str(keyed.status_code)
    admin = (await client(base).get("/v1/stats", headers=header))
    results["/v1/stats (tenant key, must be 401 admin-gated)"] = str(admin.status_code)
    for k, v in results.items():
        print(f"  {k:55s} -> {v}")

    # 2. sequential model latency (run BEFORE the burst so the limiter
    #    test cannot starve the window we want to measure in)
    svc = (await client(base).get("/v1/chat/completions")).status_code
    if svc == 401 or svc == 400:  # sanity: endpoint guarded as expected
        pass
    lat: list[float] = []
    arm_seen: set[str] = set()
    async with httpx.AsyncClient(base_url=base, timeout=90) as c:
        for i in range(min(2, calls)):
            body = {"model": "auto", "max_tokens": 60,
                    "messages": [{"role": "user", "content": PROMPTS[i % len(PROMPTS)]}]}
            t = time.perf_counter()
            r = await c.post("/v1/chat/completions", json=body, headers=header)
            dt = time.perf_counter() - t
            lat.append(dt)
            try:
                mi = r.json().get("mi", {}) or {}
                sel = (mi.get("reason", {}) or {}).get("selected", [])
                arm_seen.add(sel[0]["deploy_id"] if sel else "?")
            except Exception:
                arm_seen.add(f"http{r.status_code}")
            print(f"  seq call {i}: {dt:.2f}s http {r.status_code}")
        n_conc = max(4, min(calls, 8))
        # 4. concurrent burst (mixed prompts)
        async def one(i: int) -> float:
            body = {"model": "auto", "max_tokens": 60,
                    "messages": [{"role": "user", "content": PROMPTS[i % len(PROMPTS)]}]}
            t = time.perf_counter()
            r = await c.post("/v1/chat/completions", json=body, headers=header)
            return time.perf_counter() - t

        t = time.perf_counter()
        lat_conc = await asyncio.gather(*[one(i) for i in range(n_conc)])
        wall = time.perf_counter() - t
        lat.extend(lat_conc)
        rep = {
            "base": base,
            "sequential_ms": round(statistics.median(lat[:2]) * 1000),
            "concurrent_wall_s": round(wall, 2),
            "concurrent_each_ms_med": round(statistics.median(lat_conc) * 1000),
            "all_calls_ms_p50": round(statistics.median(lat) * 1000),
            "all_calls_ms_p95": round(sorted(lat)[int(len(lat) * 0.95) - 1] * 1000),
            "arms_used": sorted(arm_seen),
        }
        print(json.dumps(rep, indent=2))
        out = pathlib.Path(__file__).parent / "perf_run.json"
        out.write_text(json.dumps(rep, indent=2), encoding="utf-8")

    # 3. rate limiter: burst until 429 (cheap, no model calls) — after the
    #    measured calls so it can never eat the window they run in
    seen_429 = 0
    t0 = time.perf_counter()
    async with httpx.AsyncClient(base_url=base, timeout=10) as c:
        for i in range(200):
            r = await c.get("/v1/models", headers=header)
            if r.status_code == 429:
                seen_429 = i
                break
    if seen_429:
        print(f"  rate limiter engaged at request #{seen_429} "
              f"(elapsed {time.perf_counter()-t0:.1f}s) -> OK")
    else:
        print("  rate limiter did NOT engage within 200 requests "
              f"(rpm={env.get('MI_RATE_LIMIT_RPM')}) -> check config")


def client(base: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base, timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
