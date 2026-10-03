import { describe, expect, it } from 'vitest'
import {
  DONE,
  createSseReader,
  deltaText,
  miFrame,
  jsonCompletion,
  streamHeaders,
} from '@/lib/sse'

/**
 * The parsing every streamed answer depends on, and which had no test at all.
 *
 * Its failure mode is not a crash: the legacy dashboard posted without
 * `stream: true`, found no frames, and looked like it worked. So these are written
 * around the specific ways that goes wrong — a split across chunk boundaries, a
 * plain JSON body where SSE was expected, a frame that is not JSON.
 */

/** Feed a chunk sequence through the reader and collect every payload. */
function drain(chunks: string[]): string[] {
  const read = createSseReader()
  return chunks.flatMap((c) => read(c))
}

describe('createSseReader', () => {
  it('yields each data payload without its prefix', () => {
    expect(drain(['data: {"a":1}\n\n'])).toEqual(['{"a":1}'])
  })

  it('keeps a frame that is split across two chunks', () => {
    // The realistic case: one network read lands mid-frame.
    expect(drain(['data: {"cho', 'ices":[]}\n\n'])).toEqual(['{"choices":[]}'])
  })

  it('splits a chunk that carries several frames', () => {
    expect(drain(['data: {"a":1}\n\ndata: {"b":2}\n\n'])).toEqual(['{"a":1}', '{"b":2}'])
  })

  it('carries the terminator through, so a caller can act on it', () => {
    expect(drain([`data: ${DONE}\n\n`])).toEqual([DONE])
  })

  it('ignores comments and non-data lines', () => {
    // `:` lines are SSE keep-alives; a proxy in the middle may emit them.
    expect(drain([': keep-alive\n\nevent: ping\ndata: {"a":1}\n\n'])).toEqual(['{"a":1}'])
  })

  it('handles CRLF, which SSE permits', () => {
    expect(drain(['data: {"a":1}\r\n\r\n'])).toEqual(['{"a":1}'])
  })

  it('does not emit a frame that has not ended yet', () => {
    // No blank line means the frame is still open, so nothing is complete.
    expect(drain(['data: {"a":1}'])).toEqual([])
  })

  it('emits a frame that arrives with no trailing newline after a blank line', () => {
    const read = createSseReader()
    expect(read('data: {"a":1}\n\n')).toEqual(['{"a":1}'])
  })

  it('survives one character at a time', () => {
    const all = []
    const read = createSseReader()
    for (const ch of 'data: {"a":1}\n\ndata: [DONE]\n\n') all.push(...read(ch))
    expect(all).toEqual(['{"a":1}', DONE])
  })
})

describe('deltaText', () => {
  it('reads the incremental content', () => {
    expect(deltaText({ choices: [{ delta: { content: 'Hel' } }] })).toBe('Hel')
  })

  it('is empty for a frame that carries no text', () => {
    // The usage frame has an empty `choices`, and role-only frames have null.
    expect(deltaText({ choices: [] })).toBe('')
    expect(deltaText({ choices: [{ delta: { content: null } }] })).toBe('')
    expect(deltaText({})).toBe('')
    expect(deltaText(null)).toBe('')
  })
})

describe('jsonCompletion', () => {
  it('unwraps a whole completion, for an upstream that ignored `stream`', () => {
    const body = JSON.stringify({ choices: [{ message: { content: 'the answer' } }] })
    expect(jsonCompletion(body)).toBe('the answer')
  })

  it('accepts a delta shape too', () => {
    expect(jsonCompletion(JSON.stringify({ choices: [{ delta: { content: 'x' } }] }))).toBe('x')
  })

  it('returns empty rather than throwing on a non-completion body', () => {
    expect(jsonCompletion('<html>nope</html>')).toBe('')
    expect(jsonCompletion('not json at all')).toBe('')
    expect(jsonCompletion('')).toBe('')
    expect(jsonCompletion('{ broken')).toBe('')
  })
})

describe('miFrame and miFrame', () => {
  it('reads the multiplex envelope via mi or mininfer keys', () => {
    const f1 = miFrame({
      mininfer: { event: 'arm', arm: 1, deploy_id: 'vercel:m2', vendor: 'google', p_lb: 0.71 },
      choices: [],
    })
    expect(f1?.event).toBe('arm')
    expect(f1?.arm).toBe(1)
    expect(f1?.deploy_id).toBe('vercel:m2')

    const f2 = miFrame({
      mi: { event: 'arm', arm: 2, deploy_id: 'openrouter:m3', vendor: 'anthropic', p_lb: 0.85 },
      choices: [],
    })
    expect(f2?.event).toBe('arm')
    expect(f2?.arm).toBe(2)
    expect(f2?.deploy_id).toBe('openrouter:m3')
  })

  it('is null on a single-arm stream, which has no envelope', () => {
    expect(miFrame({ choices: [{ delta: { content: 'x' } }] })).toBeNull()
    expect(miFrame({})).toBeNull()
    expect(miFrame(null)).toBeNull()
  })

  it('reads usage from an end frame', () => {
    const f = miFrame({
      mi: { event: 'end', arm: 0, latency_ms: 812, usage: { prompt_tokens: 21, completion_tokens: 12 } },
    })
    expect(f?.latency_ms).toBe(812)
    expect(f?.usage?.completion_tokens).toBe(12)
  })
})

describe('streamHeaders', () => {
  const headers = (h: Record<string, string>) => new Headers(h)

  it('reads the decision envelope with X-MI-* headers', () => {
    const got = streamHeaders(
      headers({
        'X-MI-Deploy': 'openrouter:m1',
        'X-MI-Task': 'code_edit',
        'X-MI-Policy': 'free_first',
        'X-MI-Needs-Approval': 'true',
        'X-MI-Candidates': 'openrouter:m1, vercel:m2',
      }),
    )
    expect(got.deploy).toBe('openrouter:m1')
    expect(got.task).toBe('code_edit')
    expect(got.policy).toBe('free_first')
    expect(got.needsApproval).toBe(true)
    expect(got.alternatives).toEqual(['vercel:m2'])
  })

  it('reads the decision envelope with legacy X-MI-* headers as fallback', () => {
    const got = streamHeaders(
      headers({
        'X-MI-Deploy': 'openrouter:m1',
        'X-MI-Task': 'code_edit',
        'X-MI-Policy': 'free_first',
        'X-MI-Needs-Approval': 'true',
        'X-MI-Candidates': 'openrouter:m1, vercel:m2',
      }),
    )
    expect(got.deploy).toBe('openrouter:m1')
    expect(got.task).toBe('code_edit')
    expect(got.policy).toBe('free_first')
    expect(got.needsApproval).toBe(true)
    // the one that answered is not an *alternative* to itself
    expect(got.alternatives).toEqual(['vercel:m2'])
  })

  it('treats a missing approval flag as false, not as true', () => {
    expect(streamHeaders(headers({})).needsApproval).toBe(false)
    expect(streamHeaders(headers({ 'X-MI-Needs-Approval': 'false' })).needsApproval).toBe(false)
    expect(streamHeaders(headers({ 'X-MI-Needs-Approval': 'anything' })).needsApproval).toBe(false)
  })

  it('has no alternatives when the chain was one arm', () => {
    const got = streamHeaders(
      headers({ 'X-MI-Deploy': 'openrouter:m1', 'X-MI-Candidates': 'openrouter:m1' }),
    )
    expect(got.alternatives).toEqual([])
    expect(streamHeaders(headers({})).alternatives).toEqual([])
  })

  it('without a Deploy header it cannot tell which arm answered, so it drops none', () => {
    // The proxy always sets both. If Deploy is missing the candidate list is all
    // we have, and silently discarding an entry would be a worse guess than
    // keeping one that might be the answering arm.
    const got = streamHeaders(headers({ 'X-MI-Candidates': 'openrouter:m1' }))
    expect(got.deploy).toBe('')
    expect(got.alternatives).toEqual(['openrouter:m1'])
  })
})

