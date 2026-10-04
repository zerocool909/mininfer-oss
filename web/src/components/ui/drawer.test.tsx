import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
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
