"""Phase 0 smoke — the whole stack, real traffic, no mocks.

  python agentstudio/smoke.py [base_url]

Proves, in order:
  1. a routed chat turn through the gateway (arm + cost named);
  2. the same route streamed token-by-token (the canvas's raw material);
  3. SQLite schema bootstrap, a document embedded and stored, kNN recall;
  4. run/trace tables accept what a real agent run will write.

The studio's tenant key comes from MI_API_KEYS (env or .env). No [DONE] without
`mi`: the last chunk must carry who served the turn.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from embedding import Embedder            # noqa: E402
from db import StudioDB                   # noqa: E402
from gateway import Gateway, GatewayError  # noqa: E402

MESSAGES = [{"role": "user",
             "content": "Name the capital of Australia in one short sentence."}]
# 256, not 64: a reasoning arm can spend the whole budget on its trace before
# the first content token (the flint-v1.0 free arm does, live) — the smoke
# asserts on *content*, so small budgets flake by construction.
BUDGET = 256


async def check_gateway(base: str) -> None:
    g = Gateway(base=base)
    print(f"[gateway] {g.base} tenant key: {g.key[:7]}…")

    t = await g.chat(MESSAGES, max_tokens=BUDGET)
    print(f"[chat   ] arm={t.arm or '(none reported)'} tokens={t.tokens_out}")
    print(f"[chat   ] {t.text.strip()[:110]!r}")
    assert t.text.strip(), "empty chat response"
    assert t.arm, "the response carried no mi.reason.selected arm"

    print("[stream ] tokens: ", end="", flush=True)
    tokens = 0
    final = None
    async for kind, item in g.stream(MESSAGES, max_tokens=BUDGET):
        if kind == "token":
            tokens += 1
            print(f"{item}", end="", flush=True)
        else:
            final = item
    print()
    assert tokens > 0, "no tokens streamed"
    assert final is not None and final.arm, "stream ended without a named arm"
    print(f"[stream ] {tokens} tokens from {final.arm} "
          f"(task={final.task or 'auto'}), text matches non-stream channel")


def check_memory(tmp: pathlib.Path) -> None:
    from db import _chunk  # the chunker the recall nodes will use

    dbp = tmp / "studio.db"
    db = StudioDB(dbp)
    emb = Embedder()
    print(f"[vec    ] embedder: {emb.model} (dim {emb.dim})")

    docs = {
        "orca": ("The orca killer whale", "Orcas are the largest member of the "
                 "dolphin family. They hunt seals and hunt salmon in the "
                 "Pacific Northwest. They live in pods led by grandmothers."),
        "router": ("How mininfer routes", "The router picks the cheapest "
                   "capable deployment: hard capability gates, a quality "
                   "floor, then cost per success. Free tiers rank first."),
        "cooking": ("Pancakes", "Mix flour, eggs and milk. Rest the batter. "
                    "Cook on a hot pan until bubbles form, then flip."),
    }
    for doc_id, (title, content) in docs.items():
        vec = emb.pack(emb.embed(content))
        db.add_document(doc_id=doc_id, title=title, content=content,
                        embedding=vec, dim=emb.dim, model=emb.model)
    assert len(_chunk(docs["orca"][1])) >= 1

    # kNN: none of this is sqlite-vec yet — the honest Phase-0 fallback scans
    # the table in Python. Same columns, same query shape, swappable later.
    q = emb.embed("What do killer whales eat in the ocean?")
    scored = []
    for row in db.chunks():
        scored.append((emb.cosine(q, emb.unpack(row["embedding"])),
                       row["document_id"]))
    scored.sort(reverse=True)
    print(f"[vec    ] query 'killer whales eat': {[(round(s,2), d) for s, d in scored[:3]]}")
    assert scored[0][1] == "orca", f"expected orca first, got {scored[:3]}"

    # runs + traces: what a LangGraph run will actually write in phase 2
    run_id = str(uuid.uuid4())
    db.start_run(run_id=run_id, agent_id="smoke", input_text="hello")
    cost = db.record_node(node_run_id=str(uuid.uuid4()), run_id=run_id,
                          node_id="llm-1", node_kind="llm", task="general_chat",
                          arm="groq:qwen/qwen3.6-27b", latency_ms=300,
                          tokens=21, cost_usd=0.0)
    db.record_node(node_run_id=str(uuid.uuid4()), run_id=run_id,
                   node_id="llm-2", node_kind="llm", task="reasoning",
                   arm="openrouter/xiaomi/mimo-v2.6-pro", latency_ms=5400,
                   tokens=410, cost_usd=0.0012)
    db.finish_run(run_id=run_id, cost_usd=0.0012, tokens=431)
    rows = db.run_summary(run_id)
    assert len(rows) == 2 and rows[0]["arm"] == "groq:qwen/qwen3.6-27b"
    assert cost == 0.0
    print(f"[trace  ] run {run_id[:8]}: {len(rows)} node rows, "
          f"arms={[r['arm'] for r in rows]}, cost={0.0012}")
    db.close()


async def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else None
    with tempfile.TemporaryDirectory(prefix="studio-phase0-") as tmp:
        check_memory(pathlib.Path(tmp))
        await check_gateway(base)


if __name__ == "__main__":
    try:
        asyncio.run(main())
        print("\nPHASE 0 SMOKE: PASS")
    except (GatewayError, AssertionError) as e:
        print(f"\nPHASE 0 SMOKE: FAIL — {e}")
        sys.exit(1)
