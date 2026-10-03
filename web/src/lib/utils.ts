import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** 0 -> "FREE", null -> em dash, otherwise 4dp dollars. */
export function money(v: number | null | undefined): string {
  if (v === 0) return 'FREE'
  if (v === null || v === undefined) return '—'
  return `$${v < 1 ? v.toFixed(4) : v.toFixed(2)}`
}

export function num(v: number | null | undefined, digits = 3): string {
  return v === null || v === undefined ? '—' : v.toFixed(digits)
}

export function count(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toLocaleString()
}
