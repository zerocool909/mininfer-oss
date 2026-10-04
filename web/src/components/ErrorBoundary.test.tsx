import { afterAll, beforeAll, afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ErrorBoundary } from '@/components/ErrorBoundary'

/**
 * The failure this exists for: React unmounts the whole tree when a render
 * throws, so one bad field in an API payload (a `null` where a number used to be)
 * left a **blank white page**. The Python suite, `vitest` and `tsc` all pass,
 * because the type was right and only the data was not — nothing else in the
 * project renders a component that throws.
 */

afterEach(cleanup)

// React logs every caught error (and jsdom re-prints the stack). The tests assert
// on the fallback, so the noise is expected — silence it for the whole file rather
// than per test, or a passing run still looks alarming.
beforeAll(() => vi.spyOn(console, 'error').mockImplementation(() => undefined))
afterAll(() => vi.restoreAllMocks())

function Boom(): never {
  throw new Error('cannot read properties of undefined (reading \'length\')')
}

describe('a render error is contained, not fatal', () => {
  it('renders its children when nothing throws', () => {
    render(
      <ErrorBoundary>
        <p>all good</p>
      </ErrorBoundary>,
    )
    expect(screen.getByText('all good')).toBeTruthy()
  })

  it('shows a fallback instead of unmounting to a blank page', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )

    expect(screen.getByText('The dashboard hit an error')).toBeTruthy()
    // ...and the actual message travels with it, or the report is unusable.
    expect(screen.getByText(/cannot read properties of undefined/)).toBeTruthy()
  })

  it('offers a reset that remounts the subtree', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    screen.getByText('Try again').click()
    // The throwing child renders again and re-catches — the point is that the
    // boundary recovered rather than staying stuck on the error screen.
    expect(screen.getByText('The dashboard hit an error')).toBeTruthy()
  })

  it('lets a caller supply its own fallback', () => {
    render(
      <ErrorBoundary fallback={(error) => <p>custom: {error.message}</p>}>
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByText(/custom: cannot read properties/)).toBeTruthy()
  })
})
