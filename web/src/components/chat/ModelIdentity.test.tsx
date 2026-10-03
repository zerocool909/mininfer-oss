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
