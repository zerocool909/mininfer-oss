# Context — min(Infer)

A glossary of the terms this project uses about models, their facts, and the evidence for those
facts. Nothing here describes an implementation; it fixes what the words mean.

## The four axes

These are independent. Collapsing them is the error this glossary exists to prevent.

**Modality** — a *content type* that a model exchanges. Examples: `text`, `image`, `audio`,
`video`, `file`. Modality is **directional**: a model has **input modalities** (what it accepts) and
**output modalities** (what it produces). `text → text` and `audio → text` are different models. So
are `text → text` and `text → audio`.

**Input modality** — a content type the model accepts.
**Output modality** — a content type the model produces.

**Capability** — a *behaviour* the model performs, independent of the content types involved. Today:
`tools`, `structured`, `reasoning`, `caching`. A capability is three-valued: **confirmed**
(some source reported it), **refuted** (a source reported its absence), or **unknown** (no source
has said either way). Unknown is not refuted.

**Role** — what kind of thing the model is: either a *general-purpose generator* (no role), or a
specific non-chat function — a safety/moderation classifier, an embedding model, a reranker, an
ASR (speech→text) model, a TTS (text→speech) model, an image generator, a music generator.

**Domain** — the *kind of work* a model is good at: coding, extraction, summarisation, mathematics,
conversation. Domain is **evidence only**. It is never derivable from modality or capability, and
it is never a measurement.

## Containment rules

- **Capability is behaviour; modality is content.** "Can emit a tool call" is a capability. "Accepts
  an image" is an input modality. A capability is never a restatement of a modality.
- **A generative output is a modality, not a role.** A model that produces audio has
  `output_modalities = [audio]`; "generates audio" is not a capability.
- **Role is derived, never independent.** For *generative* roles, role follows from
  output modality — audio output means a music/speech generator. For *non-generative* roles
  (embedding, rerank, safety) modality cannot distinguish them at all: an embedder and a chat model
  are both `text → text`. Those roles need their own evidence.
- **Domain is evidence.** Nothing computes it; a source asserts it and the assertion is weighted.
- **Weight vs. deployment.** Facts about the *artifact* (its modalities, its architecture, its
  benchmark scores) belong to **Weights**. Facts about a *specific callable instance* (its context
  window, price, rate limits, and whether this gateway will honour tool calls) belong to
  **Deployment**. The same weights reached through two gateways are two deployments and may
  legitimately disagree.

## Facts and evidence

**Fact** — an assertion about a model that the registry holds and acts on: its modalities, whether
it supports tools, which domains it fits. A fact is the *resolved* result, not the raw input.

**Evidence** — one sourced observation supporting or contradicting a fact. Evidence carries the
source, the URL, when it was observed, and how it was obtained. Evidence is never discarded on the
grounds that it lost: a claim that loses a conflict is still a claim someone made.

**Claim vs. measurement.** A *claim* is something a page, a catalogue or a model says. A
*measurement* is something we observed by calling the model. They are not the same kind of thing and
are not weighed the same way. Every fact in the registry today is a claim.

**Provenance** — the evidence a fact was resolved from. A fact without provenance is unusable, because
it cannot be re-checked when a source changes.

**Conflict** — two pieces of evidence disagreeing about the same fact. In three-valued terms:
confirmed vs. refuted is a conflict; confirmed vs. unknown is not — an unreported value asserts
nothing. A conflict is *resolved* by precedence or *recorded* as disputed; it is never resolved by
whoever wrote last.

**Precedence** — the ranking of evidence that decides a conflict. Ranked by how directly the source
owns the fact, then by how recently it was observed.

**Unverified** — a required capability no source has ever reported. Distinct from **refuted**
(a source denied it) and from **confirmed**.

## Roles (the non-generative set)

Named so they can be referred to without ambiguity:

| Role | What it does |
|---|---|
| `safety` | Classifies or moderates content |
| `embedding` | Produces a vector representation |
| `rerank` | Orders a candidate set |
| `asr` | Speech → text |
| `tts` | Text → speech |
| `image-gen` | Produces images |
| `music` | Produces audio/music |

`asr`, `tts`, `image-gen` and `music` are *generative* roles and are derivable from output modality.
`safety`, `embedding` and `rerank` are not derivable from modality and require independent evidence.

## Routing terms

**Task** — a named workload with its own token profile, required capabilities, quality floor and
cues. **Policy** — the constraints and objective applied to ranking. **Candidate** — one deployment
scored for one task. **Eligible** — a candidate that survives every hard constraint. **Quality
floor** — the minimum posterior success probability a task demands.

## Two senses of "free"

These are both legitimate answers to different questions and must never be merged into one predicate.
An arm can be the second without being the first.

**Free** — the *durable economic state*: the list price is literally zero, or the model id declares a
free variant. This is what a report means when it says an arm is free, and it is what
`store._pricing_state` returns as `free`. Roughly a third of what the router will rank.

**Free-kind** — *why the marginal cost is approximately zero right now*. A strictly weaker and more
temporal claim than being free, and the thing that makes an arm compete on the zero-cost tier today.
It has four sources, of which only the first two imply `free`:

| Source | Implies durably free? |
|---|---|
| `zero_price` — the list price is $0 | yes |
| `free_variant` — a `:free` / `-free` id suffix | yes |
| `trial_credits` — **a balance that depletes** | **no** |
| `subscription` — **prepaid, not free** | **no** |

So a *trial* arm is `trial`, not `free`, and still ranks on the free tier until its credits run out.
Conflating the two is how a router spends money it believed was zero.

**Pricing state** — the derived, canonical durable classification of one deployment: `free`,
`free_with_quota`, `paid_after_quota`, `trial`, `subscription`, `paid`, `unknown`, `hibernated`,
`deprecated`. **The** answer to "is this arm free?" — prefer it over any local predicate.

## Dossiers

**Dossier** — the agent-written *form guide* for one arm: what it is good at, and when to pick it.

A dossier has **three roles, and only one of them is a routing input**:

- **Domain fit** (`best_for`) — the dossier's durable contribution and its **only** routing-relevant
  field. Domain is the one fact kind no catalogue publishes, which is why the dossier exists.
- **Prose** (`summary`, `warrior`, `story`) — written for a human reading the form guide. Never
  factual, never read by the router, never allowed to assert a benchmark or a capability.
- **Facts snapshot** (`facts`) — what the registry believed when the dossier was written. A
  **provenance record**, used to detect that a dossier is stale, never a source of truth. Facts
  themselves belong to the resolver.
