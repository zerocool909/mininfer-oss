import { cn } from '@/lib/utils'
import type { Funnel } from '@/lib/api'
import { CheckCircle2, Filter, Layers, Sparkles, Trophy } from 'lucide-react'

interface Segment {
  key: keyof Funnel
  label: string
  bar: string
  dot: string
  hint: string
}

const SEGMENTS: Segment[] = [
  {
    key: 'after_hard_filter',
    label: 'eligible',
    bar: 'bg-emerald-500',
    dot: 'bg-emerald-500',
    hint: 'Passed all hard constraints (tools, context, schema)',
  },
  {
    key: 'rejected_no_key',
    label: 'no API key',
    bar: 'bg-slate-500',
    dot: 'bg-slate-500',
    hint: 'Provider not credentialed locally in .env',
  },
  {
    key: 'rejected_no_evidence',
    label: 'no benchmark',
    bar: 'bg-amber-500',
    dot: 'bg-amber-500',
    hint: 'No verified leaderboard benchmark data covers this model',
  },
  {
    key: 'rejected_unsupported',
    label: 'unsupported',
    bar: 'bg-rose-500',
    dot: 'bg-rose-500',
    hint: 'Missing required capabilities for task',
  },
  {
    key: 'rejected_quality',
    label: 'below floor',
    bar: 'bg-orange-500',
    dot: 'bg-orange-500',
    hint: 'Benchmark score is below configured quality floor',
  },
]

export function FunnelBar({ funnel }: { funnel: Funnel }) {
  const total = funnel.total ?? 0
  if (!total) return null

  const eligible = funnel.after_hard_filter ?? 0
  const freeEligible = funnel.free_eligible ?? 0

  const parts = SEGMENTS.map((s) => ({ ...s, value: (funnel[s.key] as number) ?? 0 })).filter(
    (p) => p.value > 0,
  )
  const shown = parts.reduce((a, p) => a + p.value, 0)
  const other = Math.max(0, total - shown)

  const pct = (n: number) => (n / total) * 100

  return (
    <div className="space-y-5">
      {/* 4-Stage Visual Funnel Pipeline */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {/* Stage 1: Total Catalog */}
        <div className="relative flex flex-col justify-between rounded-xl border border-border/80 bg-muted/30 p-3">
          <div className="flex items-center justify-between text-muted-foreground">
            <span className="text-[11px] font-semibold uppercase tracking-wider">1. Catalog</span>
            <Layers className="h-3.5 w-3.5 text-muted-foreground/70" />
          </div>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="tnum text-xl font-bold tracking-tight text-foreground">
              {total.toLocaleString()}
            </span>
            <span className="text-[10px] text-muted-foreground">100%</span>
          </div>
          <span className="mt-1 text-[10.5px] text-muted-foreground/80">Available deployments</span>
        </div>

        {/* Stage 2: Hard Filter */}
        <div className="relative flex flex-col justify-between rounded-xl border border-border/80 bg-muted/30 p-3">
          <div className="flex items-center justify-between text-muted-foreground">
            <span className="text-[11px] font-semibold uppercase tracking-wider">2. Constraint Gate</span>
            <Filter className="h-3.5 w-3.5 text-brand" />
          </div>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="tnum text-xl font-bold tracking-tight text-foreground">
              {eligible.toLocaleString()}
            </span>
            <span className="tnum text-[10.5px] font-semibold text-brand">
              {((eligible / total) * 100).toFixed(1)}%
            </span>
          </div>
          <span className="mt-1 text-[10.5px] text-muted-foreground/80">Passed task constraints</span>
        </div>

        {/* Stage 3: Free-First Pool */}
        <div className="relative flex flex-col justify-between rounded-xl border border-free/30 bg-free/5 p-3">
          <div className="flex items-center justify-between text-free">
            <span className="text-[11px] font-semibold uppercase tracking-wider">3. Free Pool</span>
            <Sparkles className="h-3.5 w-3.5" />
          </div>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="tnum text-xl font-bold tracking-tight text-free">
              {freeEligible.toLocaleString()}
            </span>
            <span className="tnum text-[10.5px] font-semibold text-free">
              {eligible > 0 ? `${((freeEligible / eligible) * 100).toFixed(0)}%` : '0%'}
            </span>
          </div>
          <span className="mt-1 text-[10.5px] text-free/80">Zero-cost candidates</span>
        </div>

        {/* Stage 4: Champion Selection */}
        <div className="relative flex flex-col justify-between rounded-xl border border-brand/40 bg-brand/5 p-3 shadow-glow-sm">
          <div className="flex items-center justify-between text-brand">
            <span className="text-[11px] font-semibold uppercase tracking-wider">4. Winner</span>
            <Trophy className="h-3.5 w-3.5" />
          </div>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="tnum text-xl font-bold tracking-tight text-brand">1 Model</span>
            <span className="inline-flex items-center gap-0.5 text-[10.5px] font-semibold text-brand">
              <CheckCircle2 className="h-3 w-3" /> Routed
            </span>
          </div>
          <span className="mt-1 text-[10.5px] text-muted-foreground/80">Cheapest capable winner</span>
        </div>
      </div>

      {/* Stacked Proportional Bar */}
      <div>
        <div className="flex h-3 w-full overflow-hidden rounded-full bg-muted/60 p-0.5 shadow-inner">
          {parts.map((p) => (
            <div
              key={String(p.key)}
              className={cn(p.bar, 'h-full rounded-full transition-all duration-500 first:rounded-l-full last:rounded-r-full')}
              style={{ width: `${pct(p.value)}%` }}
              title={`${p.label}: ${p.value.toLocaleString()} (${pct(p.value).toFixed(1)}%) — ${p.hint}`}
            />
          ))}
          {other > 0 && (
            <div
              className="h-full rounded-full bg-muted-foreground/25"
              style={{ width: `${pct(other)}%` }}
              title={`other constraints: ${other.toLocaleString()}`}
            />
          )}
        </div>

        {/* Breakdown Tags Grid */}
        <div className="mt-3.5 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3 lg:grid-cols-5">
          {parts.map((p) => (
            <div
              key={String(p.key)}
              className="flex items-center justify-between rounded-lg border border-border/60 bg-card/40 px-2.5 py-1.5 text-xs hover:border-border transition-colors"
              title={p.hint}
            >
              <div className="flex items-center gap-1.5 min-w-0">
                <span className={cn('h-2 w-2 shrink-0 rounded-full', p.dot)} />
                <span className="truncate text-muted-foreground">{p.label}</span>
              </div>
              <div className="ml-2 flex items-baseline gap-1 shrink-0">
                <span className="tnum font-semibold text-foreground">{p.value.toLocaleString()}</span>
                <span className="tnum text-[10px] text-muted-foreground/70">
                  ({pct(p.value) < 0.1 ? '<0.1' : pct(p.value).toFixed(1)}%)
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
