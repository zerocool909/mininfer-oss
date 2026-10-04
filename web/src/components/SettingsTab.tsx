import { useEffect, useState } from 'react'
import {
  Key,
  Server,
  Cpu,
  Search,
  CheckCircle2,
  ExternalLink,
  Eye,
  EyeOff,
  RefreshCw,
  Database,
  Sliders,
  AlertCircle,
  HardDriveDownload,
  ShieldCheck,
  Play,
} from 'lucide-react'
import { api, type ProviderInfo, type ProviderModel, type LocalProbeResult } from '@/lib/api'
import { getUserKeys, setUserKey, removeUserKey, clearAllUserKeys, getLocalEndpoints, setLocalEndpoints } from '@/lib/keys'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'

export function SettingsTab() {
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [userKeys, setUserKeysState] = useState<Record<string, string>>({})
  const [keyInputs, setKeyInputs] = useState<Record<string, string>>({})
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({})
  const [savedSuccess, setSavedSuccess] = useState<Record<string, boolean>>({})

  // Local Engine State
  const [ollamaUrl, setOllamaUrl] = useState('http://localhost:11434')
  const [llamacppUrl, setLlamacppUrl] = useState('http://localhost:8080')
  const [probeResult, setProbeResult] = useState<LocalProbeResult | null>(null)
  const [probing, setProbing] = useState(false)
  const [registeredSuccess, setRegisteredSuccess] = useState<string | null>(null)

  // Model Explorer Search & Filter State
  const [searchQuery, setSearchQuery] = useState('')
  const [providerFilter, setProviderFilter] = useState('all')
  const [taskFilter, setTaskFilter] = useState('all')
  const [displayLimit, setDisplayLimit] = useState(100)

  // Connectivity Test State
  const [testingProvider, setTestingProvider] = useState<Record<string, boolean>>({})
  const [testResult, setTestResult] = useState<
    Record<
      string,
      {
        ok: boolean
        model: string
        is_free: boolean
        latency_ms?: number | null
        reply?: string | null
        error_class?: string | null
        error_detail?: string | null
        key_source?: 'custom' | 'env' | 'none'
      }
    >
  >({})

  const handleTestProvider = async (providerId: string) => {
    setTestingProvider((prev) => ({ ...prev, [providerId]: true }))
    setTestResult((prev) => {
      const next = { ...prev }
      delete next[providerId]
      return next
    })
    try {
      // Prefer what was just typed. The old order (`userKeys || keyInputs`) meant
      // a stale saved key silently won over the new one being tested — you would
      // paste a fresh key, press Test, and be told about the old key's failure.
      const activeKey = keyInputs[providerId]?.trim() || userKeys[providerId]
      const res = await api.testProvider(providerId, activeKey)
      setTestResult((prev) => ({
        ...prev,
        [providerId]: {
          ok: res.ok,
          model: res.model,
          is_free: res.is_free,
          latency_ms: res.latency_ms,
          reply: res.reply,
          error_class: res.error_class,
          error_detail: res.error_detail,
          // Fetched from the API and then dropped here, so the panel rendered
          // "none configured" for every result: the diagnostic that says *which*
          // credential was tried never reached the screen. Caught by
          // e2e/providers.spec.ts on its first run (see test_fix.md #14).
          key_source: res.key_source,
        },
      }))
    } catch (e: any) {
      setTestResult((prev) => ({
        ...prev,
        [providerId]: {
          ok: false,
          model: 'unknown',
          is_free: false,
          error_class: 'request_failed',
          error_detail: e?.message || String(e),
        },
      }))
    } finally {
      setTestingProvider((prev) => ({ ...prev, [providerId]: false }))
    }
  }

  // Load providers and keys
  const loadData = async () => {
    setLoading(true)
    try {
      const pRes = await api.providers()
      setProviders(pRes.providers)
      const currentKeys = getUserKeys()
      setUserKeysState(currentKeys)
      const initialInputs: Record<string, string> = {}
      for (const [p, k] of Object.entries(currentKeys)) {
        initialInputs[p] = k
      }
      setKeyInputs(initialInputs)

      const locals = getLocalEndpoints()
      if (locals.ollama) setOllamaUrl(locals.ollama)
      if (locals.llamacpp) setLlamacppUrl(locals.llamacpp)
    } catch (e) {
      console.error('Failed to load providers:', e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadData()
  }, [])

  const handleSaveKey = (providerId: string) => {
    const val = keyInputs[providerId] || ''
    if (val.trim()) {
      setUserKey(providerId, val.trim())
      setUserKeysState((prev) => ({ ...prev, [providerId]: val.trim() }))
      setSavedSuccess((prev) => ({ ...prev, [providerId]: true }))
      setTimeout(() => {
        setSavedSuccess((prev) => ({ ...prev, [providerId]: false }))
      }, 2000)
    } else {
      handleRemoveKey(providerId)
    }
  }

  const handleRemoveKey = (providerId: string) => {
    removeUserKey(providerId)
    setUserKeysState((prev) => {
      const next = { ...prev }
      delete next[providerId]
      return next
    })
    setKeyInputs((prev) => ({ ...prev, [providerId]: '' }))
  }

  const handleProbeLocal = async (engine: 'ollama' | 'llamacpp') => {
    setProbing(true)
    setProbeResult(null)
    setRegisteredSuccess(null)
    try {
      const url = engine === 'ollama' ? ollamaUrl : llamacppUrl
      // Save local endpoint preference
      setLocalEndpoints({
        ollama: ollamaUrl,
        llamacpp: llamacppUrl,
      })
      const res = await api.probeLocal(engine, url)
      setProbeResult(res)
    } catch (e) {
      setProbeResult({
        engine,
        url: engine === 'ollama' ? ollamaUrl : llamacppUrl,
        connected: false,
        models: [],
        error: String(e),
      })
    } finally {
      setProbing(false)
    }
  }

  const handleRegisterLocal = async () => {
    if (!probeResult || !probeResult.models || probeResult.models.length === 0) return
    try {
      const res = await api.registerLocal(probeResult.engine as 'ollama' | 'llamacpp', probeResult.models)
      setRegisteredSuccess(`Successfully registered ${res.registered.length} local model(s) into MinInfer!`)
      loadData()
    } catch (e) {
      alert('Failed to register models: ' + String(e))
    }
  }

  // Model Explorer Aggregations
  const allModels: (ProviderModel & { providerId: string; providerName: string })[] = []
  for (const p of providers) {
    for (const m of p.models) {
      allModels.push({
        ...m,
        providerId: p.id,
        providerName: p.name,
      })
    }
  }

  const filteredModels = allModels.filter((m) => {
    const q = searchQuery.toLowerCase()
    const matchesSearch =
      !q ||
      m.deploy_id.toLowerCase().includes(q) ||
      m.display_name.toLowerCase().includes(q) ||
      m.providerId.toLowerCase().includes(q)
    const matchesProvider = providerFilter === 'all' || m.providerId === providerFilter
    const matchesTask =
      taskFilter === 'all' ||
      (taskFilter === 'vision' && m.caps.vision) ||
      (taskFilter === 'tools' && m.caps.tools) ||
      (taskFilter === 'structured' && m.caps.structured) ||
      (taskFilter === 'free' && m.is_free)
    return matchesSearch && matchesProvider && matchesTask
  })

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 space-y-10 animate-fade-in text-foreground">
      {/* Page Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border-b border-border/70 pb-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground flex items-center gap-2.5">
            <Sliders className="h-6 w-6 text-brand" />
            Settings & Model Providers
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Configure your personal API keys, explore supported models across cloud and local engines, or add Gemini models on Google Cloud.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={loadData}
            disabled={loading}
            className="gap-1.5 text-xs h-8 border-border"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
            Refresh
          </Button>
          {Object.keys(userKeys).length > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={clearAllUserKeys}
              className="text-xs h-8 text-destructive hover:bg-destructive/10 border-destructive/30"
            >
              Clear Custom Keys
            </Button>
          )}
        </div>
      </div>



      {/* Local Inference Engine (Ollama & llama.cpp) Section */}
      <div className="rounded-xl border border-border bg-card p-5 shadow-xs space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 border-b border-border/60 pb-3">
          <div className="flex items-center gap-2.5">
            <Server className="h-5 w-5 text-emerald-500" />
            <div>
              <h2 className="text-base font-semibold text-foreground">Local Inference Engine (Ollama & llama.cpp)</h2>
              <p className="text-xs text-muted-foreground">
                Run models locally with 100% data privacy, keyless execution, and zero marginal cost ($0.00).
              </p>
            </div>
          </div>
          <span className="rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20 px-2.5 py-0.5 text-xs font-semibold">
            Zero Cost Tier
          </span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
          {/* Ollama Box */}
          <div className="rounded-lg border border-border/70 bg-muted/20 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="font-semibold text-foreground">Ollama Daemon</span>
              <a
                href="https://ollama.com"
                target="_blank"
                rel="noreferrer"
                className="text-[11px] text-brand hover:underline flex items-center gap-1"
              >
                ollama.com <ExternalLink className="h-3 w-3" />
              </a>
            </div>
            <div>
              <label className="text-[11px] text-muted-foreground block mb-1">Ollama Base URL</label>
              <input
                type="text"
                value={ollamaUrl}
                onChange={(e) => setOllamaUrl(e.target.value)}
                placeholder="http://localhost:11434"
                className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-xs font-mono text-foreground focus:outline-hidden focus:ring-1 focus:ring-brand"
              />
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => handleProbeLocal('ollama')}
              disabled={probing}
              className="w-full gap-1.5 text-xs"
            >
              <Cpu className="h-3.5 w-3.5 text-brand" />
              Probe Ollama Models
            </Button>
          </div>

          {/* llama.cpp Box */}
          <div className="rounded-lg border border-border/70 bg-muted/20 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="font-semibold text-foreground">llama.cpp / llama-server</span>
              <a
                href="https://github.com/ggerganov/llama.cpp"
                target="_blank"
                rel="noreferrer"
                className="text-[11px] text-brand hover:underline flex items-center gap-1"
              >
                GitHub <ExternalLink className="h-3 w-3" />
              </a>
            </div>
            <div>
              <label className="text-[11px] text-muted-foreground block mb-1">llama-server Base URL</label>
              <input
                type="text"
                value={llamacppUrl}
                onChange={(e) => setLlamacppUrl(e.target.value)}
                placeholder="http://localhost:8080"
                className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-xs font-mono text-foreground focus:outline-hidden focus:ring-1 focus:ring-brand"
              />
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => handleProbeLocal('llamacpp')}
              disabled={probing}
              className="w-full gap-1.5 text-xs"
            >
              <Cpu className="h-3.5 w-3.5 text-brand" />
              Probe llama.cpp Server
            </Button>
          </div>
        </div>

        {/* Probe Output Banner */}
        {probeResult && (
          <div className={cn(
            "p-3.5 rounded-lg border text-xs space-y-2",
            probeResult.connected ? "bg-emerald-500/10 border-emerald-500/30" : "bg-destructive/10 border-destructive/30"
          )}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {probeResult.connected ? (
                  <>
                    <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                    <span className="font-semibold text-foreground">
                      Connected to {probeResult.engine.toUpperCase()} at {probeResult.url}
                    </span>
                  </>
                ) : (
                  <>
                    <AlertCircle className="h-4 w-4 text-destructive" />
                    <span className="font-semibold text-destructive">
                      Could not reach {probeResult.engine} at {probeResult.url}
                    </span>
                  </>
                )}
              </div>
              {probeResult.connected && probeResult.models.length > 0 && (
                <Button
                  size="sm"
                  onClick={handleRegisterLocal}
                  className="h-7 text-xs gap-1 bg-emerald-600 hover:bg-emerald-700 text-white"
                >
                  <HardDriveDownload className="h-3 w-3" />
                  Sync {probeResult.models.length} Models to Router
                </Button>
              )}
            </div>

            {probeResult.connected && (
              <div className="space-y-1">
                <span className="text-[11px] text-muted-foreground">Discovered Local Models:</span>
                {probeResult.models.length > 0 ? (
                  <div className="flex flex-wrap gap-1.5 pt-1">
                    {probeResult.models.map((m) => (
                      <span
                        key={m}
                        className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 font-mono text-[11px] text-foreground font-medium"
                      >
                        {m}
                      </span>
                    ))}
                  </div>
                ) : (
                  <p className="text-muted-foreground italic">No models currently pulled/loaded in daemon.</p>
                )}
              </div>
            )}

            {!probeResult.connected && probeResult.error && (
              <p className="font-mono text-[11px] text-muted-foreground">{probeResult.error}</p>
            )}
          </div>
        )}

        {registeredSuccess && (
          <div className="p-3 bg-emerald-500/15 border border-emerald-500/40 rounded-lg text-xs text-emerald-600 dark:text-emerald-400 font-medium">
            {registeredSuccess}
          </div>
        )}
      </div>

      {/* Cloud Providers & API Keys Management */}
      <div className="space-y-4">
        <div>
          <h2 className="text-base font-semibold text-foreground flex items-center gap-2">
            <Key className="h-5 w-5 text-brand" />
            Cloud Provider API Keys
          </h2>
          <p className="text-xs text-muted-foreground">
            Enter your personal API key for any provider. If left blank, MinInfer falls back to the server's project default (.env).
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {providers
            .filter((p) => !p.is_local)
            .map((provider) => {
              const hasCustom = Boolean(userKeys[provider.id])
              const hasDefault = provider.has_project_key
              const isSaved = savedSuccess[provider.id]
              const show = showKeys[provider.id]
              const isTesting = Boolean(testingProvider[provider.id])
              const res = testResult[provider.id]

              return (
                <div
                  key={provider.id}
                  className={cn(
                    'rounded-xl border bg-card p-4 shadow-xs space-y-3 transition-all',
                    hasCustom
                      ? 'border-emerald-500/40 bg-emerald-500/5'
                      : hasDefault
                        ? 'border-border/80'
                        : 'border-border/50 opacity-90',
                  )}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <h3 className="text-sm font-semibold text-foreground flex items-center gap-1.5">
                        {provider.name}
                      </h3>
                      <p className="text-[11px] text-muted-foreground line-clamp-1">
                        {provider.description}
                      </p>
                    </div>
                    {provider.docs_url && (
                      <a
                        href={provider.docs_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-muted-foreground hover:text-brand"
                        title="Get API Key"
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                      </a>
                    )}
                  </div>

                  {/* Status Badge */}
                  <div className="flex items-center gap-2">
                    {hasCustom ? (
                      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 border border-emerald-500/30 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-600 dark:text-emerald-400">
                        <CheckCircle2 className="h-3 w-3" />
                        Custom Key Active
                      </span>
                    ) : hasDefault ? (
                      <span className="inline-flex items-center gap-1 rounded-full bg-blue-500/15 border border-blue-500/30 px-2 py-0.5 text-[10.5px] font-semibold text-blue-600 dark:text-blue-400">
                        <ShieldCheck className="h-3 w-3" />
                        Project Default Active ({provider.key_env})
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 rounded-full bg-muted border border-border/60 px-2 py-0.5 text-[10.5px] font-medium text-muted-foreground">
                        No Key Configured
                      </span>
                    )}
                  </div>

                  {/* Key Input */}
                  <div className="space-y-1.5 pt-1">
                    <div className="relative">
                      <input
                        type={show ? 'text' : 'password'}
                        value={keyInputs[provider.id] || ''}
                        onChange={(e) =>
                          setKeyInputs((prev) => ({ ...prev, [provider.id]: e.target.value }))
                        }
                        onKeyDown={(e) => {
                          // Enter saves. A key that is typed but not saved is
                          // indistinguishable from no key at all, and that is a
                          // confusing way to lose an afternoon.
                          if (e.key === 'Enter') {
                            e.preventDefault()
                            handleSaveKey(provider.id)
                          }
                        }}
                        placeholder={hasDefault ? `Override ${provider.key_env}...` : `Enter ${provider.name} key...`}
                        className="w-full rounded-md border border-border bg-background px-3 py-1.5 pr-8 text-xs font-mono text-foreground focus:outline-hidden focus:ring-1 focus:ring-brand"
                      />
                      <button
                        type="button"
                        // Icon-only, so it needs its name from here: 25 of these
                        // rendered as unlabelled buttons (axe: `button-name`).
                        aria-label={show ? `Hide the ${provider.name} key` : `Show the ${provider.name} key`}
                        onClick={() => setShowKeys((prev) => ({ ...prev, [provider.id]: !prev[provider.id] }))}
                        className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {show ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                      </button>
                    </div>

                    <div className="flex items-center justify-between pt-1">
                      <span className="text-[10px] text-muted-foreground">
                        {provider.models_count} model{provider.models_count === 1 ? '' : 's'} available
                      </span>
                      <div className="flex items-center gap-1.5">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={isTesting}
                          onClick={() => handleTestProvider(provider.id)}
                          className="h-6 text-[11px] px-2 border-border/80 text-muted-foreground hover:text-foreground"
                          title="Test upstream connectivity with a free model"
                        >
                          <Play className={cn('h-3 w-3 mr-1 text-emerald-500', isTesting && 'animate-spin')} />
                          {isTesting ? 'Testing…' : 'Test'}
                        </Button>
                        {hasCustom && (
                          <button
                            type="button"
                            onClick={() => handleRemoveKey(provider.id)}
                            className="text-[11px] text-destructive hover:underline px-1"
                          >
                            Remove
                          </button>
                        )}
                        <Button
                          size="sm"
                          onClick={() => handleSaveKey(provider.id)}
                          className={cn(
                            'h-6 text-[11px] px-2.5',
                            isSaved ? 'bg-emerald-600 text-white' : ''
                          )}
                        >
                          {isSaved ? 'Saved!' : 'Save Key'}
                        </Button>
                      </div>
                    </div>

                    {/* Test Connectivity Result */}
                    {res && (
                      <div
                        className={cn(
                          'rounded-md p-2 text-[11px] border mt-2 space-y-1 transition-all',
                          res.ok
                            ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-700 dark:text-emerald-300'
                            : 'bg-destructive/10 border-destructive/30 text-destructive',
                        )}
                      >
                        <div className="flex items-center justify-between font-medium">
                          <span className="flex items-center gap-1">
                            {res.ok ? (
                              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                            ) : (
                              <AlertCircle className="h-3.5 w-3.5 text-destructive" />
                            )}
                            {res.ok ? 'Connected successfully' : 'Connectivity failed'}
                          </span>
                          {res.latency_ms && (
                            <span className="font-mono text-[10px] opacity-80">{res.latency_ms}ms</span>
                          )}
                        </div>
                        <div className="text-[10px] opacity-90 line-clamp-1 font-mono">
                          Model: {res.model} {res.is_free && <span className="text-emerald-600 dark:text-emerald-400 font-bold">(FREE)</span>}
                        </div>
                        {/* Which key was actually tried. "Connectivity failed" with no
                            indication of the credential sends you hunting: a missing
                            Authorization header means *no key*, not a bad one. */}
                        <div className="text-[10px] opacity-80">
                          Key:{' '}
                          {res.key_source === 'custom'
                            ? 'your saved key'
                            : res.key_source === 'env'
                              ? 'project default (from .env)'
                              : 'none configured'}
                        </div>
                        {res.reply && (
                          <div className="text-[10.5px] italic opacity-95 line-clamp-2 bg-background/50 p-1 rounded">
                            "{res.reply.trim()}"
                          </div>
                        )}
                        {!res.ok && res.error_class && (
                          <div className="text-[10.5px] text-destructive/90">
                            Reason: <code className="font-mono">{res.error_class}</code>
                            {res.error_detail ? ` — ${res.error_detail}` : ''}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
        </div>
      </div>

      {/* Model & Provider Explorer */}
      <div className="space-y-4 pt-4 border-t border-border/70">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-foreground flex items-center gap-2">
              <Database className="h-5 w-5 text-brand" />
              All Models & Provider Explorer
            </h2>
            <p className="text-xs text-muted-foreground">
              Inspect context window, pricing, capabilities, and benchmarks for any deployment across your router fleet.
            </p>
          </div>
          <div className="text-xs text-muted-foreground font-mono">
            Showing {Math.min(filteredModels.length, displayLimit)} of {filteredModels.length} models
            {filteredModels.length !== allModels.length && ` (out of ${allModels.length} total)`}
          </div>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative flex-1 min-w-[220px]">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => {
                setSearchQuery(e.target.value)
                setDisplayLimit(100)
              }}
              aria-label="Search providers and models"
              placeholder="Search model name, deployment ID, or provider..."
              className="w-full rounded-md border border-border bg-background pl-8 pr-3 py-1.5 text-xs text-foreground focus:outline-hidden focus:ring-1 focus:ring-brand"
            />
          </div>

          <select
            aria-label="Filter by provider"
            value={providerFilter}
            onChange={(e) => {
              setProviderFilter(e.target.value)
              setDisplayLimit(100)
            }}
            className="rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:outline-hidden focus:ring-1 focus:ring-brand"
          >
            <option value="all">All Providers</option>
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.models_count})
              </option>
            ))}
          </select>

          <select
            aria-label="Filter by capability"
            value={taskFilter}
            onChange={(e) => {
              setTaskFilter(e.target.value)
              setDisplayLimit(100)
            }}
            className="rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:outline-hidden focus:ring-1 focus:ring-brand"
          >
            <option value="all">All Capabilities</option>
            <option value="free">Free Tier Only ($0.00)</option>
            <option value="vision">Vision Enabled</option>
            <option value="tools">Tool / Function Calling</option>
            <option value="structured">Structured Output</option>
          </select>
        </div>

        {/* Models Table / Grid */}
        <div className="rounded-xl border border-border bg-card overflow-hidden shadow-xs">
          <div className="overflow-x-auto max-h-[500px] overflow-y-auto">
            <table className="w-full text-left text-xs border-collapse">
              <thead className="sticky top-0 bg-muted/90 backdrop-blur-xs text-muted-foreground font-semibold border-b border-border z-10">
                <tr>
                  <th className="py-2.5 px-3">Model / Deployment ID</th>
                  <th className="py-2.5 px-3">Provider</th>
                  <th className="py-2.5 px-3">Context</th>
                  <th className="py-2.5 px-3">Capabilities</th>
                  <th className="py-2.5 px-3">Pricing (In / Out)</th>
                  <th className="py-2.5 px-3">Intelligence</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/50 font-mono text-[11.5px]">
                {filteredModels.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="py-8 text-center text-muted-foreground italic">
                      No models matching your search criteria.
                    </td>
                  </tr>
                ) : (
                  filteredModels.slice(0, displayLimit).map((m) => (
                    <tr key={m.deploy_id} className="hover:bg-muted/30 transition-colors">
                      <td className="py-2.5 px-3">
                        <div className="font-semibold text-foreground font-sans">
                          {m.display_name}
                        </div>
                        <div className="text-[10px] text-muted-foreground">{m.deploy_id}</div>
                      </td>
                      <td className="py-2.5 px-3 font-sans">
                        <span className="rounded-md bg-muted px-1.5 py-0.5 text-[10.5px] font-medium text-foreground">
                          {m.providerName}
                        </span>
                      </td>
                      <td className="py-2.5 px-3">
                        {m.context_window ? (
                          <span className={cn(
                            "px-1.5 py-0.5 rounded text-[10.5px] font-medium",
                            m.context_window >= 1000000
                              ? "bg-purple-500/15 text-purple-600 dark:text-purple-400 border border-purple-500/30"
                              : "text-foreground"
                          )}>
                            {m.context_window >= 1000000
                              ? `${(m.context_window / 1000000).toFixed(1)}M`
                              : `${Math.round(m.context_window / 1000)}k`}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="py-2.5 px-3 font-sans">
                        <div className="flex items-center gap-1">
                          {m.caps.vision && (
                            <span className="rounded bg-sky-500/10 text-sky-600 dark:text-sky-400 border border-sky-500/20 px-1 py-0.2 text-[9.5px]">
                              Vision
                            </span>
                          )}
                          {m.caps.tools && (
                            <span className="rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20 px-1 py-0.2 text-[9.5px]">
                              Tools
                            </span>
                          )}
                          {m.caps.structured && (
                            <span className="rounded bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20 px-1 py-0.2 text-[9.5px]">
                              JSON
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="py-2.5 px-3">
                        {m.is_free ? (
                          <span className="rounded bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30 px-1.5 py-0.5 text-[10px] font-bold">
                            FREE ($0.00)
                          </span>
                        ) : (
                          <span className="text-muted-foreground">
                            ${((m.price_in || 0) * 1e6).toFixed(2)} / ${((m.price_out || 0) * 1e6).toFixed(2)} M
                          </span>
                        )}
                      </td>
                      <td className="py-2.5 px-3 font-sans">
                        {m.benchmarks?.aa_intelligence ? (
                          <span className="font-semibold text-foreground">
                            {m.benchmarks.aa_intelligence} AA
                          </span>
                        ) : (
                          <span className="text-muted-foreground text-[10px]">Unrated</span>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {filteredModels.length > displayLimit && (
            <div className="p-3 text-center border-t border-border bg-muted/20">
              <Button
                size="sm"
                variant="outline"
                onClick={() => setDisplayLimit((prev) => prev + 100)}
                className="text-xs font-medium"
              >
                Load Next 100 Models ({filteredModels.length - displayLimit} remaining)
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
