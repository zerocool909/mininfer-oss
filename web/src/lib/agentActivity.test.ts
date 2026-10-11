import { afterEach, describe, expect, it } from 'vitest'
import {
  groupRuns,
  loadAgentSeenAt,
  markAgentsSeen,
  unreadAgentCount,
  unreadAgentRuns,
} from './agentActivity'
import type { AgentRun } from './api'

function run(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    run_id: 'scout-1',
    kind: 'scout',
    title: 'Scout pass',
    status: 'ok',
    detail: '',
    done: 0,
    total: 0,
    started_at: '2026-10-10T00:00:00+00:00',
    updated_at: '2026-10-10T00:00:00+00:00',
    finished_at: '2026-10-10T00:00:10+00:00',
    meta: {},
    ...overrides,
  }
}

afterEach(() => localStorage.clear())

describe('unreadAgentRuns', () => {
  it('counts a running run even when nothing has finished', () => {
    const runs = [run({ run_id: 'live', status: 'running', finished_at: null })]
    expect(unreadAgentRuns(runs, null).map((r) => r.run_id)).toEqual(['live'])
  })

  it('counts finished runs newer than the seen mark', () => {
    const runs = [
      run({ run_id: 'old', finished_at: '2026-10-10T00:00:01+00:00' }),
      run({ run_id: 'new', finished_at: '2026-10-10T00:00:05+00:00' }),
    ]
    expect(unreadAgentRuns(runs, '2026-10-10T00:00:03+00:00').map((r) => r.run_id)).toEqual([
      'new',
    ])
  })

  it('treats a running run as unread regardless of the seen mark', () => {
    const runs = [
      run({ run_id: 'live', status: 'running', finished_at: null }),
      run({ run_id: 'done', finished_at: '2026-10-10T00:00:01+00:00' }),
    ]
    expect(unreadAgentRuns(runs, '2026-10-10T00:00:09+00:00').map((r) => r.run_id)).toEqual([
      'live',
    ])
  })
})

describe('groupRuns', () => {
  it('collapses adjacent runs with the identical result', () => {
    const runs = [
      run({ run_id: 'c', detail: '0 written, 44 fresh, 0 errors' }),
      run({ run_id: 'b', detail: '0 written, 44 fresh, 0 errors' }),
      run({ run_id: 'a', detail: '0 written, 44 fresh, 0 errors' }),
    ]
    const groups = groupRuns(runs)
    expect(groups).toHaveLength(1)
    expect(groups[0].count).toBe(3)
    expect(groups[0].run.run_id).toBe('c')  // the newest is the representative
  })

  it('keeps a run that produced a different result as its own row', () => {
    const runs = [
      run({ run_id: 'c', detail: '2 written, 42 fresh, 0 errors' }),
      run({ run_id: 'b', detail: '0 written, 44 fresh, 0 errors' }),
      run({ run_id: 'a', detail: '0 written, 44 fresh, 0 errors' }),
    ]
    const groups = groupRuns(runs)
    expect(groups.map((g) => g.count)).toEqual([1, 2])
    expect(groups[1].run.run_id).toBe('b')
  })

  it('does not merge across a different kind', () => {
    const runs = [
      run({ run_id: 'b', kind: 'ingest', detail: 'same' }),
      run({ run_id: 'a', kind: 'scout', detail: 'same' }),
    ]
    expect(groupRuns(runs)).toHaveLength(2)
  })
})

describe('unreadAgentCount', () => {
  it('counts distinct results, not one per repeated click', () => {
    const runs = [
      run({ run_id: 'c', detail: '0 written, 44 fresh, 0 errors' }),
      run({ run_id: 'b', detail: '0 written, 44 fresh, 0 errors' }),
      run({ run_id: 'a', detail: '0 written, 44 fresh, 0 errors' }),
    ]
    expect(unreadAgentRuns(runs, null)).toHaveLength(3)  // three runs
    expect(unreadAgentCount(runs, null)).toBe(1)         // one result
  })

  it('still counts a live run that is unlike anything finished', () => {
    const runs = [
      run({ run_id: 'live', status: 'running', finished_at: null, detail: '2/44' }),
      run({ run_id: 'b', detail: '0 written, 44 fresh, 0 errors' }),
    ]
    expect(unreadAgentCount(runs, null)).toBe(2)
  })
})

describe('markAgentsSeen', () => {
  it('advances the mark to the newest finish time only', () => {
    markAgentsSeen([
      run({ run_id: 'a', finished_at: '2026-10-10T00:00:01+00:00' }),
      run({ run_id: 'b', finished_at: '2026-10-10T00:00:07+00:00' }),
    ])
    expect(loadAgentSeenAt()).toBe('2026-10-10T00:00:07+00:00')
  })

  it('leaves the mark untouched when nothing has finished', () => {
    localStorage.setItem('mininfer_agents_seen_at', '2026-10-10T00:00:03+00:00')
    markAgentsSeen([run({ run_id: 'live', status: 'running', finished_at: null })])
    expect(loadAgentSeenAt()).toBe('2026-10-10T00:00:03+00:00')
  })
})
