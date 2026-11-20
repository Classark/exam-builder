import { createContext, useContext, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

/** What's happening right now - a scan's progress, tagging, Claude reviewing papers - goes
 * in a strip under the top bar rather than in it (user-reported: wide progress text pushed
 * the top bar's buttons under the window's close buttons). The top bar stays the same width
 * whatever is running; the strip shows only while something is in it. */

const ActivityContext = createContext<{ slot: HTMLElement | null; setSlot: (el: HTMLElement | null) => void } | null>(
  null,
)

export function ActivityProvider({ children }: { children: ReactNode }) {
  const [slot, setSlot] = useState<HTMLElement | null>(null)
  return <ActivityContext.Provider value={{ slot, setSlot }}>{children}</ActivityContext.Provider>
}

/** The strip itself, placed under the header. Hidden while empty. */
export function ActivityBar() {
  const ctx = useContext(ActivityContext)
  return (
    <div
      ref={ctx?.setSlot}
      role="status"
      aria-live="polite"
      className="no-print relative z-20 flex shrink-0 flex-wrap items-center gap-2 border-t border-white/10 bg-inverse px-4 py-1.5 text-on-inverse empty:hidden lg:px-6"
    />
  )
}

/** Renders its children into the activity strip. */
export function Activity({ children }: { children: ReactNode }) {
  const ctx = useContext(ActivityContext)
  return ctx?.slot ? createPortal(children, ctx.slot) : null
}
