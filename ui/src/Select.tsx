import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown, Search } from 'lucide-react'

export interface SelectOption {
  value: string
  label: string
  // options sharing a group are listed under its heading, in the order given
  group?: string
  // a subtopic under the option before it
  indent?: boolean
  // smaller text after the label
  hint?: string
}

const GAP = 4
const MAX_HEIGHT = 320

/** A floating panel anchored to `anchor`, drawn on top of everything (a portal, fixed
 * position) so no card or dialog with overflow hidden can cut it off - below the anchor,
 * or above it when there's more room there. Closes on a click outside or Escape. */
export function Popover({
  anchor,
  open,
  onClose,
  minWidth = 0,
  align = 'left',
  children,
}: {
  anchor: RefObject<HTMLElement | null>
  open: boolean
  onClose: () => void
  minWidth?: number
  align?: 'left' | 'right'
  children: (maxHeight: number) => ReactNode
}) {
  const panel = useRef<HTMLDivElement>(null)
  const [place, setPlace] = useState<{ left: number; top?: number; bottom?: number; width: number; maxHeight: number } | null>(null)

  useLayoutEffect(() => {
    if (!open) {
      setPlace(null)
      return
    }
    const measure = () => {
      const box = anchor.current?.getBoundingClientRect()
      if (!box) return
      const below = window.innerHeight - box.bottom - GAP - 8
      const above = box.top - GAP - 8
      const up = below < 220 && above > below
      const width = Math.max(box.width, minWidth)
      const left = Math.max(8, Math.min(align === 'right' ? box.right - width : box.left, window.innerWidth - width - 8))
      setPlace({
        left,
        width,
        maxHeight: Math.min(MAX_HEIGHT, up ? above : below),
        ...(up ? { bottom: window.innerHeight - box.top + GAP } : { top: box.bottom + GAP }),
      })
    }
    measure()
    // the anchor moves with whatever scrolls it; the panel follows
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', measure, true)
    return () => {
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', measure, true)
    }
  }, [open, anchor, minWidth, align])

  useEffect(() => {
    if (!open) return
    const outside = (e: MouseEvent) => {
      const target = e.target as Node
      if (!panel.current?.contains(target) && !anchor.current?.contains(target)) onClose()
    }
    window.addEventListener('mousedown', outside)
    return () => window.removeEventListener('mousedown', outside)
  }, [open, anchor, onClose])

  if (!open || !place) return null
  return createPortal(
    <div
      ref={panel}
      className="card fixed z-[60] flex flex-col overflow-hidden p-1.5 shadow-float [app-region:no-drag]"
      style={{ left: place.left, top: place.top, bottom: place.bottom, width: place.width, maxHeight: place.maxHeight }}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation() // closes the dropdown, not the dialog it's in
          onClose()
          anchor.current?.focus()
        }
      }}
    >
      {children(place.maxHeight)}
    </div>,
    document.body,
  )
}

/** The app's dropdown: a field-styled button and a panel of options in Classark's look,
 * in place of the browser's native select. Arrow keys, Enter and Escape work as in one;
 * `searchable` adds a filter box for long lists. */
export function Select({
  value,
  options,
  onChange,
  disabled = false,
  placeholder = 'Choose…',
  searchable = false,
  className = '',
  size = 'md',
  minWidth = 0,
  'aria-label': ariaLabel,
  title,
}: {
  value: string
  options: SelectOption[]
  onChange: (value: string) => void
  disabled?: boolean
  placeholder?: string
  searchable?: boolean
  className?: string
  size?: 'sm' | 'md'
  minWidth?: number
  'aria-label'?: string
  title?: string
}) {
  const button = useRef<HTMLButtonElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(-1)

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    return q ? options.filter((o) => `${o.label} ${o.group ?? ''} ${o.hint ?? ''}`.toLowerCase().includes(q)) : options
  }, [options, query])
  const selected = options.find((o) => o.value === value)

  const show = () => {
    if (disabled) return
    setQuery('')
    setActive(Math.max(0, options.findIndex((o) => o.value === value)))
    setOpen(true)
  }
  const close = () => setOpen(false)
  const choose = (option: SelectOption | undefined) => {
    if (!option) return
    onChange(option.value)
    close()
    button.current?.focus()
  }

  // the highlighted option stays in view - scrolling the list only: scrollIntoView would
  // also scroll whatever holds the button (the Settings dialog jumped)
  useEffect(() => {
    const box = list.current
    const item = box?.querySelector<HTMLElement>(`[data-index="${active}"]`)
    if (!open || !box || !item) return
    const top = item.offsetTop - box.offsetTop
    if (top < box.scrollTop) box.scrollTop = top
    else if (top + item.offsetHeight > box.scrollTop + box.clientHeight) box.scrollTop = top + item.offsetHeight - box.clientHeight
  }, [open, active])

  const onKey = (e: KeyboardEvent) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) {
        e.preventDefault()
        show()
      }
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation() // closes the dropdown, not the dialog it's in
      close()
      button.current?.focus()
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const step = e.key === 'ArrowDown' ? 1 : -1
      setActive((i) => Math.max(0, Math.min(shown.length - 1, i + step)))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      choose(shown[active])
    } else if (e.key === 'Tab') {
      close()
    }
  }

  const pad = size === 'sm' ? 'px-2 py-1.5 text-xs' : 'px-3 py-2 text-sm'
  let lastGroup: string | undefined
  return (
    <>
      <button
        ref={button}
        type="button"
        disabled={disabled}
        onClick={() => (open ? close() : show())}
        onKeyDown={onKey}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        title={title}
        className={`field flex items-center justify-between gap-2 text-left disabled:cursor-not-allowed disabled:opacity-60 ${pad} ${
          open ? 'border-accent bg-card' : ''
        } ${className}`}
      >
        <span className={`min-w-0 truncate ${selected ? '' : 'text-faint'}`}>{selected ? selected.label : placeholder}</span>
        <ChevronDown size={size === 'sm' ? 13 : 15} className={`shrink-0 text-muted transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      <Popover anchor={button} open={open} onClose={close} minWidth={minWidth}>
        {() => (
          <>
            {searchable && (
              <div className="relative mb-1.5 shrink-0">
                <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
                <input
                  autoFocus
                  value={query}
                  onChange={(e) => {
                    setQuery(e.target.value)
                    setActive(0)
                  }}
                  onKeyDown={onKey}
                  placeholder="Search…"
                  className="field py-1.5 pl-8 text-xs"
                  aria-label="Search the options"
                />
              </div>
            )}
            <div ref={list} role="listbox" aria-label={ariaLabel} className="scroll-quiet min-h-0 overflow-auto">
              {shown.length === 0 && <div className="px-2.5 py-2 text-xs text-muted">Nothing matches.</div>}
              {shown.map((option, index) => {
                const heading = option.group !== undefined && option.group !== lastGroup ? option.group : null
                lastGroup = option.group
                const isSelected = option.value === value
                return (
                  <div key={`${option.group ?? ''}|${option.value}`}>
                    {heading && (
                      <div className="px-2.5 pb-1 pt-2 text-[0.68rem] font-semibold uppercase tracking-wide text-muted">
                        {heading}
                      </div>
                    )}
                    <button
                      type="button"
                      role="option"
                      aria-selected={isSelected}
                      data-index={index}
                      onMouseEnter={() => setActive(index)}
                      onClick={() => choose(option)}
                      className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left ${size === 'sm' ? 'text-xs' : 'text-sm'} ${
                        index === active ? 'bg-hover' : ''
                      } ${isSelected ? 'font-semibold text-accent' : 'text-ink'} ${option.indent ? 'pl-6' : ''}`}
                    >
                      <span className="min-w-0 flex-1 truncate">
                        {option.label}
                        {option.hint && <span className="ml-1.5 font-normal text-muted">{option.hint}</span>}
                      </span>
                      {isSelected && <Check size={14} className="shrink-0" />}
                    </button>
                  </div>
                )
              })}
            </div>
          </>
        )}
      </Popover>
    </>
  )
}
