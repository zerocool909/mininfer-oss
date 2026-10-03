import { useState, useMemo } from 'react'
import {
  MessageSquare,
  Plus,
  Search,
  Trash2,
  Pencil,
  Check,
  X,
  PanelLeftClose,
  PanelLeftOpen,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { StoredSession } from '@/lib/chat'

interface ChatSidebarProps {
  sessions: StoredSession[]
  currentSessionId: string
  onSelectSession: (id: string) => void
  onNewSession: () => void
  onDeleteSession: (id: string) => void
  onRenameSession: (id: string, newTitle: string) => void
  isOpen: boolean
  onToggle: () => void
}

function groupSessions(sessions: StoredSession[]) {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const yesterday = today - 86400000
  const lastWeek = today - 7 * 86400000

  const groups: { label: string; items: StoredSession[] }[] = [
    { label: 'Today', items: [] },
    { label: 'Yesterday', items: [] },
    { label: 'Previous 7 days', items: [] },
    { label: 'Older', items: [] },
  ]

  for (const s of sessions) {
    const t = s.updatedAt || 0
    if (t >= today) groups[0].items.push(s)
    else if (t >= yesterday) groups[1].items.push(s)
    else if (t >= lastWeek) groups[2].items.push(s)
    else groups[3].items.push(s)
  }

  return groups.filter((g) => g.items.length > 0)
}

export function ChatSidebar({
  sessions,
  currentSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
  onRenameSession,
  isOpen,
  onToggle,
}: ChatSidebarProps) {
  const [search, setSearch] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editTitle, setEditTitle] = useState('')

  const filteredSessions = useMemo(() => {
    if (!search.trim()) return sessions
    const q = search.toLowerCase()
    return sessions.filter((s) => s.title.toLowerCase().includes(q))
  }, [sessions, search])

  const groups = useMemo(() => groupSessions(filteredSessions), [filteredSessions])

  const startEditing = (s: StoredSession, e: React.MouseEvent) => {
    e.stopPropagation()
    setEditingId(s.id)
    setEditTitle(s.title)
  }

  const saveEditing = (id: string, e?: React.MouseEvent | React.FormEvent) => {
    if (e) e.stopPropagation()
    if (editTitle.trim()) onRenameSession(id, editTitle.trim())
    setEditingId(null)
  }

  const cancelEditing = (e: React.MouseEvent) => {
    e.stopPropagation()
    setEditingId(null)
  }

  return (
    <>
      {/* Mobile backdrop */}
      {isOpen && (
        <div
          className="fixed inset-0 z-40 bg-background/60 backdrop-blur-sm md:hidden"
          onClick={onToggle}
        />
      )}

      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex w-[268px] flex-col border-r border-border bg-card transition-all duration-300 md:static md:z-10',
          isOpen ? 'translate-x-0' : '-translate-x-full md:hidden md:w-0',
        )}
      >
        {/* Header */}
        <div className="flex h-14 items-center gap-1.5 border-b border-border px-3">
          <Button
            onClick={onNewSession}
            variant="outline"
            size="sm"
            className="h-8 flex-1 justify-start gap-2 rounded-lg border-border bg-background text-[12.5px] font-medium shadow-xs hover:border-brand/40 hover:bg-brand/5 hover:text-foreground"
          >
            <Plus className="h-3.5 w-3.5 text-brand" strokeWidth={2.2} />
            New chat
          </Button>
          <Button
            onClick={onToggle}
            variant="ghost"
            size="icon-sm"
            className="text-muted-foreground hover:text-foreground"
            title="Collapse sidebar"
          >
            <PanelLeftClose className="h-4 w-4" />
          </Button>
        </div>

        {/* Search */}
        <div className="px-3 py-3">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              placeholder="Search chats"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 w-full rounded-lg border border-border bg-muted/40 pl-8 pr-8 text-[12.5px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-brand/50 focus:bg-background"
            />
            {search && (
              <button
                onClick={() => setSearch('')}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                aria-label="Clear search"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
        </div>

        {/* Sessions */}
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-2 pb-3">
          {groups.length === 0 ? (
            <div className="px-3 py-10 text-center text-[12px] text-muted-foreground">
              {search ? 'No matching chats' : 'No chat history yet'}
            </div>
          ) : (
            groups.map((group) => (
              <div key={group.label} className="space-y-0.5">
                <div className="px-2.5 pb-1 text-[10.5px] font-semibold uppercase tracking-wider text-muted-foreground/70">
                  {group.label}
                </div>
                {group.items.map((session) => {
                  const isActive = session.id === currentSessionId
                  const isEditing = editingId === session.id

                  return (
                    <div
                      key={session.id}
                      onClick={() => onSelectSession(session.id)}
                      className={cn(
                        'group relative flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-[12.5px] transition-colors',
                        isActive
                          ? 'bg-accent font-medium text-foreground'
                          : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
                      )}
                    >
                      {isActive && (
                        <span className="absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-full bg-brand" />
                      )}
                      <MessageSquare
                        className={cn(
                          'h-3.5 w-3.5 shrink-0',
                          isActive ? 'text-brand' : 'text-muted-foreground/60',
                        )}
                        strokeWidth={1.9}
                      />

                      {isEditing ? (
                        <div className="flex flex-1 items-center gap-1">
                          <input
                            type="text"
                            value={editTitle}
                            onChange={(e) => setEditTitle(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') saveEditing(session.id, e)
                              if (e.key === 'Escape') setEditingId(null)
                            }}
                            autoFocus
                            className="w-full rounded border border-brand/60 bg-background px-1.5 py-0.5 text-[12.5px] outline-none"
                          />
                          <button
                            onClick={(e) => saveEditing(session.id, e)}
                            className="rounded p-0.5 text-free hover:bg-muted"
                            aria-label="Save name"
                          >
                            <Check className="h-3 w-3" />
                          </button>
                          <button
                            onClick={cancelEditing}
                            className="rounded p-0.5 text-muted-foreground hover:bg-muted"
                            aria-label="Cancel"
                          >
                            <X className="h-3 w-3" />
                          </button>
                        </div>
                      ) : (
                        <span className="min-w-0 flex-1 truncate" title={session.title}>
                          {session.title}
                        </span>
                      )}

                      {!isEditing && (
                        <div
                          className={cn(
                            'flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100',
                            isActive && 'opacity-100',
                          )}
                        >
                          <button
                            onClick={(e) => startEditing(session, e)}
                            className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground"
                            title="Rename"
                          >
                            <Pencil className="h-3 w-3" />
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation()
                              onDeleteSession(session.id)
                            }}
                            className="rounded p-1 text-muted-foreground transition-colors hover:text-destructive"
                            title="Delete"
                          >
                            <Trash2 className="h-3 w-3" />
                          </button>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            ))
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-border px-3.5 py-2.5 text-[11px] text-muted-foreground">
          <span className="tnum font-mono">{sessions.length} chats</span>
          <span className="text-muted-foreground/70">Stored locally</span>
        </div>
      </aside>

      {/* Reopen handle */}
      {!isOpen && (
        <button
          onClick={onToggle}
          className="fixed left-3 top-[4.5rem] z-30 hidden items-center justify-center rounded-lg border border-border bg-card p-2 text-muted-foreground shadow-soft transition-colors hover:bg-accent hover:text-foreground md:flex"
          title="Open history"
        >
          <PanelLeftOpen className="h-4 w-4" />
        </button>
      )}
    </>
  )
}
