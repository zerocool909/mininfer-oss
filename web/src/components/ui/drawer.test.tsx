import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Drawer } from './drawer'

afterEach(cleanup)

/** The dialog is always mounted, so it is queried with `hidden: true`. */
function panel() {
  return screen.getByRole('dialog', { hidden: true })
}

describe('Drawer', () => {
  it('keeps a closed panel off to the right and out of the accessibility tree', () => {
    render(
      <Drawer open={false} onClose={() => {}} title="Notifications">
        <p>body</p>
      </Drawer>,
    )
    const el = panel()
    expect(el.getAttribute('aria-hidden')).toBe('true')
    expect(el.className).toContain('translate-x-full')
    expect(el.className).not.toContain('translate-x-0')
  })

  it('slides in when open', () => {
    render(
      <Drawer open onClose={() => {}} title="Notifications">
        <p>body</p>
      </Drawer>,
    )
    const el = panel()
    expect(el.getAttribute('aria-hidden')).toBe('false')
    expect(el.className).toContain('translate-x-0')
  })

  it('closes on Escape', () => {
    const onClose = vi.fn()
    render(
      <Drawer open onClose={onClose} title="Notifications">
        <p>body</p>
      </Drawer>,
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('ignores Escape while closed', () => {
    const onClose = vi.fn()
    render(
      <Drawer open={false} onClose={onClose} title="Notifications">
        <p>body</p>
      </Drawer>,
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes from its button and from its backdrop', () => {
    const onClose = vi.fn()
    const { container } = render(
      <Drawer open onClose={onClose} title="Notifications">
        <p>body</p>
      </Drawer>,
    )
    fireEvent.click(screen.getByLabelText('Close notifications'))
    expect(onClose).toHaveBeenCalledTimes(1)

    // The backdrop is the aria-hidden fixed layer, not the panel itself.
    const backdrop = container.querySelector('[aria-hidden="true"].fixed')
    expect(backdrop).not.toBeNull()
    fireEvent.click(backdrop as Element)
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})

/**
 * The behaviours that make it a dialog rather than a floating panel. Without
 * these a keyboard user tabs into the page behind the overlay and loses their
 * place when it closes — which is exactly what a browser audit of the first
 * version found.
 */
describe('Drawer as a dialog', () => {
  function Harness() {
    const [open, setOpen] = useState(false)
    return (
      <div>
        <button onClick={() => setOpen(true)}>open drawer</button>
        <Drawer open={open} onClose={() => setOpen(false)} title="Notifications">
          <button>inside one</button>
          <button>inside two</button>
        </Drawer>
      </div>
    )
  }

  it('moves focus in on open and puts it back on the trigger on close', async () => {
    render(<Harness />)
    const trigger = screen.getByText('open drawer')
    trigger.focus()
    fireEvent.click(trigger)

    await waitFor(() => expect(panel()).toBe(document.activeElement))

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })

  it('traps Tab inside the panel', async () => {
    render(
      <Drawer open onClose={() => {}} title="Notifications">
        <button>inside one</button>
        <button>inside two</button>
      </Drawer>,
    )
    await waitFor(() => expect(panel()).toBe(document.activeElement))

    const last = screen.getByText('inside two')
    last.focus()
    fireEvent.keyDown(window, { key: 'Tab' })
    // Forward from the last focusable wraps to the first (the close button).
    expect(document.activeElement).toBe(screen.getByLabelText('Close notifications'))

    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('locks page scroll while open and restores it', () => {
    const { rerender } = render(
      <Drawer open onClose={() => {}} title="Notifications">
        <p>body</p>
      </Drawer>,
    )
    expect(document.body.style.overflow).toBe('hidden')
    rerender(
      <Drawer open={false} onClose={() => {}} title="Notifications">
        <p>body</p>
      </Drawer>,
    )
    expect(document.body.style.overflow).toBe('')
  })

  it('is inert while closed, so its content cannot be tabbed into', () => {
    const { rerender } = render(
      <Drawer open={false} onClose={() => {}} title="Notifications">
        <button>inside</button>
      </Drawer>,
    )
    expect(panel().hasAttribute('inert')).toBe(true)
    rerender(
      <Drawer open onClose={() => {}} title="Notifications">
        <button>inside</button>
      </Drawer>,
    )
    expect(panel().hasAttribute('inert')).toBe(false)
  })
})
