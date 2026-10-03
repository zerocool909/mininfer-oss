import { useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { Button } from '@/components/ui/button'

export function CopyButton({ text, className }: { text: string; className?: string }) {
  const [done, setDone] = useState(false)
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      className={className}
      title="Copy"
      aria-label="Copy"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
          setDone(true)
          setTimeout(() => setDone(false), 1400)
        } catch {
          /* clipboard blocked (http on a non-localhost host) — ignore */
        }
      }}
    >
      {done ? <Check className="h-3.5 w-3.5 text-free" /> : <Copy className="h-3.5 w-3.5" />}
    </Button>
  )
}
