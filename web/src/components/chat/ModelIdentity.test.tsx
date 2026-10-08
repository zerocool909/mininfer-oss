import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ModelIdentity } from './ModelIdentity'

afterEach(cleanup)

describe('ModelIdentity', () => {
  it('renders model name and provider cleanly when model is specified', () => {
    render(
      <ModelIdentity
        model="openrouter:qwen/qwen3.8-27b:free"
        alternatives={['vercel:poolside/laguna-s-2.1-free']}
      />,
    )
    expect(screen.getByText(/Output given by:/i)).toBeTruthy()
    expect(screen.getByText('openrouter')).toBeTruthy()
    expect(screen.getByText('qwen/qwen3.8-27b:free')).toBeTruthy()
    expect(screen.getByText('poolside/laguna-s-2.1-free')).toBeTruthy()
  })

  it('recovers the winning model from alternatives if model is blank or task name', () => {
    // When model was passed as empty or task name like "summarise",
    // the first alternative is the winning arm.
    render(
      <ModelIdentity
        model=""
        alternatives={[
          'openrouter:qwen/qwen3.8-27b:free',
          'vercel:poolside/laguna-s-2.1-free',
        ]}
      />,
    )
    // "Output given by" should now show the first model, NOT "Auto-routed model"
    expect(screen.queryByText(/Auto-routed model/i)).toBeNull()
    expect(screen.getByText('openrouter')).toBeTruthy()
    expect(screen.getByText('qwen/qwen3.8-27b:free')).toBeTruthy()

    // And other selections should ONLY contain the remaining alternative, NO repeats!
    expect(screen.getByText('poolside/laguna-s-2.1-free')).toBeTruthy()
  })

  it('filters out winning model from other selections to prevent repeats', () => {
    render(
      <ModelIdentity
        model="groq:qwen/qwen3.8-27b"
        alternatives={[
          'groq:qwen/qwen3.8-27b',
          'openrouter:qwen/qwen3.8-27b:free',
        ]}
      />,
    )
    // groq:qwen/qwen3.8-27b should appear in the primary pill, but NOT in Other selections
    expect(screen.getByText('qwen/qwen3.8-27b')).toBeTruthy()
    expect(screen.getByText('qwen/qwen3.8-27b:free')).toBeTruthy()
  })
})

describe('ModelIdentity — complexity badge', () => {
  const high = {
    level: 'high',
    needs_reasoning: true,
    confidence: 0.79,
    score: 0.85,
    signals: ['math_logic:1.0'],
    source: 'heuristic',
  }

  it('shows the level when a low-complexity prompt is routed cheaply', () => {
    render(
      <ModelIdentity
        model="groq:qwen/qwen3.8-27b"
        complexity={{ ...high, level: 'low', needs_reasoning: false, score: 0, signals: [] }}
      />,
    )
    const badge = screen.getByTestId('complexity-badge')
    expect(badge.textContent).toMatch(/low/i)
    expect(badge.textContent).not.toMatch(/needs reasoning/i)
  })

  it('flags a prompt that needs reasoning, and names the signals in the tooltip', () => {
    render(<ModelIdentity model="groq:qwen/qwen3.8-27b" complexity={high} />)
    const badge = screen.getByTestId('complexity-badge')
    expect(badge.textContent).toMatch(/high/i)
    expect(badge.textContent).toMatch(/needs reasoning/i)
    // The cue that fired has to be visible: a wrong call must be debuggable.
    expect(badge.getAttribute('title')).toContain('math_logic:1.0')
  })

  it('renders nothing when the request was not routed (a pinned model has no decision)', () => {
    render(<ModelIdentity model="groq:qwen/qwen3.8-27b" />)
    expect(screen.queryByTestId('complexity-badge')).toBeNull()
  })

  it('marks an escalated retry', () => {
    render(<ModelIdentity model="groq:qwen/qwen3.8-27b" complexity={high} complexityEscalated />)
    expect(screen.getByTestId('complexity-escalated').textContent).toMatch(/escalated/i)
  })

  it('names the judge as the source when Tier 2 decided', () => {
    render(<ModelIdentity model="groq:qwen/qwen3.8-27b" complexity={{ ...high, source: 'judge' }} />)
    expect(screen.getByTestId('complexity-badge').textContent).toMatch(/judge/i)
  })
})
