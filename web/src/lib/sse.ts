/**
 * Server-sent events, parsed.
 *
 * This lived inline in two places — the single-arm relay and the multiplexed
 * compare stream — as two copies of the same frame splitting. Two copies is why it
 * was untestable, and the failure mode of untested parsing is not a crash: the
 * legacy dashboard posted without `stream: true`, found no frames, and quietly
 * looked like it worked. So the parsing lives here, as pure functions of bytes.
 */

export const DONE = '[DONE]'

/**
 * An incremental reader: feed decoded chunks, get whole `data:` payloads back.
 *
 * A chunk boundary can land mid-frame, so the tail is kept until the next chunk
 * completes it. Returns payloads verbatim — including `[DONE]` — because the
 * multiplexed stream needs to see the terminator and the single-arm relay does not.
 */
export function createSseReader(): (chunk: string) => string[] {
  let buf = ''
  return (chunk: string) => {
    buf += chunk
    const lines = buf.split('\n')
    // The last element is either '' or a partial line: keep it for next time.
    buf = lines.pop() ?? ''
    const out: string[] = []
    for (const line of lines) {
      // `trim()` also disposes of the trailing \r SSE permits on CRLF streams.
      const s = line.trim()
      if (!s.startsWith('data:')) continue
      const payload = s.slice(5).trim()
      if (payload) out.push(payload)
    }
    return out
  }
}

/** `choices[0].delta.content` from a chunk, or '' when the chunk carries none. */
export function deltaText(payload: unknown): string {
  const j = payload as { choices?: { delta?: { content?: string | null } }[] } | null
  return j?.choices?.[0]?.delta?.content ?? ''
}

/**
 * `choices[0].message.content` from a whole (non-streamed) completion.
 *
 * The fallback for an upstream that ignores `stream: true` and answers with one
 * JSON body. Without it that reads as an empty reply, which is the same class of
 * silent failure as the parser above.
 */
export function jsonCompletion(body: string): string {
  const s = body.trim()
  if (!s.startsWith('{')) return ''
  try {
    const j = JSON.parse(s) as {
      choices?: { message?: { content?: string }; delta?: { content?: string } }[]
    }
    return j?.choices?.[0]?.message?.content ?? j?.choices?.[0]?.delta?.content ?? ''
  } catch {
    return ''
  }
}

export interface Usage {
  prompt_tokens?: number
  completion_tokens?: number
  cost?: number
}

/** The `mi` envelope a multiplexed frame carries. Absent on a single-arm stream. */
export interface MiFrame {
  event?: 'arm' | 'delta' | 'end' | 'error'
  arm?: number
  deploy_id?: string
  vendor?: string
  cost_per_success?: number | null
  p_lb?: number | null
  leaderboards?: { key: string; label: string; source: string; url?: string; value?: number }[]
  latency_ms?: number
  error?: string
  usage?: Usage
  needs_approval?: boolean
  task?: string
  policy?: string
}

/** The frame envelope from a payload, or null when the frame has none. */
export function miFrame(payload: unknown): MiFrame | null {
  const j = payload as { mi?: MiFrame; mininfer?: MiFrame } | null
  return j?.mi ?? j?.mininfer ?? null
}

/** Header values a streamed reply carries instead of a JSON envelope. */
export function streamHeaders(headers: Headers) {
  const deploy = headers.get('X-MI-Deploy') ?? ''
  const task = headers.get('X-MI-Task') ?? ''
  const policy = headers.get('X-MI-Policy') ?? ''
  const needsApproval = (headers.get('X-MI-Needs-Approval') ?? '') === 'true'
  const cands = headers.get('X-MI-Candidates') ?? ''
  return {
    deploy,
    task,
    policy,
    needsApproval,
    /** Every candidate in the fallback chain, minus the one that answered. */
    alternatives: cands
      .split(',')
      .map((s) => s.trim())
      .filter((s) => s && s !== deploy),
  }
}

