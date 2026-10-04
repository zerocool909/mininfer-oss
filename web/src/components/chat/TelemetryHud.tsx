import { useState } from 'react'
import {
  ChevronDown,
  ChevronUp,
  Award,
  Zap,
  Clock,
  Layers,
  ShieldCheck,
  CheckCircle2,
  ThumbsUp,
  ThumbsDown,
} from 'lucide-react'
import { cn, money } from '@/lib/utils'
import { splitModelId, type Meta } from '@/lib/chat'

interface TelemetryHudProps {
  meta?: Meta
  ms?: number
  tokens?: number
  className?: string
  /** The model is already named above the answer; keep only the metrics here. */
  hideIdentity?: boolean
  routingVerdict?: 'up' | 'down' | null
  onRouteVerdict?: (deployId: string, task: string, approved: boolean) => void
}

/**
 * The router's decision, as a single quiet line under the answer.
 *
 * It used to be a boxed card with its own border, background and benchmark row —
 * which meant every reply competed with a dashboard for attention. The facts a
 * reader scans for (which model, what it cost, how fast) stay on the line; the
 * reasoning, benchmarks and feedback moved into the drawer.
 */
export function TelemetryHud({
  meta,
  ms,
  tokens,
  className,
  hideIdentity,
  routingVerdict,
  onRouteVerdict,
}: TelemetryHudProps) {
  const [expanded, setExpanded] = useState(false)
  const [localVerdict, setLocalVerdict] = useState<'up' | 'down' | null>(routingVerdict ?? null)

  const handleRouteVote = (e: React.MouseEvent, approved: boolean) => {
    e.stopPropagation()
    setLocalVerdict(approved ? 'up' : 'down')
    if (onRouteVerdict && meta?.model) {
      onRouteVerdict(meta.model, meta.task ?? 'general_chat', approved)
    }
  }

  let effectiveModel = meta?.model
  let effectiveAlternatives = [...(meta?.alternatives || [])]
  const isGeneric = !effectiveModel || (!effectiveModel.includes(':') && !effectiveModel.includes('/'))
  if (isGeneric && effectiveAlternatives.length > 0) {
    effectiveModel = effectiveAlternatives[0]
    effectiveAlternatives = effectiveAlternatives.slice(1)
  }

  const modelId = effectiveModel ?? 'auto'
  const isFree = meta?.cost === 0 || meta?.cost === null
  const { provider, model: cleanModelName } = splitModelId(modelId)

  const cleanFallbacks = effectiveAlternatives.filter(
    (a) => a && a !== effectiveModel && a !== meta?.model && a !== cleanModelName && !a.endsWith(`/${cleanModelName}`) && !a.endsWith(`:${cleanModelName}`),
  )

  const tokPerSec = tokens && ms && ms > 0 ? (tokens / (ms / 1000)).toFixed(0) : null
  const verdict = localVerdict || routingVerdict

  return (
    <div className={cn('mt-1 text-[12px]', className)}>
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 border-t border-border/60 pt-2.5">
        {/* Identity */}
        {!hideIdentity && (
          <div className="flex min-w-0 items-center gap-1.5">
            {provider && (
              <span className="shrink-0 text-[10.5px] font-semibold uppercase tracking-wider text-muted-foreground">
                {provider}
              </span>
            )}
            <span className="truncate font-mono text-[12px] font-medium text-foreground" title={modelId}>
              {cleanModelName}
            </span>
          </div>
        )}

        {/* Price */}
        {isFree ? (
          <span className="inline-flex items-center gap-1 rounded-full border border-free/30 bg-free/10 px-2 py-[1px] text-[10.5px] font-semibold text-free">
            Free
          </span>
        ) : (
          <span className="inline-flex items-center rounded-full border border-paid/30 bg-paid/10 px-2 py-[1px] text-[10.5px] font-semibold text-paid">
            {money(meta?.cost)} <span className="ml-1 font-normal">/ success</span>
          </span>
        )}

        {meta?.needsApproval && (
          <span className="rounded-full border border-paid/30 bg-paid/10 px-2 py-[1px] text-[10.5px] font-medium text-paid">
            Trial
          </span>
        )}

        {/* Metrics + details toggle */}
        <div className="ml-auto flex items-center gap-2.5 text-muted-foreground">
          {ms !== undefined && (
            <span className="inline-flex items-center gap-1 font-mono text-[11px]" title="Response latency">
              <Clock className="h-3 w-3 opacity-60" />
              {ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`}
            </span>
          )}
          {tokPerSec && (
            <span className="hidden items-center gap-1 font-mono text-[11px] sm:inline-flex" title="Generation speed">
              <Zap className="h-3 w-3 opacity-60" />
              {tokPerSec} t/s
            </span>
          )}
          <button
            onClick={() => setExpanded(!expanded)}
            className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            aria-expanded={expanded}
          >
            <span>Details</span>
            {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>
        </div>
      </div>

      {expanded && (
        <div className="mt-2.5 animate-fade-in space-y-3 rounded-xl border border-border bg-muted/30 p-3.5">
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-1.5 text-[12px] font-medium text-foreground">
              <ShieldCheck className="h-3.5 w-3.5 text-free" />
              Routing decision
            </span>
            {meta?.task && (
              <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10.5px] text-muted-foreground">
                {meta.task}
              </span>
            )}
          </div>

          <div className="flex items-start gap-2 text-[12px] text-muted-foreground">
            <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-free" />
            <div className="min-w-0 space-y-0.5">
              <div className="text-foreground/90">Cheapest capable deployment for this task</div>
              {meta?.pLb !== undefined && meta?.pLb !== null && (
                <div className="text-[11px]">
                  Quality confidence {(meta.pLb * 100).toFixed(1)}%
                </div>
              )}
            </div>
          </div>

          {meta?.tags && meta.tags.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
                <Award className="h-3 w-3 text-brand" /> Benchmarks
              </span>
              {meta.tags.slice(0, 4).map((tag, idx) => (
                <span
                  key={idx}
                  className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-1.5 py-0.5 font-mono text-[10.5px] text-foreground/90"
                  title={`${tag.source}: ${tag.label}`}
                >
                  <span className="text-muted-foreground">{tag.source}</span>
                  <span className="font-semibold text-brand">{tag.value ?? tag.label}</span>
                </span>
              ))}
              {meta.tags.length > 4 && (
                <span className="text-[10.5px] text-muted-foreground">+{meta.tags.length - 4}</span>
              )}
            </div>
          )}

          {cleanFallbacks.length > 0 && (
            <div className="flex items-start gap-2 text-[11.5px] text-muted-foreground">
              <Layers className="mt-0.5 h-3.5 w-3.5 shrink-0 text-brand" />
              <div className="min-w-0">
                <span className="text-foreground/80 font-medium">Fallbacks:</span>
                <span className="ml-1.5 font-mono text-[10.5px]">
                  {cleanFallbacks.slice(0, 3).join(' · ')}
                </span>
              </div>
            </div>
          )}

          <div className="flex items-center justify-between border-t border-border/60 pt-2.5">
            <span className="text-[11.5px] text-muted-foreground">Rate this routing choice</span>
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={(e) => handleRouteVote(e, true)}
                title="This model was a good choice"
                className={cn(
                  'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors',
                  verdict === 'up'
                    ? 'border-free/40 bg-free/10 text-free'
                    : 'border-border bg-background text-muted-foreground hover:bg-accent hover:text-foreground',
                )}
              >
                <ThumbsUp className="h-3 w-3" />
                Good
              </button>
              <button
                type="button"
                onClick={(e) => handleRouteVote(e, false)}
                title="This model was a poor choice"
                className={cn(
                  'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors',
                  verdict === 'down'
                    ? 'border-destructive/40 bg-destructive/10 text-destructive'
                    : 'border-border bg-background text-muted-foreground hover:bg-accent hover:text-foreground',
                )}
              >
                <ThumbsDown className="h-3 w-3" />
                Poor
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
