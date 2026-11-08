import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Minus, Plus } from 'lucide-react'
import type { BBox } from './types'

// Pages are rendered at 150 DPI (main.py RENDER_DPI); regions are in PDF points (1/72 in).
const POINTS_TO_PIXELS = 150 / 72
const ZOOMS = [0.6, 0.75, 0.9, 1, 1.25, 1.5, 2]

type Edge = 'top' | 'bottom' | 'left' | 'right'

export interface PaneEditing {
  /** Replace the regions: after an edge drag, or a region drawn afresh. */
  onChange: (segments: BBox[]) => void
  mode: 'adjust' | 'draw' | 'split'
  onSplit: (page: number, y: number) => void
}

/** One document's pages, showing only the pages the highlighted regions sit on, scrolled to
 * the first of them. With `editing`, the regions' edges can be dragged, a new region drawn
 * over a page, or a split point picked. */
export function PagePane({
  title,
  icon,
  pageUrl,
  segments,
  editing,
  empty,
  aside,
  zoomKey,
  allPages,
}: {
  title: string
  icon: ReactNode
  pageUrl: (page: number) => string
  segments: BBox[]
  editing?: PaneEditing
  empty: ReactNode
  aside?: ReactNode
  zoomKey: string
  /** Show every page of the document (this many), not just the highlighted ones - for
   * finding an answer anywhere in a mark scheme. */
  allPages?: number
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const [zoom, setZoom] = useState(() => {
    try {
      return Number(localStorage.getItem(zoomKey)) || 1
    } catch {
      return 1
    }
  })
  const setZoomSaved = (next: number) => {
    setZoom(next)
    try {
      localStorage.setItem(zoomKey, String(next))
    } catch {
      // a convenience only
    }
  }
  const step = (by: number) => {
    const index = ZOOMS.findIndex((z) => z >= zoom)
    setZoomSaved(ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, (index < 0 ? ZOOMS.length - 1 : index) + by))])
  }

  const pages = allPages
    ? Array.from({ length: allPages }, (_, i) => i)
    : [...new Set(segments.map((s) => s.page))].sort((a, b) => a - b)
  const target = JSON.stringify(segments)

  // Bring the first highlight into view whenever the selection changes - as soon as it's
  // drawn, which for an already-loaded page is straight away, and otherwise on image load.
  const [loads, setLoads] = useState(0)
  const scrolledFor = useRef('')
  useEffect(() => {
    if (scrolledFor.current === target) return
    const box = scroller.current?.querySelector<HTMLElement>('[data-first-region]')
    const pane = scroller.current
    if (!box || !pane) return
    scrolledFor.current = target
    // on-screen distances are scaled while the app is pinch-magnified; scrollTop isn't
    const paneRect = pane.getBoundingClientRect()
    const magnified = paneRect.height / pane.offsetHeight || 1
    const top = (box.getBoundingClientRect().top - paneRect.top) / magnified + pane.scrollTop
    pane.scrollTo({ top: Math.max(0, top - 40), behavior: 'smooth' })
  }, [loads, target])

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-line px-4 py-2.5">
        <span className="text-accent">{icon}</span>
        <h2 className="text-sm font-semibold">{title}</h2>
        {pages.length > 0 && (
          <span className="text-xs text-muted">
            {pages.length === 1 ? `page ${pages[0] + 1}` : `pages ${pages.map((p) => p + 1).join(', ')}`}
          </span>
        )}
        <div className="ml-auto flex items-center gap-0.5">
          <button className="btn-ghost px-1.5" onClick={() => step(-1)} aria-label={`Zoom out of the ${title.toLowerCase()}`}>
            <Minus size={15} />
          </button>
          <button
            className="w-12 rounded-md text-center text-xs text-muted hover:text-accent"
            onClick={() => setZoomSaved(1)}
            title="Fit to width"
          >
            {Math.round(zoom * 100)}%
          </button>
          <button className="btn-ghost px-1.5" onClick={() => step(1)} aria-label={`Zoom in to the ${title.toLowerCase()}`}>
            <Plus size={15} />
          </button>
        </div>
      </div>
      {aside}
      <div ref={scroller} className="scroll-quiet min-h-0 flex-1 overflow-auto bg-sunken p-4">
        {pages.length === 0 ? (
          <div className="flex h-full items-center justify-center p-6 text-center text-sm text-muted">{empty}</div>
        ) : (
          <div className="mx-auto space-y-4" style={{ width: `${zoom * 100}%` }}>
            {pages.map((page, pageIndex) => (
              <Page
                key={page}
                url={pageUrl(page)}
                page={page}
                all={segments}
                firstPage={pageIndex === 0}
                editing={editing}
                onLoaded={() => setLoads((n) => n + 1)}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function Page({
  url,
  page,
  all,
  firstPage,
  editing,
  onLoaded,
}: {
  url: string
  page: number
  all: BBox[]
  firstPage: boolean
  editing?: PaneEditing
  onLoaded: () => void
}) {
  const img = useRef<HTMLImageElement>(null)
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null)
  // edits in progress stay local until the mouse is released - one save per drag
  const [draft, setDraft] = useState<BBox[] | null>(null)
  const [drag, setDrag] = useState<{ index: number; edge: Edge } | null>(null)
  const [drawFrom, setDrawFrom] = useState<{ x: number; y: number } | null>(null)

  const segments = draft ?? all
  const onThisPage = segments.map((s, index) => ({ s, index })).filter(({ s }) => s.page === page)

  const toPoint = (clientX: number, clientY: number) => {
    const el = img.current
    if (!el || !el.naturalWidth) return null
    const rect = el.getBoundingClientRect()
    return {
      x: (((clientX - rect.left) / rect.width) * el.naturalWidth) / POINTS_TO_PIXELS,
      y: (((clientY - rect.top) / rect.height) * el.naturalHeight) / POINTS_TO_PIXELS,
    }
  }

  const pct = (s: BBox) =>
    natural && {
      left: `${((s.x0 * POINTS_TO_PIXELS) / natural.w) * 100}%`,
      top: `${((s.y0 * POINTS_TO_PIXELS) / natural.h) * 100}%`,
      width: `${(((s.x1 - s.x0) * POINTS_TO_PIXELS) / natural.w) * 100}%`,
      height: `${(((s.y1 - s.y0) * POINTS_TO_PIXELS) / natural.h) * 100}%`,
    }

  const onMove = (e: React.MouseEvent) => {
    const point = toPoint(e.clientX, e.clientY)
    if (!point) return
    if (drag) {
      const next = [...segments]
      const s = { ...next[drag.index] }
      if (drag.edge === 'top') s.y0 = Math.min(point.y, s.y1 - 4)
      if (drag.edge === 'bottom') s.y1 = Math.max(point.y, s.y0 + 4)
      if (drag.edge === 'left') s.x0 = Math.min(point.x, s.x1 - 4)
      if (drag.edge === 'right') s.x1 = Math.max(point.x, s.x0 + 4)
      next[drag.index] = s
      setDraft(next)
    } else if (drawFrom) {
      const drawn: BBox = {
        page,
        x0: Math.min(drawFrom.x, point.x),
        y0: Math.min(drawFrom.y, point.y),
        x1: Math.max(drawFrom.x, point.x),
        y1: Math.max(drawFrom.y, point.y),
      }
      // a region drawn on a page replaces that page's regions; other pages' stay
      setDraft([...all.filter((s) => s.page !== page), drawn].sort((a, b) => a.page - b.page || a.y0 - b.y0))
    }
  }

  const finish = () => {
    if ((drag || drawFrom) && draft && editing) {
      const drawnTooSmall = drawFrom && draft.some((s) => s.page === page && (s.x1 - s.x0 < 8 || s.y1 - s.y0 < 8))
      if (!drawnTooSmall) editing.onChange(draft)
    }
    setDrag(null)
    setDrawFrom(null)
    setDraft(null)
  }

  const onDown = (e: React.MouseEvent) => {
    if (!editing || e.button !== 0) return
    const point = toPoint(e.clientX, e.clientY)
    if (!point) return
    if (editing.mode === 'split') {
      editing.onSplit(page, point.y)
    } else if (editing.mode === 'draw') {
      e.preventDefault()
      setDrawFrom(point)
    }
  }

  const cursor = editing?.mode === 'split' ? 'cursor-row-resize' : editing?.mode === 'draw' ? 'cursor-crosshair' : ''

  return (
    <div
      className={`relative select-none overflow-hidden rounded-lg bg-paper shadow-float ${cursor}`}
      onMouseMove={onMove}
      onMouseUp={finish}
      onMouseLeave={finish}
      onMouseDown={onDown}
    >
      <img
        ref={img}
        src={url}
        alt={`Page ${page + 1}`}
        draggable={false}
        className="block w-full"
        onLoad={(e) => {
          setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
          onLoaded()
        }}
        onError={onLoaded}
      />
      {!natural && <div className="aspect-[1/1.414] animate-pulse bg-sunken" />}
      {natural &&
        onThisPage.map(({ s, index }, i) => (
          <div
            key={index}
            data-first-region={firstPage && i === 0 ? '' : undefined}
            className="region-highlight pointer-events-none absolute"
            style={pct(s) ?? undefined}
          >
            {editing?.mode === 'adjust' &&
              (['top', 'bottom', 'left', 'right'] as Edge[]).map((edge) => (
                <div
                  key={edge}
                  onMouseDown={(e) => {
                    e.stopPropagation()
                    e.preventDefault()
                    setDrag({ index, edge })
                  }}
                  title="Drag to adjust"
                  className={`pointer-events-auto absolute ${
                    edge === 'top' || edge === 'bottom'
                      ? 'inset-x-0 h-3 cursor-ns-resize'
                      : 'inset-y-0 w-3 cursor-ew-resize'
                  } ${edge === 'top' ? '-top-1.5' : edge === 'bottom' ? '-bottom-1.5' : edge === 'left' ? '-left-1.5' : '-right-1.5'}`}
                >
                  {(edge === 'top' || edge === 'bottom') && (
                    <span className="absolute left-1/2 top-1/2 h-1.5 w-8 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent" />
                  )}
                </div>
              ))}
          </div>
        ))}
    </div>
  )
}
