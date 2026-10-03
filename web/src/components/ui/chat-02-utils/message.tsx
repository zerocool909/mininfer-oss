import * as React from 'react'
import { cn } from '@/lib/utils'

export function Message({
  align = 'start',
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { align?: 'start' | 'end' }) {
  return (
    <div
      data-align={align}
      className={cn('group/message flex w-full flex-col', className)}
      {...props}
    />
  )
}

export function MessageContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex w-full flex-col gap-1', className)} {...props} />
}

/** Row of per-message actions; revealed on hover or keyboard focus. */
export function MessageFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex items-center opacity-0 transition-opacity duration-150',
        'group-hover/message:opacity-100 focus-within:opacity-100',
        className,
      )}
      {...props}
    />
  )
}
