import { Card, CardContent } from '@/components/ui/card'
import { cn, count } from '@/lib/utils'
import type { LucideIcon } from 'lucide-react'

/** A modern elevated registry counter with ambient hover glow. */
export function StatCard({
  label,
  value,
  icon: Icon,
  className,
}: {
  label: string
  value: number | undefined
  icon: LucideIcon
  className?: string
}) {
  return (
    <Card
      className={cn(
        'group relative overflow-hidden rounded-xl border border-border bg-card transition-all duration-200 hover:-translate-y-0.5 hover:border-brand/40 hover:shadow-glow-sm',
        className,
      )}
    >
      <div className="absolute inset-x-0 top-0 h-px bg-brand/30" />
      <CardContent className="p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/80">
            {label}
          </span>
          <div className="grid h-6 w-6 place-items-center rounded-md bg-brand/10 text-brand transition-colors group-hover:bg-brand/15">
            <Icon className="h-3.5 w-3.5" />
          </div>
        </div>
        <div className="tnum mt-2.5 text-[26px] font-semibold leading-none tracking-tight text-foreground">
          {count(value)}
        </div>
      </CardContent>
    </Card>
  )
}
