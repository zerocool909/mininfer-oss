/**
 * A tiny window-level command bus.
 *
 * The command palette lives in the app shell, but the actions it triggers
 * (start a chat, toggle compare, flip the theme) live inside deeply nested
 * components. Threading callbacks through every layer for four one-shot actions
 * would be worse than one event name, so the shell emits and the owners listen.
 */
export type AppCommand = 'new-chat' | 'toggle-compare' | 'toggle-theme' | 'focus-composer'

const EVENT = 'mi:command'

export function emitCommand(cmd: AppCommand): void {
  window.dispatchEvent(new CustomEvent<AppCommand>(EVENT, { detail: cmd }))
}

export function onCommand(cmd: AppCommand, handler: () => void): () => void {
  const listener = (e: Event) => {
    if ((e as CustomEvent<AppCommand>).detail === cmd) handler()
  }
  window.addEventListener(EVENT, listener)
  return () => window.removeEventListener(EVENT, listener)
}
