/**
 * Client-side user API key and local endpoint storage in localStorage.
 *
 * Keys remain strictly on the user's browser and are sent to the proxy
 * via headers at request time. If a custom key is present, the proxy
 * uses it; otherwise it falls back to project defaults from .env.
 */

const STORAGE_KEYS_KEY = 'mininfer_user_api_keys'
const STORAGE_LOCAL_KEY = 'mininfer_local_endpoints'

export function getUserKeys(): Record<string, string> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = localStorage.getItem(STORAGE_KEYS_KEY)
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

export function setUserKey(provider: string, key: string): void {
  if (typeof window === 'undefined') return
  const current = getUserKeys()
  const trimmed = key.trim()
  if (trimmed) {
    current[provider] = trimmed
  } else {
    delete current[provider]
  }
  localStorage.setItem(STORAGE_KEYS_KEY, JSON.stringify(current))
  window.dispatchEvent(new Event('mininfer_keys_changed'))
}

export function removeUserKey(provider: string): void {
  if (typeof window === 'undefined') return
  const current = getUserKeys()
  delete current[provider]
  localStorage.setItem(STORAGE_KEYS_KEY, JSON.stringify(current))
  window.dispatchEvent(new Event('mininfer_keys_changed'))
}

export function clearAllUserKeys(): void {
  if (typeof window === 'undefined') return
  localStorage.removeItem(STORAGE_KEYS_KEY)
  window.dispatchEvent(new Event('mininfer_keys_changed'))
}

export function getLocalEndpoints(): { ollama?: string; llamacpp?: string } {
  if (typeof window === 'undefined') return {}
  try {
    const raw = localStorage.getItem(STORAGE_LOCAL_KEY)
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

export function setLocalEndpoints(endpoints: { ollama?: string; llamacpp?: string }): void {
  if (typeof window === 'undefined') return
  localStorage.setItem(STORAGE_LOCAL_KEY, JSON.stringify(endpoints))
  window.dispatchEvent(new Event('mininfer_keys_changed'))
}

export function getUserKeysHeader(): Record<string, string> {
  const keys = getUserKeys()
  const locals = getLocalEndpoints()
  const headers: Record<string, string> = {}
  if (Object.keys(keys).length > 0) {
    headers['X-User-API-Keys'] = JSON.stringify(keys)
  }
  if (Object.keys(locals).length > 0) {
    headers['X-Local-Endpoints'] = JSON.stringify(locals)
  }
  return headers
}
