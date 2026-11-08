import { useEffect, useLayoutEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

/** Classark's dialog: a navy scrim, a 20px card, the title and a close button. */
export function Modal({
  title,
  onClose,
  children,
  width = 'max-w-lg',
}: {
  title: string
  onClose: () => void
  children: ReactNode
  width?: string
}) {
  const card = useRef<HTMLDivElement>(null)
  // Callers pass `onClose` inline, so it's a new function on every render. The effect below
  // must not depend on it: re-running it moved focus to the card (and back to whatever had
  // it before the dialog opened) on every re-render - and the live poll re-renders the app
  // every few seconds, which kept pulling the cursor out of a field mid-word.
  const closeRef = useRef(onClose)
  useLayoutEffect(() => {
    closeRef.current = onClose
  })

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    // leave focus where an autoFocus field inside the dialog already put it
    if (!card.current?.contains(document.activeElement)) card.current?.focus()
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && closeRef.current()
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      previous?.focus()
    }
  }, [])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-[var(--surface-overlay)] p-4 backdrop-blur-[4px]"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        ref={card}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`card w-full ${width} max-h-[85vh] overflow-hidden shadow-modal outline-none`}
      >
        <div className="flex items-center justify-between border-b border-line px-6 py-4">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button onClick={onClose} className="btn-ghost -mr-2" aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <div className="scroll-quiet max-h-[calc(85vh-4rem)] overflow-auto px-6 py-5">{children}</div>
      </div>
    </div>
  )
}
