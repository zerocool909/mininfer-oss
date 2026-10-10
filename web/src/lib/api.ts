/**
 * Typed client for the MinInfer proxy.
 *
 * The dashboard is a pure consumer of the existing OpenAI-compatible API — it
 * adds no server behaviour. That keeps the CLI, this UI and any other OpenAI
 * client on exactly one contract.
 */
import { getUserKeysHeader } from './keys'
import { exploreQueryString } from './explore'

export interface LeaderboardTag {
  key: string
  label: string
  source: string
  url?: string
  value?: number
}

export interface SelectedModel {
  deploy_id: string
  p_lb: number
  cost_per_success: number | null
  free_kind: string | null
  availability: number
  prior_key?: string
  headroom?: number | null
  leaderboards?: LeaderboardTag[]
}

export interface Funnel {
  total?: number
  after_hard_filter?: number
  free_eligible?: number
  distinct_providers?: number
  rejected_unsupported?: number
  rejected_unverified?: number
  rejected_quality?: number
  rejected_no_evidence?: number
  rejected_no_key?: number
  [k: string]: number | undefined
}

export interface Plan {
  task: string
  funnel: Funnel
  diversity: string
  strategy: string
  chosen: SelectedModel[]
}

export interface Counts {
  weights: number
  deployments: number
  evidence: number
  snapshots: number
  observations: number
  quarantine: number
  decisions: number
  [k: string]: number
}

export interface ProviderRow {
  provider: string
  n: number
  free_n: number
  min_in: number | null
  /** Which source that cheapest price came from — a price with no attribution is
   *  a number the reader has to take on faith. */
  min_in_source?: string | null
  min_in_state?: string | null
}

export interface QuotaRow {
  deploy_id: string
  window: string
  used_n: number
  limit_n: number | null
  headroom: number | null
  reset_at: string | null
  /** Which limit set the headroom: configured | observed | hybrid | unknown. */
  headroom_source?: string
  /** Provider-reported numbers, when a response header carried them. */
  observed_limit_n?: number | null
  observed_remaining_n?: number | null
  observed_at?: string | null
  /**
   * A *probabilistic* exhaustion estimate. Deliberately not a bare timestamp:
   * usage is stochastic, so it travels with its basis and a confidence that rises
   * with the number of observations behind it.
   */
  exhaustion?: {
    estimated_exhaustion_at: string | null
    confidence: number
    basis: Record<string, unknown>
  }
}

export interface DecisionOrigin {
  ip?: string
  tz?: string
  country?: string
  source?: string
  is_local?: boolean
}

export interface DecisionRow {
  id?: number
  ts: string
  task: string
  policy: string
  /** auto | compare | direct | rejected | blocked — every request is logged, whatever it did. */
  mode?: string
  /** Empty when the request was refused before any arm was chosen. */
  chosen: string
  /** Why the arm won, lifted out of the stored decision blob. */
  why?: WhyTag[]
  origin?: DecisionOrigin
  compare_models?: string[]
  preferred?: string
}

/** One reason a decision went the way it did (see `router._why`). */
export interface WhyTag {
  key: string
  label: string
  detail?: string | null
  /** Other leaderboards that also covered the model, when there are several. */
  also?: string
}

export interface Stats {
  counts: Counts
  providers: ProviderRow[]
  quota: QuotaRow[]
  decisions: DecisionRow[]
  tasks: string[]
  savings?: Savings
}

export interface ChatOption {
  index: number
  deploy_id: string
  cost_per_success: number | null
  free_kind: string | null
  p_lb: number | null
  leaderboards?: LeaderboardTag[]
  /** That option's own upstream call time — options run serially. */
  latency_ms?: number | null
}

export interface ChatResponse {
  id: string
  model: string
  choices: { index: number; message: { role: string; content: string | null } }[]
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number }
  mininfer: {
    selected_model: string
    task: string
    policy: string
    options?: ChatOption[]
    alternatives?: string[]
    needs_approval?: boolean
    /** Upstream call time, as measured server-side. */
    latency_ms?: number | null
    reason: {
      funnel?: Funnel
      diversity?: string
      selected?: SelectedModel[]
      skipped_no_key?: string[]
      needs_approval?: boolean
    }
  }
}

export interface ApiError {
  error: { message: string; type: string; code: number }
}

export interface ProviderModel {
  deploy_id: string
  model_id: string
  display_name: string
  context_window?: number | null
  max_output?: number | null
  price_in?: number | null
  price_out?: number | null
  is_free: boolean
  caps: { structured?: boolean; tools?: boolean; vision?: boolean }
  benchmarks?: Record<string, number>
}

export interface ProviderInfo {
  id: string
  name: string
  description: string
  base_url: string
  key_env: string | null
  has_project_key: boolean
  is_local: boolean
  docs_url: string
  setup_guide: string
  models_count: number
  models: ProviderModel[]
}

/** One deployment hibernated for review: it was free, and now it charges. */
export interface Review {
  deploy_id: string
  provider: string
  provider_model_id: string
  price_in: number | null
  price_out: number | null
  status: string
  status_reason: string | null
  status_changed_at: string | null
  display_name: string | null
  /** The pricing state it came from, so the UI can say *free → paid* as a fact. */
  pricing_type_before?: string | null
}

/**
 * One pricing disagreement the reconciler refused or flagged (P3).
 *
 * `severity` is a banded factor against the market median, or `warning` when there
 * is no factor to band (a sudden change with too few sources to check).
 */
export interface PricingAnomaly {
  anomaly_id: string
  deploy_id: string
  dimension: string
  kind: 'unit_scale' | 'spread' | 'history_jump' | string
  severity: 'info' | 'warning' | 'high' | 'critical' | string
  state: string
  expected: number | null
  observed: number | null
  factor: number | null
  source_count: number
  detail: string | null
  status: 'open' | 'acknowledged' | 'resolved' | string
  opened_at: string
  last_seen_at: string
}

/**
 * Everything the Overview page's free/cost elements read, in one call (P7).
 *
 * A superset of `Stats`, so the page cannot render a price beside a *different*
 * moment's trust state.
 */
export interface EconomicsOverview extends Stats {
  reviews: Review[]
  anomalies: PricingAnomaly[]
  /** How many deployments are in each derived pricing state. */
  pricing_states: Record<string, number>
  generated_at: string
}

export interface LocalProbeResult {
  engine: string
  url: string
  connected: boolean
  models: string[]
  error?: string | null
}

/** One registry row, as the model explorer sees it (`GET /v1/models/explore`). */
export interface ExploreModel {
  deploy_id: string
  weights_id: string
  provider: string
  provider_model_id: string
  display_name: string | null
  family: string | null
  params_b: number | null
  context_window: number | null
  max_output: number | null
  quantization: string | null
  price_in: number | null
  price_out: number | null
  /** True free-tier facts; `subscription` is separate because it is prepaid. */
  free: boolean
  free_kind: string | null
  subscription: number
  pushed: boolean
  status: string
  /** Observed calls and wins, from `routing_stats`. */
  n: number
  wins: number
  success_rate: number | null
  mean_latency_ms: number | null
  /** `{tools: true}` means confirmed; a null is unknown, not unsupported. */
  caps: Record<string, boolean | null>
  modalities: string[]
  /** Benchmark scores for the weights, e.g. `{coding: 60}`. Empty when unscored. */
  benchmark: Record<string, number>
  benchmark_source: string | null
  trial_status?: string
}

export interface ExploreResult {
  total: number
  limit: number
  offset: number
  count: number
  models: ExploreModel[]
  providers: string[]
  capabilities: Record<string, number>
  sort: string
}

export interface ExploreQuery {
  q?: string
  provider?: string
  capability?: string
  free_only?: boolean
  untried_only?: boolean
  max_price_out?: number
  min_context?: number
  sort?: string
  limit?: number
  offset?: number
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const tz = typeof Intl !== 'undefined' ? Intl.DateTimeFormat().resolvedOptions().timeZone : ''
  const userHeaders = getUserKeysHeader()
  const res = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-Client-Timezone': tz,
      'X-MI-Client': 'playground',
      ...userHeaders,
      ...init?.headers,
    },
  })
  const body = await res.json().catch(() => null)
  if (!res.ok) {
    const e = (body as ApiError | null)?.error
    throw new Error(e?.message ?? `HTTP ${res.status}`)
  }
  return body as T
}

export interface SessionUsage {
  session_id: string
  calls: number
  tokens_in: number
  tokens_out: number
  tokens: number
  cost_usd: number
  model_cost_usd: number
  searches: number
  search_cost_usd: number
  limit: number | null
  remaining: number | null
  cost_limit: number | null
  cost_remaining: number | null
  /** What a session spent, and what it avoided by taking the cheap arm. */
  savings?: Savings
}

/**
 * Spend vs. the cheapest paid sibling of the same artifact.
 *
 * `saved_usd` counts only calls that cost nothing; `unpriced_free_calls` counts
 * the free calls with no paid sibling to price them against, so a total is never
 * mistaken for complete when it is not.
 */
export interface Savings {
  calls: number
  free_calls: number
  actual_cost_usd: number
  saved_usd: number
  unpriced_free_calls: number
  session_id: string | null
  days: number | null
  /** How the counterfactual was priced, so the figure can be labelled. */
  price_basis?: string
  /** Fraction of free calls that had a paid sibling to price against: a *coverage*
   *  measure, not a statistical confidence. */
  priced_free_share?: number | null
}

/** One stored turn, as `GET /v1/session/messages` returns it. */
export interface TranscriptMessage {
  id: number
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  task: string | null
  deploy_id: string | null
  ts: string
  meta: Record<string, unknown>
}

export const api = {
  stats: () => req<Stats>('/v1/stats'),
  /**
   * The Overview page's single source (P7): counts and telemetry, plus the
   * reconciled price provenance, quota sources, open anomalies, and the pricing
   * state distribution.
   */
  economics: () => req<EconomicsOverview>('/v1/economics/overview'),
  /** The server's own count for a session — the number the cap is enforced on. */
  session: (id: string) =>
    req<SessionUsage>('/v1/session', { headers: { 'X-MI-Session': id } }),
  /** The server-stored transcript for a session, oldest first. */
  messages: (id: string) =>
    req<{ session_id: string; messages: TranscriptMessage[] }>('/v1/session/messages', {
      headers: { 'X-MI-Session': id },
    }),
  /** Erase the transcript. The spend ledger is deliberately left alone. */
  clearMessages: (id: string) =>
    req<{ session_id: string; cleared: number }>('/v1/session/messages', {
      method: 'DELETE',
      headers: { 'X-MI-Session': id },
    }),
  plan: (task: string) => req<Plan>(`/v1/plan?task=${encodeURIComponent(task)}`),
  chat: (
    body: {
      model: string
      messages: { role: string; content: string }[]
      mi_options?: number
    },
    /** `signal` lets the caller abort an in-flight request (the Stop button). */
    init?: { signal?: AbortSignal },
  ) =>
    req<ChatResponse>('/v1/chat/completions', {
      method: 'POST',
      body: JSON.stringify(body),
      ...init,
    }),
  approve: (body: { task: string; chosen: string; rejected: string[]; decision_id?: number }) =>
    req<{ ok: boolean; decision_id?: number }>('/v1/approve', { method: 'POST', body: JSON.stringify(body) }),
  routeVerdict: (body: { deploy_id: string; task: string; approved: boolean; reason?: string }) =>
    req<{ ok: boolean }>('/v1/route-verdict', { method: 'POST', body: JSON.stringify(body) }),
  /**
   * Fold older turns into a short brief. The caller caches the result and sends
   * it instead of the transcript, so the saving compounds over a conversation.
   */
  compact: (messages: { role: string; content: string }[], summary?: string) =>
    req<{ summary: string; model: string | null; compacted: number }>('/v1/compact', {
      method: 'POST',
      body: JSON.stringify({ messages, summary }),
    }),
  providers: () => req<{ providers: ProviderInfo[] }>('/v1/providers'),
  /** The review queue for pricing disagreements. `status=""` is the whole history. */
  anomalies: (status = 'open') =>
    req<{ status: string; count: number; anomalies: PricingAnomaly[] }>(
      `/v1/economics/anomalies?status=${encodeURIComponent(status)}`),
  decideAnomaly: (anomaly_id: string, status: 'acknowledged' | 'resolved', note = '') =>
    req<{ ok: boolean; anomaly_id: string; status: string }>('/v1/anomalies/decide', {
      method: 'POST',
      body: JSON.stringify({ anomaly_id, status, note }),
    }),
  /**
   * Import the declared free-tier limits from `config/quotas.yaml`. The Docker
   * entrypoint does this after its bootstrap ingest; a native install does not, so
   * the dashboard offers it as a one-click action.
   */
  seedQuotas: (opts: { config?: string; dry_run?: boolean } = {}) =>
    req<{
      ok: boolean
      dry_run: boolean
      entries: number
      buckets: number
      matched: Record<string, number>
    }>('/v1/quota/seed', { method: 'POST', body: JSON.stringify(opts) }),
  // Reviews, anomalies and the pricing-state distribution all arrive on
  // `economics()` now, so the page reads them from the same moment as the prices
  // they describe. The `/v1/reviews` endpoint remains for API clients.
  decideReview: (deploy_id: string, approve: boolean) =>
    req<{ ok: boolean; status: string }>('/v1/reviews/decide', {
      method: 'POST',
      body: JSON.stringify({ deploy_id, approve }),
    }),
  /** Browse the registry itself. Read-only, no upstream calls. */
  explore: (params: ExploreQuery = {}) =>
    req<ExploreResult>(`/v1/models/explore${exploreQueryString(params)}`),
  probeLocal: (engine: 'ollama' | 'llamacpp' = 'ollama', url?: string) =>
    req<LocalProbeResult>(`/v1/local/probe?engine=${engine}${url ? `&url=${encodeURIComponent(url)}` : ''}`),
  registerLocal: (engine: 'ollama' | 'llamacpp', models: string[]) =>
    req<{ ok: boolean; registered: string[] }>('/v1/local/register', {
      method: 'POST',
      body: JSON.stringify({ engine, models }),
    }),
  testProvider: (provider: string, apiKey?: string) =>
    req<{
      ok: boolean
      provider: string
      deploy_id: string
      model: string
      is_free: boolean
      /** Which credential the test used: 'custom' | 'env' | 'none'. */
      key_source?: 'custom' | 'env' | 'none'
      latency_ms: number | null
      reply: string | null
      error_class: string | null
      error_detail: string | null
    }>('/v1/providers/test', {
      method: 'POST',
      body: JSON.stringify({ provider, api_key: apiKey }),
    }),
  /**
   * Verify a provider key and, only if the provider accepts it, persist it to the
   * server's `.env`. `status` is `already_set` when a value was already there and
   * was deliberately left untouched.
   */
  saveProviderKey: (provider: string, apiKey: string) =>
    req<{
      ok: boolean
      stored: boolean
      status: 'created' | 'written' | 'already_set'
      provider: string
      env_var: string
      reply: string
    }>('/v1/keys', {
      method: 'POST',
      body: JSON.stringify({ provider, api_key: apiKey }),
    }),
  trialModel: (deploy_id: string, task = 'general_chat', prompt?: string) =>
    req<{
      ok: boolean
      deploy_id: string
      task: string
      latency_ms: number | null
      judge_reason: string
      reply: string | null
      error_class: string | null
      error_detail: string | null
    }>('/v1/models/trial', {
      method: 'POST',
      body: JSON.stringify({ deploy_id, task, prompt }),
    }),

  /**
   * Warm tier — the optional background provider probe. `status` carries the
   * on/off, cadence, and the last health verdict per provider.
   */
  probeStatus: () => req<ProbeStatus>('/v1/probe'),
  setProbeConfig: (cfg: { enabled?: boolean; interval_seconds?: number }) =>
    req<ProbeStatus>('/v1/probe/config', {
      method: 'POST',
      body: JSON.stringify(cfg),
    }),
  runProbe: () =>
    req<ProbeRunResult>('/v1/probe/run', { method: 'POST' }),
  /** The model form guide: an agent-written dossier per free arm. */
  dossiers: (q?: string, limit?: number) => {
    const params = new URLSearchParams()
    if (q) params.set('q', q)
    if (limit) params.set('limit', String(limit))
    const qs = params.toString()
    return req<{ count: number; dossiers: Dossier[] }>(`/v1/dossiers${qs ? `?${qs}` : ''}`)
  },
  /** The free tier, each arm with its agent-written summary when scouted. */
  freeModels: (q?: string, limit?: number) => {
    const params = new URLSearchParams()
    if (q) params.set('q', q)
    if (limit) params.set('limit', String(limit))
    const qs = params.toString()
    return req<{ count: number; models: FreeModel[] }>(`/v1/free-models${qs ? `?${qs}` : ''}`)
  },
  /** Run one scout pass now (admin). `search` toggles the web search per arm. */
  dossiersRefresh: (body: { search?: boolean; force?: boolean; limit?: number } = {}) =>
    req<ScoutSummary>('/v1/dossiers/refresh', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
}

export interface ProbeHealth {
  provider: string
  /** `ok` | `auth_error` | `network_error` | `tls_error` | `http_NNN` */
  status: string
  detail: string | null
  latency_ms: number | null
  n_models: number | null
  checked_at: string
}

export interface ProbeStatus {
  enabled: boolean
  interval_seconds: number
  last_run: string | null
  /** How long a verdict is trusted before it stops excluding a provider. */
  ttl_seconds: number
  /** Providers the server currently holds a key for (what the probe will check). */
  configured: string[]
  health: Record<string, ProbeHealth>
}

export interface ProbeRunResult {
  checked: number
  results: ProbeHealth[]
  last_run: string | null
}

/**
 * A free model plus its agent-written summary (`GET /v1/free-models`). The
 * dossier fields are null until the scout has reached the arm.
 */
export interface FreeModel {
  deploy_id: string
  provider: string
  provider_model_id: string
  price_in: number | null
  price_out: number | null
  context_window: number | null
  status: string
  pricing_state: string | null
  display_name: string | null
  family: string | null
  params_b: number | null
  benchmark: string | null
  core_competency: string | null
  summary: string | null
  warrior: string | null
  story: string | null
  strengths: string | null
  weaknesses: string | null
  when_to_use: string | null
  when_not_to_use: string | null
  best_for: string | null
  search_sources: string | null
  confidence: number | null
  generated_at: string | null
}

/** The result of one `mi scout` pass (`POST /v1/dossiers/refresh`). */
export interface ScoutSummary {
  scanned: number
  written: number
  skipped_fresh: number
  errors: number
}

/** One row of the model form guide (`/v1/dossiers`). */
export interface Dossier {
  deploy_id: string
  weights_id: string | null
  display_name: string | null
  provider: string | null
  price_out: number | null
  core_competency: string | null
  summary: string | null
  warrior: string | null
  story: string | null
  strengths: string | null // JSON array
  weaknesses: string | null // JSON array
  when_to_use: string | null // JSON array
  when_not_to_use: string | null // JSON array
  best_for: string | null // JSON array
  search_sources: string | null // JSON array
  facts: string | null // JSON object
  confidence: number | null
  generated_at: string | null
}

