import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { NotificationsDrawer } from './NotificationsDrawer'
import { api, type PricingAnomaly, type ProviderInfo } from '@/lib/api'

// `restoreMocks: true` resets every mock before each test, so the implementations
// are (re)armed in `beforeEach` rather than in the factory. The factory therefore
// references nothing from module scope, which also keeps `vi.mock` hoisting safe.
vi.mock('@/lib/api', () => ({
  api: { providers: vi.fn(), anomalies: vi.fn(), decideAnomaly: vi.fn() },
}))

const ANOMALY_ID = 'openrouter:x:free|input|2026-10-04T10:00:00+00:00'

const PROVIDERS: ProviderInfo[] = [
  {
    id: 'openrouter', name: 'OpenRouter', description: '', base_url: '',
    key_env: 'OPENROUTER_API_KEY', has_project_key: true, is_local: false,
    docs_url: '', setup_guide: '', models_count: 466, models: [],
  },
  {
    id: 'groq', name: 'Groq', description: '', base_url: '',
    key_env: 'GROQ_API_KEY', has_project_key: false, is_local: false,
    docs_url: '', setup_guide: '', models_count: 0, models: [],
  },
]

const ANOMALIES: PricingAnomaly[] = [
  {
    anomaly_id: ANOMALY_ID, deploy_id: 'openrouter:x:free', dimension: 'input',
    kind: 'unit_scale', severity: 'high', state: 'quarantined',
    expected: 0.075, observed: 0.75, factor: 10, source_count: 3,
    detail: '10x the market median', status: 'open',
    opened_at: '2026-10-04T10:00:00+00:00', last_seen_at: '2026-10-04T10:00:00+00:00',
  },
]

beforeEach(() => {
  vi.mocked(api.providers).mockResolvedValue({ providers: PROVIDERS })
  vi.mocked(api.anomalies).mockResolvedValue({
    status: 'open', count: ANOMALIES.length, anomalies: ANOMALIES,
  })
  vi.mocked(api.decideAnomaly).mockResolvedValue({
    ok: true, anomaly_id: ANOMALY_ID, status: 'acknowledged',
  })
})

afterEach(cleanup)

describe('NotificationsDrawer', () => {
  it('shows each provider and how it is authenticated', async () => {
    render(<NotificationsDrawer open onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('OpenRouter')).toBeTruthy())

    // The server variable is named, so "active" is auditable rather than a dot.
    expect(screen.getByText(/server · OPENROUTER_API_KEY/)).toBeTruthy()
    expect(screen.getByText(/not configured · GROQ_API_KEY/)).toBeTruthy()
  })

  it('acknowledging an anomaly calls the API and removes the row', async () => {
    render(<NotificationsDrawer open onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('openrouter:x:free')).toBeTruthy())

    screen.getByRole('button', { name: 'Acknowledge' }).click()

    await waitFor(() =>
      expect(vi.mocked(api.decideAnomaly)).toHaveBeenCalledWith(ANOMALY_ID, 'acknowledged'),
    )
    await waitFor(() => expect(screen.queryByText('openrouter:x:free')).toBeNull())
    expect(screen.getByText('Nothing to review')).toBeTruthy()
  })

  it('resolving sends the other decision', async () => {
    render(<NotificationsDrawer open onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('openrouter:x:free')).toBeTruthy())

    screen.getByRole('button', { name: 'Resolve' }).click()
    await waitFor(() =>
      expect(vi.mocked(api.decideAnomaly)).toHaveBeenCalledWith(ANOMALY_ID, 'resolved'),
    )
  })

  it('a failed decision keeps the row and shows why', async () => {
    vi.mocked(api.decideAnomaly).mockRejectedValue(new Error('no such anomaly'))
    render(<NotificationsDrawer open onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('openrouter:x:free')).toBeTruthy())

    screen.getByRole('button', { name: 'Acknowledge' }).click()
    await waitFor(() => expect(screen.getByText('no such anomaly')).toBeTruthy())
    // Nothing was decided, so the queue must not shrink.
    expect(screen.getByText('openrouter:x:free')).toBeTruthy()
  })

  it('does not fetch while closed', () => {
    render(<NotificationsDrawer open={false} onClose={() => {}} />)
    expect(vi.mocked(api.anomalies)).not.toHaveBeenCalled()
    expect(vi.mocked(api.providers)).not.toHaveBeenCalled()
  })
})
