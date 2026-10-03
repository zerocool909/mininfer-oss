import { useState } from 'react'
import { Check, Copy, Terminal, X, Code2, Cpu } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface ConnectModalProps {
  open: boolean
  onClose: () => void
}

type TabType = 'python' | 'curl' | 'ts'

export function ConnectModal({ open, onClose }: ConnectModalProps) {
  const [tab, setTab] = useState<TabType>('python')
  const [copied, setCopied] = useState(false)

  if (!open) return null

  const SNIPPETS: Record<TabType, { title: string; lang: string; code: string }> = {
    python: {
      title: 'Python (OpenAI SDK)',
      lang: 'python',
      code: `from openai import OpenAI

# Point directly to your local MinInfer proxy
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="none",  # Authentication handled by MinInfer
)

# Route automatically to the cheapest capable model
response = client.chat.completions.create(
    model="auto",  # or task: "coding", "general_chat", etc.
    messages=[{"role": "user", "content": "Hello MinInfer!"}],
)

print(response.choices[0].message.content)
`,
    },
    curl: {
      title: 'cURL Command',
      lang: 'bash',
      code: `curl http://localhost:8000/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "auto",
    "messages": [
      {"role": "user", "content": "Explain quantum tunneling in one sentence"}
    ]
  }'`,
    },
    ts: {
      title: 'TypeScript (Vercel AI SDK)',
      lang: 'typescript',
      code: `import { createOpenAI } from '@ai-sdk/openai'
import { generateText } from 'ai'

const mininfer = createOpenAI({
  baseURL: 'http://localhost:8000/v1',
  apiKey: 'none',
})

const { text } = await generateText({
  model: mininfer('auto'),
  prompt: 'Write a TypeScript function to debounce API calls',
})

console.log(text)`,
    },
  }

  const activeSnippet = SNIPPETS[tab]

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(activeSnippet.code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch {
      // ignore
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-background/80 backdrop-blur-md transition-opacity"
        onClick={onClose}
      />

      {/* Modal Dialog */}
      <div className="relative w-full max-w-2xl rounded-2xl border border-border/80 bg-card p-6 shadow-2xl animate-scale-in">
        <div className="flex items-center justify-between pb-4 border-b border-border/60">
          <div className="flex items-center gap-2.5">
            <div className="grid h-8 w-8 place-items-center rounded-lg bg-brand/10 text-brand">
              <Cpu className="h-4 w-4" />
            </div>
            <div>
              <h3 className="text-base font-semibold tracking-tight">Connect to MinInfer</h3>
              <p className="text-xs text-muted-foreground">
                Drop-in OpenAI API replacement. Free first; pay only when needed.
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Tab selection */}
        <div className="mt-4 flex items-center justify-between">
          <div className="flex items-center gap-1 rounded-lg bg-muted p-1 text-xs">
            <button
              onClick={() => setTab('python')}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-3 py-1 font-medium transition-colors',
                tab === 'python'
                  ? 'bg-background text-foreground shadow-xs'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              <Code2 className="h-3.5 w-3.5" />
              Python
            </button>
            <button
              onClick={() => setTab('curl')}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-3 py-1 font-medium transition-colors',
                tab === 'curl'
                  ? 'bg-background text-foreground shadow-xs'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              <Terminal className="h-3.5 w-3.5" />
              cURL
            </button>
            <button
              onClick={() => setTab('ts')}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-3 py-1 font-medium transition-colors',
                tab === 'ts'
                  ? 'bg-background text-foreground shadow-xs'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              <Code2 className="h-3.5 w-3.5" />
              TypeScript
            </button>
          </div>

          <Button
            size="sm"
            variant="outline"
            className="h-8 gap-1.5 text-xs font-medium"
            onClick={handleCopy}
          >
            {copied ? (
              <>
                <Check className="h-3.5 w-3.5 text-free" />
                Copied
              </>
            ) : (
              <>
                <Copy className="h-3.5 w-3.5" />
                Copy code
              </>
            )}
          </Button>
        </div>

        {/* Code Snippet Box */}
        <div className="relative mt-3 overflow-hidden rounded-xl border border-border/80 bg-muted/40 font-mono text-xs">
          <div className="flex items-center justify-between border-b border-border/60 bg-muted/70 px-4 py-2 text-[11px] text-muted-foreground">
            <span>{activeSnippet.title}</span>
            <span className="font-semibold text-brand">http://localhost:8000/v1</span>
          </div>
          <pre className="overflow-x-auto p-4 leading-relaxed text-foreground/90 selection:bg-brand/20">
            <code>{activeSnippet.code}</code>
          </pre>
        </div>

        {/* Footer info */}
        <div className="mt-4 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
          <div className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full bg-free animate-pulse-subtle" />
            <span>Proxy endpoint runs on port 8000 by default</span>
          </div>
          <span className="font-mono text-[11px]">mi proxy --port 8000</span>
        </div>
      </div>
    </div>
  )
}
