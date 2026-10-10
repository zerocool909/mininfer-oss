/**
 * Chat state: the message types, the compaction rules, and persistence.
 *
 * All three live together because they are one concern — what a turn *is*, how
 * much of it is worth re-sending, and where it survives a refresh. Keeping the
 * compaction here rather than in the component means it is a pure function of the
 * turn list, which is the only way to reason about it.
 */
import type { LeaderboardTag, TranscriptMessage } from '@/lib/api'
import type { SearchState } from '@/lib/sse'

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export interface Meta {
  model: string
  cost: number | null
  pLb?: number | null
  tags: LeaderboardTag[]
  alternatives?: string[]
  /** The winning arm is on trial: free, under-observed, and a comparison exists. */
  needsApproval?: boolean
  task?: string
  policy?: string
  /** What this turn did with the live web (grounding), if search was considered. */
  search?: SearchState | null
  /** Prompt complexity the arm was chosen under, straight from the route frame. */
  complexity?: ComplexityMeta | null
  /** The low-complexity arm failed validation and the router retried a tier up. */
  complexityEscalated?: boolean
}

/**
 * What the complexity estimator decided, as routed.
 *
 * Arrives in the streaming `route` frame (`complexity`), because the fuller
 * `mi.reason` block is only on the non-streaming response and the playground
 * always streams. `signals` are the cue names that fired, so a wrong call is
 * debuggable — the same contract `intent` keeps.
 */
export interface ComplexityMeta {
  level: string
  needs_reasoning: boolean
  confidence: number
  score: number
  signals: string[]
  source: string
  reason?: string
}

/**
 * Split a deploy id into its provider and its bare model name.
 *
 * `groq:qwen/qwen3.8-27b` -> { provider: 'groq', model: 'qwen3.8-27b' }: the
 * provider badge already implies the vendor namespace, so repeating it reads as
 * a mistake. Shared so the header above an answer and the telemetry line under
 * it cannot disagree about the name.
 */
export function splitModelId(modelId: string): { provider: string | null; model: string } {
  let provider: string | null = null
  let model = modelId
  if (modelId.includes(':')) {
    const i = modelId.indexOf(':')
    provider = modelId.slice(0, i)
    model = modelId.slice(i + 1)
  } else if (modelId.includes('/')) {
    const i = modelId.indexOf('/')
    provider = modelId.slice(0, i)
    model = modelId.slice(i + 1)
  }
  if (provider && model.includes('/')) {
    const [ns, rest] = model.split('/', 2)
    if (provider.toLowerCase().endsWith(ns.toLowerCase())) model = rest
  }
  return { provider, model }
}

/** Why a request failed, kept so the transcript can explain itself later. */
export interface TurnError {
  kind: string
  message: string
  at: number
  /** The request was cancelled by the user, which is not a failure of ours. */
  aborted?: boolean
}

export interface Turn {
  id: string
  role: 'user' | 'assistant'
  text: string
  streaming?: boolean
  meta?: Meta
  verdict?: 'up' | 'down' | null
  routingVerdict?: 'up' | 'down' | null
  /** Wall-clock start of this turn's request, for the live timer. */
  t0?: number
  /** Settled elapsed time in ms; undefined while still running. */
  ms?: number
  /** The user text that produced this answer, so it can be re-asked. */
  prompt?: string
  /** Set on a compare-mode answer: which option was chosen. */
  picked?: boolean
  /** Shared id across the answers of one compare round. */
  groupId?: string
  /** The other deploy_ids in this compare round, sent to `/v1/approve`. */
  siblings?: string[]
  /** This arm could not be called at all (it gets a slot, not an answer). */
  armError?: string
  /** Tokens this answer cost, when the upstream reported usage. */
  tokens?: number
  /** ID of the router decision record, for linking human feedback. */
  decisionId?: number
  /** Why this request failed. The turn is kept, not deleted. */
  error?: TurnError
}

// --------------------------------------------------------------------------- #
// compaction
// --------------------------------------------------------------------------- #
// A follow-up is unanswerable if the model cannot see what it follows up on — the
// chat used to send `[{role:'user'}]` and nothing else, so "and the other one?"
// arrived with no "other one" in it.
//
// The whole transcript cannot simply be re-sent either: assistant answers are the
// token hogs and an unbounded history walks into the context window (and the
// session's token budget) a few turns in. So the window is explicit and the
// truncation is explicit.

/** How many turns are re-sent verbatim. Older ones are elided, not summarised. */
export const KEEP_TURNS = 5

/** Per-answer character cap. Answers are what get truncated; questions are not. */
export const MAX_ANSWER_CHARS = 1200

/** Longest opening-question quote kept as the anchor when older turns are elided. */
const ANCHOR_CHARS = 200

function clip(text: string, max: number): string {
  const s = (text ?? '').trim()
  if (s.length <= max) return s
  // Head and tail rather than a plain prefix: a conclusion usually lives at the
  // end, and cutting only there loses the framing, only here loses the answer.
  const head = Math.floor(max * 0.7)
  const tail = max - head
  return `${s.slice(0, head)}\n…\n${s.slice(-tail)}`
}

/**
 * Turns worth re-sending: non-empty, not failed, and one per compare round.
 *
 * A compare round produces two assistant turns for one question. Sending both
 * would present the model with two different answers to a single prompt, so only
 * the chosen one is kept (or the first, if the user has not chosen).
 */
export function usableTurns(turns: Turn[]): Turn[] {
  const out: Turn[] = []
  const seenGroup = new Map<string, number>()
  for (const t of turns) {
    if (!(t.text ?? '').trim()) continue
    if (t.error || t.armError) continue
    if (t.streaming) continue
    if (t.groupId) {
      const at = seenGroup.get(t.groupId)
      if (at !== undefined) {
        // Replace the earlier same-group turn only if this one was picked.
        if (t.picked) out[at] = t
        continue
      }
      seenGroup.set(t.groupId, out.length)
    }
    out.push(t)
  }
  return out
}

/**
 * The history to send alongside a new question.
 *
 * `keep` of the most recent turns are sent, answers clipped to `maxChars`; anything
 * older is dropped and replaced by one line naming how much was dropped and what
 * the conversation started on, so a compacted thread keeps its subject.
 */
export function compactHistory(
  turns: Turn[],
  { keep = KEEP_TURNS, maxChars = MAX_ANSWER_CHARS, summary = '' }: {
    keep?: number
    maxChars?: number
    summary?: string
  } = {},
): ChatMessage[] {
  const usable = usableTurns(turns)
  if (!usable.length) return []
  const window = keep > 0 ? usable.slice(-keep) : []
  const elided = usable.length - window.length
  const out: ChatMessage[] = []

  if (elided > 0) {
    // A real summary beats a "N messages omitted" note: the note tells the model
    // that context is missing, the summary tells it what the context *was*.
    if (summary.trim()) {
      out.push({
        role: 'system',
        content: `Earlier in this conversation (compacted summary):\n${summary.trim()}`,
      })
    } else {
      const firstUser = usable.find((t) => t.role === 'user')
      const anchor = firstUser ? clip(firstUser.text, ANCHOR_CHARS) : ''
      out.push({
        role: 'system',
        content:
          `${elided} earlier message${elided === 1 ? '' : 's'} in this conversation ` +
          `were omitted to stay within context.` +
          (anchor ? ` The conversation began with: ${anchor}` : ''),
      })
    }
  }

  for (const t of window) {
    out.push({
      role: t.role,
      content: t.role === 'assistant' ? clip(t.text, maxChars) : (t.text ?? '').trim(),
    })
  }
  return out
}

/** History plus the new question — what actually goes on the wire. */
export function buildMessages(
  turns: Turn[],
  question: string,
  opts?: { keep?: number; maxChars?: number; summary?: string },
): ChatMessage[] {
  return [...compactHistory(turns, opts), { role: 'user', content: question.trim() }]
}

// --------------------------------------------------------------------------- #
// persistence
// --------------------------------------------------------------------------- #

const KEY = 'mininfer.chat.v1'
const LEGACY_KEY = 'mininfer.chat.v1'
const MAX_SESSIONS = 20
const MAX_TURNS = 200

export interface StoredSession {
  id: string
  title: string
  updatedAt: number
  turns: Turn[]
  /** A brief covering the turns before the re-sent window, from `/v1/compact`. */
  summary?: string
  /** How many of the oldest usable turns that brief already covers. */
  summaryCount?: number
}

interface Stored {
  current: string | null
  sessions: Record<string, StoredSession>
}

function emptyStore(): Stored {
  return { current: null, sessions: {} }
}

/**
 * localStorage, but never fatal.
 *
 * Private browsing and a full quota both throw on write. A chat that refuses to
 * render because history could not be saved is worse than one that forgets, so
 * every access is guarded and a failure degrades to in-memory only.
 */
function read(): Stored {
  try {
    const raw = localStorage.getItem(KEY) ?? localStorage.getItem(LEGACY_KEY)
    if (!raw) return emptyStore()

    const parsed = JSON.parse(raw) as Stored
    if (!parsed || typeof parsed !== 'object' || !parsed.sessions) return emptyStore()
    return parsed
  } catch {
    return emptyStore()
  }
}

function write(store: Stored): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(store))
  } catch {
    /* quota or private mode: keep the session in memory and carry on */
  }
}

export function newSessionId(): string {
  try {
    return crypto.randomUUID()
  } catch {
    return `s-${Date.now()}-${Math.floor(Math.random() * 1e6)}`
  }
}

export function listSessions(): StoredSession[] {
  return Object.values(read().sessions).sort((a, b) => b.updatedAt - a.updatedAt)
}

export function currentSessionId(): string | null {
  const store = read()
  if (store.current && store.sessions[store.current]) return store.current
  const newest = listSessions()[0]
  return newest ? newest.id : null
}

/** Title from the first user turn, which is the only naming signal there is. */
function titleFor(turns: Turn[]): string {
  const first = turns.find((t) => t.role === 'user' && (t.text ?? '').trim())
  if (!first) return 'New chat'
  const one = first.text.trim().replace(/\s+/g, ' ')
  return one.length > 48 ? `${one.slice(0, 48)}…` : one
}

export function saveSession(id: string, turns: Turn[]): void {
  const store = read()
  store.current = id
  const existing = store.sessions[id]
  store.sessions[id] = {
    id,
    title: titleFor(turns),
    updatedAt: Date.now(),
    // Keep the tail: an unbounded transcript is a quota error waiting to happen.
    turns: turns.slice(-MAX_TURNS),
    // The summary lives beside the turns, so saving the transcript must not drop
    // it — this function replaces the whole record.
    summary: existing?.summary,
    summaryCount: existing?.summaryCount,
  }
  const ids = Object.keys(store.sessions)
  if (ids.length > MAX_SESSIONS) {
    ids
      .sort((a, b) => (store.sessions[a].updatedAt ?? 0) - (store.sessions[b].updatedAt ?? 0))
      .slice(0, ids.length - MAX_SESSIONS)
      .forEach((old) => delete store.sessions[old])
  }
  write(store)
}

export function loadSession(id: string): Turn[] {
  return read().sessions[id]?.turns ?? []
}

export function setCurrentSession(id: string): void {
  const store = read()
  store.current = id
  write(store)
}

export function deleteSession(id: string): void {
  const store = read()
  delete store.sessions[id]
  if (store.current === id) store.current = null
  write(store)
}

export function renameSession(id: string, newTitle: string): void {
  const store = read()
  if (store.sessions[id]) {
    store.sessions[id].title = newTitle.trim() || 'Untitled'
    store.sessions[id].updatedAt = Date.now()
    write(store)
  }
}

/** The compacted brief for a session, and how many turns it already covers. */
export function sessionSummary(id: string): { text: string; count: number } {
  const s = read().sessions[id]
  return { text: s?.summary ?? '', count: s?.summaryCount ?? 0 }
}

export function setSessionSummary(id: string, summary: string, count: number): void {
  const store = read()
  const s = store.sessions[id]
  if (!s) return
  s.summary = summary
  s.summaryCount = count
  write(store)
}

/** Every failure in a transcript, newest first — the request log. */
export function failures(turns: Turn[]): { turn: Turn; error: TurnError }[] {
  return turns
    .filter((t): t is Turn & { error: TurnError } => Boolean(t.error))
    .map((t) => ({ turn: t, error: t.error }))
    .reverse()
}

// --------------------------------------------------------------------------- #
// server transcript -> local turns
// --------------------------------------------------------------------------- #

/**
 * A server-side transcript as local turns.
 *
 * The server stores a flat role/content stream; the UI wants `Turn`s with ids and
 * metadata. `system` and `tool` rows are dropped — they are request plumbing (the
 * compaction line, a tool call), not conversation, and rendering them would put
 * the router's own bookkeeping into the visible thread.
 *
 * Used only when localStorage has nothing: the local copy is the fast path and
 * carries metadata (cost, leaderboards, verdicts) the server does not store.
 */
export function serverTurns(messages: TranscriptMessage[]): Turn[] {
  const out: Turn[] = []
  for (const m of messages ?? []) {
    if (m.role !== 'user' && m.role !== 'assistant') continue
    if (!(m.content ?? '').trim()) continue
    out.push({
      id: `srv-${m.id ?? out.length}`,
      role: m.role,
      text: m.content,
      meta: m.deploy_id
        ? { model: m.deploy_id, cost: null, tags: [], task: m.task ?? undefined }
        : undefined,
    })
  }
  return out
}
