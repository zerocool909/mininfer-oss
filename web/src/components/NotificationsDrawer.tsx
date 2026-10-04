import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, CheckCircle2 } from 'lucide-react'
import { Drawer } from '@/components/ui/drawer'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/Skeleton'
import { api, type PricingAnomaly, type ProviderInfo } from '@/lib/api'
import { getUserKeys } from '@/lib/keys'
import { cn } from '@/lib/utils'

const SEVERITY_TONE: Record<string, string> = {
  critical: 'border-destructive/50 text-destructive',
  high: 'border-destructive/40 text-destructive',
  warning: 'border-amber-500/40 text-amber-500',
  info: 'border-border text-muted-foreground',
}

/**
 * The notification surface: which providers are actually live, and the pricing
 * disagreements that need a decision.
 *
 * The ack/resolve actions live here rather than on the Overview page so there is
 * one review queue. `Acknowledge` is "seen, still wrong" — it stops the re-alert
 * without touching a price; `Resolve` closes it, and a genuinely still-wrong price
 * simply opens a new row on the next reconcile.
 */
export function NotificationsDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [anomalies, setAnomalies] = useState<PricingAnomaly[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [p, a] = await Promise.all([api.providers(), api.anomalies('open')])
      setProviders(p.providers)
      setAnomalies(a.anomalies)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load notifications')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  async function decide(anomalyId: string, status: 'acknowledged' | 'resolved') {
    setBusy(anomalyId)
    setError(null)
    try {
      await api.decideAnomaly(anomalyId, status)
      setAnomalies((prev) => prev.filter((a) => a.anomaly_id !== anomalyId))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not update the anomaly')
    } finally {
      setBusy(null)
    }
  }

  const portalKeys = getUserKeys()

  return (
    <Drawer open={open} onClose={onClose} title="Notifications">
      {error && (
        <div
          role="alert"
          className="mb-4 flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2 text-[12px] text-destructive"
        >
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 flex-1">{error}</span>
          <button
            type="button"
            onClick={() => void load()}
            className="shrink-0 font-medium underline underline-offset-2 hover:no-underline"
          >
            Retry
          </button>
        </div>
      )}

      <section aria-labelledby="notif-anomalies">
        <h3
          id="notif-anomalies"
          className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground"
        >
          Pricing review {!loading && anomalies.length > 0 && `(${anomalies.length})`}
        </h3>
        {loading ? (
          <div className="mt-2 space-y-2" aria-hidden="true">
            <Skeleton className="h-[74px] w-full" />
            <Skeleton className="h-[74px] w-full" />
          </div>
        ) : anomalies.length === 0 ? (
          <p className="mt-2 flex items-center gap-1.5 text-[12px] text-muted-foreground">
            <CheckCircle2 className="h-3.5 w-3.5 text-free" /> Nothing to review
          </p>
        ) : (
          <ul className="mt-2 space-y-2">
            {anomalies.map((a) => (
              <li key={a.anomaly_id} className="rounded-lg border border-border px-3 py-2">
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      'shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
                      SEVERITY_TONE[a.severity] ?? 'border-border text-muted-foreground',
                    )}
                  >
                    {a.severity}
                  </span>
                  <span
                    className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-foreground"
                    title={a.deploy_id}
                  >
                    {a.deploy_id}
                  </span>
                </div>
                {a.detail && <p className="mt-1 text-[11px] text-muted-foreground">{a.detail}</p>}
                <div className="mt-2 flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy === a.anomaly_id}
                    onClick={() => void decide(a.anomaly_id, 'acknowledged')}
                    className="h-7 text-[11.5px]"
                  >
                    Acknowledge
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy === a.anomaly_id}
                    onClick={() => void decide(a.anomaly_id, 'resolved')}
                    className="h-7 text-[11.5px]"
                  >
                    Resolve
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="notif-providers" className="mt-6">
        <h3
          id="notif-providers"
          className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground"
        >
          Providers
        </h3>
        {loading ? (
          <div className="mt-2 space-y-2" aria-hidden="true">
            <Skeleton className="h-[46px] w-full" />
            <Skeleton className="h-[46px] w-full" />
            <Skeleton className="h-[46px] w-full" />
            <Skeleton className="h-[46px] w-full" />
          </div>
        ) : (
          <ul className="mt-2 space-y-2">
            {providers.map((p) => {
              const viaPortal = Boolean(portalKeys[p.id])
              const on = p.has_project_key || p.is_local || viaPortal
              const source = p.is_local
                ? 'local engine'
                : p.has_project_key
                  ? `server · ${p.key_env}`
                  : viaPortal
                    ? 'portal key'
                    : p.key_env
                      ? `not configured · ${p.key_env}`
                      : 'not configured'
              return (
                <li
                  key={p.id}
                  className="flex items-center gap-2 rounded-lg border border-border px-3 py-2"
                >
                  <span
                    aria-hidden="true"
                    className={cn(
                      'h-1.5 w-1.5 shrink-0 rounded-full',
                      on ? 'bg-free' : 'bg-muted-foreground',
                    )}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="text-[12.5px] font-medium text-foreground">{p.name}</div>
                    <div className="text-[11px] text-muted-foreground">{source}</div>
                  </div>
                  <span
                    className="font-mono text-[11px] text-muted-foreground"
                    title={`${p.models_count} models in the registry`}
                  >
                    {p.models_count}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </section>
    </Drawer>
  )
}
