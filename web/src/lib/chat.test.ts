import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  KEEP_TURNS,
  MAX_ANSWER_CHARS,
  buildMessages,
  compactHistory,
  currentSessionId,
  deleteSession,
  failures,
  listSessions,
  loadSession,
  newSessionId,
  saveSession,
  serverTurns,
  sessionSummary,
  setSessionSummary,
  usableTurns,
  type Turn,
} from '@/lib/chat'

/**
 * Compaction is the logic that regresses silently: a follow-up that stops
 * carrying its context still *looks* like it works, right up until the model
 * answers a question about something it was never told.
 */

const U = (text: string, extra: Partial<Turn> = {}): Turn => ({
  id: `u:${text}`,
  role: 'user',
  text,
  ...extra,
})
const A = (text: string, extra: Partial<Turn> = {}): Turn => ({
  id: `a:${text}`,
  role: 'assistant',
  text,
  ...extra,
})

describe('a follow-up carries what it follows up on', () => {
  it('sends the prior turn before the new question', () => {
    const m = buildMessages(
      [U('what is postgres?'), A('Postgres is a relational database.')],
      'and how does it compare to supabase?',
    )
    expect(m.map((x) => x.role)).toEqual(['user', 'assistant', 'user'])
    expect(m.at(-1)?.content).toBe('and how does it compare to supabase?')
  })

  it('is not the old single-message behaviour', () => {
    const m = buildMessages([U('a'), A('b')], 'c')
    expect(m.length).toBeGreaterThan(1)
  })
})

describe('the window', () => {
  const many = () => {
    const turns: Turn[] = []
    for (let i = 1; i <= 9; i++) turns.push(U(`q${i}`), A(`a${i}`))
    return turns
  }

  it(`keeps only the last ${KEEP_TURNS} turns`, () => {
    const m = buildMessages(many(), 'q10')
    expect(m.filter((x) => x.role !== 'system')).toHaveLength(KEEP_TURNS + 1)
  })

  it('drops the oldest and keeps the newest', () => {
    const m = buildMessages(many(), 'q10')
    expect(JSON.stringify(m)).not.toContain('"q1"')
    expect(JSON.stringify(m)).toContain('"q9"')
  })

  it('says how much was dropped, and what the conversation was about', () => {
    // 18 usable turns, 5 kept.
    const sys = buildMessages(many(), 'q10').find((x) => x.role === 'system')
    expect(sys?.content).toContain('13 earlier messages')
    expect(sys?.content).toContain('q1')
  })

  it('adds no note when nothing was dropped', () => {
    const m = buildMessages([U('hi'), A('there')], 'more')
    expect(m.some((x) => x.role === 'system')).toBe(false)
  })
})

describe('clipping', () => {
  it('clips long answers, keeping a head and a tail', () => {
    const ans = buildMessages([U('t'), A('x'.repeat(MAX_ANSWER_CHARS * 3))], 'again').find(
      (x) => x.role === 'assistant',
    )
    expect(ans?.content.length).toBeLessThanOrEqual(MAX_ANSWER_CHARS + 8)
    expect(ans?.content).toContain('\n…\n')
  })

  it('does not clip questions', () => {
    const q = buildMessages([U('y'.repeat(4000)), A('ok')], 'z').find((x) => x.role === 'user')
    expect(q?.content).toHaveLength(4000)
  })
})

describe('nothing broken is replayed as context', () => {
  it('excludes failed answers', () => {
    const m = compactHistory([
      U('q'),
      A('', { error: { kind: 'http_500', message: 'boom', at: 1 } }),
      U('q2'),
    ])
    expect(m.map((x) => x.content)).toEqual(['q', 'q2'])
  })

  it('excludes an answer that is still streaming', () => {
    const m = compactHistory([U('q'), A('partial', { streaming: true })])
    expect(m).toHaveLength(1)
    expect(usableTurns([U('q'), A('partial', { streaming: true })])).toHaveLength(1)
  })

  it('excludes an arm that could not be called', () => {
    expect(compactHistory([U('q'), A('', { armError: '429' })])).toHaveLength(1)
  })

  it('excludes empty text', () => {
    expect(compactHistory([U('q'), A('   ')])).toHaveLength(1)
  })
})

describe('a compare round is one answer, not two', () => {
  const round = (picked?: 'first' | 'second') => [
    U('which is better?'),
    A('answer A', { groupId: 'r', siblings: ['B'], ...(picked === 'first' ? { picked: true } : {}) }),
    A('answer B', { groupId: 'r', siblings: ['A'], ...(picked === 'second' ? { picked: true } : {}) }),
  ]

  it('sends one answer per round, so the model is not shown two', () => {
    const answers = compactHistory(round()).filter((x) => x.role === 'assistant')
    expect(answers).toHaveLength(1)
  })

  it('sends the chosen one when a choice was made', () => {
    const answers = compactHistory(round('second')).filter((x) => x.role === 'assistant')
    expect(answers[0].content).toBe('answer B')
  })

  it('sends the first when nothing was chosen yet', () => {
    const answers = compactHistory(round()).filter((x) => x.role === 'assistant')
    expect(answers[0].content).toBe('answer A')
  })
})

describe('the request log', () => {
  const turns = [
    U('q'),
    A('', { error: { kind: 'session_budget_exceeded', message: 'over budget', at: 5 } }),
    A('a fine answer'),
    A('', { error: { kind: 'aborted', message: 'stopped', at: 9, aborted: true } }),
  ]

  it('lists every failure, newest first', () => {
    const f = failures(turns)
    expect(f).toHaveLength(2)
    expect(f.map((x) => x.error.at)).toEqual([9, 5])
  })

  it('keeps the router’s own error type', () => {
    expect(failures(turns)[1].error.kind).toBe('session_budget_exceeded')
  })

  it('distinguishes a stop from a failure', () => {
    expect(failures(turns)[0].error.aborted).toBe(true)
    expect(failures(turns)[1].error.aborted).toBeUndefined()
  })

  it('ignores successful turns', () => {
    expect(failures([U('q'), A('fine')])).toEqual([])
  })
})

describe('edges', () => {
  it('empty history is just the question', () => {
    expect(buildMessages([], 'hello')).toEqual([{ role: 'user', content: 'hello' }])
  })

  it('trims the question', () => {
    expect(buildMessages([], '  hi  ')[0].content).toBe('hi')
  })
})

describe('persistence', () => {
  afterEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('round-trips a thread', () => {
    const id = newSessionId()
    saveSession(id, [U('hello there'), A('hi')])
    expect(loadSession(id)).toHaveLength(2)
    expect(currentSessionId()).toBe(id)
  })

  it('titles a thread from its first question', () => {
    const id = newSessionId()
    saveSession(id, [U('how do I route a request?'), A('like this')])
    expect(listSessions()[0].title).toBe('how do I route a request?')
  })

  it('truncates a long title rather than the whole prompt', () => {
    const id = newSessionId()
    saveSession(id, [U('x'.repeat(200))])
    expect(listSessions()[0].title.length).toBeLessThanOrEqual(49)
  })

  it('falls back to the newest thread when the pointer is stale', () => {
    const id = newSessionId()
    saveSession(id, [U('a')])
    deleteSession(id)
    expect(currentSessionId()).toBeNull()
  })

  it('forgets a deleted thread', () => {
    const id = newSessionId()
    saveSession(id, [U('a')])
    deleteSession(id)
    expect(loadSession(id)).toEqual([])
    expect(listSessions()).toEqual([])
  })

  it('caps the number of retained threads', () => {
    for (let i = 0; i < 25; i++) saveSession(`s${i}`, [U(`question ${i}`)])
    expect(listSessions().length).toBeLessThanOrEqual(20)
    // and it is the oldest that go
    expect(listSessions().some((s) => s.title === 'question 24')).toBe(true)
  })

  it('caps the turns kept per thread', () => {
    const id = newSessionId()
    saveSession(id, Array.from({ length: 250 }, (_, i) => U(`q${i}`)))
    expect(loadSession(id)).toHaveLength(200)
    // the tail, not the head
    expect(loadSession(id).at(-1)?.text).toBe('q249')
  })

  it('survives storage that throws, because private mode does', () => {
    // A chat that refuses to render because history could not be saved is worse
    // than one that forgets.
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('QuotaExceededError')
    })
    expect(() => saveSession(newSessionId(), [U('a')])).not.toThrow()
  })

  it('survives unreadable storage', () => {
    localStorage.setItem('mininfer.chat.v1', 'not json')
    expect(listSessions()).toEqual([])
    expect(currentSessionId()).toBeNull()
  })

  it('reads legacy mininfer.chat.v1 storage if mininfer.chat.v1 is absent', () => {
    localStorage.removeItem('mininfer.chat.v1')
    localStorage.setItem('mininfer.chat.v1', JSON.stringify({
      current: 's-legacy',
      sessions: {
        's-legacy': { id: 's-legacy', title: 'Legacy Title', updatedAt: 1000, turns: [] }
      }
    }))
    expect(listSessions()).toHaveLength(1)
    expect(listSessions()[0].title).toBe('Legacy Title')
  })

})

describe('serverTurns', () => {
  const msg = (over: Partial<import('@/lib/api').TranscriptMessage>) => ({
    id: 1, role: 'user' as const, content: 'hi', task: null,
    deploy_id: null, ts: '2026-01-01T00:00:00+00:00', meta: {}, ...over,
  })

  it('maps a transcript to turns and attaches the model to answers', () => {
    const turns = serverTurns([
      msg({ id: 1, role: 'user', content: 'hello' }),
      msg({ id: 2, role: 'assistant', content: 'the answer', deploy_id: 'openrouter:m', task: 't' }),
    ])
    expect(turns.map((t) => [t.role, t.text])).toEqual([
      ['user', 'hello'],
      ['assistant', 'the answer'],
    ])
    expect(turns[0].meta).toBeUndefined()
    expect(turns[1].meta?.model).toBe('openrouter:m')
    expect(turns[1].meta?.task).toBe('t')
  })

  it('drops system and tool rows — they are plumbing, not conversation', () => {
    const turns = serverTurns([
      msg({ id: 1, role: 'system', content: '3 earlier messages were omitted' }),
      msg({ id: 2, role: 'tool', content: '{"ok":true}' }),
      msg({ id: 3, role: 'user', content: 'real question' }),
    ])
    expect(turns.map((t) => t.text)).toEqual(['real question'])
  })

  it('drops blank content', () => {
    expect(serverTurns([msg({ role: 'assistant', content: '   ' })])).toEqual([])
  })

  it('is empty for no transcript', () => {
    expect(serverTurns([])).toEqual([])
  })
})

describe('a compacted brief replaces the elision note', () => {
  const many = () => {
    const turns: Turn[] = []
    for (let i = 1; i <= 9; i++) turns.push(U(`q${i}`), A(`a${i}`))
    return turns
  }

  it('sends the brief instead of a "were omitted" note', () => {
    const m = buildMessages(many(), 'q10', { summary: '- goal: build a planet' })
    const sys = m.find((x) => x.role === 'system')
    expect(sys?.content).toContain('compacted summary')
    expect(sys?.content).toContain('build a planet')
    expect(JSON.stringify(m)).not.toContain('were omitted')
  })

  it('falls back to the elision note when there is no brief', () => {
    const sys = buildMessages(many(), 'q10').find((x) => x.role === 'system')
    expect(sys?.content).toContain('were omitted')
  })
})

describe('the brief survives a transcript save', () => {
  it('is not wiped when saveSession rewrites the record', () => {
    const id = 's-summary'
    saveSession(id, [U('a'), A('b')])
    setSessionSummary(id, '- goal: X', 2)
    expect(sessionSummary(id)).toEqual({ text: '- goal: X', count: 2 })

    // The chat persists on every change, and that replaces the whole record.
    saveSession(id, [U('a'), A('b'), U('c')])
    expect(sessionSummary(id).text).toBe('- goal: X')
  })
})
