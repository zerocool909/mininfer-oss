import { useEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'

/** What a Tab press should stop at. `[tabindex="-1"]` is excluded — that is the
 *  panel itself, focusable only by script. */
const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * A panel that slides in from the right edge and slides back out when closed.
 *
 * Deliberately not a dialog library: the repo has no dialog dependency. But a
 * panel that only *looks* like a dialog is worse than none, so this does the
 * three things that make it behave like one:
 *
 * * **Focus** — remembers the trigger, moves focus into the panel, keeps Tab
 *   inside it, and returns focus on close. Otherwise Tab walks the page behind
 *   the overlay and a keyboard user loses their place when it shuts.
 * * **Scroll lock** — a modal panel must not have the page scrolling underneath.
 * * **`inert` while closed** — `aria-hidden` alone leaves the off-screen content
 *   in the tab order, so a closed drawer is still reachable by keyboard.
 *
 * It stays mounted so the exit transition runs. Motion is neutralised globally
 * under `prefers-reduced-motion`, so nothing here needs to special-case it.
 */
export function Drawer({
  open,
  onClose,
  title,
  children,
  className,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  className?: string
}) {
  const panelRef = useRef<HTMLElement>(null)
  const restoreRef = useRef<HTMLElement | null>(null)
  // Read the latest `onClose` without making it an effect dependency: an inline
  // arrow from the parent changes identity every render, and re-running the focus
  // effect would yank focus back to the panel mid-use.
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  })

  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    if (!open) {
      panel.setAttribute('inert', '')
      return
    }
    panel.removeAttribute('inert')
    restoreRef.current = document.activeElement as HTMLElement | null
    panel.focus()

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onCloseRef.current()
        return
      }
      if (e.key !== 'Tab') return
      const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
      if (items.length === 0) {
        e.preventDefault()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      const el = restoreRef.current
      // After this commit's other effects, so a background the app just marked
      // `inert` is interactive again before focus returns to the trigger.
      requestAnimationFrame(() => {
        if (el?.isConnected) el.focus()
      })
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [open])

  return (
    <>
      <div
        aria-hidden="true"
        onClick={onClose}
        className={cn(
          'fixed inset-0 z-40 bg-background/60 backdrop-blur-sm transition-opacity duration-200',
          open ? 'opacity-100' : 'pointer-events-none opacity-0',
        )}
      />
      <aside
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        aria-hidden={!open}
        className={cn(
          'fixed inset-y-0 right-0 z-50 flex w-full max-w-[400px] flex-col border-l border-border bg-background shadow-lift transition-transform duration-200 ease-out focus:outline-none',
          open ? 'translate-x-0' : 'pointer-events-none translate-x-full',
          className,
        )}
      >
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
          <h2 className="text-sm font-semibold text-foreground">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close notifications"
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
      </aside>
    </>
  )
}
