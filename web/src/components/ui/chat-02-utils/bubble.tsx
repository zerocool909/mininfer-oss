import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const bubbleVariants = cva('group/bubble flex w-full', {
  variants: {
    // The bubble is a direct child of a flex column, so alignment is the
    // cross-axis (`self-*`), not `justify-*` inside the box — which is what left
    // the user's messages hugging the left edge of a max-width bubble.
    align: { start: 'self-start justify-start', end: 'self-end justify-end' },
    variant: { muted: '', ghost: '' },
  },
  defaultVariants: { align: 'start', variant: 'ghost' },
})

export interface BubbleProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof bubbleVariants> {}

/**
 * `data-variant` is read by BubbleContent via `group-data-[variant=ghost]/bubble`,
 * which is how a ghost bubble renders as bare text while a muted one gets a
 * surface. Both live under one component so a thread can mix them freely.
 */
export function Bubble({ className, align, variant, ...props }: BubbleProps) {
  return (
    <div
      data-variant={variant}
      className={cn(bubbleVariants({ align, variant }), className)}
      {...props}
    />
  )
}

export function BubbleContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'w-fit max-w-full rounded-[18px] bg-muted px-3.5 py-2',
        'group-data-[variant=ghost]/bubble:bg-transparent',
        'group-data-[variant=ghost]/bubble:px-0',
        'group-data-[variant=ghost]/bubble:py-0',
        className,
      )}
      {...props}
    />
  )
}
