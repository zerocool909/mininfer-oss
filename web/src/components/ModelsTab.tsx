import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, ChevronLeft, ChevronRight, RefreshCw, Search } from 'lucide-react'
import { api, type ExploreModel, type ExploreResult } from '@/lib/api'
import { benchmarkTags, confirmedCaps, EXPLORE_SORTS, modelLabel, priceLabel, successLabel } from '@/lib/explore'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, SectionHeading } from '@/components/ui/card'
import { Select, Switch } from '@/components/ui/form'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableWrap,
} from '@/components/ui/table'
import { TableSkeleton } from '@/components/Skeleton'
import { cn, count } from '@/lib/utils'

const PAGE = 25

/**
 * Browse the registry, not just the routable models.
 *
 * `/v1/models` hides the 1,700 deployments behind one virtual id per task; this
 * exposes them, including the arms the router would reject, because a registry
 * you cannot inspect is a registry you cannot audit. Read-only: every filter is
 * a GET, and the table never calls a provider.
 */
export function ModelsTab() {
  const [data, setData] = useState<ExploreResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const [q, setQ] = useState('')
  const [debouncedQ, setDebouncedQ] = useState('')
  const [provider, setProvider] = useState('')
  const [capability, setCapability] = useState('')
  const [freeOnly, setFreeOnly] = useState(false)
  const [untriedOnly, setUntriedOnly] = useState(false)
  const [sort, setSort] = useState<string>('name')
  const [offset, setOffset] = useState(0)

  // Debounce the text box: each keystroke is a query, and the registry is large.
  useEffect(() => {
    const id = setTimeout(() => setDebouncedQ(q.trim()), 250)
    return () => clearTimeout(id)
  }, [q])

  // Any filter change is a new result set, so page one again.
  useEffect(() => {
    setOffset(0)
  }, [debouncedQ, provider, capability, freeOnly, untriedOnly, sort])

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      const result = await api.explore({
        q: debouncedQ || undefined,
        provider: provider || undefined,
        capability: capability || undefined,
        free_only: freeOnly || undefined,
        untried_only: untriedOnly || undefined,
        sort,
        limit: PAGE,
        offset,
      })
      if (!signal?.aborted) {
        setData(result)
        setErr(null)
      }
    } catch (e) {
      if (!signal?.aborted) setErr(e instanceof Error ? e.message : String(e))
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [debouncedQ, provider, capability, freeOnly, untriedOnly, sort, offset])

  const loadRef = useRef(load)
  loadRef.current = load

  useEffect(() => {
    const ctrl = new AbortController()
    loadRef.current(ctrl.signal)
    return () => ctrl.abort()
  }, [load])

  const caps = useMemo(() => Object.keys(data?.capabilities ?? {}), [data])

  const first = data ? Math.min(data.offset + 1, data.total) : 0
  const last = data ? Math.min(data.offset + data.count, data.total) : 0

  return (
    <div className="mx-auto w-full max-w-6xl space-y-6 p-4 sm:p-6">
      <SectionHeading
        title="Model explorer"
        hint="Every deployment in the registry — price, context, confirmed capabilities and observed success. Filters are conjunctive; nothing here calls a provider."
      />

      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="flex flex-wrap items-end gap-3">
            <div className="relative min-w-[220px] flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search deploy id, provider model, weights…"
                aria-label="Search the registry"
                className="h-9 w-full rounded-md border border-input bg-background pl-9 pr-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>

            <Select
              aria-label="Provider"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="min-w-[160px]"
            >
              <option value="">All providers</option>
              {(data?.providers ?? []).map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </Select>

            <Select
              aria-label="Sort"
              value={sort}
              onChange={(e) => setSort(e.target.value)}
            >
              {EXPLORE_SORTS.map((s) => (
                <option key={s} value={s}>{`sort: ${s}`}</option>
              ))}
            </Select>

            <Switch
              id="free-only"
              checked={freeOnly}
              onCheckedChange={setFreeOnly}
              label="Free only"
              className="pb-1.5"
            />

            <Switch
              id="untried-only"
              checked={untriedOnly}
              onCheckedChange={setUntriedOnly}
              label="Untried free"
              className="pb-1.5"
            />

            <Button
              variant="outline"
              size="sm"
              onClick={() => load()}
              disabled={loading}
              className="h-9"
            >
              <RefreshCw className={cn('mr-1.5 h-3.5 w-3.5', loading && 'animate-spin')} />
              Refresh
            </Button>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted-foreground">Capability</span>
            <button
              onClick={() => setCapability('')}
              className={cn(
                'rounded-full border px-2.5 py-0.5 text-xs',
                capability === '' ? 'border-brand/40 bg-brand/10 text-brand' : 'border-border text-muted-foreground',
              )}
            >
              any
            </button>
            {caps.map((c) => (
              <button
                key={c}
                onClick={() => setCapability(capability === c ? '' : c)}
                title={`${data?.capabilities[c] ?? 0} deployments confirm (a null is unknown, not unsupported)`}
                className={cn(
                  'rounded-full border px-2.5 py-0.5 text-xs',
                  capability === c ? 'border-brand/40 bg-brand/10 text-brand' : 'border-border text-muted-foreground',
                )}
              >
                {c}
                <span className="ml-1 opacity-60">{data?.capabilities[c] ?? 0}</span>
              </button>
            ))}
          </div>
        </CardContent>
      </Card>

      {err && (
        <div className="flex items-center gap-2 rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive-foreground">
          <AlertCircle className="h-4 w-4" /> {err}
        </div>
      )}

      {!data && loading && <TableSkeleton rows={8} />}

      {data && (
        <Card>
          <CardContent className="pt-6">
            <TableWrap>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Model</TableHead>
                    <TableHead>Provider</TableHead>
                    <TableHead>Price (in / out)</TableHead>
                    <TableHead className="text-right">Context</TableHead>
                    <TableHead>Capabilities</TableHead>
                    <TableHead className="text-right">Success</TableHead>
                    <TableHead className="text-right">Benchmarks</TableHead>
                    <TableHead>Cost</TableHead>
                    <TableHead className="text-right">Trial</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.models.map((m) => (
                    <ModelRow key={m.deploy_id} model={m} onTrialed={() => load()} />
                  ))}
                  {data.models.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={9} className="py-8 text-center text-sm text-muted-foreground">
                        No deployment matches these filters.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </TableWrap>

            <div className="mt-4 flex items-center justify-between text-xs text-muted-foreground">
              <span>
                {data.total === 0 ? 'No results' : `${first}–${last} of ${count(data.total)}`}
              </span>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-8"
                  disabled={offset === 0 || loading}
                  onClick={() => setOffset((o) => Math.max(0, o - PAGE))}
                >
                  <ChevronLeft className="h-3.5 w-3.5" /> Prev
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-8"
                  disabled={offset + data.count >= data.total || loading}
                  onClick={() => setOffset((o) => o + PAGE)}
                >
                  Next <ChevronRight className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

function ModelRow({ model, onTrialed }: { model: ExploreModel; onTrialed?: () => void }) {
  const [trialing, setTrialing] = useState(false)
  const [trialResult, setTrialResult] = useState<{ ok: boolean; reason: string } | null>(null)

  const handleTrial = async () => {
    setTrialing(true)
    try {
      const res = await api.trialModel(model.deploy_id)
      setTrialResult({ ok: res.ok, reason: res.judge_reason || (res.ok ? 'passed' : 'failed') })
      if (onTrialed) onTrialed()
    } catch (err) {
      setTrialResult({ ok: false, reason: err instanceof Error ? err.message : String(err) })
    } finally {
      setTrialing(false)
    }
  }

  return (
    <TableRow>
      <TableCell>
        <div className="flex flex-col">
          <span className="font-medium text-foreground">{modelLabel(model)}</span>
          <span className="font-mono text-[11px] text-muted-foreground">{model.deploy_id}</span>
        </div>
      </TableCell>
      <TableCell className="text-muted-foreground">{model.provider}</TableCell>
      <TableCell className="whitespace-nowrap text-muted-foreground">
        {priceLabel(model.price_in, model.price_out)}
      </TableCell>
      <TableCell className="text-right tabular-nums text-muted-foreground">
        {model.context_window ? `${Math.round(model.context_window / 1000)}k` : '—'}
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap gap-1">
          {confirmedCaps(model).map((c) => (
            <Badge key={c} variant="secondary" className="text-[10px]">{c}</Badge>
          ))}
          {model.pushed && <Badge variant="outline" className="text-[10px]">pinned</Badge>}
        </div>
      </TableCell>
      <TableCell className="text-right tabular-nums">
        <span title={model.n ? `${model.wins}/${model.n} calls won` : 'no observations yet'}>
          {successLabel(model.success_rate, model.n)}
        </span>
        {model.n > 0 && <span className="ml-1 text-[11px] text-muted-foreground">({model.n})</span>}
      </TableCell>
      <TableCell className="text-right">
        <div className="flex flex-wrap justify-end gap-1">
          {benchmarkTags(model).map((b) => (
            <Badge
              key={b.key}
              variant="outline"
              className="text-[10px]"
              title={`${b.key}${model.benchmark_source ? ` · ${model.benchmark_source}` : ''}`}
            >
              {b.key} {b.value}
            </Badge>
          ))}
        </div>
      </TableCell>
      <TableCell>
        {model.free
          ? <Badge variant="free">{model.free_kind}</Badge>
          : model.free_kind === 'subscription'
            ? <Badge variant="paid">subscription</Badge>
            : <Badge variant="outline">paid</Badge>}
      </TableCell>
      <TableCell className="text-right whitespace-nowrap">
        <div className="flex items-center justify-end gap-1.5">
          {model.n === 0 ? (
            <Badge variant="outline" className="text-[10px] border-amber-500/40 text-amber-500 bg-amber-500/10">
              Untried
            </Badge>
          ) : model.n < 3 ? (
            <Badge variant="outline" className="text-[10px] border-blue-500/40 text-blue-500 bg-blue-500/10">
              Trialing ({model.n}/3)
            </Badge>
          ) : (
            <Badge variant="outline" className="text-[10px] border-emerald-500/40 text-emerald-500 bg-emerald-500/10">
              Proven
            </Badge>
          )}
          {trialResult ? (
            <Badge
              variant="outline"
              title={trialResult.reason}
              className={cn(
                'text-[10px] cursor-help',
                trialResult.ok
                  ? 'border-emerald-500/40 text-emerald-500 bg-emerald-500/10'
                  : 'border-destructive/40 text-destructive bg-destructive/10'
              )}
            >
              {trialResult.ok ? 'Pass' : 'Fail'}
            </Badge>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              disabled={trialing}
              onClick={handleTrial}
              title="Run on-demand test trial with judge evaluation"
              className="h-6 px-1.5 text-[11px] text-muted-foreground hover:text-foreground"
            >
              {trialing ? <RefreshCw className="h-3 w-3 animate-spin" /> : 'Test'}
            </Button>
          )}
        </div>
      </TableCell>
    </TableRow>
  )
}
