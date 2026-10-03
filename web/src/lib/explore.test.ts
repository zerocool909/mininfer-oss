import { describe, expect, it } from 'vitest'
import {
  benchmarkTags,
  confirmedCaps,
  exploreQueryString,
  modelLabel,
  priceLabel,
  successLabel,
} from '@/lib/explore'

/**
 * The explorer's decisions that are testable without a browser: which filters
 * reach the server, what a capability blob means, and how an unmeasured arm
 * reads. The failure modes are quiet ones — a dropped filter shows more than the
 * operator asked for, and a null capability shown as confirmed is a lie.
 */

describe('exploreQueryString', () => {
  it('is empty when there is nothing to ask', () => {
    expect(exploreQueryString({})).toBe('')
  })

  it('drops an empty or false filter rather than sending it', () => {
    // `free_only=false` means "no preference" — sending it would be a filter the
    // server has to interpret instead of a default it can assume.
    expect(exploreQueryString({ q: '', provider: '', free_only: false })).toBe('')
  })

  it('keeps a zero-valued numeric filter', () => {
    // `max_price_out=0` is a real constraint ("free only on price"), not absence.
    expect(exploreQueryString({ max_price_out: 0 })).toBe('?max_price_out=0')
  })

  it('encodes a search term', () => {
    expect(exploreQueryString({ q: 'qwen 7b', free_only: true }))
      .toBe('?q=qwen+7b&free_only=true')
  })

  it('emits keys in a stable order', () => {
    expect(exploreQueryString({ offset: 25, limit: 25, sort: 'price' }))
      .toBe('?sort=price&limit=25&offset=25')
  })
})

describe('confirmedCaps', () => {
  it('keeps only true, and sorts', () => {
    expect(confirmedCaps({ caps: { vision: true, tools: true, structured: false } }))
      .toEqual(['tools', 'vision'])
  })

  it('treats a null as unknown, not as supported', () => {
    // Null is 22,655 of 23,799 rows in the real registry; showing it as a
    // capability would promise more than the registry knows.
    expect(confirmedCaps({ caps: { tools: null, vision: true } })).toEqual(['vision'])
  })

  it('copes with a missing blob', () => {
    expect(confirmedCaps({ caps: {} })).toEqual([])
  })
})

describe('successLabel', () => {
  it('says untried rather than 0% for no evidence', () => {
    expect(successLabel(null, 0)).toBe('untried')
    expect(successLabel(0, 0)).toBe('untried')
  })

  it('rounds a measured rate', () => {
    expect(successLabel(2 / 3, 3)).toBe('67%')
    expect(successLabel(1, 4)).toBe('100%')
  })
})

describe('modelLabel', () => {
  it('falls back to the provider model id when there is no display name', () => {
    expect(modelLabel({ display_name: null, provider_model_id: 'qwen7b' } as any))
      .toBe('qwen7b')
    expect(modelLabel({ display_name: '  ', provider_model_id: 'qwen7b' } as any))
      .toBe('qwen7b')
  })
})

describe('benchmarkTags', () => {
  it('is empty for an unscored model, not a zero', () => {
    expect(benchmarkTags({ benchmark: {} })).toEqual([])
  })

  it('orders best first and honours the limit', () => {
    const tags = benchmarkTags({ benchmark: { coding: 60, reasoning: 91, math: 75 } }, 2)
    expect(tags).toEqual([
      { key: 'reasoning', value: 91 },
      { key: 'math', value: 75 },
    ])
  })

  it('ignores a non-numeric score', () => {
    expect(benchmarkTags({ benchmark: { coding: 60, note: 'n/a' } as any }))
      .toEqual([{ key: 'coding', value: 60 }])
  })
})

describe('priceLabel', () => {
  it('spells out an unknown price instead of showing $0', () => {
    // The router's whole premise is that unknown price is not free; a UI that
    // renders it as $0 undoes that.
    expect(priceLabel(null, null)).toBe('price unknown')
  })

  it('renders both directions and a true zero', () => {
    expect(priceLabel(0, 0)).toBe('$0 / $0 per Mtok')
    expect(priceLabel(0.2, 0.6)).toBe('$0.2 / $0.6 per Mtok')
    expect(priceLabel(1, null)).toBe('$1 / $— per Mtok')
  })
})
