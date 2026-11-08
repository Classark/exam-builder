import { useEffect, useState } from 'react'
import { api, type PreviewRequest } from './api'

// Toggling several parts in a row shouldn't re-render the paper once per click.
const DEBOUNCE_MS = 250

/** Rendered pages of `request` as the export would produce them; null renders nothing. */
function usePreviewPages(request: PreviewRequest | null) {
  const [pages, setPages] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const key = request ? JSON.stringify(request) : ''

  useEffect(() => {
    if (!key) {
      setPages([])
      setLoading(false)
      return
    }
    // a slower, older response must never overwrite a newer one
    let stale = false
    setLoading(true)
    const timer = setTimeout(() => {
      api
        .previewQp(JSON.parse(key) as PreviewRequest)
        .then((result) => {
          if (stale) return
          setPages(result.pages)
          setError('')
        })
        .catch((e: unknown) => !stale && setError(e instanceof Error ? e.message : String(e)))
        .finally(() => !stale && setLoading(false))
    }, DEBOUNCE_MS)
    return () => {
      stale = true
      clearTimeout(timer)
    }
  }, [key])

  return { pages, loading, error }
}

export function PagePreview({ request, empty }: { request: PreviewRequest | null; empty: string }) {
  const { pages, loading, error } = usePreviewPages(request)

  if (!request) return empty ? <div className="p-4 text-center text-sm text-muted">{empty}</div> : null
  return (
    <div className="relative space-y-4">
      {loading && (
        <div className="sticky top-0 z-10 mx-auto flex w-fit items-center gap-2 rounded-full bg-inverse px-3 py-1 text-xs text-on-inverse shadow-float">
          <span className="size-2 animate-pulse rounded-full bg-accent" />
          Updating preview…
        </div>
      )}
      {error && <div className="rounded-lg bg-danger-wash px-3 py-2 text-xs text-danger">{error}</div>}
      {pages.length === 0 && loading && <div className="aspect-[1/1.414] animate-pulse rounded-lg bg-card" />}
      {pages.map((src, index) => (
        <img
          key={index}
          src={src}
          alt={`Page ${index + 1}`}
          className={`w-full rounded-lg bg-paper shadow-float transition-opacity ${loading ? 'opacity-60' : ''}`}
        />
      ))}
    </div>
  )
}
