/**
 * Which agent runs the bell still has to announce.
 *
 * The "seen" mark lives in the browser, not the registry: it is a per-viewer
 * fact ("I have looked at this tab"), and the agent registry itself is
 * process-local telemetry with no notion of a reader. Storing it here also means
 * opening the drawer clears the badge without a round trip.
 *
 * This is separate from `lib/api.ts` because the rules are pure functions over
 * `AgentRun[]` — no fetch, no React — which is what makes them testable without
 * a DOM.
 */
import type { AgentRun } from './api'

const STORAGE_KEY = 'mininfer_agents_seen_at'

/** The newest finish time the viewer has acknowledged, or null. */
export function loadAgentSeenAt(): string | null {
  if (typeof window === 'undefined') return null
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    // Private mode / disabled storage: treat everything as unread rather than
    // throwing inside a render.
    return null
  }
}

/** Runs the bell counts: every live run, plus finished runs since `seenAt`.
 *
 * A running run is announced regardless of `seenAt`: it is happening now, and
 * marking it seen does not make it stop. Finished runs clear once their
 * `finished_at` is at or before the mark.
 */
export function unreadAgentRuns(runs: AgentRun[], seenAt: string | null): AgentRun[] {
  return runs.filter((run) => {
    if (run.status === 'running') return true
    if (!run.finished_at) return false
    return seenAt === null || run.finished_at > seenAt
  })
}

/**
 * Collapse adjacent runs that produced the identical result into one entry.
 *
 * Every click is a real run — a scout pass that finds all dossiers fresh is a
 * legitimate "checked, nothing to do" — so the registry keeps them. But eight
 * identical no-op rows (and a badge of "8") is noise, not history: the drawer
 * shows one row with a count, and the bell counts distinct results rather than
 * clicks. The newest run's `run_id` is kept as the key.
 */
export function groupRuns(runs: AgentRun[]): { run: AgentRun; count: number }[] {
  const out: { run: AgentRun; count: number }[] = []
  for (const run of runs) {
    const last = out[out.length - 1]
    const same = last && last.run.kind === run.kind && last.run.status === run.status
      && last.run.title === run.title && last.run.detail === run.detail
    if (same) last.count += 1
    else out.push({ run, count: 1 })
  }
  return out
}

/** Distinct results the bell should announce — grouped, not one per click. */
export function unreadAgentCount(runs: AgentRun[], seenAt: string | null): number {
  return groupRuns(unreadAgentRuns(runs, seenAt)).length
}

/** Acknowledge every finished run in `runs`; live runs are left uncounted. */
export function markAgentsSeen(runs: AgentRun[]): void {
  const newest = runs.reduce<string | null>(
    (acc, run) => (run.finished_at && (!acc || run.finished_at > acc) ? run.finished_at : acc),
    null,
  )
  if (!newest || typeof window === 'undefined') return
  try {
    localStorage.setItem(STORAGE_KEY, newest)
  } catch {
    /* best-effort */
  }
}
