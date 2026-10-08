import { Sparkles, Layers, Gauge, ArrowUpRight } from 'lucide-react'
import { splitModelId, type ComplexityMeta } from '@/lib/chat'
import { cn } from '@/lib/utils'

export interface ModelIdentityProps {
  model?: string
  task?: string
  alternatives?: string[]
  streaming?: boolean
  className?: string
  /** What the complexity estimator decided for this prompt, if it was routed. */
  complexity?: ComplexityMeta | null
  /** The low-complexity arm failed and the router retried a tier up. */
  complexityEscalated?: boolean
}

/** The tooltip: every number the router used, so a wrong call is debuggable. */
function complexityTitle(c: ComplexityMeta, escalated?: boolean): string {
  const lines = [
    `complexity: ${c.level} (score ${c.score}, confidence ${c.confidence})`,
    `needs reasoning: ${c.needs_reasoning}`,
    `decided by: ${c.source}`,
  ]
  if (c.signals?.length) lines.push(`signals: ${c.signals.join(', ')}`)
  if (c.reason) lines.push(c.reason)
  if (escalated) lines.push('escalated: the low-complexity arm failed, retried a tier up')
  return lines.join('\n')
}

/**
 * The model that answered, named above its reply with any shortlisted alternatives.
 *
 * Clearly displays:
 * 1. "Output given by: [PROVIDER] [MODEL_NAME]"
 * 2. "Other selections: [ALT 1] [ALT 2] ..."
 */
export function ModelIdentity({
  model,
  task: _task,
  alternatives = [],
  streaming,
  className,
  complexity,
  complexityEscalated,
}: ModelIdentityProps) {
  let effectiveModel = model
  let effectiveAlternatives = [...(alternatives || [])]

  // If model is missing or is just a task name (no provider prefix), but alternatives
  // has candidate models, the first candidate is the chosen model that was displaced.
  const isGeneric = !effectiveModel || (!effectiveModel.includes(':') && !effectiveModel.includes('/'))
  if (isGeneric && effectiveAlternatives.length > 0) {
    effectiveModel = effectiveAlternatives[0]
    effectiveAlternatives = effectiveAlternatives.slice(1)
  }

  const isPending = !effectiveModel && streaming
  const isFallback = !effectiveModel && !streaming
  const displayName = isPending ? 'Selecting best model…' : isFallback ? 'Auto-routed model' : null
  const { provider, model: name } = effectiveModel
    ? splitModelId(effectiveModel)
    : { provider: null, model: displayName || '' }

  const cleanAlternatives = effectiveAlternatives.filter(
    (a) => a && a !== effectiveModel && a !== model && a !== name,
  )

  return (
    <div className={cn('mb-2.5 flex flex-wrap items-center gap-x-3.5 gap-y-1.5', className)}>
      {/* Primary: Output given by */}
      <div className="flex items-center gap-1.5 min-w-0">
        <span className="grid h-5 w-5 shrink-0 place-items-center rounded-md bg-brand text-brand-foreground shadow-glow-sm">
          <Sparkles className="h-3 w-3" strokeWidth={2.3} />
        </span>
        <span className="shrink-0 text-[11px] font-medium text-muted-foreground">
          Output given by:
        </span>
        <div className="inline-flex min-w-0 items-center gap-1.5 rounded-md border border-brand/25 bg-brand/[0.07] px-2 py-0.5 shadow-sm">
          {provider && (
            <span className="shrink-0 rounded bg-brand/15 px-1 py-[1px] text-[9.5px] font-bold uppercase tracking-wider text-brand">
              {provider}
            </span>
          )}
          <span
            className={cn(
              'truncate font-mono text-[12px] font-semibold text-foreground',
              isPending && 'animate-pulse text-muted-foreground font-normal',
            )}
            title={model || undefined}
          >
            {name}
          </span>
        </div>
      </div>

      {/* Complexity (#complexity routing): the level that drove this choice.
          Present only when the request was routed, so a pinned model shows
          nothing rather than a misleading default. */}
      {complexity && (
        <div className="flex shrink-0 items-center gap-1.5">
          <span
            title={complexityTitle(complexity, complexityEscalated)}
            data-testid="complexity-badge"
            className={cn(
              'inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
              complexity.needs_reasoning
                ? 'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400'
                : 'border-border/80 bg-muted/40 text-muted-foreground',
            )}
          >
            <Gauge className="h-3 w-3 opacity-70" strokeWidth={2.2} />
            {complexity.level}
            {complexity.needs_reasoning && (
              <span className="font-medium normal-case tracking-normal">· needs reasoning</span>
            )}
            {complexity.source !== 'heuristic' && (
              <span className="font-normal normal-case tracking-normal opacity-70">
                ({complexity.source})
              </span>
            )}
          </span>
          {complexityEscalated && (
            <span
              title="The low-complexity arm failed validation; the router retried a tier up"
              data-testid="complexity-escalated"
              className="inline-flex items-center gap-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-amber-600 dark:text-amber-400"
            >
              <ArrowUpRight className="h-3 w-3" strokeWidth={2.4} />
              escalated
            </span>
          )}
        </div>
      )}

      {/* Other selections / router candidate alternatives */}
      {cleanAlternatives.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
          <span className="flex shrink-0 items-center gap-1 text-muted-foreground/80 font-medium">
            <Layers className="h-3 w-3 opacity-70" />
            Other selections:
          </span>
          <div className="flex flex-wrap items-center gap-1">
            {cleanAlternatives.slice(0, 3).map((alt, idx) => {
              const { provider: altProv, model: altName } = splitModelId(alt)
              return (
                <span
                  key={idx}
                  title={`Alternative candidate: ${alt}`}
                  className="inline-flex items-center gap-1 rounded-md border border-border/80 bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground transition-colors hover:border-border hover:bg-muted"
                >
                  {altProv && (
                    <span className="text-[9px] font-semibold uppercase tracking-wider">
                      {altProv}:
                    </span>
                  )}
                  <span className="truncate max-w-[130px] font-medium">{altName}</span>
                </span>
              )
            })}
            {cleanAlternatives.length > 3 && (
              <span
                className="rounded-md border border-border/60 bg-muted/30 px-1.5 py-0.5 text-[10px] text-muted-foreground font-mono"
                title={cleanAlternatives.slice(3).join(', ')}
              >
                +{cleanAlternatives.length - 3} more
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
