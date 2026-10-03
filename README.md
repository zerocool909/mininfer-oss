# MinInfer

> **Find the cheapest capable model for every task. Prefer free. Pay only when necessary.**

MinInfer is an open-source **model intelligence registry and economic constraint router**.

It continuously collects evidence from provider APIs, model catalogs, pricing pages, benchmark sources, and other public webpages; normalizes that evidence into a deployment registry; and uses task intent, hard capability constraints, quality thresholds, availability, quotas, and cost-per-success to select a model for each request.

The goal is simple:

**Don't ask "Which model is best?" Ask "Which model is capable enough for this task at the lowest effective cost?"**

---

## Why MinInfer?

The LLM ecosystem changes constantly.

Models are released, deprecated, repriced, rate-limited, moved between providers, exposed through new gateways, or temporarily offered through free tiers.

A model can also have very different economics depending on where it is served.

MinInfer treats these as separate concerns:

```text
Model / Weights
      │
      ▼
Capabilities + Benchmarks
      │
      ▼
Deployment
(provider + endpoint + price + limits + latency + availability)
      │
      ▼
Task Constraints
      │
      ├── capable?
      ├── context sufficient?
      ├── modality supported?
      ├── quality floor met?
      ├── free quota available?
      ├── reliable enough?
      └── independent fallback available?
      │
      ▼
Cheapest Capable Deployment
```

This makes MinInfer less of a traditional load balancer and more of a **model decision engine backed by continuously refreshed evidence**.

---

## Core Principles

### 1. Identity is separate from economics

A model's weights and a callable deployment are not the same thing.

For example:

```text
Artifact:
    Qwen / SomeModel

Deployments:
    Provider A → $0.10 / $0.40 per M tokens
    Provider B → $0.05 / $0.20 per M tokens
    Gateway C  → free tier with quota
    Local       → user's own infrastructure
```

The same model can therefore have different price, latency, availability, quota, context, quantization, and reliability characteristics depending on the deployment.

---

### 2. Free is a quota constraint

A deployment is not automatically "free" just because its price is currently `$0`.

MinInfer distinguishes between:

```text
free_quota      recurring zero-price allowance
trial_credit    temporary promotional / trial balance
paid            normal paid usage
local           user-owned compute
unknown         economics not yet verified
```

A free deployment with exhausted quota is not economically equivalent to a genuinely available free deployment.

MinInfer therefore tracks:

* request limits
* token limits
* quota windows
* observed `429` responses
* current headroom when available
* provider-specific usage signals
* trial / promotional expiration where known

---

### 3. Unknown is not false

Capability, pricing, quota, and availability can be:

```text
True
False
Unknown
```

Missing evidence should not automatically mean unsupported.

This prevents incomplete provider metadata from silently eliminating potentially useful deployments.

---

### 4. Fallbacks should be independent

Three model entries are not necessarily three independent options.

For example:

```text
OpenRouter
   └── Upstream Provider A
          └── Model X

Hugging Face
   └── Upstream Provider A
          └── Model X
```

Those are different gateways but potentially the same underlying failure domain.

MinInfer therefore attempts to separate fallbacks across:

1. gateway/provider
2. upstream infrastructure
3. model family

The router reports the independence it actually achieved instead of assuming it.

---

### 5. Don't collapse everything into one score

MinInfer deliberately avoids a simple weighted sum such as:

```text
quality * 0.5
+ cost * 0.3
+ latency * 0.2
```

Instead:

```text
1. Hard capability constraints
2. Minimum quality threshold
3. Economic eligibility
4. Reliability / quota constraints
5. Diversity constraints
6. Minimize effective cost per successful task
```

This prevents a very cheap but incapable model from winning simply because its price dominates the score.

---

## Key Features

### Economic Routing

Prioritizes available free-tier deployments when they satisfy the task constraints.

Falls back to paid deployments when free capacity is unavailable or when the task requires capabilities that the available free pool does not satisfy.

### Evidence-Driven Model Registry

MinInfer ingests model and deployment evidence from:

* provider APIs
* public model catalogs
* pricing pages
* benchmark leaderboards
* performance endpoints
* arbitrary webpages

Fetched evidence is stored with source metadata and timestamps so routing decisions can be traced back to the information used to make them.

> Registry size is dynamic. An example cold-start snapshot currently contains approximately **23,799 deployments** and **200,000+ evidence records**. These numbers change as sources are refreshed.

### Prompt Intent Classification

Requests are mapped into task profiles such as:

```text
general_chat
summarise
code_generation
code_edit
hard_reasoning
sql_generation
structured_output
classification
vision
```

A fast heuristic classifier is used first, with optional model-based classification when confidence is insufficient.

### Conservative Quality Estimation

MinInfer combines benchmark evidence with observed outcomes.

The router uses a conservative lower-bound estimate rather than allowing a small number of lucky successful requests to immediately promote an untested deployment.

Where Wilson-based estimation is used, observed evidence and benchmark priors are kept explicit rather than treating an untested model as either perfect or useless.

### Reliability-Aware Fallbacks

MinInfer records:

* latency
* timeouts
* errors
* HTTP `429`
* availability
* throughput
* provider-level failures

This allows routing decisions to account for more than nominal token price.

### Contextual Bandit

Optional Thompson-sampling exploration allows MinInfer to learn which deployments perform well for specific task families.

The bandit can incorporate:

* successful outcomes
* failed outcomes
* latency
* quota failures
* human preference feedback

Human preference can be submitted through the Arena interface.

### OpenAI-Compatible Proxy

MinInfer exposes an OpenAI-compatible endpoint:

```text
POST /v1/chat/completions
```

Applications can request:

```json
{
  "model": "auto"
}
```

and let MinInfer select the deployment dynamically.

### Arena Compare Mode

Multiple eligible deployments can be executed concurrently for comparison.

The Playground can display:

* response quality
* latency
* generation speed
* estimated cost
* selected deployment
* routing rationale

Human selection can then be fed back into the routing system.

### Explainable Routing

Every routing decision can be inspected.

The router exposes:

```text
selected model
task classification
candidate models
rejection reasons
quality evidence
cost assumptions
quota state
fallback separation
policy used
```

Responses may include explainability headers such as:

```text
X-MI-Deploy
X-MI-Task
X-MI-Policy
X-MI-Candidates
```

---

# Quick Start

## 1. Install

```bash
git clone <your-repository-url>
cd <repository>

python3 -m venv .venv
source .venv/bin/activate

# Base install: registry, ingest, routing and the CLI (`mi`).
pip install -e .

# To also run the OpenAI-compatible proxy / dashboard, add the server extra.
# Without it, `mi proxy` exits with a message telling you exactly this.
pip install -e '.[server]'
```

---

## 2. Build the registry

Ingest public sources:

```bash
python3 -m mininfer ingest
```

Resolve model identities across sources:

```bash
python3 -m mininfer resolve
```

Inspect the registry:

```bash
python3 -m mininfer stats
```

---

## 3. Test routing

Route a task:

```bash
python3 -m mininfer route general_chat
```

Inspect why deployments were selected or rejected:

```bash
python3 -m mininfer explain code_edit
```

Or use the CLI entrypoint:

```bash
mi ingest
mi route general_chat
```

---

## Example Routing Output

Example output from one registry snapshot:

```text
FUNNEL
23799 deployments
        ↓
97 eligible
        ↓
42 currently classified as zero-marginal-cost
        ↓
14 distinct upstreams

Rejected:
  0 unsupported
  0 unverified
  206 below quality floor
  927 without benchmark coverage

DIVERSITY
Gateway + upstream separation achieved

1st candidate:
  openrouter:xiaomi/mimo-v2.6-pro

  price:
    $0.435 / M input
    $0.870 / M output

  estimated success:
    0.70 conservative lower bound

  prior:
    0.88 from benchmark evidence

  availability:
    1.00 observed

  observed 429 rate:
    0%

  estimated cost per success:
    $0.0019
```

> The exact numbers above are an example snapshot, not a permanent benchmark or guarantee.

---

# CLI

| Command                    | Purpose                                                 |
| -------------------------- | ------------------------------------------------------- |
| `mi sources`               | Show configured sources and authentication requirements |
| `mi ingest [src…]`         | Fetch and snapshot source evidence                      |
| `mi resolve`               | Resolve model/deployment identities across sources      |
| `mi stats`                 | Show registry and provider statistics                   |
| `mi route TASK`            | Resolve the best eligible deployments for a task        |
| `mi explain TASK`          | Explain ranking and rejection reasons                   |
| `mi observe`               | Record inference outcomes                               |
| `mi quota seed`            | Seed declared quota configuration                       |
| `mi quota show`            | Inspect configured and observed quota state             |
| `mi bench FAMILY`          | Run RouterBench evaluations                             |
| `mi bandit TASK`           | Inspect contextual-bandit posteriors                    |
| `mi leaderboards [SUBSTR]` | Compare ingested benchmark sources                      |
| `mi metrics`               | Enrich deployments with performance metrics             |
| `mi add-url URL`           | Ingest evidence from an arbitrary webpage               |
| `mi proxy`                 | Start the OpenAI-compatible routing proxy               |

---

# Running the Proxy

Requires the `server` extra (`pip install -e '.[server]'`).

Set API keys only for providers you intend to use:

```bash
export OPENROUTER_API_KEY="..."
export GROQ_API_KEY="..."
export GEMINI_API_KEY="..."
```

Start MinInfer:

```bash
mi proxy --port 8765
```

Open:

```text
http://127.0.0.1:8765/
```

The dashboard includes:

* registry statistics
* routing funnel
* quota information
* recent routing decisions
* task classification
* Playground
* Arena Compare
* routing explanations

---

# Using MinInfer with OpenAI Clients

## Python

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="not-needed",
)

response = client.chat.completions.create(
    model="auto",
    messages=[
        {
            "role": "user",
            "content": "Write an async connection pool in Python."
        }
    ],
)

print(response.choices[0].message.content)
```

You can also explicitly select a task profile:

```python
model="code_edit"
model="hard_reasoning"
model="sql_generation"
```

---

## TypeScript / Vercel AI SDK

```typescript
import { createOpenAI } from "@ai-sdk/openai";
import { generateText } from "ai";

const mininfer = createOpenAI({
  baseURL: "http://127.0.0.1:8765/v1",
  apiKey: "not-needed",
});

const { text } = await generateText({
  model: mininfer("auto"),
  prompt: "Explain optimistic vs pessimistic locking in distributed databases.",
});

console.log(text);
```

---

## cURL

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {
        "role": "user",
        "content": "How do distributed hash tables work?"
      }
    ]
  }'
```

---

# Arena Comparison

Run multiple candidates for the same task:

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "mi_options": 2,
    "stream": true,
    "messages": [
      {
        "role": "user",
        "content": "Compare Raft vs Paxos in simple terms."
      }
    ]
  }'
```

The Playground can compare the responses and submit a human preference.

Human feedback can then update the routing policy and bandit state.

---

# Architecture

```text
                    ┌────────────────────────────┐
                    │       Evidence Sources     │
                    │                            │
                    │ APIs / Catalogs / Pricing  │
                    │ Benchmarks / Webpages      │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                    ┌────────────────────────────┐
                    │        Ingestion Layer     │
                    │                            │
                    │ adapters + snapshots       │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                    ┌────────────────────────────┐
                    │       Evidence Store       │
                    │                            │
                    │ source / timestamp / hash  │
                    │ confidence / provenance     │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                    ┌────────────────────────────┐
                    │    Identity Resolution     │
                    │                            │
                    │ artifact ↔ deployment      │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                    ┌────────────────────────────┐
                    │   Deployment Registry      │
                    │                            │
                    │ capability / price / quota │
                    │ latency / uptime / context │
                    └─────────────┬──────────────┘
                                  │
                       user prompt│
                                  ▼
                    ┌────────────────────────────┐
                    │      Intent Classifier     │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                    ┌────────────────────────────┐
                    │       Routing Policy       │
                    │                            │
                    │ capabilities               │
                    │ quality floor              │
                    │ quota                      │
                    │ economics                  │
                    │ reliability                │
                    │ fallback diversity         │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                    ┌────────────────────────────┐
                    │        Model Router        │
                    │                            │
                    │ cheapest capable candidate │
                    └─────────────┬──────────────┘
                                  │
                ┌─────────────────┼─────────────────┐
                ▼                 ▼                 ▼
             Gateway A         Gateway B         Local
             Provider X        Provider Y        vLLM/Ollama
                │                 │                 │
                └─────────────────┼─────────────────┘
                                  ▼
                           Outcome Telemetry
                                  │
                                  ▼
                         Bandit / Feedback
```

---

# Repository Layout

```text
mininfer/

├── proxy.py
├── router.py
├── store.py
├── schema.py
├── intent.py
├── execute.py
├── quota.py
├── bandit.py
├── resolve.py
├── ingest.py
├── bench.py
├── fetch.py
│
├── web/
│   ├── legacy.py
│   └── dist/
│
config/
├── policy.yaml
├── quotas.yaml
└── profiles/
    ├── local.env          # the local variant, as declared state
    └── cloud.env          # the cloud variant

deploy/
├── local/                 # docker-compose.yml + README
├── cloud/                 # fly.toml, k8s.yaml, litestream.yml, keys.example.json
└── README.md              # which variant, and why profiles and not forks

docker/
└── entrypoint.sh

Dockerfile                 # one image, used by both variants
docker-compose.yml         # `include:` shim -> deploy/local/

web/
└── React / TypeScript / Vite application
```

---

# Sources and Providers

MinInfer is designed to ingest information from multiple classes of sources.

### Public catalogs and gateways

Examples include:

* OpenRouter
* Hugging Face Inference Providers
* Vercel AI Gateway
* DeepInfra
* Featherless
* Chutes
* Novita
* SambaNova
* NVIDIA NIM

### Direct inference providers

Examples include:

* Groq
* Google Gemini
* Cerebras
* Mistral
* GitHub Models
* Cloudflare Workers AI
* Cohere
* Together AI
* Nebius
* Hyperbolic
* Fireworks AI
* Zhipu
* Moonshot
* DashScope

### Local runtimes

* Ollama
* vLLM
* LM Studio

Provider pricing, quotas, model availability, and free-tier policies can change. MinInfer therefore treats provider information as **time-stamped evidence**, not immutable truth.

Run:

```bash
mi sources
```

to inspect the current configured source state.

---

# Production Deployment

MinInfer can run as a containerized service with the API and web application packaged together.

```bash
docker compose up -d
```

Health check:

```bash
curl http://localhost:8765/healthz
```

Deployment examples are documented in:

```text
cloud_deploy.md
```

---

# Security and Privacy

MinInfer is designed to run with user-owned provider credentials.

### Important

Do not commit API keys, credentials, provider tokens, or private configuration to the repository.

Do not enable unrestricted public inference against your own provider accounts.

For publicly hosted deployments, use one or more of:

* user-provided API keys
* authenticated access
* strict request quotas
* an allowlisted free-only provider pool
* hard spend limits
* disabled paid fallback
* per-user rate limiting

Inference telemetry should avoid storing raw prompts and responses by default.

---

# Evidence Provenance

Evidence should be traceable.

A deployment record should make it possible to answer:

```text
Where did this price come from?
When was it observed?
Which source reported it?
How confident are we?
Has the evidence expired?
What changed since the previous snapshot?
```

Raw webpage snapshots should not automatically be redistributed without checking the source's licensing and terms.

For public releases, prefer publishing:

* normalized metadata
* source URLs
* timestamps
* hashes
* derived facts
* compact provenance records

rather than blindly committing large bodies of third-party webpage content.

---

# What MinInfer Is Not

MinInfer is not:

* a benchmark leaderboard
* a universal "best model" ranking
* a replacement for every AI gateway
* a guarantee that free inference is always available
* a way to bypass provider rate limits
* a mechanism for circumventing provider terms or quotas

Its purpose is to make **model selection an explicit, evidence-backed engineering decision**.

---

# Roadmap

### Phase 1 — Foundation

* [x] Multi-source ingestion
* [x] Deployment registry
* [x] Identity resolution
* [x] Capability filtering
* [x] Economic routing
* [x] OpenAI-compatible proxy
* [x] Explainability
* [x] Quota tracking
* [x] Arena comparison

### Phase 2 — Learning

* [x] Thompson-sampling exploration
* [x] Human feedback
* [ ] Automated outcome graders
* [ ] Task-specific success models
* [ ] Better uncertainty calibration

### Phase 3 — Model Intelligence

* [ ] Automated benchmark ingestion
* [ ] Benchmark freshness tracking
* [ ] Provider reliability scoring
* [ ] Change detection for pricing and quotas
* [ ] Model deprecation detection
* [ ] Capability conflict detection

### Phase 4 — Ecosystem

* [ ] Public registry snapshots
* [ ] Reproducible RouterBench suite
* [ ] Community provider adapters
* [ ] Hosted registry explorer
* [ ] Hosted routing playground
* [ ] Model-routing research reports

---

# Contributing

The most useful contributions are provider adapters, benchmark adapters, routing policies, evaluation datasets, bug fixes, documentation, and reproducible experiments.

See:

```text
CONTRIBUTING.md
CODE_OF_CONDUCT.md
SECURITY.md
```

---

# License

MIT
