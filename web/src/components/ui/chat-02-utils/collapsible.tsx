import * as React from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { Markdown } from '@/components/ui/markdown'
import { cn } from '@/lib/utils'

/**
 * Long model answers and long user questions are collapsed to a readable
 * height until expanded.
 *
 * A single reply or question can run to thousands of characters, turning
 * scrolling into a chore. Collapsing is decided by length (characters or lines)
 * rather than measured DOM height, so it needs no layout pass and cannot jitter.
 */
const MAX_ANSWER_CHARS = 3000
const MAX_ANSWER_LINES = 45

const MAX_QUESTION_CHARS = 1000
const MAX_QUESTION_LINES = 16

export function CollapsibleMarkdown({
  text,
  className,
  streaming,
}: {
  text: string
  className?: string
  /** Never collapse a live reply: the newest tokens would land below the fold. */
  streaming?: boolean
}) {
  const [open, setOpen] = React.useState(true)
  const long = !streaming && (text.length > MAX_ANSWER_CHARS || text.split('\n').length > MAX_ANSWER_LINES)

  if (!long) return <Markdown className={className}>{text}</Markdown>

  return (
    <div className={className}>
      <div className={cn(!open && 'fade-bottom max-h-[22rem] overflow-hidden')}>
        <Markdown>{text}</Markdown>
      </div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          'mt-2.5 inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1',
          'text-[12px] font-medium text-muted-foreground shadow-soft transition-colors',
          'hover:border-brand/50 hover:text-foreground',
        )}
      >
        {open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        {open ? 'Show less' : 'Show full answer'}
      </button>
    </div>
  )
}

export function CollapsibleQuestion({
  text,
  className,
}: {
  text: string
  className?: string
}) {
  const [open, setOpen] = React.useState(true)
  const long = text.length > MAX_QUESTION_CHARS || text.split('\n').length > MAX_QUESTION_LINES

  if (!long) return <span className={className}>{text}</span>

  return (
    <div className={className}>
      <div className={cn(!open && 'fade-bottom max-h-[16rem] overflow-hidden')}>
        <span>{text}</span>
      </div>
      <div className="mt-2 flex justify-end">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-full border border-brand/30 bg-background/85 backdrop-blur-xs px-2.5 py-1',
            'text-[11.5px] font-medium text-foreground/80 shadow-xs transition-colors',
            'hover:border-brand/60 hover:bg-background hover:text-foreground',
          )}
        >
          {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          {open ? 'Show less' : 'Show full question'}
        </button>
      </div>
    </div>
  )
}
