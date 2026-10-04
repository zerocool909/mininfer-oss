import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  /** Rendered instead of the app. A test supplies its own to assert on. */
  fallback?: (error: Error, reset: () => void) => ReactNode
}

interface State {
  error: Error | null
}

/**
 * The app's last line of defence.
 *
 * React unmounts the whole tree when a render throws, so without this a single
 * bad field — a `null` where the API used to send a number, a table row reading
 * `.length` of `undefined` — leaves a **blank white page**. Nothing else catches
 * it: the Python suite, `vitest` (which tests functions, not the mounted app) and
 * `tsc` all pass, because the type is right and only the *data* was not.
 *
 * Deliberately a class: `componentDidCatch` has no hook equivalent, and the
 * boundary must be able to render its fallback *outside* the tree that just
 * threw.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Keep the component stack: "cannot read properties of undefined" is only
    // actionable with the component that read it.
    console.error('MinInfer UI error:', error, info.componentStack)
  }

  reset = (): void => {
    this.setState({ error: null })
  }

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children

    if (this.props.fallback) return this.props.fallback(error, this.reset)

    return (
      <div className="flex min-h-screen items-center justify-center bg-background p-6">
        <div className="w-full max-w-lg rounded-xl border border-destructive/40 bg-destructive/5 p-6">
          <h1 className="text-base font-semibold text-foreground">
            The dashboard hit an error
          </h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            The API is probably fine — this is a rendering failure, so a reload
            usually clears it. Nothing was sent to a provider.
          </p>
          <pre className="mt-3 max-h-40 overflow-auto rounded-md border border-border/60 bg-background/60 p-3 font-mono text-[11px] text-destructive">
            {error.message || String(error)}
          </pre>
          <div className="mt-4 flex gap-2">
            <button
              onClick={this.reset}
              className="rounded-lg border border-border bg-card px-3 py-1.5 text-[12.5px] font-medium hover:bg-muted"
            >
              Try again
            </button>
            <button
              onClick={() => window.location.reload()}
              className="rounded-lg border border-border bg-card px-3 py-1.5 text-[12.5px] font-medium hover:bg-muted"
            >
              Reload
            </button>
          </div>
        </div>
      </div>
    )
  }
}
