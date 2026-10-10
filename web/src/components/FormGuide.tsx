import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCw, Search, Sparkles, X } from 'lucide-react'
import { api, type FreeModel } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableWrap } from '@/components/ui/table'
import { TableSkeleton } from '@/components/Skeleton'
import { cn } from '@/lib/utils'

/** Parse a JSON-string column the store keeps as TEXT ([] or {} shapes). */
function parseJson<T>(raw: string | null, fallback: T): T {
  if (!raw) return fallback
  try {
    return JSON.parse(raw) as T
  } catch {
    return fallback
  }
}

interface Source {
  title?: string
  url?: string
}

/**
 * Free models — every free arm with the agent-written summary, plus a pinned
 * reading column.
 *
 * Two columns, not an overlay: the table scrolls independently while the pinned
 * dossiers sit in a sticky sidebar (they stack on small screens instead of
 * covering the table). Clicking a model pins its full tale; a card closes on
 * click, the newest on Escape. Reading the column bottom-to-top replays the order
 * you clicked. Summaries are TinyFish-backed, so each claim is citable.
 */
export function FormGuide() {
  const [rows, setRows] = useState<FreeModel[]>([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [q, setQ] = useState('')
  const [pinned, setPinned] = useState<FreeModel[]>([])
  const [searchWeb, setSearchWeb] = useState(true)
  const [scouting, setScouting] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    api
      .freeModels()
      .then((r) => {
        setRows(r.models)
        setErr(null)
      })
      .catch((e) => setErr(e.message ?? 'failed to load free models'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // Escape dismisses the most recently pinned card.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setPinned((prev) => (prev.length ? prev.slice(0, -1) : prev))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  function togglePinned(m: FreeModel) {
    if (!m.core_competency && !m.story) return
    setPinned((prev) =>
      prev.some((p) => p.deploy_id === m.deploy_id)
        ? prev.filter((p) => p.deploy_id !== m.deploy_id)
        : [...prev, m],
    )
  }

  async function scoutNow() {
    setScouting(true)
    try {
      await api.dossiersRefresh({ search: searchWeb })
      load()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'the scout pass failed')
    } finally {
      setScouting(false)
    }
  }

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    if (!needle) return rows
    return rows.filter(
      (m) =>
        (m.display_name ?? '').toLowerCase().includes(needle) ||
        (m.provider ?? '').toLowerCase().includes(needle) ||
        (m.core_competency ?? '').toLowerCase().includes(needle) ||
        (m.summary ?? '').toLowerCase().includes(needle),
    )
  }, [rows, q])

  const scouted = rows.filter((m) => m.core_competency).length
  const isPinned = (id: string) => pinned.some((p) => p.deploy_id === id)

  if (loading) return <TableSkeleton rows={8} />

  return (
    <div
      className={cn(
        'grid items-start gap-5',
        pinned.length > 0 ? 'lg:grid-cols-[minmax(0,1fr)_360px]' : 'grid-cols-1',
      )}
    >
      <Card className="min-w-0">
        <CardContent className="p-0">
          {/* Header */}
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border/70 px-5 py-4">
            <div className="min-w-0">
              <h2 className="flex items-center gap-2 text-[15px] font-semibold tracking-tight text-foreground">
                <span className="h-4 w-1 shrink-0 rounded-full bg-brand" />
                Free models
              </h2>
              <p className="mt-1 text-[12.5px] text-muted-foreground">
                {rows.length} free model{rows.length === 1 ? '' : 's'} · {scouted} mustered.
                Each a warrior, each task a capability it commands. Summaries via{' '}
                <span className="text-foreground/80">TinyFish</span>.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="Filter…"
                  className="h-8 w-44 rounded-lg border border-border bg-background pl-8 pr-2 text-[12.5px] outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              <label
                className="flex cursor-pointer items-center gap-1.5 text-[12px] text-muted-foreground"
                title="Let the scout run a TinyFish web search for each arm it researches"
              >
                <input
                  type="checkbox"
                  checked={searchWeb}
                  onChange={(e) => setSearchWeb(e.target.checked)}
                  className="h-3.5 w-3.5 rounded border-border"
                />
                Web search
              </label>
              <Button size="sm" variant="outline" className="h-8 gap-1.5" title="Reload" onClick={load}>
                <RefreshCw className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">Reload</span>
              </Button>
              <Button
                size="sm"
                className="h-8 gap-1.5"
                title="Research the free models now (admin — spends agent + search calls)"
                onClick={scoutNow}
                disabled={scouting}
              >
                <Sparkles className={cn('h-3.5 w-3.5', scouting && 'animate-pulse')} />
                <span className="hidden sm:inline">{scouting ? 'Scouting…' : 'Scout now'}</span>
              </Button>
            </div>
          </div>

          {err && (
            <p className="m-4 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-[12.5px] text-destructive">
              {err}
            </p>
          )}

          {filtered.length === 0 ? (
            <p className="py-12 text-center text-sm text-muted-foreground">
              {rows.length === 0 ? 'No free models in the registry.' : 'Nothing matches that filter.'}
            </p>
          ) : (
            <TableWrap>
              {/* `table-fixed` makes the header widths binding. Without it the
                  competency column is sized by its longest line and, because
                  `TableCell` is `whitespace-nowrap` by default, spills into
                  Capabilities. */}
              <Table className="table-fixed">
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[26%]">Warrior</TableHead>
                    <TableHead>Core competency</TableHead>
                    <TableHead className="w-[24%]">Capabilities</TableHead>
                    <TableHead className="w-[92px] text-right">Confidence</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((m) => {
                    const best = parseJson<string[]>(m.best_for, [])
                    const ready = Boolean(m.core_competency || m.story)
                    return (
                      <TableRow
                        key={m.deploy_id}
                        onClick={() => togglePinned(m)}
                        className={cn('align-top', ready && 'cursor-pointer', isPinned(m.deploy_id) && 'bg-brand/[0.06]')}
                      >
                        <TableCell className="overflow-hidden">
                          <div className="truncate font-medium text-foreground/90" title={m.display_name ?? m.deploy_id}>
                            {m.display_name ?? m.deploy_id}
                          </div>
                          {m.warrior && (
                            <div className="truncate text-[11px] font-semibold italic text-brand" title={m.warrior}>
                              ⚔ {m.warrior}
                            </div>
                          )}
                          <div className="truncate font-mono text-[10.5px] text-muted-foreground" title={m.provider}>
                            {m.provider}
                          </div>
                        </TableCell>
                        <TableCell className="overflow-hidden whitespace-normal">
                          {ready ? (
                            <>
                              <div className="break-words text-[12.5px] leading-5 text-foreground/85">
                                {m.core_competency}
                              </div>
                              {m.summary && (
                                <div className="mt-0.5 line-clamp-2 break-words text-[11.5px] leading-5 text-muted-foreground">
                                  {m.summary}
                                </div>
                              )}
                            </>
                          ) : (
                            <span className="text-[12px] italic text-muted-foreground">Not scouted yet</span>
                          )}
                        </TableCell>
                        <TableCell className="whitespace-normal">
                          <div className="flex flex-wrap gap-1">
                            {best.length === 0 ? (
                              <span className="text-[12px] text-muted-foreground">—</span>
                            ) : (
                              best.slice(0, 3).map((b) => (
                                <Badge key={b} variant="secondary" className="font-mono text-[10px]">
                                  {b}
                                </Badge>
                              ))
                            )}
                          </div>
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-right font-mono text-[12px]">
                          {m.confidence == null ? '—' : `${(m.confidence * 100).toFixed(0)}%`}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </TableWrap>
          )}
        </CardContent>
      </Card>

      {pinned.length > 0 && (
        <aside
          className="flex min-w-0 flex-col gap-2 lg:sticky lg:top-20 lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto lg:pr-1"
          aria-label="Pinned free models"
        >
          <p className="text-[10.5px] text-muted-foreground">
            {pinned.length} pinned · newest on top · click a card or press{' '}
            <kbd className="rounded border border-border bg-muted px-1 font-mono">Esc</kbd>
          </p>
          {/* Reversed array so the newest sits on top and the first click settles
              at the bottom: reading bottom-to-top replays your click order. */}
          {[...pinned].reverse().map((m) => (
            <PinnedCard key={m.deploy_id} model={m} onDismiss={() => togglePinned(m)} />
          ))}
        </aside>
      )}
    </div>
  )
}

/** One pinned dossier. Clicking anywhere on it (except a source link) dismisses it. */
function PinnedCard({ model, onDismiss }: { model: FreeModel; onDismiss: () => void }) {
  const strengths = parseJson<string[]>(model.strengths, [])
  const weaknesses = parseJson<string[]>(model.weaknesses, [])
  const whenToUse = parseJson<string[]>(model.when_to_use, [])
  const whenNot = parseJson<string[]>(model.when_not_to_use, [])
  const sources = parseJson<Source[]>(model.search_sources, [])
  const best = parseJson<string[]>(model.best_for, [])

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onDismiss}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') onDismiss()
      }}
      title="Click to dismiss"
      className="cursor-pointer rounded-xl border border-border bg-card p-3.5 shadow-sm transition-colors hover:border-brand/40"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-[13px] font-semibold text-foreground">
            {model.display_name ?? model.deploy_id}
          </div>
          <div className="truncate font-mono text-[10px] text-muted-foreground">{model.deploy_id}</div>
        </div>
        <X className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      </div>

      {(model.warrior || model.story) && (
        <div className="mt-2.5 rounded-lg border border-brand/30 bg-brand/[0.06] px-3 py-2">
          {model.warrior && (
            <div className="text-[12.5px] font-bold tracking-tight text-brand">⚔ {model.warrior}</div>
          )}
          {model.story && (
            <p className="mt-1 text-[12px] italic leading-5 text-foreground/85">{model.story}</p>
          )}
        </div>
      )}

      {best.length > 0 && (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {best.map((b) => (
            <Badge key={b} variant="secondary" className="font-mono text-[10.5px]">
              {b}
            </Badge>
          ))}
        </div>
      )}

      {model.core_competency && (
        <p className="mt-2.5 text-[12.5px] font-medium leading-5 text-foreground/90">
          {model.core_competency}
        </p>
      )}
      {model.summary && (
        <p className="mt-1.5 text-[12px] leading-5 text-muted-foreground">{model.summary}</p>
      )}

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <BulletList title="When to use" items={whenToUse} tone="text-free" />
        <BulletList title="When not to use" items={whenNot} tone="text-destructive" />
        <BulletList title="Strengths" items={strengths} tone="text-foreground/80" />
        <BulletList title="Weaknesses" items={weaknesses} tone="text-muted-foreground" />
      </div>

      {sources.length > 0 && (
        <div className="mt-3 border-t border-border/60 pt-2">
          <p className="text-[10.5px] font-medium text-muted-foreground">
            Sources <span className="font-normal">(TinyFish)</span>
          </p>
          <ul className="mt-1 space-y-0.5">
            {sources.map((s, i) => (
              <li key={i} className="truncate text-[11.5px]">
                <a
                  href={s.url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  className="text-brand underline-offset-2 hover:underline"
                >
                  {s.title || s.url}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function BulletList({ title, items, tone }: { title: string; items: string[]; tone: string }) {
  if (items.length === 0) return null
  return (
    <div className="min-w-0">
      <p className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">{title}</p>
      <ul className="mt-1 space-y-0.5">
        {items.map((it, i) => (
          <li key={i} className={cn('flex gap-1.5 text-[12px] leading-5', tone)}>
            <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-current opacity-50" />
            <span className="min-w-0">{it}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
