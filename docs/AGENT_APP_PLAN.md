# Plan — drag-and-drop agent builder on top of the min(Infer) gateway

Status: proposal. The gateway it builds on is live at `http://aditya:8765/v1`
(tailnet) and `http://127.0.0.1:8765` locally, with tenant keys, rate limiting
and the Postgres/Redis cloud shape already rehearsed in Docker.

## 0. Using the existing UI right now

- **Dashboard (registry, routing decisions, economics):** open
  `http://aditya:8765/` from any tailnet device. Browser Basic-auth prompt:
  *username anything, password = admin token* (`MI_ADMIN_TOKEN` in `.env`).
  Tenant keys intentionally get 401 here — the dashboard shows provider
  pricing, caller IPs and the router's rejections.
- **Realtime chat:** the dashboard's chat tab posts to `/v1/chat/completions`
  with SSE streaming, so answers stream token-by-token. Anything an OpenAI
  client can do works the same way: `model: "auto"` routes by task;
  `"mininfer/<task>"` aliases (`mininfer/sql_generation`, …) pin a *task*;
  a full `deploy_id` (`groq:qwen/qwen3.6-27b`) pins a *specific arm*.
- **Where a call landed:** the response carries `mi.reason.selected` — arm,
  cost, runners-up and why they lost. The dashboard `Usage` view shows it per
  session.

The dashboard is the registry and the form guide. What it does not try to be is
an agent builder. That is the app this document plans.

## 1. The app in one screen

```
  ┌─────────────────────────── canvas (drag & drop) ───────────────────────────┐
  │  [Input]──►[Router]──►[Cheap LLM ×5]──┬──►[Tools: search]──┐              │
  │                                       │                    ├─►[Merge]──►[Output]
  │                      [Vector recall]──┴──►[Frontier LLM]──┘              │
  └──────────────────────────────────────────────────────────────────────────┘
      compile ────► LangGraph StateGraph ────► run ────► token stream back
      every LLM node ──► mininfer /v1/chat/completions (task, pin, budget)
      every Recall node ──► local vector DB
```

A user assembles a graph, hits Run, and watches tokens stream through the exact
node that produced them, with per-node cost and arm attribution from `mi.reason`.

## 2. Backend choice — LangGraph, and why it is the right one here

**Yes, LangGraph.** The alternatives fail this specific shape:

| Option | Verdict |
| :-- | :-- |
| **LangGraph** | **Graph-native, state-machine semantics (exactly what the canvas compiles to), SQLite checkpointer for resumable runs, per-step streaming callbacks, conditional edges for branches/loops.** MIT, big ecosystem. |
| n8n / flowise-style engine | The canvas *is* the product, but their node set and model-facing defaults conflict; embedding an external runtime we cannot patch costs more than wrapping LangGraph. |
| Pydantic AI | Great typed agents, but single-agent-centric; a multi-node DAG with join points is what LangGraph is for. |
| LlamaIndex Workflows | Event-driven steps, not graph-shaped; weaker for cycles and fan-in. |
| Hand-rolled executor | Weeks of re-learning: checkpointing, retries, parallel fan-out, streaming token relay — all solved in LangGraph. |

The one thing LangGraph does *not* handle is model selection — and that is
mininfer's job, which is the point of the pairing: **LangGraph owns control
flow, mininfer owns model economics.** Every `ChatModel` node in the graph gets
a `ChatOpenAI(base_url=http://aditya:8765/v1, model=..., metadata=...)`; the
router then puts cheap free arms on summarise/extract/classify nodes and the
frontier arm only where the node declares it needs it — the same way README's
"agents are mostly plumbing" table describes.

Integration details that matter:

- **Per-node task annotation.** A node can declare `task` (e.g. `structured`,
  `tools`) or a pin. The gateway URL carries it: `model: "auto"` + optional
  `mi_options.pin`. Node runs are attributed to arms in traces because the
  response's `mi.reason.selected` names the arm and the losers.
- **Streaming relay.** `ChatOpenAI(streaming=True)` → LangGraph
  `astream_events` → our WebSocket, one event per token plus node-enter/exit.
- **Checkpointer = `SqliteSaver`**, same engine the rest of the project uses,
  gave the free resumability.
- **Concurrency caution.** One SQLite file per backend is the pattern that
  failed before (the OneDrive lesson). Single-file data stores live on a
  local-disk path, not a synced folder, and the *chat* writes go through
  mininfer, which already solved its own write-lock issues.

## 3. Vector DB — local, three real candidates

No `/v1/embeddings` surface exists on the gateway yet (roles are in the
registry, the endpoint is not), so embeddings come from a local model by
default — `sentence-transformers` `all-MiniLM-L6-v2` (~90 MB, offline, good
enough for recall) — or a direct provider call if a user wants cloud quality.

| Store | Why take it | Why not |
| :-- | :-- | :-- |
| **`sqlite-vec`** (recommended) | One file, one extension, no service, pairs with the existing SQLite obsession, `select kNN` over a plain BLOB column, fast enough for ≤1M vectors | Younger than the others; ANN is brute-force + quantization, fine at hobby scale |
| ChromaDB | Most popular, persistent-client mode, metadata filtering, docs everywhere | Another process-ish dependency for embedded use; heavier |
| LanceDB | Columnar, great filtering, scales further | Newer API surface; overkill until >1M vectors |

**Decision: `sqlite-vec` + `all-MiniLM-L6-v2`.** One less moving part; the
anchors (agent definitions, runs, traces) already live in SQLite, so memory
nodes read/write the same file family as everything else. Chroma stays the
documented upgrade path if filtering demands grow.

## 4. Data model (SQLite, beside the canvas)

| Table | What it holds |
| :-- | :-- |
| `agents` | id, name, canvas JSON (nodes + edges + layout), version chain |
| `nodes` / `edges` | denormalized from the canvas for fast edits (LLM node: task, pin, max_tokens, temperature, system prompt) |
| `runs` | agent id, input, status, started, finished, total cost, total tokens |
| `node_runs` | one row per node per run: arm that served it (from `mi.reason`), latency, tokens, cost, error |
| `documents` | user-uploaded text for RAG nodes (embedded to `vec` table) |
| `vec_chunks` | `sqlite-vec` virtual table: chunk → embedding, document, agent |

`node_runs` is the payoff table: "this agent ran 214 times, its cheap steps
cost $0.00, its frontier step cost $1.19, and here is which arm failed when."
It is the same provenance discipline mininfer applies to models, applied to
agents.

## 5. Realtime protocol (WebSocket, app → browser)

```
{type: "run_started", run_id}
{type: "node_entered", node_id, ts}
{type: "token", node_id, delta, model, provider}   ← streamed tokens
{type: "node_exit",  node_id, duration_ms, cost, arm, tokens}
{type: "edge_active", edge_id}                      ← drag-and-drop sparkle
{type: "branch_taken", from, to, why}               ← conditional edges named
{type: "run_finished|run_failed", run_id, error?}
```

Token events go to whatever node is producing them; branches say *why* they
fired. Node-exit events carry the arm — the dashboard's explainability, one
level up.

## 6. Node palette (phase 1 minimum, deliberately small)

| Node | Behaviour |
| :-- | :-- |
| **Input** | user text / file |
| **Prompt** | template with `{{variables}}` |
| **LLM** | mininfer call — task, pin, max_tokens, temp |
| **Branch** | conditional edges (LLM-routed or rule) |
| **Recall** | sqlite-vec similarity over the agent's docs |
| **Tool** | whitelisted tools (web search via mininfer `/v1/search`, http fetch, code exec in a sandbox container — later) |
| **Merge** | fan-in join |
| **Output** | final text / JSON schema |

Every LLM node is also *drop-in* swappable: right-click → "pin this node to
`groq:openai/gpt-oss-20b`" or "let mininfer choose (`auto`)".

## 7. Stack summary

- **Frontend:** React + **React Flow (xyflow)** — the de-facto drag-and-drop
  graph canvas, MIT, node-resize/minimap built in. Served as a second Vite SPA
  or one app with two tabs.
- **Backend:** FastAPI (app API + WebSocket) + LangGraph runner, one process.
- **Storage:** SQLite (canvas/checkpoints/traces) + sqlite-vec (memory).
- **Model plane:** mininfer only. The app holds the tenant key server-side;
  end users of the app get app-level accounts, never provider keys.
- **Deployment:** same host, one `docker compose` alongside the gateway, or
  the cloud shape when this graduates from the tailnet to the internet.

## 8. Build phases

| Phase | Deliverable | Proof it is done |
| :-- | :-- | :-- |
| 0 | Repo skeleton, gateway client (chat + streaming + `mi.reason` parse), sqlite-vec bootstrap | a script chats through the gateway and streams tokens |
| 1 | Canvas MVP: LLM/Prompt/Input/Output nodes drag-drop → LangGraph compile → WS token stream to canvas | two nodes wired on screen, token text appears per node |
| 2 | Branch + Recall + Tools; runs table; cost attribution from `mi.reason` into `node_runs` | a 4-node agent runs, trace shows arms + costs |
| 3 | Canvas save/load/versioning; retry-on-429 at the node level; run replay | same graph, two runs, both traced |
| 4 | Document upload + RAG demo; export/import canvas JSON | ask about an uploaded doc, answer cites it |
| 5 (later) | Public hosting: fly.toml clone of the gateway's cloud profile, Postgres instead of SQLite | same suite green on the cloud shape |

## 9. Risks, stated plainly

1. **Token streaming through WS to the right node** is the fiddliest part —
   `astream_events` gives the node context; the frontend just needs discipline
   about which pane is live.
2. **Free-arm quota** burns fast under agent loops (25/50 in the last eval).
   The gateway's demotion covers balance across arms, but a long canvas loop
   can still starve the free pool for everyone on your tailnet — set a
   per-run token budget in the app and enforce it in the LangGraph state.
3. **Tool code-exec nodes are a sandbox problem**, not an LLM problem — keep
   them out of phase 1 or put them in a seccomp container from day one.
4. **LangGraph version churn** is real; pin the dependency and wrap every
   LangGraph import in one place so upgrades are a one-file diff.
