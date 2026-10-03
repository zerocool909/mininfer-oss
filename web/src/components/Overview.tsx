import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle, Boxes, Camera, Check, Eye, GitBranch, Globe, Maximize2, Minimize2, Play, RefreshCw, ScrollText, Server, ShieldAlert,
} from 'lucide-react'
import { api, type DecisionRow, type EconomicsOverview, type Plan, type ProviderRow, type QuotaRow, type Review, type WhyTag } from '@/lib/api'
import { Card, CardContent, SectionHeading } from '@/components/ui/card'
import { CostBadge } from '@/components/ui/badge'
import { Chip } from '@/components/ui/form'
import { Button } from '@/components/ui/button'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableWrap,
} from '@/components/ui/table'
import { LeaderboardTags } from './LeaderboardTags'
import { cn } from '@/lib/utils'
import { FunnelBar } from './FunnelBar'
import { StatCard } from './StatCard'
import { StatSkeleton, TableSkeleton } from './Skeleton'
import { count, money, num } from '@/lib/utils'

const CARDS = [
  { key: 'deployments', label: 'deployments', icon: Server },
  { key: 'weights', label: 'weights', icon: Boxes },
  { key: 'evidence', label: 'evidence', icon: ScrollText },
  { key: 'observations', label: 'observations', icon: Eye },
  { key: 'decisions', label: 'decisions', icon: GitBranch },
  { key: 'quarantine', label: 'quarantine', icon: ShieldAlert },
  { key: 'snapshots', label: 'snapshots', icon: Camera },
] as const

export function Overview({ onTestModel }: { onTestModel?: (deployId: string, task: string) => void } = {}) {
  const [stats, setStats] = useState<EconomicsOverview | null>(null)
  const [task, setTask] = useState('')
  const [plan, setPlan] = useState<Plan | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [autoRefreshInterval, setAutoRefreshInterval] = useState<number>(10)
  const [now, setNow] = useState<number>(Date.now())
  const [decisionsView, setDecisionsView] = useState<'full' | 'split'>('full')
  /** Deployments hibernated for review: they were free, and now they charge. */
  const [reviews, setReviews] = useState<Review[]>([])

  // Ticker for human-friendly "Updated Xs ago"
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const timeAgoText = useMemo(() => {
    if (!lastUpdated) return ''
    const diffSec = Math.max(0, Math.floor((now - lastUpdated.getTime()) / 1000))
    if (diffSec < 3) return 'Updated just now'
    if (diffSec < 60) return `Updated ${diffSec}s ago`
    const diffMin = Math.floor(diffSec / 60)
    return `Updated ${diffMin}m ago`
  }, [lastUpdated, now])

  const refresh = useCallback(
    async (showSpinner = true) => {
      if (showSpinner) setIsRefreshing(true)
      try {
        // One call, so the page cannot render a price beside a *different*
        // moment's trust state (P7).
        const s = await api.economics()
        setStats(s)
        setReviews(s.reviews ?? [])
        const currentTask = task || s.tasks[0] || ''
        if (!task && currentTask) {
          setTask(currentTask)
        }
        if (currentTask) {
          const p = await api.plan(currentTask)
          setPlan(p)
        }
        setLastUpdated(new Date())
        setErr(null)
      } catch (e: any) {
        if (!stats) setErr(e.message)
      } finally {
        if (showSpinner) setIsRefreshing(false)
      }
    },
    [task, stats],
  )

  /** Resolve a hibernation: optimistic, then re-read the plan. */
  const decideReview = async (deployId: string, approve: boolean) => {
    setReviews((prev) => prev.filter((r) => r.deploy_id !== deployId))
    await api.decideReview(deployId, approve).catch(() => undefined)
    refresh(false)
  }

  // Initial load
  useEffect(() => {
    refresh(false)
  }, [])

  // Auto-refresh timer
  useEffect(() => {
    if (autoRefreshInterval <= 0) return
    const id = setInterval(() => {
      refresh(false)
    }, autoRefreshInterval * 1000)
    return () => clearInterval(id)
  }, [autoRefreshInterval, refresh])

  // Refetch on window focus so switching back from terminal or playground is always fresh
  useEffect(() => {
    const onFocus = () => refresh(false)
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [refresh])

  // Keyboard shortcut 'r' to refresh
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (
        (e.key === 'r' || e.key === 'R') &&
        !['INPUT', 'TEXTAREA'].includes((e.target as HTMLElement)?.tagName)
      ) {
        refresh(true)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [refresh])

  // Refetch plan when switching selected task chip
  useEffect(() => {
    if (!task) return
    api
      .plan(task)
      .then(setPlan)
      .catch((e: Error) => setErr(e.message))
  }, [task])

  if (err) return <ErrorState message={err} />
  if (!stats) {
    // A failed load must say so. Rendering the skeleton forever is what made a
    // dead API look like a page that simply never finished loading.
    if (err) {
      return (
        <div className="mx-auto flex max-w-md flex-col items-center py-20 text-center">
          <div className="grid h-11 w-11 place-items-center rounded-2xl border border-destructive/30 bg-destructive/10">
            <AlertCircle className="h-5 w-5 text-destructive" />
          </div>
          <h2 className="mt-4 text-[16px] font-semibold text-foreground">
            Couldn’t load the registry
          </h2>
          <p className="mt-1.5 text-[13px] text-muted-foreground">{err}</p>
          <Button className="mt-5" onClick={() => refresh()} size="sm" variant="outline">
            <RefreshCw className="h-3.5 w-3.5" /> Retry
          </Button>
        </div>
      )
    }
    return <StatSkeleton />
  }

  const f = plan?.funnel ?? {}

  return (
    <div className="animate-fade-up space-y-6">
      {/* Live Sync & Refresh Bar */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-border/60 pb-3">
        <div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-base font-bold tracking-tight text-foreground">
              Router Intelligence & Telemetry
            </h1>
            {autoRefreshInterval > 0 ? (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-400">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
                </span>
                Live ({autoRefreshInterval}s)
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-muted-foreground/30 bg-muted/40 px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground" />
                Paused
              </span>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Active deployments, real-time routing funnel, and ledger of recent dispatch decisions
          </p>
        </div>

        <div className="flex items-center gap-2">
          {lastUpdated && (
            <span className="text-[11px] text-muted-foreground tnum hidden sm:inline-block">
              {timeAgoText}
            </span>
          )}

          <div className="flex items-center rounded-lg border border-border/70 bg-card p-0.5 text-xs">
            {[
              { label: '5s', value: 5 },
              { label: '10s', value: 10 },
              { label: '30s', value: 30 },
              { label: 'Off', value: 0 },
            ].map((opt) => (
              <button
                key={opt.value}
                onClick={() => setAutoRefreshInterval(opt.value)}
                className={cn(
                  'rounded px-2 py-1 text-[11px] font-medium transition-colors',
                  autoRefreshInterval === opt.value
                    ? 'bg-muted text-foreground shadow-xs font-semibold'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {opt.label}
              </button>
            ))}
          </div>

          <Button
            variant="outline"
            size="sm"
            onClick={() => refresh(true)}
            disabled={isRefreshing}
            className="h-8 gap-1.5 text-xs border-border/80 hover:bg-muted/80 shadow-xs"
            title="Refresh now (or press R)"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', isRefreshing && 'animate-spin text-brand')} />
            <span>Refresh</span>
          </Button>
        </div>
      </div>

      {/* What the reconciler refused or flagged. Above the fold because it is the
          one thing on this page that says the registry might be wrong. */}
      {stats.anomalies.length > 0 && (
        <section className="rounded-xl border border-destructive/40 bg-destructive/5 p-4">
          <div className="flex items-center gap-2">
            <AlertCircle className="h-4 w-4 text-destructive" />
            <h2 className="text-sm font-semibold text-foreground">
              {stats.anomalies.length} pricing anomal
              {stats.anomalies.length === 1 ? 'y' : 'ies'} open
            </h2>
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            The reconciler refused or flagged a price against the market. The
            deployment keeps its own last verified value — these are for review, and
            nothing was rewritten automatically.
          </p>
          <div className="mt-3 space-y-2">
            {stats.anomalies.map((a) => (
              <div
                key={a.anomaly_id}
                className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-3 py-2"
              >
                <span
                  className={cn(
                    'shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
                    SEVERITY_TONE[a.severity] ?? 'border-border text-muted-foreground',
                  )}
                >
                  {a.severity}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-[12px] text-foreground" title={a.deploy_id}>
                    {a.deploy_id}
                    <span className="ml-1.5 text-[10px] uppercase text-muted-foreground">
                      {a.dimension}
                    </span>
                  </div>
                  <div className="text-[11px] text-muted-foreground">{a.detail}</div>
                </div>
                {a.factor != null && (
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {a.factor.toFixed(1)}×
                  </span>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {reviews.length > 0 && (
        <section className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-4">
          <div className="flex items-center gap-2">
            <AlertCircle className="h-4 w-4 text-amber-500" />
            <h2 className="text-sm font-semibold text-foreground">
              {reviews.length} model{reviews.length === 1 ? '' : 's'} hibernated for review
            </h2>
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            These were free and now charge. They are out of routing until you decide.
          </p>
          <div className="mt-3 space-y-2">
            {reviews.map((r) => (
              <div
                key={r.deploy_id}
                className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-[12px] text-foreground">
                    {r.deploy_id}
                  </div>
                  <div className="text-[11px] text-muted-foreground">
                    {r.pricing_type_before ? `${r.pricing_type_before} → paid · ` : ''}
                    {r.status_reason}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 rounded-full text-[11px]"
                  onClick={() => decideReview(r.deploy_id, false)}
                >
                  Keep out
                </Button>
                <Button
                  size="sm"
                  className="h-7 rounded-full text-[11px]"
                  onClick={() => decideReview(r.deploy_id, true)}
                >
                  Approve
                </Button>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
        {CARDS.map(({ key, label, icon }) => (
          <StatCard key={key} label={label} value={stats.counts[key]} icon={icon} />
        ))}
      </section>

      {/* The product's headline: it is not "which model won" but "what did that
          cost, and what did it avoid". Kept out of the counts grid because it is
          a ratio, not a count, and the estimate's caveat has to travel with it. */}
      {stats.savings && (
        <Card>
          <CardContent className="flex flex-wrap items-center gap-x-10 gap-y-3 p-5">
            <div>
              <div className="text-[11px] uppercase tracking-wide text-muted-foreground">Spend</div>
              <div className="text-xl font-semibold tabular-nums">
                ${stats.savings.actual_cost_usd.toFixed(4)}
              </div>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wide text-muted-foreground">Avoided</div>
              <div className="text-xl font-semibold tabular-nums text-free">
                ${stats.savings.saved_usd.toFixed(4)}
              </div>
            </div>
            <p className="max-w-xl text-xs text-muted-foreground">
              Avoided is what the {count(stats.savings.free_calls)} free call
              {stats.savings.free_calls === 1 ? '' : 's'} would have cost at the cheapest{' '}
              <em>paid</em> deployment of the same model.
              {stats.savings.unpriced_free_calls > 0 && (
                <> {count(stats.savings.unpriced_free_calls)} free call
                  {stats.savings.unpriced_free_calls === 1 ? '' : 's'} had no paid sibling to price
                  against, so the total is a floor.</>
              )}{' '}
              Prices are the registry's current ones, so this is an estimate.
            </p>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="p-5">
          <SectionHeading
            title="Routing funnel"
            hint={
              f.total
                ? `${count(f.total)} deployments → ${count(f.after_hard_filter)} eligible`
                : 'how the registry narrows to a shortlist'
            }
            action={
              <span className="font-mono text-[11px] text-muted-foreground">
                strategy {plan?.strategy ?? '—'} · diversity {plan?.diversity ?? '—'}
              </span>
            }
          />

          <div className="mb-4 flex flex-wrap gap-1.5">
            {stats.tasks.map((t) => (
              <Chip key={t} active={t === task} onClick={() => setTask(t)}>
                {t}
              </Chip>
            ))}
          </div>

          <FunnelBar funnel={f} />
        </CardContent>
      </Card>

      <section>
        <SectionHeading
          title="Selected"
          hint="the shortlist this task would route to, cheapest-capable first"
        />
        {!plan ? (
          <TableSkeleton rows={3} cols={6} />
        ) : (
          <>
            <TableWrap>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-10 text-right">#</TableHead>
                    <TableHead>deployment</TableHead>
                    <TableHead>$ / success</TableHead>
                    <TableHead className="w-40">p(success)</TableHead>
                    <TableHead>prior</TableHead>
                    <TableHead className="text-right">avail</TableHead>
                    <TableHead className="text-right">headroom</TableHead>
                    <TableHead className="w-16 text-right">test</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {plan.chosen.map((c, i) => (
                    <TableRow key={c.deploy_id} className="group">
                      <TableCell className="tnum text-right text-muted-foreground">{i + 1}</TableCell>
                      <TableCell>
                        <DeployCell deployId={c.deploy_id} />
                      </TableCell>
                      <TableCell>
                        <CostBadge value={c.cost_per_success} />
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 w-16 overflow-hidden rounded-full bg-muted">
                            <div
                              className="h-full rounded-full bg-brand"
                              style={{ width: `${Math.min(100, (c.p_lb ?? 0) * 100)}%` }}
                            />
                          </div>
                          <span className="tnum text-xs text-muted-foreground">{num(c.p_lb)}</span>
                        </div>
                      </TableCell>
                      <TableCell
                        className="text-xs text-muted-foreground"
                        title={(c.leaderboards ?? [])
                          .map((t) => `${t.source} · ${t.label}${t.value !== undefined ? `: ${t.value}` : ''}`)
                          .join('\n')}
                      >
                        {c.prior_key ?? '—'}
                        {!!c.leaderboards?.length && (
                          <span className="ml-1 text-muted-foreground/60">
                            ({c.leaderboards.length})
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="tnum text-right">{c.availability.toFixed(2)}</TableCell>
                      <TableCell className="tnum text-right">
                        {c.headroom == null ? '—' : `${Math.round(c.headroom * 100)}%`}
                      </TableCell>
                      <TableCell className="text-right">
                        <button
                          onClick={() => {
                            // Carry the model and task across, not just the tab:
                            // switching to an empty composer is not "testing" it.
                            onTestModel?.(c.deploy_id, task)
                            window.location.hash = 'playground'
                          }}
                          className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5 text-[11px] font-medium text-muted-foreground transition-colors hover:border-brand/40 hover:bg-brand/5 hover:text-brand"
                          title={`Test ${c.deploy_id} in the Playground`}
                        >
                          <Play className="h-2.5 w-2.5 fill-current" />
                          Test
                        </button>
                      </TableCell>
                    </TableRow>
                  ))}
                  {plan.chosen.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={8} className="py-8 text-center text-muted-foreground">
                        no eligible deployment for this task
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </TableWrap>
            {plan.chosen[0] && (
              <div className="animate-fade-up -mt-px rounded-b-lg border border-t-0 bg-card/60 pt-0.5 pb-3">
                <LeaderboardTags tags={plan.chosen[0].leaderboards} />
              </div>
            )}
          </>
        )}
      </section>

      {/* Upstreams & Quotas / Decisions sections */}
      {decisionsView === 'full' ? (
        <>
          {/* Twin 2-column grid for Provider Volume & Quota Headroom */}
          <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Deployments by provider */}
            <div className="flex flex-col h-[400px]">
              <SectionHeading
                title="Deployments by provider"
                hint="top 20 by volume"
                action={
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {stats.providers.length} providers
                  </span>
                }
              />
              <TableWrap className="flex-1 overflow-auto rounded-xl border border-border/80 bg-card shadow-xs">
                <Table className="min-w-[380px]">
                  <TableHeader className="sticky top-0 bg-card/95 backdrop-blur-sm z-10">
                    <TableRow>
                      <TableHead>provider</TableHead>
                      <TableHead className="text-right">n</TableHead>
                      <TableHead className="text-right">free-ish</TableHead>
                      <TableHead className="text-right">min $/Mtok in</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {stats.providers.map((p) => (
                      <TableRow key={p.provider}>
                        <TableCell className="font-mono text-xs font-medium">{p.provider}</TableCell>
                        <TableCell className="tnum text-right">
                          <div className="flex items-center justify-end gap-2">
                            <div className="h-1.5 w-12 overflow-hidden rounded-full bg-muted">
                              <div
                                className="h-full rounded-full bg-brand"
                                style={{ width: `${Math.min(100, (p.n / (stats.providers[0]?.n || 1)) * 100)}%` }}
                              />
                            </div>
                            <span className="text-xs font-mono">{count(p.n)}</span>
                          </div>
                        </TableCell>
                        <TableCell className="tnum text-right text-xs font-mono">{count(p.free_n)}</TableCell>
                        <MinPriceCell p={p} />
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableWrap>
            </div>

            {/* Quota headroom */}
            <div className="flex flex-col h-[400px]">
              <SectionHeading
                title="Quota headroom"
                hint="tightest buckets first"
                action={
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {stats.quota.length} bucket{stats.quota.length === 1 ? '' : 's'}
                  </span>
                }
              />
              <TableWrap className="flex-1 overflow-auto rounded-xl border border-border/80 bg-card shadow-xs">
                <Table className="min-w-[380px]">
                  <TableHeader className="sticky top-0 bg-card/95 backdrop-blur-sm z-10">
                    <TableRow>
                      <TableHead>deployment</TableHead>
                      <TableHead>window</TableHead>
                      <TableHead className="text-right">used</TableHead>
                      <TableHead className="text-right">head</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {stats.quota.map((q) => (
                      <TableRow key={`${q.deploy_id}-${q.window}`}>
                        <TableCell className="max-w-[14rem] truncate font-mono text-xs" title={q.deploy_id}>
                          <DeployCell deployId={q.deploy_id} />
                        </TableCell>
                        <TableCell className="text-muted-foreground text-xs">{q.window}</TableCell>
                        <TableCell className="tnum text-right text-xs font-mono">
                          {q.used_n}/{q.limit_n ?? '—'}
                        </TableCell>
                        <HeadroomCell q={q} />
                      </TableRow>
                    ))}
                    {stats.quota.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={4} className="py-16 text-center text-muted-foreground">
                          <div className="flex flex-col items-center justify-center gap-1.5">
                            <ShieldAlert className="h-6 w-6 opacity-30 text-muted-foreground" />
                            <span className="text-xs font-medium">No declared quota buckets</span>
                            <span className="text-[11px] opacity-70">Deployments operate without active limit caps</span>
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </TableWrap>
            </div>
          </section>

          {/* Recent decisions (Full Width, matching Selected shortlist table size and grid) */}
          <section className="space-y-2">
            <SectionHeading
              title="Recent decisions"
              hint="every request is recorded, whatever the outcome"
              action={
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {count(stats.counts.decisions)} recorded · showing {stats.decisions.length}
                  </span>
                  <button
                    onClick={() => setDecisionsView('split')}
                    className="hidden sm:inline-flex items-center gap-1 rounded-md border border-border/70 bg-card px-2 py-1 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground transition-colors cursor-pointer"
                    title="Switch to 2-column split view"
                  >
                    <Minimize2 className="h-3 w-3" />
                    <span>Split view</span>
                  </button>
                </div>
              }
            />
            <TableWrap className="overflow-x-auto rounded-xl border border-border/80 bg-card shadow-xs">
              <Table className="min-w-[640px]">
                <TableHeader className="sticky top-0 bg-card/95 backdrop-blur-sm z-10">
                  <TableRow>
                    <TableHead className="w-36">time (IST)</TableHead>
                    <TableHead className="w-36">task</TableHead>
                    <TableHead className="w-72">chosen</TableHead>
                    <TableHead>why</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {stats.decisions.map((d, i) => (
                    <TableRow key={i} className="group hover:bg-muted/40 transition-colors">
                      <TableCell className="w-36 tnum">
                        <DecisionTimeCell d={d} />
                      </TableCell>
                      <TableCell className="w-36">
                        <div className="flex flex-col gap-1">
                          <span className="inline-flex items-center rounded-md border border-border/70 bg-muted/50 px-2 py-0.5 font-mono text-[11px] font-medium text-foreground/90 max-w-[130px] truncate" title={d.task}>
                            {d.task}
                          </span>
                          {d.mode === 'compare' && (
                            <span className="inline-flex items-center gap-0.5 rounded-full border border-purple-500/30 bg-purple-500/10 px-1.5 py-0.2 text-[9.5px] font-semibold text-purple-600 dark:text-purple-400 w-fit">
                              compare (2)
                            </span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="w-72">
                        {d.mode === 'compare' ? (
                          <CompareDecisionCell d={d} />
                        ) : d.chosen ? (
                          <DeployCell deployId={d.chosen} />
                        ) : (
                          <ModeCell mode={d.mode} />
                        )}
                      </TableCell>
                      <TableCell>
                        <WhyTags why={d.why} />
                      </TableCell>
                    </TableRow>
                  ))}
                  {stats.decisions.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={4} className="py-12 text-center text-muted-foreground">
                        <div className="flex flex-col items-center justify-center gap-1.5">
                          <GitBranch className="h-6 w-6 opacity-30 text-muted-foreground" />
                          <span className="text-xs font-medium">No decisions recorded yet</span>
                          <span className="text-[11px] opacity-70">
                            Routes triggered via proxy or playground will be logged here
                          </span>
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </TableWrap>
          </section>
        </>
      ) : (
        <>
          {/* Full-width Deployments by provider */}
          <section>
            <SectionHeading title="Deployments by provider" hint="top 20 by volume" />
            <TableWrap className="overflow-x-auto rounded-xl border border-border/80 bg-card shadow-xs">
              <Table className="min-w-[480px]">
                <TableHeader>
                  <TableRow>
                    <TableHead>provider</TableHead>
                    <TableHead className="text-right">n</TableHead>
                    <TableHead className="text-right">free-ish</TableHead>
                    <TableHead className="text-right">min $/Mtok in</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {stats.providers.map((p) => (
                    <TableRow key={p.provider}>
                      <TableCell className="font-mono text-xs font-medium">{p.provider}</TableCell>
                      <TableCell className="tnum text-right">
                        <div className="flex items-center justify-end gap-2">
                          <div className="h-1.5 w-12 overflow-hidden rounded-full bg-muted">
                            <div
                              className="h-full rounded-full bg-brand"
                              style={{ width: `${Math.min(100, (p.n / (stats.providers[0]?.n || 1)) * 100)}%` }}
                            />
                          </div>
                          <span>{count(p.n)}</span>
                        </div>
                      </TableCell>
                      <TableCell className="tnum text-right">{count(p.free_n)}</TableCell>
                      <MinPriceCell p={p} />
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableWrap>
          </section>

          {/* Split 2-column grid for Quota & Decisions with equal heights */}
          <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div className="flex flex-col h-[460px]">
              <SectionHeading title="Quota headroom" hint="tightest buckets first" />
              <TableWrap className="flex-1 overflow-auto rounded-xl border border-border/80 bg-card shadow-xs">
                <Table className="min-w-[360px]">
                  <TableHeader className="sticky top-0 bg-card/95 backdrop-blur-sm z-10">
                    <TableRow>
                      <TableHead>deployment</TableHead>
                      <TableHead>window</TableHead>
                      <TableHead className="text-right">used</TableHead>
                      <TableHead className="text-right">head</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {stats.quota.map((q) => (
                      <TableRow key={`${q.deploy_id}-${q.window}`}>
                        <TableCell className="max-w-[14rem] truncate font-mono text-xs" title={q.deploy_id}>
                          <DeployCell deployId={q.deploy_id} />
                        </TableCell>
                        <TableCell className="text-muted-foreground text-xs">{q.window}</TableCell>
                        <TableCell className="tnum text-right text-xs">
                          {q.used_n}/{q.limit_n ?? '—'}
                        </TableCell>
                        <HeadroomCell q={q} />
                      </TableRow>
                    ))}
                    {stats.quota.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={4} className="py-16 text-center text-muted-foreground">
                          <div className="flex flex-col items-center justify-center gap-1.5">
                            <ShieldAlert className="h-6 w-6 opacity-30 text-muted-foreground" />
                            <span className="text-xs font-medium">no declared quota buckets</span>
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </TableWrap>
            </div>

            <div className="flex flex-col h-[460px]">
              <SectionHeading
                title="Recent decisions"
                hint="every request is recorded, whatever the outcome"
                action={
                  <button
                    onClick={() => setDecisionsView('full')}
                    className="hidden sm:inline-flex items-center gap-1 rounded-md border border-border/70 bg-card px-2 py-1 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground transition-colors cursor-pointer"
                    title="Switch to full width table"
                  >
                    <Maximize2 className="h-3 w-3" />
                    <span>Full width</span>
                  </button>
                }
              />
              <TableWrap className="flex-1 overflow-auto rounded-xl border border-border/80 bg-card shadow-xs">
                <Table className="min-w-[480px]">
                  <TableHeader className="sticky top-0 bg-card/95 backdrop-blur-sm z-10">
                    <TableRow>
                      <TableHead className="w-32">time (IST)</TableHead>
                      <TableHead className="w-24">task</TableHead>
                      <TableHead className="w-44">chosen</TableHead>
                      <TableHead>why</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {stats.decisions.map((d, i) => (
                      <TableRow key={i} className="group hover:bg-muted/40 transition-colors">
                        <TableCell className="w-32 tnum">
                          <DecisionTimeCell d={d} />
                        </TableCell>
                        <TableCell className="w-24">
                          <div className="flex flex-col gap-0.5">
                            <span className="inline-block rounded border border-border/70 bg-muted/50 px-1.5 py-0.5 font-mono text-[10.5px] text-foreground/90 max-w-[90px] truncate" title={d.task}>
                              {d.task}
                            </span>
                            {d.mode === 'compare' && (
                              <span className="inline-block rounded bg-purple-500/10 text-purple-600 dark:text-purple-400 border border-purple-500/20 px-1 text-[8.5px] font-semibold w-fit">
                                compare
                              </span>
                            )}
                          </div>
                        </TableCell>
                        <TableCell className="w-44">
                          {d.mode === 'compare' ? (
                            <CompareDecisionCell d={d} />
                          ) : d.chosen ? (
                            <DeployCell deployId={d.chosen} />
                          ) : (
                            <ModeCell mode={d.mode} />
                          )}
                        </TableCell>
                        <TableCell>
                          <WhyTags why={d.why} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableWrap>
            </div>
          </section>
        </>
      )}
    </div>
  )
}

/** A request that was logged without ever reaching an arm.

 * The decision log covers refusals too, so an empty `chosen` has to say *why*
 * rather than render as a bare dash.
 */
function ModeCell({ mode }: { mode?: string }) {
  const label = mode || 'rejected'
  const tone =
    label === 'blocked'
      ? 'border-amber-500/40 bg-amber-500/10 text-amber-400'
      : 'border-border/70 bg-muted/50 text-muted-foreground'
  return (
    <span
      className={`inline-flex items-center rounded-md border px-2 py-0.5 font-mono text-[11px] ${tone}`}
      title={
        label === 'blocked'
          ? 'Refused before routing: session budget'
          : 'Refused before any arm was chosen'
      }
    >
      {label}
    </span>
  )
}

/**
 * How bad an anomaly is, as colour. `high`/`critical` are *refused* prices, so they
 * read as alarms; `warning` is a flag that kept its value, so it reads as a note.
 */
const SEVERITY_TONE: Record<string, string> = {
  critical: 'border-destructive/50 bg-destructive/15 text-destructive',
  high: 'border-destructive/40 bg-destructive/10 text-destructive',
  warning: 'border-amber-500/40 bg-amber-500/10 text-amber-500',
  info: 'border-border text-muted-foreground',
}

/**
 * The cheapest input price for a provider, with the source it came from.
 *
 * The attribution is the point: a reconciled price is a claim, and a claim with no
 * provenance is one the reader has to take on faith.
 */
function MinPriceCell({ p }: { p: ProviderRow }) {
  return (
    <TableCell className="tnum text-right">
      <span className="font-mono text-xs">{money(p.min_in)}</span>
      {p.min_in_source && (
        <span
          className="ml-1.5 rounded border border-border/70 bg-muted/60 px-1 py-0.5 text-[9px] uppercase tracking-wide text-muted-foreground"
          title={`cheapest verified input from ${p.min_in_source}${
            p.min_in_state ? ` · ${p.min_in_state}` : ''
          }`}
        >
          {p.min_in_source}
        </span>
      )}
    </TableCell>
  )
}

/**
 * Quota headroom, its source, and a *probabilistic* exhaustion estimate.
 *
 * The headroom is `min(configured, observed)` computed server-side, so the chip
 * says which side set it: `observed` means the provider told us, which is a
 * different kind of fact from a number someone wrote in `quotas.yaml`.
 */
function HeadroomCell({ q }: { q: QuotaRow }) {
  const ex = q.exhaustion
  const title = [
    `headroom from: ${q.headroom_source ?? 'unknown'}`,
    q.observed_limit_n != null
      ? `provider reports ${q.observed_remaining_n ?? '?'}/${q.observed_limit_n}` +
        (q.observed_at ? ` (${q.observed_at})` : '')
      : null,
    ex?.estimated_exhaustion_at
      ? `exhausts ~${ex.estimated_exhaustion_at} (p=${ex.confidence.toFixed(2)})`
      : null,
  ]
    .filter(Boolean)
    .join('\n')

  return (
    <TableCell className="tnum text-right">
      <div className="flex items-center justify-end gap-2" title={title}>
        <div className="h-1.5 w-12 overflow-hidden rounded-full bg-muted">
          <div
            className={cn(
              'h-full rounded-full transition-all',
              q.headroom == null
                ? 'bg-muted-foreground'
                : q.headroom > 0.4
                  ? 'bg-free'
                  : q.headroom > 0.15
                    ? 'bg-amber-500'
                    : 'bg-destructive',
            )}
            style={{ width: `${Math.max(0, Math.min(100, (q.headroom ?? 0) * 100))}%` }}
          />
        </div>
        <span className="w-8 text-right font-mono text-xs">
          {q.headroom == null ? '—' : `${Math.round(q.headroom * 100)}%`}
        </span>
        {q.headroom_source && q.headroom_source !== 'configured' && (
          <span className="rounded border border-border/70 bg-muted/60 px-1 text-[9px] uppercase text-muted-foreground">
            {q.headroom_source}
          </span>
        )}
      </div>
    </TableCell>
  )
}

function DeployCell({ deployId }: { deployId: string }) {
  if (!deployId) return <span className="font-mono text-xs text-muted-foreground">—</span>
  let provider = ''
  let model = deployId

  // Provider:model format (standard across MinInfer)
  if (deployId.includes(':')) {
    const idx = deployId.indexOf(':')
    provider = deployId.slice(0, idx)
    model = deployId.slice(idx + 1)
  } else if (deployId.includes('/')) {
    const idx = deployId.indexOf('/')
    provider = deployId.slice(0, idx)
    model = deployId.slice(idx + 1)
  }

  // Clean provider prefix if it has nested slashes e.g. openrouter/thinkingmachines/nvfp4
  if (provider.includes('/')) {
    provider = provider.split('/')[0]
  }

  return (
    <div className="flex items-center gap-1.5 max-w-[24rem] truncate" title={deployId}>
      {provider && (
        <span className="shrink-0 rounded border border-border/80 bg-muted/70 px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {provider}
        </span>
      )}
      <span className="truncate font-mono text-xs text-foreground/90 font-medium">
        {model}
      </span>
    </div>
  )
}

function CompareDecisionCell({ d }: { d: DecisionRow }) {
  const models = d.compare_models || (d.chosen.includes(' vs ') ? d.chosen.split(' vs ') : [d.chosen])
  const preferred = d.preferred

  return (
    <div className="flex flex-col gap-1 py-1 max-w-[28rem]">
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="shrink-0 rounded border border-purple-500/40 bg-purple-500/10 px-1.5 py-0.2 font-mono text-[9px] font-bold text-purple-600 dark:text-purple-400">
          COMPARE
        </span>
        <div className="flex items-center gap-1.5 flex-wrap text-xs">
          {models.map((m, idx) => {
            const isWinner = preferred === m
            let prov = ''
            let modelName = m
            if (m.includes(':')) {
              const colonIdx = m.indexOf(':')
              prov = m.slice(0, colonIdx)
              modelName = m.slice(colonIdx + 1)
            }
            if (prov.includes('/')) prov = prov.split('/')[0]

            return (
              <span
                key={idx}
                className={cn(
                  'inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-[10.5px] border transition-colors',
                  isWinner
                    ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 font-semibold shadow-xs'
                    : 'border-border/70 bg-muted/40 text-foreground/80'
                )}
                title={m}
              >
                {isWinner && <Check className="h-3 w-3 stroke-[2.5] text-emerald-500 shrink-0" />}
                {prov && <span className="text-[9px] text-muted-foreground uppercase">{prov}</span>}
                <span className="truncate max-w-[120px]">{modelName}</span>
              </span>
            )
          })}
        </div>
      </div>
      {preferred ? (
        <div className="text-[10px] text-emerald-600 dark:text-emerald-400 font-medium flex items-center gap-1">
          <Check className="h-3 w-3 shrink-0" />
          <span>User preferred:</span>
          <span className="font-mono font-semibold truncate max-w-[200px]">{preferred}</span>
        </div>
      ) : (
        <div className="text-[9.5px] text-muted-foreground/75 italic">
          No preference submitted yet
        </div>
      )}
    </div>
  )
}

/**
 * Why a decision went the way it did.
 *
 * The colour is the *kind* of reason, not decoration: evidence-backed reasons
 * (a leaderboard, the quality floor) read differently from price reasons, and
 * `unbenchmarked` is deliberately amber because "cheapest, and nothing has ever
 * measured it" is a caveat rather than a recommendation.
 */
const WHY_TONE: Record<string, string> = {
  leaderboard: 'border-brand/40 bg-brand/10 text-brand',
  quality: 'border-brand/30 bg-brand/5 text-brand',
  free: 'border-free/40 bg-free/10 text-free',
  'on-trial': 'border-paid/40 bg-paid/10 text-paid',
  unbenchmarked: 'border-paid/30 bg-paid/5 text-paid',
  cheapest: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400',
  fastest: 'border-sky-500/40 bg-sky-500/10 text-sky-400',
  'only-option': 'border-amber-500/40 bg-amber-500/10 text-amber-400',
  direct: 'border-border bg-muted/40 text-muted-foreground',
  objective: 'border-border text-muted-foreground',
}

function WhyTags({ why }: { why?: WhyTag[] }) {
  if (!why?.length) return <span className="text-muted-foreground">—</span>
  return (
    <div className="flex flex-wrap items-center gap-1">
      {why.map((w, i) => (
        <span
          key={`${w.key}-${i}`}
          title={w.also ? `${w.key} · also ${w.also}` : w.key}
          className={cn(
            'whitespace-nowrap rounded-full border px-2 py-0.5 text-[10.5px]',
            WHY_TONE[w.key] ?? 'border-border text-muted-foreground',
          )}
        >
          {w.label}
          {w.detail ? <span className="ml-1.5 opacity-70">{w.detail}</span> : null}
        </span>
      ))}
    </div>
  )
}

function parseUtcDate(ts: string): Date | null {
  if (!ts) return null
  const hasTz = ts.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(ts)
  const d = new Date(hasTz ? ts : `${ts}Z`)
  return isNaN(d.getTime()) ? null : d
}

function DecisionTimeCell({ d }: { d: DecisionRow }) {
  const date = parseUtcDate(d.ts)
  if (!date) {
    return <span className="font-mono text-xs text-muted-foreground">—</span>
  }

  // Primary Time: Indian Standard Time (IST, UTC+5:30)
  const istTime = date.toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })

  // Full IST date for hover tooltip
  const fullIstDate = date.toLocaleDateString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })

  // Check where msg is coming from
  const origin = d.origin
  const localBrowserTz = typeof Intl !== 'undefined' ? Intl.DateTimeFormat().resolvedOptions().timeZone : 'Asia/Kolkata'
  const isBrowserIndia = localBrowserTz === 'Asia/Kolkata' || localBrowserTz === 'Asia/Calcutta'

  const originTz = origin?.tz || ''
  const originCountry = origin?.country || ''
  const isOriginLocal = origin?.is_local ?? true
  const originSource = origin?.source || 'API'

  // Is origin in India?
  const isOriginIndia =
    originCountry === 'IN' ||
    originTz === 'Asia/Kolkata' ||
    originTz === 'Asia/Calcutta' ||
    (isOriginLocal && isBrowserIndia && !originTz && !originCountry)

  // Secondary origin time if origin is other than India
  let secondaryTime: string | null = null
  let originLabel: string | null = null

  if (!isOriginIndia) {
    // If explicit non-India timezone specified:
    if (originTz && originTz !== 'Asia/Kolkata' && originTz !== 'Asia/Calcutta') {
      try {
        secondaryTime = date.toLocaleTimeString('en-US', {
          timeZone: originTz,
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false,
        })
        const tzShort = originTz.split('/').pop()?.replace(/_/g, ' ') || originTz
        originLabel = originCountry ? `${originCountry} · ${tzShort}` : tzShort
      } catch {
        secondaryTime = null
      }
    }

    // Fallback to UTC origin time if no custom tz or format failed
    if (!secondaryTime) {
      secondaryTime = date.toLocaleTimeString('en-US', {
        timeZone: 'UTC',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      })
      originLabel = originCountry ? `${originCountry} · UTC` : 'UTC'
    }
  }

  const tooltipTitle = `${fullIstDate}, ${istTime} IST${
    secondaryTime
      ? `\nOrigin: ${secondaryTime} (${originLabel})\nClient: ${originSource}${origin?.ip ? ` · ${origin.ip}` : ''}`
      : `\nOrigin: India · ${originSource}${origin?.ip ? ` (${origin.ip})` : ''}`
  }`

  return (
    <div className="flex flex-col justify-center whitespace-nowrap leading-tight" title={tooltipTitle}>
      <div className="flex items-center gap-1.5 font-mono text-xs font-semibold text-foreground/90">
        <span>{istTime}</span>
        <span className="inline-flex items-center rounded bg-brand/10 px-1 py-0.5 text-[9px] font-semibold text-brand tracking-wider">
          IST
        </span>
      </div>
      {secondaryTime ? (
        <div className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground/80 mt-0.5">
          <Globe className="h-2.5 w-2.5 shrink-0 opacity-60 text-muted-foreground" />
          <span>{secondaryTime}</span>
          <span className="rounded bg-muted/80 px-1 py-0.2 text-[8.5px] text-muted-foreground/90 truncate max-w-[85px]">
            {originLabel}
          </span>
        </div>
      ) : (
        <div className="flex items-center gap-1 font-mono text-[9.5px] text-muted-foreground/60 mt-0.5">
          <span className="truncate max-w-[100px]">{originSource}</span>
          {origin?.ip && <span className="opacity-70">· {origin.ip}</span>}
        </div>
      )}
    </div>
  )
}

function ErrorState({ message }: { message: string }) {
  return (
    <Card className="border-destructive/40 bg-destructive/5">
      <CardContent className="flex items-start gap-3 p-5">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
        <div>
          <div className="text-sm font-medium">Could not reach the router</div>
          <p className="mt-1 text-sm text-muted-foreground">
            {message}. Is <code className="font-mono">mi proxy</code> running?
          </p>
        </div>
      </CardContent>
    </Card>
  )
}
