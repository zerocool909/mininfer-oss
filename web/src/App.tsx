import { useEffect, useRef, useState } from 'react'
import { LayoutDashboard, Terminal, Settings, Boxes, Command, MessageSquarePlus, Scale, SunMoon, Keyboard, Bell, Trophy } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Overview } from '@/components/Overview'
import { ModelsTab } from '@/components/ModelsTab'
import { FormGuide } from '@/components/FormGuide'
import { Playground } from '@/components/Playground'
import type { TestTarget } from '@/components/Playground'
import { SettingsTab } from '@/components/SettingsTab'
import { NotificationsDrawer } from '@/components/NotificationsDrawer'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { ThemeToggle } from '@/components/ThemeToggle'
import { ConnectModal } from '@/components/ConnectModal'
import { CommandPalette, type PaletteCommand } from '@/components/CommandPalette'
import { emitCommand } from '@/lib/bus'
import { api, type Stats } from '@/lib/api'
import { Button } from '@/components/ui/button'

const NAV = [
  { id: 'playground', label: 'Playground', icon: Terminal },
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'formguide', label: 'Free models', icon: Trophy },
  { id: 'models', label: 'Models', icon: Boxes },
  { id: 'settings', label: 'Providers', icon: Settings },
] as const

export function App() {
  const [tab, setTab] = useState<string>(() => {
    const h = window.location.hash.replace('#', '')
    return NAV.some((n) => n.id === h) ? h : 'playground'
  })
  const [healthy, setHealthy] = useState<boolean | null>(null)
  // Live registry counts for the footer. It used to be a hardcoded "23,799
  // models indexed" plus "Local SQLite registry" — a number that was never true
  // of any actual registry, and an engine claim that is wrong on Postgres.
  const [stats, setStats] = useState<Stats | null>(null)
  const [connectOpen, setConnectOpen] = useState(false)
  // The review queue surface. The count is refetched whenever the drawer opens or
  // closes, so a decision made inside it is reflected on the bell.
  const [notifOpen, setNotifOpen] = useState(false)
  const [notifCount, setNotifCount] = useState(0)
  const rootRef = useRef<HTMLDivElement>(null)
  /** A model handed over from the Overview's "Test" button, consumed by the chat. */
  const [testTarget, setTestTarget] = useState<TestTarget | null>(null)

  useEffect(() => {
    fetch('/healthz')
      .then((r) => setHealthy(r.ok))
      .catch(() => setHealthy(false))
  }, [])

  useEffect(() => {
    api.stats().then(setStats).catch(() => undefined)
  }, [])

  useEffect(() => {
    api.anomalies('open')
      .then((a) => setNotifCount(a.count))
      .catch(() => undefined)
  }, [notifOpen])

  // A modal drawer has to take the page behind it out of the tab order and the
  // accessibility tree, not merely cover it. `inert` on the header/main/footer
  // does that; the drawer and the other overlays are siblings, so they stay live.
  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const behind = root.querySelectorAll(':scope > header, :scope > main, :scope > footer')
    for (const el of behind) {
      if (notifOpen) el.setAttribute('inert', '')
      else el.removeAttribute('inert')
    }
  }, [notifOpen])

  // Deep-linkable tabs: /#playground survives a refresh and is screenshot-able.
  useEffect(() => {
    const onHash = () => {
      const h = window.location.hash.replace('#', '')
      if (NAV.some((n) => n.id === h)) setTab(h)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  function go(id: string) {
    setTab(id)
    window.location.hash = id
  }

  const isChat = tab === 'playground'

  const commands: PaletteCommand[] = [
    ...NAV.map((n) => ({
      id: `nav-${n.id}`,
      label: `Go to ${n.label}`,
      group: 'Navigate',
      icon: n.icon,
      run: () => go(n.id),
    })),
    {
      id: 'new-chat',
      label: 'New chat',
      group: 'Actions',
      icon: MessageSquarePlus,
      run: () => {
        go('playground')
        emitCommand('new-chat')
      },
    },
    {
      id: 'compare',
      label: 'Toggle compare (two models)',
      group: 'Actions',
      icon: Scale,
      run: () => {
        go('playground')
        emitCommand('toggle-compare')
      },
    },
    {
      id: 'theme',
      label: 'Toggle theme',
      group: 'Actions',
      icon: SunMoon,
      run: () => emitCommand('toggle-theme'),
    },
    {
      id: 'focus-composer',
      label: 'Focus the composer',
      group: 'Actions',
      icon: Keyboard,
      run: () => {
        go('playground')
        emitCommand('focus-composer')
      },
    },
  ]

  return (
    <div ref={rootRef} className="relative flex min-h-screen flex-col bg-background">
      <header className="sticky top-0 z-30 h-14 border-b border-border/70 bg-background/60 backdrop-blur-xl">
        <div className="relative z-10 mx-auto flex h-full w-full items-center gap-3 px-4 sm:px-6">
          {/* Brand */}
          <button
            onClick={() => go('playground')}
            className="group flex shrink-0 items-center gap-3 rounded-lg outline-none transition-opacity hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <img
              src="/mininfer-icon.svg"
              alt="min(Infer)"
              className="h-9 w-9 rounded-[9px] object-contain shadow-glow-sm transition-transform group-hover:scale-105"
            />
            {/* Mirrors the wordmark in mininfer-logo.svg: `min(Infer)` with the
                function-notation parentheses dimmed against the product name. */}
            <span className="hidden text-base font-semibold tracking-tight text-foreground sm:block">
              <span className="text-brand-teal">min</span>
              <span className="font-normal text-muted-foreground">(</span>
              Infer
              <span className="font-normal text-muted-foreground">)</span>
            </span>
          </button>

          <span className="hidden h-5 w-px bg-border sm:block" />

          {/* Navigation */}
          <nav className="flex h-full items-stretch gap-0.5 overflow-x-auto">
            {NAV.map(({ id, label, icon: Icon }) => {
              const active = tab === id
              return (
                <button
                  key={id}
                  // The label is hidden below `md`, which left the button with no
                  // accessible name at all on a phone — a screen reader (and a
                  // test) saw four unlabelled buttons. Caught by
                  // e2e/responsive.spec.ts.
                  aria-label={label}
                  onClick={() => go(id)}
                  className={cn(
                    'relative inline-flex items-center gap-2 px-3 text-[13px] font-medium transition-colors',
                    active
                      ? 'text-foreground'
                      : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  <Icon className="h-4 w-4" strokeWidth={active ? 2.1 : 1.8} />
                  <span className="hidden md:inline">{label}</span>
                  {active && (
                    <span className="absolute inset-x-2.5 bottom-0 h-[2px] rounded-t-full bg-brand" />
                  )}
                </button>
              )
            })}
          </nav>

          {/* Actions */}
          <div className="ml-auto flex items-center gap-2">
            <div
              className="hidden items-center gap-2 rounded-full border border-border bg-card/60 py-1 pl-2.5 pr-3 sm:flex"
              title={healthy ? 'API reachable' : healthy === null ? 'Checking…' : 'API unreachable'}
            >
              <span className="relative flex h-1.5 w-1.5">
                {healthy && (
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-free/70" />
                )}
                <span
                  className={cn(
                    'relative inline-flex h-1.5 w-1.5 rounded-full',
                    healthy ? 'bg-free' : healthy === null ? 'bg-muted-foreground' : 'bg-destructive',
                  )}
                />
              </span>
              <span className="text-[11px] font-medium tracking-wide text-muted-foreground">
                {healthy === null ? 'Checking' : healthy ? 'Live' : 'Offline'}
              </span>
            </div>

            <Button
              size="sm"
              variant="outline"
              onClick={() => setConnectOpen(true)}
              className="h-8 gap-1.5 rounded-lg border-border bg-card/40 px-2.5 text-[12.5px] font-medium"
            >
              <Terminal className="h-3.5 w-3.5 text-brand" strokeWidth={2} />
              <span className="hidden sm:inline">Connect</span>
            </Button>

            <Button
              size="sm"
              variant="ghost"
              onClick={() => window.dispatchEvent(new Event('mi:palette'))}
              className="hidden h-8 gap-1.5 rounded-lg px-2 text-[12.5px] font-medium text-muted-foreground hover:text-foreground sm:inline-flex"
              title="Command palette"
            >
              <Command className="h-3.5 w-3.5" />
              <kbd className="rounded border border-border bg-muted px-1 font-mono text-[10px] leading-4">
                ⌘K
              </kbd>
            </Button>

            <Button
              size="sm"
              variant="ghost"
              onClick={() => setNotifOpen(true)}
              aria-label={
                notifCount > 0 ? `Notifications, ${notifCount} open` : 'Notifications'
              }
              className="relative h-8 gap-1.5 rounded-lg px-2 text-[12.5px] font-medium text-muted-foreground hover:text-foreground"
            >
              <Bell className="h-3.5 w-3.5" />
              {notifCount > 0 && (
                <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-semibold leading-none text-destructive-foreground">
                  {notifCount > 9 ? '9+' : notifCount}
                </span>
              )}
            </Button>

            <ThemeToggle />
          </div>
        </div>
      </header>

      <main
        className={cn(
          'relative z-10 w-full flex-1',
          !isChat && 'mx-auto max-w-[1400px] px-6 py-8',
        )}
      >
        {/* Keyed by tab, so a crash on one page does not brick the rest: the
            header and nav stay live, and switching tabs remounts a fresh subtree. */}
        <ErrorBoundary key={tab}>
        {tab === 'overview' ? (
          <Overview
            onTestModel={(deployId, task) => {
              setTestTarget({ deployId, task })
              go('playground')
            }}
          />
        ) : tab === 'formguide' ? (
          <FormGuide />
        ) : tab === 'models' ? (
          <ModelsTab />
        ) : tab === 'settings' ? (
          <SettingsTab />
        ) : (
          <Playground testTarget={testTarget} onTestConsumed={() => setTestTarget(null)} />
        )}
        </ErrorBoundary>
      </main>

      {tab === 'overview' && (
        <footer className="relative z-10 mx-auto flex w-full max-w-[1400px] flex-wrap items-center justify-between gap-2 border-t border-border/60 px-6 py-5 text-[12px] text-muted-foreground">
          <div className="flex items-center gap-2">
            <span className="font-mono font-medium text-foreground/80">mi proxy</span>
            <span className="text-border">•</span>
            <span>Free first — pay only when free isn’t good enough</span>
          </div>
          <div className="flex items-center gap-3 text-[11.5px]">
            {stats && (
              <span>
                {stats.counts.weights.toLocaleString()} models{' '}
                <span className="text-border">·</span>{' '}
                {stats.counts.deployments.toLocaleString()} deployments
              </span>
            )}
          </div>
        </footer>
      )}

      <ConnectModal open={connectOpen} onClose={() => setConnectOpen(false)} />
      <NotificationsDrawer open={notifOpen} onClose={() => setNotifOpen(false)} />
      <CommandPalette commands={commands} />
    </div>
  )
}
