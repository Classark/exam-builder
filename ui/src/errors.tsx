import { Component, type ErrorInfo, type ReactNode } from 'react'
import { RotateCcw, TriangleAlert } from 'lucide-react'

/** A crash in the UI itself, sent to the engine for app.log (POST /api/client_error) - the
 * console isn't something a teacher can send us. Never throws: reporting a failure mustn't
 * be a second one. */
export function reportClientError(error: unknown, where: string, componentStack = ''): void {
  const e = error instanceof Error ? error : new Error(String(error))
  void fetch('/api/client_error', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: e.message, stack: e.stack ?? '', component_stack: componentStack, where }),
  }).catch(() => undefined)
}

/** Catches a screen that crashed and shows a way on, instead of a blank white window
 * (user-asked: the app should feel finished, not like a web page). The details go to
 * app.log; "Try again" re-draws the screen, "Reload the app" starts the UI afresh. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(error)
    reportClientError(error, window.location.pathname + window.location.hash, info.componentStack ?? '')
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div className="flex h-screen items-center justify-center bg-page p-6">
        <div className="card max-w-md p-8 text-center">
          <TriangleAlert size={28} className="mx-auto text-warning" />
          <h1 className="mt-4 text-lg font-semibold text-ink">Something went wrong</h1>
          <p className="mt-2 text-sm text-ink-2">
            This screen ran into a problem and couldn't carry on. Nothing you saved has been lost. The details were
            written to the app's log.
          </p>
          <div className="mt-6 flex justify-center gap-2">
            <button className="btn-ghost" onClick={() => this.setState({ failed: false })}>
              Try again
            </button>
            <button className="btn-primary" onClick={() => window.location.reload()}>
              <RotateCcw size={14} /> Reload the app
            </button>
          </div>
        </div>
      </div>
    )
  }
}

function editable(target: EventTarget | null): boolean {
  const el = target instanceof Element ? target : null
  return !!el?.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]')
}

/** What makes a page feel like a web page, switched off for the whole app:
 * - the browser's right-click menu (Back, Refresh, Save as, Inspect) - kept in text boxes,
 *   where it's cut/copy/paste, and over selected text, where it's copy;
 * - a file dropped anywhere but a drop zone, which opened the file in place of the app.
 * The window's own browser keys (reload, print, zoom) are switched off in the launcher. */
export function installAppFeel(): void {
  window.addEventListener('contextmenu', (event) => {
    if (editable(event.target)) return
    if (window.getSelection()?.toString()) return
    event.preventDefault()
  })
  // drop zones handle their own drop before it reaches the window; anything else is ignored
  window.addEventListener('dragover', (event) => {
    if (!event.defaultPrevented) {
      event.preventDefault()
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'none'
    }
  })
  window.addEventListener('drop', (event) => event.preventDefault())
  window.addEventListener('error', (event) => reportClientError(event.error ?? event.message, 'window'))
  window.addEventListener('unhandledrejection', (event) => {
    // an AppError is a request that failed and was shown where it happened; the rest are bugs
    if (!(event.reason instanceof Error && event.reason.name === 'AppError')) {
      reportClientError(event.reason, 'promise')
    }
  })
}
