'use client'

import {
  IconAlertTriangle,
  IconArrowUp,
  IconHistory,
  IconMessagePlus,
  IconPlayerStop,
  IconCheck,
  IconChevronDown,
  IconCopy,
  IconDots,
  IconPencil,
  IconPin,
  IconPlus,
  IconRefresh,
  IconScale,
  IconThumbDown,
  IconThumbUp,
  IconX,
} from '@tabler/icons-react'
import type React from 'react'
import { useEffect, useRef, useState } from 'react'
import { Bubble, BubbleContent } from '@/components/ui/chat-02-utils/bubble'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Message, MessageContent, MessageFooter } from '@/components/ui/chat-02-utils/message'
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerTopButton,
  MessageScrollerViewport,
} from '@/components/ui/chat-02-utils/message-scroller'
import { Textarea } from '@/components/ui/textarea'
import { CollapsibleMarkdown, CollapsibleQuestion } from '@/components/ui/chat-02-utils/collapsible'
import { ModelIdentity } from '@/components/chat/ModelIdentity'
import { api, type LeaderboardTag, type SessionUsage } from '@/lib/api'
import { getUserKeysHeader } from '@/lib/keys'
import { onCommand } from '@/lib/bus'
import { DONE, createSseReader, deltaText, miFrame, jsonCompletion, streamHeaders } from '@/lib/sse'
import {
  buildMessages,
  currentSessionId,
  deleteSession,
  renameSession,
  failures,
  listSessions,
  loadSession,
  newSessionId,
  saveSession,
  serverTurns,
  sessionSummary,
  setCurrentSession,
  setSessionSummary,
  usableTurns,
  KEEP_TURNS,
  type Meta,
  type StoredSession,
  type Turn,
  type TurnError,
} from '@/lib/chat'
import { ChatSidebar } from '@/components/chat/ChatSidebar'
import { TelemetryHud } from '@/components/chat/TelemetryHud'
import { EmptyState } from '@/components/chat/EmptyState'
import { Cpu } from 'lucide-react'
import { cn } from '@/lib/utils'

const easeOut = 'ease-swift'
const press = `${easeOut} transition-[scale,background-color] duration-150 active:scale-[0.96] motion-reduce:active:scale-100`
const enter = `${easeOut} motion-reduce:slide-in-from-bottom-0 fade-in slide-in-from-bottom-1 animate-in duration-200`
const item = 'rounded-lg px-2.5 py-1.75 text-[13px]'

/**
 * A failure worth logging, carrying the router's own error type.
 *
 * `AbortError` is separated out because a user pressing Stop is not a failure of
 * the system, and it should not read like one in the transcript.
 */
class RequestFailure extends Error {
  constructor(readonly kind: string, message: string, readonly aborted = false) {
    super(message)
  }
}

function requestError(kind: string, message: string): RequestFailure {
  return new RequestFailure(kind, message)
}

function asTurnError(err: unknown): TurnError {
  const at = Date.now()
  if (err instanceof RequestFailure) {
    return { kind: err.kind, message: err.message, at, aborted: err.aborted }
  }
  if (err instanceof DOMException && err.name === 'AbortError') {
    return { kind: 'aborted', message: 'Stopped by the user.', at, aborted: true }
  }
  const e = err as Error
  return { kind: e?.name || 'error', message: e?.message || String(err), at }
}

/** Sub-second in ms, above that in seconds — 1400 ms is harder to read than 1.4 s. */
function formatMs(n: number): string {
  return n < 1000 ? `${Math.round(n)} ms` : `${(n / 1000).toFixed(1)} s`
}

/**
 * Elapsed time that ticks while a request is in flight.
 *
 * A spinner alone cannot distinguish "thinking for a long time" from "nothing is
 * happening"; a number that keeps moving can, and it is the same number the
 * answer settles on when it finishes.
 */
function Elapsed({ since, running }: { since?: number; running: boolean }) {
  const [now, setNow] = useState(() => performance.now())
  useEffect(() => {
    if (!running || since === undefined) return
    const id = window.setInterval(() => setNow(performance.now()), 100)
    return () => window.clearInterval(id)
  }, [running, since])
  if (since === undefined) return null
  return <span className="tnum">{formatMs(Math.max(0, now - since))}</span>
}

/**
 * A failed request, rendered in place.
 *
 * The transcript used to *delete* the assistant turn on failure, so the only
 * trace of a broken request was a line under the composer that the next attempt
 * overwrote. Keeping the turn is what makes the log a log.
 */
function ErrorCard({ error, task }: { error: TurnError; task?: string }) {
  return (
    <div className="rounded-[18px] border border-destructive/40 bg-destructive/5 px-3.5 py-2.5">
      <div className="flex items-center gap-2 text-[12.5px] font-medium text-destructive">
        {error.aborted ? 'Request stopped' : 'Request failed'}
        <span className="rounded-full border border-destructive/30 px-1.5 py-px font-mono text-[10px] font-normal">
          {error.kind}
        </span>
        {task && <span className="font-mono text-[10px] font-normal">{task}</span>}
      </div>
      <p className="mt-1 font-mono text-[11.5px] leading-5 break-words text-muted-foreground">
        {error.message}
      </p>
      <p className="mt-0.5 text-[10.5px] text-muted-foreground">
        {new Date(error.at).toLocaleTimeString()}
      </p>
    </div>
  )
}

/** What the session counter actually consists of. Model calls are free here often
 *  enough that tokens alone would hide the spend that matters. */
function sessionBudgetTitle(u: SessionUsage): string {
  const parts = [
    `${u.calls} model call${u.calls === 1 ? '' : 's'}`,
    `${u.tokens_in.toLocaleString()} tokens in / ${u.tokens_out.toLocaleString()} out`,
  ]
  if (u.searches) {
    parts.push(`${u.searches} search(es) at $${u.search_cost_usd.toFixed(4)}`)
  }
  parts.push(`model spend $${u.model_cost_usd.toFixed(4)}`)
  if (u.savings && u.savings.saved_usd > 0) {
    parts.push(`avoided ~$${u.savings.saved_usd.toFixed(4)} at the cheapest paid sibling`)
  }
  if (u.savings && u.savings.unpriced_free_calls > 0) {
    parts.push(`${u.savings.unpriced_free_calls} free call(s) had no paid sibling to price`)
  }
  if (u.limit) parts.push(`token budget ${u.limit.toLocaleString()}`)
  if (u.cost_limit) parts.push(`spend budget $${u.cost_limit.toFixed(2)}`)
  return parts.join('\n')
}

export default function Chat02({
  testTarget,
  onTestConsumed,
}: {
  testTarget?: { deployId: string; task: string } | null
  onTestConsumed?: () => void
} = {}) {
  const [turns, setTurns] = useState<Turn[]>([])
  const [sessions, setSessions] = useState<StoredSession[]>([])
  const [sessionId, setSessionId] = useState<string>('')
  const [draft, setDraft] = useState('')
  const composerRef = useRef<HTMLTextAreaElement>(null)
  /** A deployment pinned from the Overview's "Test" button: the next request
   *  goes to it by id instead of being routed. */
  const [pinned, setPinned] = useState<string | null>(null)
  /** The compacted brief for this session, and how many turns it covers. */
  const summaryRef = useRef<{ text: string; count: number }>({ text: '', count: 0 })
  const compactBusy = useRef(false)
  const [tasks, setTasks] = useState<string[]>([])
  const [task, setTask] = useState('auto')
  const [compare, setCompare] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [ms, setMs] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** The server's tally for this session. Read from the server, never counted
   *  locally: a local count drifts from the number the cap actually enforces. */
  const [usage, setUsage] = useState<SessionUsage | null>(null)
  const [edit, setEdit] = useState<{ id: string; text: string } | null>(null)
  const tasksLoaded = useRef(false)
  /** Aborts the in-flight request. Replaced per request, so Stop can never
   *  cancel the wrong one. */
  const abortRef = useRef<AbortController | null>(null)

  // Restore the previous conversation on mount. A refresh used to destroy the
  // whole thread, which also meant a failed request left no evidence behind.
  useEffect(() => {
    const id = currentSessionId() ?? newSessionId()
    setSessionId(id)
    setTurns(loadSession(id))
    setSessions(listSessions())
    summaryRef.current = sessionSummary(id)
    hydrate(id)
  }, [])

  useEffect(() => {
    if (tasksLoaded.current) return
    tasksLoaded.current = true
    api
      .stats()
      .then((s) => setTasks(s.tasks))
      .catch(() => undefined)
  }, [])

  const patch = (id: string, next: Partial<Turn>) =>
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...next } : t)))

  /**
   * What actually goes on the wire: the cached brief for anything that has
   * fallen out of the window, then the last few turns verbatim.
   */
  const messagesFor = (prior: Turn[], question: string) =>
    buildMessages(prior, question, { summary: summaryRef.current.text })

  /** Cost + leaderboards are not in the SSE stream, so resolve them after. */
  async function metaFor(
    deploy: string,
    resolvedTask: string,
    needsApproval = false,
    existingAlternatives?: string[],
  ): Promise<Meta> {
    try {
      const plan = await api.plan(resolvedTask)
      let effectiveDeploy = deploy
      if (!effectiveDeploy || !plan.chosen.some((c) => c.deploy_id === effectiveDeploy)) {
        if (plan.chosen.length > 0) {
          effectiveDeploy = plan.chosen[0].deploy_id
        }
      }
      const s = plan.chosen.find((c) => c.deploy_id === effectiveDeploy)
      const rawAlts = (existingAlternatives && existingAlternatives.length > 0)
        ? existingAlternatives
        : plan.chosen.map((c) => c.deploy_id)
      const alts = rawAlts.filter((c) => c && c !== effectiveDeploy)
      return {
        model: effectiveDeploy,
        cost: s?.cost_per_success ?? null,
        pLb: s?.p_lb ?? null,
        tags: s?.leaderboards ?? [],
        alternatives: alts,
        needsApproval,
        task: resolvedTask,
      }
    } catch {
      return {
        model: deploy,
        cost: null,
        tags: [],
        alternatives: (existingAlternatives ?? []).filter((c) => c && c !== deploy),
        needsApproval,
        task: resolvedTask,
      }
    }
  }

  async function streamInto(
    id: string,
    messages: { role: string; content: string }[],
    signal?: AbortSignal,
  ) {
    const t0 = performance.now()
    patch(id, { t0, ms: undefined, streaming: true })
    const clientTz = typeof Intl !== 'undefined' ? Intl.DateTimeFormat().resolvedOptions().timeZone : ''
    const userKeysHeader = getUserKeysHeader()
    const res = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-MI-Session': sessionId,
        'X-Client-Timezone': clientTz,
        'X-MI-Client': 'playground',
        ...userKeysHeader,
      },
      body: JSON.stringify({ model: pinned ?? task, messages, stream: true, max_tokens: 2048 }),
      signal,
    })

    if (!res.ok || !res.body) {
      const j = await res.json().catch(() => null)
      const e = j?.error
      // `type` is the router's own classification (`session_budget_exceeded`,
      // `no_api_key`, …) and is more useful in the log than the HTTP status.
      throw requestError(e?.type ?? `http_${res.status}`, e?.message ?? `HTTP ${res.status}`)
    }
    const { deploy, task: resolvedTask, policy, needsApproval, alternatives: initialAlts } =
      streamHeaders(res.headers)
    let currentAlternatives = initialAlts
    let currentTask = resolvedTask
    const decHeader = res.headers.get('X-MI-Decision-Id') ?? res.headers.get('X-MI-Decision-Id')
    const decisionId = decHeader ? parseInt(decHeader, 10) : undefined
    let currentDeploy = deploy || pinned || ''
    patch(id, {
      decisionId,
      meta: { model: currentDeploy, cost: null, tags: [], alternatives: currentAlternatives, needsApproval,
              task: currentTask || task, policy: policy || undefined },
    })

    const read = createSseReader()
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let raw = ''
    let text = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      const chunk = decoder.decode(value, { stream: true })
      raw += chunk
      for (const payload of read(chunk)) {
        if (payload === DONE) continue
        try {
          const parsed = JSON.parse(payload)
          const frame = miFrame(parsed)
          if (frame && (frame.deploy_id || (frame as Record<string, unknown>).deploy || (frame.event === 'route' && (frame as Record<string, unknown>).model))) {
            const chosen = frame.deploy_id || (frame as Record<string, unknown>).deploy || (frame as Record<string, unknown>).model || ''
            if (chosen && typeof chosen === 'string') {
              currentDeploy = chosen
              const fAlts = (frame as Record<string, unknown>).alternatives
              if (Array.isArray(fAlts) && fAlts.length > 0) {
                currentAlternatives = fAlts as string[]
              }
              if (frame.task) currentTask = frame.task
              patch(id, {
                meta: { model: currentDeploy, cost: null, tags: [], alternatives: currentAlternatives, needsApproval,
                        task: currentTask || task, policy: policy || undefined },
              })
            }
          } else if (!currentDeploy && parsed.model) {
            currentDeploy = parsed.model
            patch(id, {
              meta: { model: currentDeploy, cost: null, tags: [], alternatives: currentAlternatives, needsApproval,
                      task: currentTask || task, policy: policy || undefined },
            })
          }
          const delta = deltaText(parsed)
          if (delta) {
            text += delta
            patch(id, { text, streaming: true })
          }
        } catch {
          /* unparseable frame: the payload is retained in `raw` for the fallback */
        }
      }
    }
    // An upstream that ignored `stream` answers with one JSON body; left alone
    // that reads as an empty reply.
    if (!text) text = jsonCompletion(raw)
    if (!currentDeploy) {
      try {
        const j = JSON.parse(raw)
        if (j.model) currentDeploy = j.model
      } catch {
        /* ignore parse error */
      }
    }
    const finalDeploy = currentDeploy || deploy || pinned || ''
    patch(id, { text, streaming: false, ms: performance.now() - t0,
                meta: await metaFor(finalDeploy, currentTask || task, needsApproval, currentAlternatives) })
  }

  const compareTurn = async (turn: Turn) => {
    if (!turn.prompt || busy) return
    const at = turns.findIndex((t) => t.id === turn.id)
    const messages = messagesFor(turns.slice(0, at), turn.prompt)
    const signal = beginRequest()
    setBusy(true)
    setError(null)
    try {
      await compareInto(turn.id, messages, signal)
    } catch (err) {
      const failure = asTurnError(err)
      patch(turn.id, { streaming: false, error: failure })
      if (!failure.aborted) setError(failure.message)
    } finally {
      setBusy(false)
      abortRef.current = null
    }
  }

  /** Record a compare-mode choice. The pick is what ends a free arm's trial. */
  const pick = async (turn: Turn) => {
    setTurns((prev) =>
      prev.map((t) =>
        t.groupId && t.groupId === turn.groupId ? { ...t, picked: t.id === turn.id } : t,
      ),
    )
    await api
      .approve({
        task: turn.meta?.task ?? 'unrouted',
        chosen: turn.meta?.model ?? '',
        rejected: turn.siblings ?? [],
        decision_id: turn.decisionId,
      })
      .catch(() => undefined)
  }

  /**
   * Compare mode: one connection, both arms, streamed concurrently.
   *
   * The frames are tagged by arm (`mininfer.event` / `mininfer.arm`), so the two
   * answers interleave as they are written instead of arriving together at the
   * end. The arm announcement also carries cost, p(success) and leaderboards, so
   * this needs no follow-up `/v1/plan` round trip to label the answers.
   */
  async function compareInto(
    placeholderId: string,
    messages: { role: string; content: string }[],
    signal?: AbortSignal,
  ) {
    const t0 = performance.now()
    patch(placeholderId, { t0, ms: undefined, streaming: true, text: '' })
    const clientTz = typeof Intl !== 'undefined' ? Intl.DateTimeFormat().resolvedOptions().timeZone : ''
    const userKeysHeader = getUserKeysHeader()
    const res = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-MI-Session': sessionId,
        'X-Client-Timezone': clientTz,
        'X-MI-Client': 'playground',
        ...userKeysHeader,
      },
      body: JSON.stringify({ model: task, messages, stream: true, mi_options: 2 }),
      signal,
    })
    if (!res.ok || !res.body) {
      const j = await res.json().catch(() => null)
      const e = j?.error
      throw requestError(e?.type ?? `http_${res.status}`, e?.message ?? `HTTP ${res.status}`)
    }
    const resolvedTask = res.headers.get('X-MI-Task') ?? res.headers.get('X-MI-Task') ?? task
    const policy = res.headers.get('X-MI-Policy') ?? res.headers.get('X-MI-Policy') ?? ''
    const decHeader = res.headers.get('X-MI-Decision-Id') ?? res.headers.get('X-MI-Decision-Id')
    const decisionId = decHeader ? parseInt(decHeader, 10) : undefined

    const prompt = messages[messages.length - 1]?.content

    type Arm = {
      deploy: string
      cost: number | null
      pLb: number | null
      tags: LeaderboardTag[]
      text: string
      ms?: number
      tokens?: number
      error?: string
      decisionId?: number
    }
    const arms = new Map<number, Arm>()

    const sync = () => {
      const list = [...arms.entries()].sort((a, b) => a[0] - b[0])
      const ids = list.map(([, a]) => a.deploy)
      setTurns((prev) => [
        ...prev.filter((t) => t.id !== placeholderId && !t.id.startsWith(`${placeholderId}-`)),
        ...list.map(([i, a]) => ({
          id: `${placeholderId}-${i}`,
          role: 'assistant' as const,
          text: a.text,
          streaming: a.ms === undefined,
          ms: a.ms,
          prompt,
          groupId: placeholderId,
          siblings: ids.filter((d) => d !== a.deploy),
          armError: a.error,
          tokens: a.tokens,
          decisionId: a.decisionId ?? decisionId,
          meta: { model: a.deploy, cost: a.cost, pLb: a.pLb, tags: a.tags,
                  task: resolvedTask, policy },
        })),
      ])
    }

    const read = createSseReader()
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      for (const payload of read(decoder.decode(value, { stream: true }))) {
        if (payload === DONE) continue
        let frame: ReturnType<typeof miFrame>
        try {
          frame = miFrame(JSON.parse(payload))
        } catch {
          continue // unparseable frame — the next read completes it
        }
        if (!frame || frame.arm === undefined || frame.arm >= 2) continue
        if (frame.event === 'arm') {
          arms.set(frame.arm, {
            deploy: frame.deploy_id ?? '',
            cost: frame.cost_per_success ?? null,
            pLb: frame.p_lb ?? null,
            tags: frame.leaderboards ?? [],
            text: '',
          })
          sync()
          continue
        }
        const arm = arms.get(frame.arm)
        if (!arm) continue
        if (frame.event === 'delta') {
          arm.text += deltaText(JSON.parse(payload))
        } else if (frame.event === 'end') {
          arm.ms = frame.latency_ms
          arm.tokens = frame.usage?.completion_tokens
        } else if (frame.event === 'error') {
          arm.error = frame.error
          arm.ms = performance.now() - t0
        }
        sync()
      }
    }
  }


  /** Persist on every change, so a refresh or a crash cannot lose the thread. */
  useEffect(() => {
    // An empty thread is not a conversation worth remembering, and saving on load
    // would add a blank entry to history every time the app is opened.
    if (!sessionId || turns.length === 0) return
    saveSession(sessionId, turns)
    setSessions(listSessions())
  }, [turns, sessionId])

  /** One request at a time; aborting the previous is the caller's job. */
  function beginRequest(): AbortSignal {
    abortRef.current?.abort()
    const ctl = new AbortController()
    abortRef.current = ctl
    return ctl.signal
  }

  /** Every failure in the transcript, newest first. Derived, never stored
   *  separately — two copies of the same fact drift apart. */
  const failed = failures(turns)

  const refreshUsage = () => {
    if (!sessionId) return
    api
      .session(sessionId)
      .then(setUsage)
      .catch(() => undefined)
  }

  const stop = () => {
    abortRef.current?.abort()
    abortRef.current = null
  }

  const openSession = (id: string) => {
    if (id === sessionId) return
    stop()
    setCurrentSession(id)
    setSessionId(id)
    setTurns(loadSession(id))
    setEdit(null)
    setError(null)
    summaryRef.current = sessionSummary(id)
    api.session(id).then(setUsage).catch(() => setUsage(null))
    hydrate(id)
  }

  /**
   * Fill an empty thread from the server transcript.
   *
   * localStorage is the fast path and holds metadata the server does not (cost,
   * leaderboards, verdicts), so the server is only consulted when the local copy
   * is gone — a cleared cache, a different browser. Transient failure is silent:
   * a server transcript is a recovery path, and a chat that refuses to render
   * because it could not fetch history is worse than one that forgets.
   */
  const hydrate = async (id: string) => {
    if (loadSession(id).length) return
    try {
      const { messages } = await api.messages(id)
      const turns = serverTurns(messages)
      // Guard against clobbering: the user may have started typing while this
      // request was in flight, and their turn is newer than any of these.
      if (turns.length) setTurns((prev) => (prev.length ? prev : turns))
    } catch {
      /* no server transcript, or none reachable */
    }
  }

  const startNewSession = () => {
    stop()
    const id = newSessionId()
    setCurrentSession(id)
    setSessionId(id)
    setTurns([])
    setEdit(null)
    setError(null)
    setUsage(null)
    summaryRef.current = { text: '', count: 0 }
  }

  // ⌘K palette actions. `startNewSession` is redefined every render, so it goes
  // through a ref: a listener that captured the first render's closure would
  // clear the wrong session list.
  const newSessionRef = useRef(startNewSession)
  newSessionRef.current = startNewSession
  useEffect(() => {
    const offs = [
      onCommand('new-chat', () => newSessionRef.current()),
      onCommand('toggle-compare', () => setCompare((c) => !c)),
      onCommand('focus-composer', () => composerRef.current?.focus()),
    ]
    return () => offs.forEach((off) => off())
  }, [])

  // A model handed over from the Overview's "Test" button: pin it, seed a prompt
  // and focus the composer, so "Test" lands somewhere useful instead of an empty
  // chat that only looks like it did nothing.
  useEffect(() => {
    if (!testTarget) return
    setPinned(testTarget.deployId)
    if (testTarget.task) setTask(testTarget.task)
    setDraft((d) => d || 'Reply with exactly: pong')
    requestAnimationFrame(() => composerRef.current?.focus())
    onTestConsumed?.()
  }, [testTarget, onTestConsumed])

  /**
   * Fold turns that have fallen out of the re-sent window into a brief.
   *
   * Runs after a turn settles, so the reader never waits on it, and the brief is
   * cached on the session, so the extra call is paid once per fold rather than
   * on every request. Re-sending a summary instead of the transcript is the
   * point: it is input tokens, and input tokens are where the latency is.
   */
  useEffect(() => {
    if (busy || !sessionId) return
    const usable = usableTurns(turns)
    const overflow = usable.length - KEEP_TURNS
    const covered = summaryRef.current.count
    if (overflow <= 0 || covered >= overflow) return
    if (compactBusy.current) return
    compactBusy.current = true
    // Only the turns the brief does not already cover go to the summariser; the
    // brief itself is passed alongside, so the fold is incremental, not a redo.
    const fresh = usable.slice(covered, overflow).map((t) => ({ role: t.role, content: t.text }))
    api
      .compact(fresh, summaryRef.current.text)
      .then((r) => {
        if (!r.summary) return
        summaryRef.current = { text: r.summary, count: overflow }
        setSessionSummary(sessionId, r.summary, overflow)
        setSessions(listSessions())
      })
      .catch(() => undefined)
      .finally(() => {
        compactBusy.current = false
      })
  }, [busy, turns, sessionId])

  const forgetSession = (id: string) => {
    deleteSession(id)
    // Erase the server copy too, so "forget this chat" means it everywhere the
    // transcript exists — content, not the spend ledger, which stays.
    api.clearMessages(id).catch(() => undefined)
    const rest = listSessions()
    setSessions(rest)
    if (id === sessionId) {
      const next = rest[0]
      if (next) openSession(next.id)
      else startNewSession()
    }
  }

  const renameSessionHandler = (id: string, newTitle: string) => {
    renameSession(id, newTitle)
    setSessions(listSessions())
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    const text = draft.trim()
    if (!text || busy) return
    const uid = crypto.randomUUID()
    const aid = crypto.randomUUID()
    // The transcript as it stands is the context: without it a follow-up has
    // nothing to follow up on. Compacted rather than replayed in full.
    const prior = turns
    const messages = messagesFor(prior, text)
    const signal = beginRequest()
    setTurns((prev) => [
      ...prev,
      { id: uid, role: 'user', text },
      { id: aid, role: 'assistant', text: '', streaming: true, prompt: text, meta: pinned ? { model: pinned, cost: null, tags: [], task } : undefined },
    ])
    setDraft('')
    if (composerRef.current) composerRef.current.style.height = ''
    setError(null)
    setBusy(true)
    setMs(null)
    const t0 = performance.now()
    try {
      if (compare) await compareInto(aid, messages, signal)
      else await streamInto(aid, messages, signal)
    } catch (err) {
      // The turn stays and carries the reason. Deleting it is what made failures
      // invisible; the next request would overwrite the only trace of them.
      const failure = asTurnError(err)
      patch(aid, { streaming: false, error: failure, ms: performance.now() - t0 })
      if (!failure.aborted) setError(failure.message)
    } finally {
      setMs(Math.round(performance.now() - t0))
      setBusy(false)
      abortRef.current = null
      // Read back what the server thinks this session has spent. It is the same
      // number the cap refuses on, so the UI cannot disagree with the enforcement.
      refreshUsage()
    }
  }

  const verdict = async (turn: Turn, up: boolean) => {
    patch(turn.id, { verdict: up ? 'up' : 'down' })
    await api
      .approve({
        task: turn.meta?.task ?? task,
        chosen: up ? (turn.meta?.model ?? '') : '',
        rejected: up ? [] : [turn.meta?.model ?? ''],
        decision_id: turn.decisionId,
      })
      .catch(() => undefined)
  }

  const handleRouteVerdict = async (
    turnId: string,
    deployId: string,
    resolvedTask: string,
    approved: boolean,
  ) => {
    patch(turnId, { routingVerdict: approved ? 'up' : 'down' })
    await api
      .routeVerdict({
        deploy_id: deployId,
        task: resolvedTask,
        approved,
      })
      .catch(() => undefined)
  }

  /** Re-ask an edited question. Everything before it is its context. */
  const resend = () => {
    const text = edit?.text.trim()
    if (!(edit && text)) return
    const at = turns.findIndex((t) => t.id === edit.id)
    const next = turns[at + 1]
    const prior = turns.slice(0, at)
    const messages = messagesFor(prior, text)
    setTurns((prev) => prev.map((t) => (t.id === edit.id ? { ...t, text } : t)))
    setEdit(null)
    if (next?.role === 'assistant') {
      const signal = beginRequest()
      patch(next.id, { text: '', streaming: true, error: undefined, ms: undefined })
      setBusy(true)
      setError(null)
      const t0 = performance.now()
      streamInto(next.id, messages, signal)
        .catch((err: unknown) => {
          const failure = asTurnError(err)
          patch(next.id, { streaming: false, error: failure })
          if (!failure.aborted) setError(failure.message)
        })
        .finally(() => {
          setMs(Math.round(performance.now() - t0))
          setBusy(false)
          abortRef.current = null
        })
    }
  }

  const body = (turn: Turn, align: 'start' | 'end') => {
    if (edit?.id === turn.id) {
      return (
        <>
          <Textarea
            aria-label="Edit message"
            autoFocus
            className={cn(
              enter,
              'min-h-0 w-md max-w-full resize-none rounded-[18px] border-0 bg-muted px-3.5 py-2.5 text-[15px] leading-6 focus-visible:ring-0',
            )}
            onChange={(e) => setEdit({ ...edit, text: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setEdit(null)
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                resend()
              }
            }}
            value={edit.text}
          />
          <div className={cn(enter, 'flex items-center gap-1.5 self-end')}>
            <Button
              className={cn(press, 'rounded-full')}
              onClick={() => setEdit(null)}
              size="sm"
              variant="secondary"
            >
              Cancel
            </Button>
            <Button
              className={cn(press, 'rounded-full')}
              disabled={!edit.text.trim() || busy}
              onClick={resend}
              size="sm"
            >
              Send
            </Button>
          </div>
        </>
      )
    }
    if (turn.error) {
      return <ErrorCard error={turn.error} task={turn.meta?.task} />
    }
    // A stopped request that had already produced text keeps the text, and says so.
    if (turn.streaming && !turn.text && !turn.armError) {
      return (
        <span className="flex items-center gap-2 text-[15px] leading-6">
          <span className="shimmer">Thinking…</span>
          <span className="text-[11px] text-muted-foreground">
            <Elapsed since={turn.t0} running />
          </span>
        </span>
      )
    }
    // An arm that could not be called still owns a slot in the comparison —
    // hiding it would read as "the router only offered one option".
    if (turn.armError) {
      return (
        <div className="rounded-[18px] border border-destructive/40 bg-destructive/5 px-3.5 py-2.5">
          <div className="text-[12.5px] font-medium text-destructive">
            This arm could not be called
          </div>
          <p className="mt-0.5 font-mono text-[11.5px] text-muted-foreground">
            {turn.armError}
          </p>
        </div>
      )
    }
    if (!turn.text) {
      if (turn.streaming) {
        return (
          <Bubble align={align} className="w-full" variant="ghost">
            <BubbleContent className="w-full">
              <ModelIdentity
                model={turn.meta?.model}
                task={turn.meta?.task}
                alternatives={turn.meta?.alternatives}
                streaming={turn.streaming}
              />
              <div className="flex items-center gap-2 py-2 text-[14px] text-muted-foreground">
                <span className="shimmer font-medium">Generating response…</span>
                <span className="text-[11px] opacity-75">
                  <Elapsed since={turn.t0} running />
                </span>
              </div>
            </BubbleContent>
          </Bubble>
        )
      }
      return null
    }
    // The user's own words are shown verbatim; model output is markdown. A
    // heading in a question is a heading, not a formatting instruction.
    if (turn.role === 'user') {
      return (
        <Bubble align={align} className="max-w-[88%] sm:max-w-[72%]" variant="muted">
          <BubbleContent className="whitespace-pre-wrap rounded-2xl rounded-br-md border border-brand/25 bg-brand/[0.09] px-4 py-2.5 text-[15px] leading-7 shadow-soft">
            <CollapsibleQuestion text={turn.text} />
          </BubbleContent>
        </Bubble>
      )
    }
    return (
      <Bubble align={align} className="w-full" variant="ghost">
        <BubbleContent className="w-full">
          <ModelIdentity
            model={turn.meta?.model}
            task={turn.meta?.task}
            alternatives={turn.meta?.alternatives}
            streaming={turn.streaming}
          />
          <CollapsibleMarkdown text={turn.text} streaming={turn.streaming} />
        </BubbleContent>
      </Bubble>
    )
  }

  const action = (label: string, icon: React.ReactNode, onClick?: () => void) => (
    <Button
      aria-label={label}
      title={label}
      className={cn(press, 'rounded-lg')}
      onClick={onClick}
      size="icon-sm"
      variant="ghost"
    >
      {icon}
    </Button>
  )

  function CompareGrid({
    groupTurns,
  }: {
    groupTurns: Turn[]
  }) {
    return (
      <div className="grid w-full grid-cols-1 md:grid-cols-2 gap-4 items-stretch my-2">
        {groupTurns.slice(0, 2).map((armTurn, idx) => {
          const armModel = armTurn.meta?.model || `Candidate ${idx + 1}`
          const isChosen = Boolean(armTurn.picked)
          const isArmLoading = armTurn.streaming && !armTurn.text && !armTurn.armError

          return (
            <div
              key={armTurn.id}
              className={cn(
                'relative flex flex-col rounded-2xl border bg-card p-4 transition-colors',
                isChosen
                  ? 'border-free/50 shadow-glow-free ring-1 ring-free/20'
                  : 'border-border shadow-soft hover:border-brand/40',
              )}
            >
              {/* Arm Header: Model identifier badge & Winner tag */}
              <div className="mb-3 flex items-center justify-between gap-2 border-b border-border/60 pb-3">
                <div className="flex items-center gap-1.5 overflow-hidden">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-brand/10 text-brand">
                    <IconScale className="h-3 w-3" />
                  </span>
                  {(() => {
                    let prov = ''
                    let mod = armModel
                    if (armModel.includes(':')) {
                      const idx = armModel.indexOf(':')
                      prov = armModel.slice(0, idx)
                      mod = armModel.slice(idx + 1)
                    } else if (armModel.includes('/')) {
                      const idx = armModel.indexOf('/')
                      prov = armModel.slice(0, idx)
                      mod = armModel.slice(idx + 1)
                    }
                    if (prov && mod.includes('/')) {
                      const [ns, rest] = mod.split('/', 2)
                      if (prov.toLowerCase().endsWith(ns.toLowerCase())) {
                        mod = rest
                      }
                    }
                    return (
                      <div className="flex items-center gap-1.5 overflow-hidden truncate">
                        {prov && (
                          <span className="shrink-0 rounded border border-border/80 bg-muted/80 px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                            {prov}
                          </span>
                        )}
                        <span
                          className="font-mono text-xs font-semibold text-foreground truncate max-w-[200px]"
                          title={armModel}
                        >
                          {mod}
                        </span>
                      </div>
                    )
                  })()}
                </div>
                {isChosen && (
                  <span className="inline-flex items-center gap-1 rounded-full border border-free/40 bg-free/10 px-2.5 py-0.5 text-[10.5px] font-semibold text-free shrink-0">
                    <IconCheck className="h-3 w-3 stroke-[2.5]" /> Chosen Winner
                  </span>
                )}
              </div>

              {/* Arm Content Area */}
              <div className="flex-1 min-h-[80px]">
                {armTurn.error ? (
                  <ErrorCard error={armTurn.error} task={armTurn.meta?.task} />
                ) : armTurn.armError ? (
                  <div className="rounded-[14px] border border-destructive/40 bg-destructive/5 p-3">
                    <div className="text-[12px] font-medium text-destructive">
                      This arm could not be called
                    </div>
                    <p className="mt-1 font-mono text-[11px] text-muted-foreground break-words">
                      {armTurn.armError}
                    </p>
                  </div>
                ) : isArmLoading ? (
                  <div className="flex items-center gap-2 py-4 text-[14px] text-muted-foreground">
                    <span className="shimmer font-medium">Thinking…</span>
                    <span className="text-[11px] opacity-75">
                      <Elapsed since={armTurn.t0} running />
                    </span>
                  </div>
                ) : (
                  <div className="w-full text-foreground">
                    <CollapsibleMarkdown text={armTurn.text} streaming={armTurn.streaming} />
                  </div>
                )}
              </div>

              {/* Arm Telemetry & Meta */}
              <div className="mt-3 pt-2.5 border-t border-border/40">
                <TelemetryHud
                  meta={armTurn.meta}
                  ms={armTurn.streaming ? undefined : armTurn.ms}
                  tokens={armTurn.tokens}
                  hideIdentity
                  routingVerdict={armTurn.routingVerdict}
                  onRouteVerdict={(deployId, rTask, approved) =>
                    handleRouteVerdict(armTurn.id, deployId, rTask, approved)
                  }
                />
              </div>

              {/* Arm Actions / Footer: Consolidated single Pick as winner button */}
              <div className="mt-2.5 flex items-center justify-between gap-2 pt-1 border-t border-border/30">
                <div className="flex items-center gap-1">
                  {action('Copy response', <IconCopy size={15} stroke={1.6} />, () =>
                    navigator.clipboard.writeText(armTurn.text),
                  )}
                  <DropdownMenu>
                    <DropdownMenuTrigger
                      render={action('Rate response', <IconDots size={15} stroke={1.6} />)}
                    />
                    <DropdownMenuContent align="start" className="w-48 rounded-[14px] p-1.5">
                      <DropdownMenuGroup className="flex flex-col gap-0.5">
                        <DropdownMenuItem
                          className={cn(item, 'gap-2.5')}
                          onClick={() => verdict(armTurn, true)}
                        >
                          <IconThumbUp size={15} stroke={1.6} />
                          {armTurn.verdict === 'up' ? 'Marked good' : 'Good response'}
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          className={cn(item, 'gap-2.5')}
                          onClick={() => verdict(armTurn, false)}
                        >
                          <IconThumbDown size={15} stroke={1.6} />
                          {armTurn.verdict === 'down' ? 'Marked bad' : 'Bad response'}
                        </DropdownMenuItem>
                      </DropdownMenuGroup>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>

                {isChosen ? (
                  <div className="inline-flex items-center gap-1.5 text-xs font-medium text-free">
                    <IconCheck className="h-3.5 w-3.5 stroke-[2.5]" />
                    <span>Selected</span>
                  </div>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    className={cn(
                      press,
                      'h-7 rounded-full px-3 text-xs font-medium border-brand/50 text-foreground hover:bg-brand hover:text-brand-foreground transition-all shadow-xs',
                    )}
                    onClick={() => pick(armTurn)}
                  >
                    Pick as winner
                  </Button>
                )}
              </div>
            </div>
          )
        })}
      </div>
    )
  }

  const renderedGroups = new Set<string>()

  return (
    <div className="flex h-[calc(100dvh-3.5rem)] w-full overflow-hidden">
      <ChatSidebar
        sessions={sessions}
        currentSessionId={sessionId}
        onSelectSession={openSession}
        onNewSession={startNewSession}
        onDeleteSession={forgetSession}
        onRenameSession={renameSessionHandler}
        isOpen={sidebarOpen}
        onToggle={() => setSidebarOpen((v) => !v)}
      />

      <div className="relative flex flex-1 flex-col overflow-hidden">
        <MessageScrollerProvider autoScroll>
          <MessageScroller>
            <MessageScrollerViewport>
              <MessageScrollerContent
                className="mx-auto w-full max-w-3xl gap-7 px-4 pb-10 pt-8 sm:px-6"
              >
                {turns.length === 0 && (
                  <EmptyState
                    onSelectPrompt={(p, t, c) => {
                      setDraft(p)
                      if (t) setTask(t)
                      if (c !== undefined) setCompare(c)
                    }}
                  />
                )}
                {turns.map((turn, index) => {
                  const mine = turn.role === 'user'
                  const align = mine ? 'end' : 'start'

                  // If this turn belongs to a compare round
                  if (turn.groupId) {
                    if (renderedGroups.has(turn.groupId)) {
                      return null
                    }
                    renderedGroups.add(turn.groupId)
                    const groupTurns = turns.filter((t) => t.groupId === turn.groupId)

                    return (
                      <MessageScrollerItem
                        className="w-full pt-1"
                        key={turn.groupId}
                        messageId={turn.groupId}
                      >
                        <CompareGrid groupTurns={groupTurns} />
                      </MessageScrollerItem>
                    )
                  }

                  // Check if the upcoming turns are a compare group for this user request
                  const nextTurn = turns[index + 1]
                  const compareGroupForUser = mine && nextTurn?.groupId
                    ? turns.filter((t) => t.groupId === nextTurn.groupId)
                    : null
                  const modelNames = compareGroupForUser
                    ? compareGroupForUser.map((t) => t.meta?.model).filter(Boolean)
                    : []

                  return (
                    <MessageScrollerItem
                      className={cn(mine && 'pt-4.5')}
                      key={turn.id}
                      messageId={turn.id}
                    >
                      <Message align={align}>
                        <MessageContent className="gap-2.5">
                          {body(turn, align)}

                          {/* Model names label directly under the user prompt when comparing */}
                          {mine && modelNames.length > 0 && (
                            <div className="flex items-center gap-1.5 self-end px-0.5 pt-1 text-[11.5px] text-muted-foreground animate-fade-in">
                              <IconScale className="h-3.5 w-3.5 shrink-0 text-brand" />
                              <span>Comparing</span>
                              <div className="flex flex-wrap items-center gap-1">
                                {modelNames.map((m, mIdx) => (
                                  <span
                                    key={mIdx}
                                    className="rounded-md border border-border bg-muted/60 px-1.5 py-0.5 font-mono text-[10.5px] text-foreground/80"
                                  >
                                    {m}
                                  </span>
                                ))}
                              </div>
                            </div>
                          )}

                          {!mine && (
                            <TelemetryHud
                              meta={turn.meta}
                              ms={turn.streaming ? undefined : turn.ms}
                              tokens={turn.tokens}
                              hideIdentity
                              routingVerdict={turn.routingVerdict}
                              onRouteVerdict={(deployId, rTask, approved) =>
                                handleRouteVerdict(turn.id, deployId, rTask, approved)
                              }
                            />
                          )}

                        {/*
                         * The router only *flags* a trial arm — it does not
                         * spend a second call on its own. The flag used to be
                         * invisible here, so a genuinely uncertain answer looked
                         * exactly like a proven one. This is the decision, not a
                         * notice: one click buys the second opinion.
                         */}
                        {!mine && turn.meta?.needsApproval && !turn.groupId && (
                          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-paid/25 bg-paid/5 px-3.5 py-2.5">
                            <IconAlertTriangle
                              className="shrink-0 text-paid"
                              size={15}
                              stroke={1.8}
                            />
                            <p className="min-w-0 flex-1 text-[12.5px] leading-5 text-muted-foreground">
                              This arm is free but has no track record yet — the router
                              picked it on price, not on evidence.
                            </p>
                            <Button
                              className={cn(press, 'rounded-full')}
                              disabled={busy}
                              onClick={() => compareTurn(turn)}
                              size="sm"
                              variant="secondary"
                            >
                              <IconScale size={14} stroke={1.7} />
                              Compare 2 models
                            </Button>
                          </div>
                        )}

                        {turn.text && edit?.id !== turn.id && (
                          <MessageFooter
                            className={cn(
                              '-mx-2 gap-0.5',
                              turn.groupId && 'opacity-100',
                            )}
                          >
                            {action('Copy', <IconCopy size={16} stroke={1.6} />, () =>
                              navigator.clipboard.writeText(turn.text),
                            )}
                            {mine
                              ? action('Edit', <IconPencil size={16} stroke={1.6} />, () =>
                                  setEdit({ id: turn.id, text: turn.text }),
                                )
                              : action('Regenerate', <IconRefresh size={16} stroke={1.6} />, () => {
                                  const at = turns.findIndex((t) => t.id === turn.id)
                                  const q = turns[at - 1]
                                  if (!q || busy) return
                                  const messages = messagesFor(turns.slice(0, at - 1), q.text)
                                  const signal = beginRequest()
                                  patch(turn.id, { text: '', streaming: true,
                                                   error: undefined, ms: undefined })
                                  setBusy(true)
                                  setError(null)
                                  const t0 = performance.now()
                                  streamInto(turn.id, messages, signal)
                                    .catch((err: unknown) => {
                                      const failure = asTurnError(err)
                                      patch(turn.id, { streaming: false, error: failure })
                                      if (!failure.aborted) setError(failure.message)
                                    })
                                    .finally(() => {
                                      setMs(Math.round(performance.now() - t0))
                                      setBusy(false)
                                      abortRef.current = null
                                    })
                                })}
                            {!mine && (
                              <DropdownMenu>
                                <DropdownMenuTrigger
                                  render={action('More', <IconDots size={16} stroke={1.6} />)}
                                />
                                <DropdownMenuContent
                                  align="start"
                                  className="w-48 rounded-[14px] p-1.5"
                                >
                                  <DropdownMenuGroup className="flex flex-col gap-0.5">
                                    <DropdownMenuItem
                                      className={cn(item, 'gap-2.5')}
                                      onClick={() => verdict(turn, true)}
                                    >
                                      <IconThumbUp size={16} stroke={1.6} />
                                      {turn.verdict === 'up' ? 'Marked good' : 'Good response'}
                                    </DropdownMenuItem>
                                    <DropdownMenuItem
                                      className={cn(item, 'gap-2.5')}
                                      onClick={() => verdict(turn, false)}
                                    >
                                      <IconThumbDown size={16} stroke={1.6} />
                                      {turn.verdict === 'down' ? 'Marked bad' : 'Bad response'}
                                    </DropdownMenuItem>
                                  </DropdownMenuGroup>
                                  <DropdownMenuSeparator />
                                  <DropdownMenuLabel className="px-2.5 text-[11px] font-normal text-muted-foreground">
                                    Both are recorded as observations
                                  </DropdownMenuLabel>
                                </DropdownMenuContent>
                              </DropdownMenu>
                            )}
                          </MessageFooter>
                        )}
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>
                )
              })}
            </MessageScrollerContent>
          </MessageScrollerViewport>
          <MessageScrollerButton
            className={cn(easeOut, 'bottom-2')}
            size="sm"
          >
            <IconChevronDown className="size-3.5" stroke={1.75} />
            Jump to latest
          </MessageScrollerButton>
          <MessageScrollerTopButton>
            <IconArrowUp className="size-3.5" stroke={1.75} />
            Top
          </MessageScrollerTopButton>
        </MessageScroller>
      </MessageScrollerProvider>

      <form
        className="w-full px-4 pb-4 pt-2 sm:px-6"
        onSubmit={submit}
      >
        <div className="mx-auto flex w-full max-w-3xl flex-col items-center gap-2.5">
        <div className="glass-dock w-full rounded-2xl p-1.5 transition-shadow focus-within:ring-2 focus-within:ring-brand/50">
          <textarea
            ref={composerRef}
            aria-label="Message"
            className="max-h-48 min-h-[2.5rem] w-full resize-none border-0 bg-transparent px-2.5 py-2 text-[15px] leading-6 text-foreground outline-none placeholder:text-muted-foreground"
            onChange={(e) => {
              setDraft(e.target.value)
              const el = e.currentTarget
              el.style.height = 'auto'
              el.style.height = `${Math.min(el.scrollHeight, 192)}px`
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                e.currentTarget.form?.requestSubmit()
              }
            }}
            placeholder={compare ? 'Ask both models anything…' : 'Ask anything…'}
            rows={1}
            value={draft}
          />
          <div className="flex flex-wrap items-center gap-1.5 px-0.5 pt-0.5">
            {pinned && (
              <span className="inline-flex h-8 items-center gap-1.5 rounded-full border border-brand/40 bg-brand/10 pl-2.5 pr-1.5 text-[12px] font-medium text-brand">
                <IconPin className="h-3.5 w-3.5" />
                <span className="max-w-[180px] truncate font-mono" title={pinned}>
                  {pinned}
                </span>
                <button
                  type="button"
                  aria-label="Clear pinned model"
                  onClick={() => setPinned(null)}
                  className="grid h-5 w-5 place-items-center rounded-full transition-colors hover:bg-brand/20"
                >
                  <IconX className="h-3 w-3" />
                </button>
              </span>
            )}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button aria-label="More options" className="h-8 w-8 rounded-full" size="icon-sm" variant="ghost">
                  <IconPlus size={17} stroke={1.9} />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-56 rounded-[14px] p-1.5" sideOffset={10}>
                <DropdownMenuGroup className="flex flex-col gap-0.5">
                  <DropdownMenuItem
                    className={cn(item, 'gap-2.5')}
                    onClick={() => setCompare((c) => !c)}
                  >
                    <IconScale size={16} stroke={1.6} />
                    {compare ? 'Stop comparing' : 'Compare 2 models'}
                  </DropdownMenuItem>
                  <DropdownMenuItem className={cn(item, 'gap-2.5')} onClick={startNewSession}>
                    <IconMessagePlus size={16} stroke={1.6} />
                    New chat
                  </DropdownMenuItem>
                </DropdownMenuGroup>
                {sessions.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuLabel className="px-2.5 text-[11px] font-normal text-muted-foreground">
                      History ({sessions.length})
                    </DropdownMenuLabel>
                    <DropdownMenuGroup className="flex max-h-64 flex-col gap-0.5 overflow-y-auto">
                      {sessions.map((sn) => (
                        <DropdownMenuItem
                          key={sn.id}
                          className={cn(item, 'gap-2.5', sn.id === sessionId && 'bg-accent')}
                          onClick={() => openSession(sn.id)}
                        >
                          <IconHistory size={14} stroke={1.6} />
                          <span className="min-w-0 flex-1 truncate" title={sn.title}>
                            {sn.title}
                          </span>
                          {sn.id === sessionId && (
                            <button
                              aria-label="Delete this chat"
                              className="text-muted-foreground hover:text-destructive"
                              onClick={(ev) => {
                                ev.stopPropagation()
                                forgetSession(sn.id)
                              }}
                              type="button"
                            >
                              ×
                            </button>
                          )}
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuGroup>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
            {/* Task as a chip you can see, not a menu you have to find. */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  aria-label="Task"
                  className="h-8 gap-1.5 rounded-full border border-border bg-card px-2.5 text-[12px] font-medium text-muted-foreground hover:text-foreground"
                  size="sm"
                  variant="ghost"
                >
                  <Cpu className="size-3.5 shrink-0 text-brand" />
                  <span className="font-mono">{task}</span>
                  <IconChevronDown className="size-3 shrink-0 opacity-60" stroke={2} />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-49 rounded-[14px] p-1.5" side="top" sideOffset={10}>
                <DropdownMenuGroup className="flex flex-col gap-0.5">
                  <DropdownMenuLabel className="px-2.5 pt-1 pb-0.5 text-[11px]">Task</DropdownMenuLabel>
                  <DropdownMenuRadioGroup onValueChange={setTask} value={task}>
                    {['auto', ...tasks].map((t) => (
                      <DropdownMenuRadioItem className={cn(item, 'pr-8 pl-2.5')} key={t} value={t}>
                        {t}
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>

            {/* Compare changes what the request *does*, so it stays visible. */}
            <Button
              aria-label={pinned ? 'Clear the pinned model to compare' : compare ? 'Stop comparing models' : 'Compare two models'}
              aria-pressed={compare}
              disabled={!!pinned}
              className={cn(
                'h-8 gap-1.5 rounded-full border px-2.5 text-[12px] font-medium disabled:opacity-40',
                compare
                  ? 'border-brand/40 bg-brand/10 text-brand'
                  : 'border-border bg-card text-muted-foreground hover:text-foreground',
              )}
              onClick={() => setCompare((c) => !c)}
              size="sm"
              variant="ghost"
            >
              <IconScale size={14} stroke={1.9} />
              <span className="hidden sm:inline">{compare ? 'Comparing' : 'Compare'}</span>
            </Button>

            <div className="ml-auto flex items-center gap-2 pr-0.5">
              <span className="hidden text-[11px] text-muted-foreground md:inline">
                ↵ send · ⇧↵ newline
              </span>
              {busy ? (
                <Button
                  aria-label="Stop generating"
                  className="rounded-full"
                  onClick={stop}
                  size="icon-sm"
                  title="Stop generating"
                  variant="secondary"
                >
                  <IconPlayerStop size={14} stroke={2} />
                </Button>
              ) : (
                <Button
                  aria-label="Send"
                  className={cn('glow-brand rounded-full bg-brand text-brand-foreground hover:brightness-110', press)}
                  disabled={!draft.trim()}
                  size="icon-sm"
                  type="submit"
                >
                  <IconArrowUp size={16} stroke={2} />
                </Button>
              )}
            </div>
          </div>
        </div>

        <p className="flex flex-wrap items-center justify-center gap-x-2 text-xs text-muted-foreground">
          {error ? (
            <span className="text-destructive">{error}</span>
          ) : ms !== null ? (
            <span className="tnum">
              routed in {ms} ms · {turns.filter((t) => t.role === 'assistant').length} replies
            </span>
          ) : (
            <span>
              Routed live through MinInfer — every answer shows the model, cost and leaderboards.
            </span>
          )}

          {/* The count is separate from `error` so a cleared banner does not erase
              the fact that something failed earlier in the session. */}
          {failed.length > 0 && (
            <span className="tnum text-destructive">
              · {failed.length} failed
            </span>
          )}
          {usage && (usage.tokens > 0 || usage.searches > 0) && (
            <span className="tnum" title={sessionBudgetTitle(usage)}>
              · {usage.tokens.toLocaleString()}
              {usage.limit ? ` / ${usage.limit.toLocaleString()}` : ''} tok
              {usage.searches > 0 && ` · ${usage.searches} search`}
              {usage.searches > 1 ? 'es' : ''}
              {usage.cost_usd > 0 && ` · $${usage.cost_usd.toFixed(4)}`}
              {usage.savings && usage.savings.saved_usd > 0 &&
                ` · saved $${usage.savings.saved_usd.toFixed(4)}`}
            </span>
          )}
        </p>
        </div>
      </form>
    </div>
  </div>
  )
}
