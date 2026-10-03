import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva(
  'inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors',
  {
    variants: {
      variant: {
        default: 'border-transparent bg-primary text-primary-foreground',
        secondary: 'border-transparent bg-secondary text-secondary-foreground',
        outline: 'text-muted-foreground',
        free: 'border-free/30 bg-free/15 text-free',
        paid: 'border-paid/30 bg-paid/15 text-paid',
        destructive: 'border-destructive/40 bg-destructive/15 text-destructive-foreground',
      },
    },
    defaultVariants: { variant: 'default' },
  },
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}

/** Cost badge: FREE when it costs nothing per success, amber otherwise. */
export function CostBadge({ value, className }: { value: number | null | undefined; className?: string }) {
  const free = value === 0
  return (
    <Badge variant={free ? 'free' : 'paid'} className={className}>
      {free ? 'FREE' : `${value === null || value === undefined ? '—' : `$${value.toFixed(4)}`} / success`}
    </Badge>
  )
}
