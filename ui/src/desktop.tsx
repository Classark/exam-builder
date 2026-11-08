import { useEffect, useState } from 'react'
import { Copy, Minus, Square, X } from 'lucide-react'

// What the desktop app's window offers the UI (packaging/launcher.py's Desktop, through
// pywebview). Absent in a browser tab, so everything using it is optional.
export interface DesktopApi {
  open_in_browser: () => Promise<void>
  window_controls: () => Promise<boolean>
  minimize: () => Promise<void>
  toggle_maximize: () => Promise<void>
  is_maximized: () => Promise<boolean>
  close: () => Promise<void>
}

declare global {
  interface Window {
    pywebview?: { api: DesktopApi }
  }
}

export function useDesktop(): DesktopApi | null {
  const [api, setApi] = useState<DesktopApi | null>(() => window.pywebview?.api ?? null)
  useEffect(() => {
    if (api) return
    // pywebview injects its api a moment after the page loads
    const ready = () => setApi(window.pywebview?.api ?? null)
    window.addEventListener('pywebviewready', ready)
    return () => window.removeEventListener('pywebviewready', ready)
  }, [api])
  return api
}

// sized and rounded like the bar's other icon buttons, but borderless, as window buttons are
const controlButton =
  'flex h-[38px] w-[38px] items-center justify-center rounded-xl text-on-inverse/80 transition-colors hover:text-on-inverse'

// Minimise, maximise and close, at the top bar's right edge, when the bar is the window's
// title bar (the desktop app on Windows; a Mac keeps its own traffic lights).
export function WindowControls({ desktop }: { desktop: DesktopApi }) {
  const [shown, setShown] = useState(false)
  const [maximized, setMaximized] = useState(false)
  useEffect(() => {
    void desktop.window_controls().then(setShown)
    const sync = () => void desktop.is_maximized().then(setMaximized)
    sync()
    window.addEventListener('resize', sync)
    return () => window.removeEventListener('resize', sync)
  }, [desktop])
  if (!shown) return null
  return (
    <div className="-mr-1 flex shrink-0 items-center gap-1 border-l border-white/15 pl-3">
      <button className={`${controlButton} hover:bg-white/10`} onClick={() => void desktop.minimize()} aria-label="Minimise" title="Minimise">
        <Minus size={16} strokeWidth={1.5} />
      </button>
      <button
        className={`${controlButton} hover:bg-white/10`}
        onClick={() => void desktop.toggle_maximize()}
        aria-label={maximized ? 'Restore' : 'Maximise'}
        title={maximized ? 'Restore down' : 'Maximise'}
      >
        {maximized ? <Copy size={14} strokeWidth={1.5} className="-scale-x-100" /> : <Square size={13} strokeWidth={1.5} />}
      </button>
      <button className={`${controlButton} hover:bg-[#c42b1c]`} onClick={() => void desktop.close()} aria-label="Close" title="Close">
        <X size={18} strokeWidth={1.5} />
      </button>
    </div>
  )
}
