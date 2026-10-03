import * as React from 'react'
import { cn } from '@/lib/utils'

/**
 * A scroll container that knows whether the reader is at the bottom.
 *
 * Two behaviours matter and both are easy to get wrong: following new content
 * *only* while the reader is already at the bottom (so scrolling up to read is
 * never yanked away), and an escape hatch back down. `atBottom` is derived from
 * the viewport rather than tracked with a flag, so it stays correct when content
 * grows during streaming.
 */
interface ScrollerCtx {
  viewportRef: React.RefObject<HTMLDivElement>
  atBottom: boolean
  atTop: boolean
  scrollToBottom: (smooth?: boolean) => void
  scrollToTop: (smooth?: boolean) => void
}

const Ctx = React.createContext<ScrollerCtx | null>(null)

function useScroller() {
  const ctx = React.useContext(Ctx)
  if (!ctx) throw new Error('MessageScroller* must be used inside <MessageScrollerProvider>')
  return ctx
}

export function MessageScrollerProvider({
  autoScroll = false,
  children,
}: {
  autoScroll?: boolean
  children: React.ReactNode
}) {
  const viewportRef = React.useRef<HTMLDivElement>(null)
  const [atBottom, setAtBottom] = React.useState(true)
  const [atTop, setAtTop] = React.useState(true)

  const scrollToBottom = React.useCallback((smooth = true) => {
    const el = viewportRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  }, [])

  const scrollToTop = React.useCallback((smooth = true) => {
    const el = viewportRef.current
    if (!el) return
    el.scrollTo({ top: 0, behavior: smooth ? 'smooth' : 'auto' })
  }, [])

  React.useEffect(() => {
    const el = viewportRef.current
    if (!el) return
    const onScroll = () => {
      setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 48)
      setAtTop(el.scrollTop < 24)
    }
    onScroll()
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  // No dependency array: this must also run while a reply streams in, when the
  // content changes without a render we can key on.
  React.useEffect(() => {
    if (!autoScroll || !atBottom) return
    const el = viewportRef.current
    if (el) el.scrollTop = el.scrollHeight
  })

  const value = React.useMemo(
    () => ({ viewportRef, atBottom, atTop, scrollToBottom, scrollToTop }),
    [atBottom, atTop, scrollToBottom, scrollToTop],
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function MessageScroller({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('relative flex min-h-0 flex-1 flex-col', className)} {...props} />
}

export function MessageScrollerViewport({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  const { viewportRef } = useScroller()
  return (
    <div ref={viewportRef} className={cn('min-h-0 flex-1 overflow-y-auto', className)} {...props} />
  )
}

export function MessageScrollerContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex flex-col', className)} {...props} />
}

export function MessageScrollerItem({
  messageId,
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { messageId?: string }) {
  return <div data-message-id={messageId} className={cn(className)} {...props} />
}

export function MessageScrollerButton({
  className,
  children,
  size = 'sm',
  ...props
}: Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'size'> & { size?: 'sm' | 'default' }) {
  const { atBottom, scrollToBottom } = useScroller()
  return (
    <button
      type="button"
      data-active={!atBottom}
      onClick={() => scrollToBottom()}
      className={cn(
        'absolute bottom-2 left-1/2 inline-flex -translate-x-1/2 items-center gap-1.5 rounded-full border bg-background font-medium text-muted-foreground shadow-sm transition-all duration-200 hover:text-foreground',
        size === 'sm' ? 'h-7 px-3.5 text-xs' : 'h-9 px-4 text-sm',
        atBottom
          ? 'pointer-events-none translate-y-1 opacity-0'
          : 'pointer-events-auto translate-y-0 opacity-100',
        className,
      )}
      {...props}
    >
      {children}
    </button>
  )
}

/** The mirror of the button above: visible once the reader has scrolled away
 *  from the top, so a long transcript never traps them at the bottom. */
export function MessageScrollerTopButton({
  className,
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const { atTop, scrollToTop } = useScroller()
  return (
    <button
      type="button"
      data-active={!atTop}
      onClick={() => scrollToTop()}
      className={cn(
        'glass-panel absolute right-4 top-2 inline-flex h-7 items-center gap-1.5 rounded-full px-3 text-[11.5px] font-medium text-muted-foreground shadow-soft transition-all duration-200 hover:text-foreground',
        atTop
          ? 'pointer-events-none -translate-y-1 opacity-0'
          : 'pointer-events-auto translate-y-0 opacity-100',
        className,
      )}
      {...props}
    >
      {children}
    </button>
  )
}
