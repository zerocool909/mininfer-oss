import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '@/lib/utils'

/**
 * Model answers are markdown — headings, fenced code, and (because GFM is on)
 * tables. Rendering them as plain text is what made a table read as a wall of
 * pipes and `###` lines.
 *
 * Two deliberate choices:
 *
 * * **A fixed element map, not a prose plugin.** Every tag the model can emit is
 *   styled explicitly here. A typography plugin would cascade rules we cannot see
 *   from the call site, and this thread's typography is already decided by the
 *   bubble it sits in.
 * * **No `rehype-raw`.** Raw HTML in a model answer is dropped rather than
 *   rendered, so a response can never inject markup into the dashboard. That is
 *   the default, and it is worth stating because the fix for "make it render
 *   HTML" is the exact thing that would break it.
 */
const components: Components = {
  h1: ({ className, ...p }) => (
    <h1 className={cn('mt-4 mb-2 text-[17px] font-semibold first:mt-0', className)} {...p} />
  ),
  h2: ({ className, ...p }) => (
    <h2 className={cn('mt-4 mb-2 text-[16px] font-semibold first:mt-0', className)} {...p} />
  ),
  h3: ({ className, ...p }) => (
    <h3 className={cn('mt-3.5 mb-1.5 text-[15px] font-semibold first:mt-0', className)} {...p} />
  ),
  h4: ({ className, ...p }) => (
    <h4 className={cn('mt-3 mb-1.5 text-[14px] font-semibold first:mt-0', className)} {...p} />
  ),
  h5: ({ className, ...p }) => (
    <h5 className={cn('mt-3 mb-1 text-[13px] font-semibold first:mt-0', className)} {...p} />
  ),
  h6: ({ className, ...p }) => (
    <h6
      className={cn(
        'mt-3 mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground first:mt-0',
        className,
      )}
      {...p}
    />
  ),
  p: ({ className, ...p }) => <p className={cn('my-2 first:mt-0 last:mb-0', className)} {...p} />,
  a: ({ className, ...p }) => (
    <a
      className={cn('text-brand underline underline-offset-2 hover:opacity-80', className)}
      rel="noreferrer noopener"
      target="_blank"
      {...p}
    />
  ),
  strong: ({ className, ...p }) => <strong className={cn('font-semibold', className)} {...p} />,
  em: ({ className, ...p }) => <em className={cn('italic', className)} {...p} />,
  del: ({ className, ...p }) => (
    <del className={cn('text-muted-foreground line-through', className)} {...p} />
  ),
  hr: ({ className, ...p }) => <hr className={cn('my-4 border-border', className)} {...p} />,
  blockquote: ({ className, ...p }) => (
    <blockquote
      className={cn(
        'my-3 border-l-2 border-border pl-3 text-muted-foreground italic',
        className,
      )}
      {...p}
    />
  ),
  ul: ({ className, ...p }) => (
    <ul className={cn('my-2 ml-4 list-disc space-y-1 first:mt-0 last:mb-0', className)} {...p} />
  ),
  ol: ({ className, ...p }) => (
    <ol
      className={cn('my-2 ml-4 list-decimal space-y-1 first:mt-0 last:mb-0', className)}
      {...p}
    />
  ),
  li: ({ className, ...p }) => (
    // `[&>ul]:my-1` keeps nested lists tight against their parent item.
    <li className={cn('pl-0.5 [&>ol]:my-1 [&>ul]:my-1', className)} {...p} />
  ),
  // Inline code. The fenced case is handled by `pre` below, which resets these
  // back to plain text so a block never renders as a chip inside a card.
  code: ({ className, ...p }) => (
    <code
      className={cn(
        'rounded bg-muted px-1.5 py-0.5 font-mono text-[0.85em] break-words',
        className,
      )}
      {...p}
    />
  ),
  pre: ({ className, ...p }) => (
    <pre
      className={cn(
        'my-3 overflow-x-auto rounded-lg border border-border bg-muted/60 p-3',
        'text-[12.5px] leading-5',
        '[&>code]:bg-transparent [&>code]:p-0 [&>code]:text-[12.5px] [&>code]:break-normal',
        className,
      )}
      {...p}
    />
  ),
  // A table cannot shrink below its content, so it gets its own scroll container
  // rather than stretching the bubble.
  table: ({ className, ...p }) => (
    <div className="my-3 w-full overflow-x-auto rounded-lg border border-border">
      <table className={cn('w-full border-collapse text-[13px]', className)} {...p} />
    </div>
  ),
  thead: ({ className, ...p }) => <thead className={cn('bg-muted/60', className)} {...p} />,
  th: ({ className, ...p }) => (
    <th
      className={cn(
        'border-b border-border px-2.5 py-1.5 text-left font-semibold whitespace-nowrap',
        className,
      )}
      {...p}
    />
  ),
  td: ({ className, ...p }) => (
    <td
      className={cn('border-b border-border/60 px-2.5 py-1.5 align-top', className)}
      {...p}
    />
  ),
  tr: ({ className, ...p }) => (
    <tr className={cn('last:[&>td]:border-b-0', className)} {...p} />
  ),
  img: ({ className, alt, ...p }) => (
    <img
      alt={alt ?? ''}
      className={cn('my-2 max-w-full rounded-lg border border-border', className)}
      loading="lazy"
      {...p}
    />
  ),
}

export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div
      className={cn(
        'text-[15px] leading-6 break-words',
        // A long unbroken token (a model id, a URL) must wrap, not widen the
        // bubble past the viewport.
        '[overflow-wrap:anywhere]',
        className,
      )}
    >
      <ReactMarkdown components={components} remarkPlugins={[remarkGfm]}>
        {children}
      </ReactMarkdown>
    </div>
  )
}
