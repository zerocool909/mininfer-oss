import { Badge } from '@/components/ui/badge'
import type { LeaderboardTag } from '@/lib/api'

/**
 * Provenance tags. Each score is labelled with the leaderboard it came from, so
 * a prior is never an unexplained number: "Artificial Analysis · Coding Index 68.1"
 * vs "Design Arena · Elo 1327".
 */
export function LeaderboardTags({ tags }: { tags?: LeaderboardTag[] }) {
  if (!tags?.length) return null
  return (
    <div className="flex flex-wrap gap-1.5 px-5 pt-3">
      {tags.map((t) => (
        <Badge key={t.key} variant="outline" title={t.url || t.source} className="gap-1 font-normal">
          <span className="text-muted-foreground">{t.source}</span>
          <span className="text-muted-foreground/60">·</span>
          <span>{t.label}</span>
          {t.value !== undefined && (
            <span className="tnum font-semibold text-foreground">{t.value}</span>
          )}
        </Badge>
      ))}
    </div>
  )
}
