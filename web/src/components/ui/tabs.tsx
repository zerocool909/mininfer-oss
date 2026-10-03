import * as React from 'react'
import { cn } from '@/lib/utils'

interface TabsCtx {
  value: string
  setValue: (v: string) => void
}

const Ctx = React.createContext<TabsCtx | null>(null)

function useTabs() {
  const ctx = React.useContext(Ctx)
  if (!ctx) throw new Error('Tabs components must be used inside <Tabs>')
  return ctx
}

export function Tabs({
  value,
  onValueChange,
  children,
  className,
}: {
  value: string
  onValueChange: (v: string) => void
  children: React.ReactNode
  className?: string
}) {
  return (
    <Ctx.Provider value={{ value, setValue: onValueChange }}>
      <div className={className}>{children}</div>
    </Ctx.Provider>
  )
}

export function TabsList({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      role="tablist"
      className={cn('inline-flex items-center gap-1 rounded-lg border bg-muted p-1', className)}
      {...props}
    />
  )
}

export function TabsTrigger({
  value,
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { value: string }) {
  const { value: active, setValue } = useTabs()
  const on = active === value
  return (
    <button
      role="tab"
      type="button"
      aria-selected={on}
      onClick={() => setValue(value)}
      className={cn(
        'rounded-md px-3.5 py-1.5 text-sm font-medium transition-colors',
        on ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
        className,
      )}
      {...props}
    />
  )
}

export function TabsContent({
  value,
  children,
  className,
}: {
  value: string
  children: React.ReactNode
  className?: string
}) {
  const { value: active } = useTabs()
  if (active !== value) return null
  return <div className={className}>{children}</div>
}
