import { useEffect, useState } from 'react'
import { Moon, Sun } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { onCommand } from '@/lib/bus'

const KEY = 'mininfer-theme'
const LEGACY_KEY = 'mininfer-theme'

export function ThemeToggle() {
  const [dark, setDark] = useState<boolean>(() => {
    try {
      const stored = localStorage.getItem(KEY) ?? localStorage.getItem(LEGACY_KEY)
      if (stored) return stored === 'dark'

      return document.documentElement.classList.contains('dark')
    } catch {
      return true
    }
  })

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    try {
      localStorage.setItem(KEY, dark ? 'dark' : 'light')
    } catch {
      /* private mode — the toggle still works for this session */
    }
  }, [dark])

  // Reachable from the ⌘K palette as well as the button.
  useEffect(() => onCommand('toggle-theme', () => setDark((d) => !d)), [])

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => setDark((d) => !d)}
      title="Toggle theme"
      aria-label="Toggle theme"
    >
      {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
    </Button>
  )
}
