import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Search } from 'lucide-react'
import { comparePapers, paperCode, sitting } from './bankFolders'
import type { PaperSummary } from './types'

type Filter = 'open' | 'all' | 'done'

export function paperStatus(p: PaperSummary): { label: string; tone: 'success' | 'accent' | 'muted' } {
  if (p.n_issues === 0) return { label: 'Clean', tone: 'success' }
  if (p.n_open === 0) return { label: 'Checked', tone: 'success' }
  return { label: `${p.n_open} to review`, tone: 'accent' }
}

export function StatusPill({ paper }: { paper: PaperSummary }) {
  const { label, tone } = paperStatus(paper)
  const cls =
    tone === 'success' ? 'bg-success-wash text-success' : tone === 'accent' ? 'bg-accent-container text-accent' : 'bg-sunken text-muted'
  return (
    <span className={`pill ${cls}`}>
      {tone === 'success' && <Check size={12} strokeWidth={2.5} />}
      {label}
    </span>
  )
}

/** The paper being reviewed, as a button opening a searchable list of every paper. */
export function PaperPicker({
  papers,
  current,
  onPick,
}: {
  papers: PaperSummary[]
  current: PaperSummary | null
  onPick: (key: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('open')
  const root = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    input.current?.focus()
    const close = (e: MouseEvent) => !root.current?.contains(e.target as Node) && setOpen(false)
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    window.addEventListener('mousedown', close)
    window.addEventListener('keydown', esc)
    return () => {
      window.removeEventListener('mousedown', close)
      window.removeEventListener('keydown', esc)
    }
  }, [open])

  const counts = useMemo(
    () => ({
      open: papers.filter((p) => p.n_open > 0).length,
      done: papers.filter((p) => p.n_open === 0).length,
      all: papers.length,
    }),
    [papers],
  )

  const shown = useMemo(() => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean)
    return papers
      .filter((p) => (filter === 'open' ? p.n_open > 0 : filter === 'done' ? p.n_open === 0 : true))
      .filter((p) => {
        const hay = `${p.paper_key} ${paperCode(p)} ${sitting(p)} ${p.subject_name} ${p.paper_title}`.toLowerCase()
        return words.every((w) => hay.includes(w))
      })
      .sort(comparePapers)
  }, [papers, filter, query])

  const chips: [Filter, string][] = [
    ['open', 'To review'],
    ['done', 'Done'],
    ['all', 'All'],
  ]

  return (
    <div ref={root} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 rounded-2xl border-[1.5px] border-line bg-card px-4 py-3 text-left transition-colors hover:border-accent"
      >
        <span className="min-w-0 flex-1">
          {current ? (
            <>
              <span className="block truncate text-[1.05rem] font-semibold">
                {/* the name, when the paper gives one - a bare code would print twice ("0417 0417/11") */}
                {current.subject_name && current.subject_name !== current.subject ? `${current.subject_name} ` : ''}
                {paperCode(current)}
              </span>
              <span className="block truncate text-xs text-muted">
                {sitting(current)}
                {current.paper_title ? ` · ${current.paper_title}` : ''}
              </span>
            </>
          ) : (
            <span className="text-sm text-muted">Choose a paper</span>
          )}
        </span>
        <ChevronDown size={18} className={`shrink-0 text-muted transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="card absolute inset-x-0 top-full z-40 mt-2 flex max-h-[70vh] flex-col overflow-hidden rounded-2xl shadow-float">
          <div className="space-y-2.5 border-b border-line p-3">
            <div className="relative">
              <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
              <input
                ref={input}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Find a paper: 0620 41 2024…"
                className="field pl-9"
              />
            </div>
            <div className="flex gap-1.5" role="tablist" aria-label="Show papers">
              {chips.map(([value, label]) => (
                <button
                  key={value}
                  role="tab"
                  aria-selected={filter === value}
                  onClick={() => setFilter(value)}
                  className={`pill px-3 py-1 text-xs transition-colors ${
                    filter === value ? 'bg-accent text-white' : 'bg-sunken text-ink-2 hover:bg-accent-wash hover:text-accent'
                  }`}
                >
                  {label} <span className="opacity-75">{counts[value]}</span>
                </button>
              ))}
            </div>
          </div>
          <ul className="scroll-quiet min-h-0 flex-1 overflow-auto p-1.5">
            {shown.map((p) => (
              <li key={p.paper_key}>
                <button
                  onClick={() => {
                    onPick(p.paper_key)
                    setOpen(false)
                  }}
                  className={`flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left transition-colors hover:bg-accent-wash ${
                    p.paper_key === current?.paper_key ? 'bg-accent-wash-strong' : ''
                  }`}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">
                      {paperCode(p)} <span className="font-normal text-ink-2">{sitting(p)}</span>
                    </span>
                    <span className="block truncate text-xs text-muted">{p.paper_title || p.subject_name}</span>
                  </span>
                  <StatusPill paper={p} />
                </button>
              </li>
            ))}
            {shown.length === 0 && (
              <li className="px-3 py-6 text-center text-sm text-muted">
                {filter === 'open' && !query ? 'Every paper is reviewed.' : 'No papers match.'}
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  )
}
