import { useEffect, useMemo, useRef, useState } from 'react'
import { Search, CornerDownLeft } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface PaletteCommand {
  id: string
  label: string
  group: string
  icon: LucideIcon
  hint?: string
  run: () => void
}

/**
 * ⌘K. One place to jump, switch and act without hunting the chrome.
 *
 * Deliberately dependency-free: a filtered list, arrow keys and Enter. A palette
 * is a keyboard surface first, so the keyboard handling is the feature, not the
 * styling.
 */
export function CommandPalette({ commands }: { commands: PaletteCommand[] }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const results = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return commands
    return commands.filter(
      (c) => c.label.toLowerCase().includes(q) || c.group.toLowerCase().includes(q),
    )
  }, [commands, query])

  // Group headers, computed from whatever the filter left behind.
  const grouped = useMemo(() => {
    const out: { group: string; items: PaletteCommand[] }[] = []
    for (const cmd of results) {
      const last = out[out.length - 1]
      if (last && last.group === cmd.group) last.items.push(cmd)
      else out.push({ group: cmd.group, items: [cmd] })
    }
    return out
  }, [results])

  const flat = useMemo(() => grouped.flatMap((g) => g.items), [grouped])

  useEffect(() => {
    const openPalette = () => {
      setOpen(true)
      setQuery('')
      setActive(0)
    }
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((v) => !v)
        setQuery('')
        setActive(0)
      }
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('mi:palette', openPalette)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mi:palette', openPalette)
    }
  }, [])

  useEffect(() => {
    if (open) {
      // Focus after paint, or the input is not mounted yet on first open.
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [open])

  useEffect(() => {
    setActive(0)
  }, [query])

  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [active])

  if (!open) return null

  const run = (cmd: PaletteCommand) => {
    setOpen(false)
    cmd.run()
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((i) => Math.min(i + 1, flat.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const cmd = flat[active]
      if (cmd) run(cmd)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-background/60 px-4 pt-[12vh] backdrop-blur-sm"
      onClick={() => setOpen(false)}
    >
      <div
        role="dialog"
        aria-label="Command palette"
        className="w-full max-w-lg overflow-hidden rounded-2xl border border-border bg-popover shadow-lift"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <div className="flex items-center gap-2.5 border-b border-border px-4">
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search commands…"
            className="h-12 w-full bg-transparent text-[14px] text-foreground outline-none placeholder:text-muted-foreground"
          />
          <kbd className="shrink-0 rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
            esc
          </kbd>
        </div>

        <div ref={listRef} className="max-h-[52vh] overflow-y-auto p-1.5">
          {flat.length === 0 ? (
            <div className="px-3 py-8 text-center text-[13px] text-muted-foreground">
              No matching command
            </div>
          ) : (
            grouped.map((group) => (
              <div key={group.group} className="mb-1">
                <div className="px-2.5 py-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-muted-foreground">
                  {group.group}
                </div>
                {group.items.map((cmd) => {
                  const index = flat.indexOf(cmd)
                  const Icon = cmd.icon
                  return (
                    <button
                      key={cmd.id}
                      data-index={index}
                      onMouseEnter={() => setActive(index)}
                      onClick={() => run(cmd)}
                      className={cn(
                        'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors',
                        index === active ? 'bg-accent text-foreground' : 'text-muted-foreground',
                      )}
                    >
                      <Icon className="h-4 w-4 shrink-0 text-brand" strokeWidth={1.9} />
                      <span className="flex-1 truncate">{cmd.label}</span>
                      {cmd.hint && (
                        <span className="shrink-0 font-mono text-[10.5px] text-muted-foreground">
                          {cmd.hint}
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            ))
          )}
        </div>

        <div className="flex items-center gap-3 border-t border-border px-4 py-2 text-[11px] text-muted-foreground">
          <span className="flex items-center gap-1">
            <CornerDownLeft className="h-3 w-3" /> to run
          </span>
          <span className="flex items-center gap-1">
            <kbd className="rounded border border-border bg-muted px-1 font-mono text-[10px]">↑</kbd>
            <kbd className="rounded border border-border bg-muted px-1 font-mono text-[10px]">↓</kbd>
            to navigate
          </span>
        </div>
      </div>
    </div>
  )
}
