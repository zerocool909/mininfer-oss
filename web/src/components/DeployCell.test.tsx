import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { DeployCell } from '@/components/Overview'

/**
 * The Selected table must not show two different deployments as identical.
 *
 * `openrouter:qwen/qwen3.8-27b:free` and
 * `openrouter/modelrun/fp4:qwen/qwen3.8-27b:free` are different deployments of the
 * same weights through different upstreams — separate price, separate rate limit,
 * separate failure. `DeployCell` stripped everything after the first `/` as a
 * "clean provider prefix", so both rendered as `openrouter  qwen/qwen3.8-27b:free`
 * and the router looked like it was offering the same arm twice.
 */
afterEach(cleanup)

const text = (deployId: string) => {
  const { container } = render(<DeployCell deployId={deployId} />)
  return container.textContent ?? ''
}

describe('DeployCell', () => {
  it('keeps the upstream, so two deployments of one model differ', () => {
    const direct = text('openrouter:qwen/qwen3.8-27b:free')
    const viaUpstream = text('openrouter/modelrun/fp4:qwen/qwen3.8-27b:free')

    expect(direct).not.toEqual(viaUpstream)
    expect(direct).toContain('openrouter')
    expect(viaUpstream).toContain('openrouter/modelrun/fp4')
  })

  it('keeps a nested upstream intact rather than truncating it to the gateway', () => {
    expect(text('openrouter/thinkingmachines/nvfp4:m/x:free'))
      .toContain('openrouter/thinkingmachines/nvfp4')
  })

  it('still splits provider from model', () => {
    expect(text('groq:qwen/qwen3.8-27b')).toBe('groqqwen/qwen3.8-27b')
  })

  it('renders a placeholder for an empty id', () => {
    expect(text('')).toBe('—')
  })
})
