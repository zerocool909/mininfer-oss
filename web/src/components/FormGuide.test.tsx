import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { FormGuide } from './FormGuide'
import { api } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  api: {
    freeModels: vi.fn(),
    dossiersRefresh: vi.fn(),
    agentActivity: vi.fn(),
  },
}))

const FREE_MODEL = {
  deploy_id: 'openrouter:m:free',
  provider: 'openrouter',
  provider_model_id: 'm:free',
  price_in: 0,
  price_out: 0,
  context_window: 1000,
  status: 'live',
  pricing_state: 'free',
  display_name: 'M',
  family: null,
  params_b: null,
  benchmark: null,
  core_competency: null,
  summary: null,
  leaderboards: null,
  strengths: null,
  weaknesses: null,
  when_to_use: null,
  when_not_to_use: null,
  best_for: null,
  search_sources: null,
  confidence: null,
  generated_at: null,
}

beforeEach(() => {
  vi.mocked(api.freeModels).mockResolvedValue({ count: 1, models: [FREE_MODEL] })
  vi.mocked(api.dossiersRefresh).mockResolvedValue({
    started: true, already_running: false, run_id: 'scout-1',
  })
})

afterEach(cleanup)

describe('FormGuide scout', () => {
  it('starts the pass and follows it without blocking, then reloads', async () => {
    vi.mocked(api.agentActivity)
      .mockResolvedValueOnce({
        count: 1,
        running: 1,
        runs: [{
          run_id: 'scout-1', kind: 'scout', title: 'Scout pass', status: 'running',
          detail: 'wrote M', done: 1, total: 2,
          started_at: '2026-10-10T00:00:00+00:00',
          updated_at: '2026-10-10T00:00:01+00:00', finished_at: null, meta: {},
        }],
      })
      .mockResolvedValue({
        count: 1,
        running: 0,
        runs: [{
          run_id: 'scout-1', kind: 'scout', title: 'Scout pass', status: 'ok',
          detail: '1 written, 0 fresh, 0 errors', done: 2, total: 2,
          started_at: '2026-10-10T00:00:00+00:00',
          updated_at: '2026-10-10T00:00:02+00:00',
          finished_at: '2026-10-10T00:00:02+00:00', meta: {},
        }],
      })

    render(<FormGuide />)
    await waitFor(() => expect(screen.getByText('M')).toBeTruthy())

    screen.getByRole('button', { name: /scout now/i }).click()

    await waitFor(() => expect(vi.mocked(api.dossiersRefresh)).toHaveBeenCalled())

    // It followed the run to completion and reloaded the table (mount + reload).
    await waitFor(() => expect(vi.mocked(api.freeModels).mock.calls.length).toBeGreaterThan(1), {
      timeout: 4000,
    })
    // The button is interactive again once the run settles.
    await waitFor(() => expect(screen.getByRole('button', { name: /scout now/i })).toBeTruthy())
  })
})
