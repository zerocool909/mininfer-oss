/**
 * Pure query/format helpers for the model explorer.
 *
 * Kept out of the component so the interesting parts — which filters survive a
 * request, what a capability blob means, how an untried arm reads — are unit
 * tested rather than only visible on a screen.
 */
import type { ExploreModel, ExploreQuery } from './api'

/** The filter keys, in a stable order so a URL is reproducible. */
export const EXPLORE_KEYS = [
  'q',
  'provider',
  'capability',
  'free_only',
  'untried_only',
  'max_price_out',
  'min_context',
  'sort',
  'limit',
  'offset',
] as const

export const EXPLORE_SORTS = ['name', 'price', 'context', 'success', 'calls'] as const

/**
 * `""` when there is nothing to send, else a `?a=1&b=2` suffix.
 *
 * A `false` boolean and an empty string are *absent*, not "filter by zero":
 * `free_only=false` means "no preference", and sending it would be a filter the
 * server has to interpret rather than a default it can assume.
 */
export function exploreQueryString(params: ExploreQuery): string {
  const qs = new URLSearchParams()
  for (const key of EXPLORE_KEYS) {
    const value = params[key]
    if (value === undefined || value === null || value === '' || value === false) continue
    qs.set(key, String(value))
  }
  const s = qs.toString()
  return s ? `?${s}` : ''
}

/** Capabilities confirmed `true`. A null is unknown and must not be shown as one. */
export function confirmedCaps(model: Pick<ExploreModel, 'caps'>): string[] {
  return Object.entries(model.caps ?? {})
    .filter(([, v]) => v === true)
    .map(([k]) => k)
    .sort()
}

/** `"67%"`, or `"untried"` when the registry has no observation to divide by. */
export function successLabel(rate: number | null, n: number): string {
  if (!n) return 'untried'
  return `${Math.round((rate ?? 0) * 100)}%`
}

/** A model with no display name falls back to the provider's own model id. */
export function modelLabel(model: ExploreModel): string {
  return model.display_name?.trim() || model.provider_model_id
}

/** `"$0.20 / $0.60 per Mtok"` (in/out), spelling out unknown prices. */
export function priceLabel(priceIn: number | null, priceOut: number | null): string {
  if (priceIn === null && priceOut === null) return 'price unknown'
  const fmt = (v: number | null) => (v === null ? '—' : v === 0 ? '0' : String(v))
  return `$${fmt(priceIn)} / $${fmt(priceOut)} per Mtok`
}

/**
 * The highest benchmark scores for a model, best first and capped.
 *
 * Empty for an unscored weights row — an absent benchmark is not a score of
 * zero, and showing nothing is the honest rendering.
 */
export function benchmarkTags(
  model: Pick<ExploreModel, 'benchmark'>,
  limit = 3,
): { key: string; value: number }[] {
  return Object.entries(model.benchmark ?? {})
    .filter((entry): entry is [string, number] => typeof entry[1] === 'number')
    .map(([key, value]) => ({ key, value }))
    .sort((a, b) => b.value - a.value)
    .slice(0, limit)
}
