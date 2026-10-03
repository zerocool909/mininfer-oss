import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { CollapsibleMarkdown, CollapsibleQuestion } from './collapsible'

afterEach(cleanup)

describe('CollapsibleMarkdown', () => {
  it('renders short answer without collapse button', () => {
    render(<CollapsibleMarkdown text="Short model answer" />)
    expect(screen.getByText('Short model answer')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /show full answer/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /show less/i })).toBeNull()
  })

  it('renders long answer expanded by default with Show less button and toggles cleanly', () => {
    const longText = 'A'.repeat(3200)
    render(<CollapsibleMarkdown text={longText} />)

    // Starts expanded by default so user can read complete text without half-cut answers
    const toggleBtn = screen.getByRole('button', { name: /show less/i })
    expect(toggleBtn).toBeTruthy()
    expect(toggleBtn.textContent).toContain('Show less')

    // Click to collapse
    fireEvent.click(toggleBtn)
    expect(toggleBtn.textContent).toContain('Show full answer')

    // Click to expand again
    fireEvent.click(toggleBtn)
    expect(toggleBtn.textContent).toContain('Show less')
  })

  it('does not show button when streaming even if long', () => {
    const longText = 'A'.repeat(3200)
    render(<CollapsibleMarkdown text={longText} streaming />)
    expect(screen.queryByRole('button', { name: /show full answer/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /show less/i })).toBeNull()
  })
})

describe('CollapsibleQuestion', () => {
  it('renders short question directly without collapse button', () => {
    render(<CollapsibleQuestion text="What is dynamic model routing?" />)
    expect(screen.getByText('What is dynamic model routing?')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /show full question/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /show less/i })).toBeNull()
  })

  it('renders long question expanded by default with Show less button and toggles cleanly', () => {
    const longQuestion = 'Explain '.repeat(200) + 'how routing works across different models.'
    render(<CollapsibleQuestion text={longQuestion} />)

    // Starts expanded so question is not cut off
    const toggleBtn = screen.getByRole('button', { name: /show less/i })
    expect(toggleBtn).toBeTruthy()
    expect(toggleBtn.textContent).toContain('Show less')

    // Click to collapse
    fireEvent.click(toggleBtn)
    expect(toggleBtn.textContent).toContain('Show full question')

    // Click to expand again
    fireEvent.click(toggleBtn)
    expect(toggleBtn.textContent).toContain('Show less')
  })

  it('renders Show less button for multi-line questions with more than 16 lines', () => {
    const multiLine = Array.from({ length: 20 }, (_, i) => `Line ${i + 1}`).join('\n')
    render(<CollapsibleQuestion text={multiLine} />)

    const toggleBtn = screen.getByRole('button', { name: /show less/i })
    expect(toggleBtn).toBeTruthy()

    fireEvent.click(toggleBtn)
    expect(toggleBtn.textContent).toContain('Show full question')
  })
})
