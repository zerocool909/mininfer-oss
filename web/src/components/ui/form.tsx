import * as React from 'react'
import { cn } from '@/lib/utils'

export function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label
      className={cn('text-xs font-medium uppercase tracking-wider text-muted-foreground', className)}
      {...props}
    />
  )
}

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      'w-full resize-y rounded-md border border-input bg-background px-3 py-2 font-mono text-[13px] leading-relaxed',
      'placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
      'focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:opacity-50',
      className,
    )}
    {...props}
  />
))
Textarea.displayName = 'Textarea'

export const Select = React.forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement>
>(({ className, ...props }, ref) => (
  <select
    ref={ref}
    className={cn(
      'h-9 rounded-md border border-input bg-background px-3 text-sm',
      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
      'focus-visible:ring-offset-background',
      className,
    )}
    {...props}
  />
))
Select.displayName = 'Select'

export function Switch({
  checked,
  onCheckedChange,
  id,
  label,
  className,
}: {
  checked: boolean
  onCheckedChange: (v: boolean) => void
  id?: string
  label: string
  className?: string
}) {
  return (
    <label
      htmlFor={id}
      className={cn(
        'inline-flex cursor-pointer select-none items-center gap-2 pb-2 text-sm text-muted-foreground',
        className,
      )}
    >
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(e) => onCheckedChange(e.target.checked)}
        className="h-4 w-4 rounded border-input bg-transparent accent-[hsl(var(--free))]"
      />
      {label}
    </label>
  )
}

/** Pill used for task selection and leaderboard provenance tags. */
export function Chip({
  active,
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  return (
    <button
      type="button"
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition-colors',
        active
          ? 'border-primary bg-primary font-medium text-primary-foreground'
          : 'border-border text-muted-foreground hover:border-ring hover:text-foreground',
        className,
      )}
      {...props}
    />
  )
}
