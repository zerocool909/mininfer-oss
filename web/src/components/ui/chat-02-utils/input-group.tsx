import * as React from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'

export function InputGroup({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      data-slot="input-group"
      className={cn('flex w-full items-center gap-0 border bg-background', className)}
      {...props}
    />
  )
}

export function InputGroupAddon({
  align = 'inline-start',
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { align?: 'inline-start' | 'inline-end' }) {
  return (
    <div
      data-align={align}
      className={cn('flex shrink-0 items-center', align === 'inline-end' && 'ml-auto', className)}
      {...props}
    />
  )
}

/**
 * Must be a `forwardRef`: Radix's `asChild` clones this and attaches a ref for
 * menu anchoring. As a plain function component the ref was silently dropped, so
 * trigger menus opened unanchored — which is why the compare/task menus looked
 * like they did nothing.
 */
export const InputGroupButton = React.forwardRef<
  HTMLButtonElement,
  Omit<React.ComponentProps<typeof Button>, 'size' | 'variant'> & {
    size?: React.ComponentProps<typeof Button>['size']
    variant?: React.ComponentProps<typeof Button>['variant']
  }
>(({ size = 'icon-sm', variant = 'ghost', className, ...props }, ref) => (
  <Button
    ref={ref}
    size={size}
    variant={variant}
    className={cn('rounded-full', className)}
    {...props}
  />
))
InputGroupButton.displayName = 'InputGroupButton'

export function InputGroupTextarea({
  className,
  ...props
}: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      data-slot="input-group-control"
      className={cn(
        'min-w-0 flex-1 resize-none border-0 bg-transparent px-3 py-2 text-sm outline-none',
        'placeholder:text-muted-foreground focus-visible:outline-none',
        className,
      )}
      {...props}
    />
  )
}
