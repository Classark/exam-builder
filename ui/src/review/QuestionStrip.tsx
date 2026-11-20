import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, FileWarning, PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import type { PaperModel } from './model'

export type View = number | 'paper'

const COLLAPSED_KEY = 'qbx.review.questionsCollapsed'

function storedCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === 'yes'
  } catch {
    return false
  }
}

/** The paper's questions down the left, like a table of contents: "Paper", then one row per
 * question with its marks and ✓ when nothing's open, ⚠ n when something is, "missing" when
 * the scheme answers a question the paper as read hasn't got. A column, not a strip across
 * the top: a 40-question paper's tabs wrapped into rows that took the pages' height.
 * Collapsed, it's a rail of bare numbers - amber where there's something to check - and
 * stays collapsed next time. "To check only" hides the clean ones. */
export function QuestionList({
  model,
  view,
  onView,
}: {
  model: PaperModel
  view: View
  onView: (v: View) => void
}) {
  const list = useRef<HTMLDivElement>(null)
  const [onlyToCheck, setOnlyToCheck] = useState(false)
  const [collapsed, setCollapsed] = useState(storedCollapsed)
  const toggle = () => {
    setCollapsed((was) => {
      try {
        localStorage.setItem(COLLAPSED_KEY, was ? 'no' : 'yes')
      } catch {
        // a convenience only
      }
      return !was
    })
  }

  const toCheck = model.questions.filter((q) => q.question === null || q.open > 0)
  const shown = onlyToCheck
    ? model.questions.filter((q) => q.question === null || q.open > 0 || q.number === view)
    : model.questions

  // keep the open question in sight ([ and ] move through them off-screen too)
  useEffect(() => {
    list.current?.querySelector('[aria-current]')?.scrollIntoView({ block: 'nearest' })
  }, [view, onlyToCheck, collapsed])

  // nothing left to check: the switch has nothing to hide
  useEffect(() => {
    if (toCheck.length === 0) setOnlyToCheck(false)
  }, [toCheck.length])

  const paperTitle = 'The paper as a whole: its cover total, question numbering and file'
  return (
    <nav
      className={`card flex shrink-0 flex-col overflow-hidden transition-[width] ${collapsed ? 'w-[52px]' : 'w-[136px]'}`}
      aria-label="Questions"
    >
      <div className={`flex items-center border-b border-line p-1.5 ${collapsed ? 'flex-col gap-1' : 'gap-1'}`}>
        {collapsed ? (
          <Rail
            active={view === 'paper'}
            onClick={() => onView('paper')}
            tone={model.paperOpen ? 'problem' : 'ok'}
            title={paperTitle}
            label="P"
          />
        ) : (
          <Row
            active={view === 'paper'}
            onClick={() => onView('paper')}
            tone={model.paperOpen ? 'problem' : 'ok'}
            title={paperTitle}
            label="Paper"
            status={model.paperOpen > 0 ? <Count n={model.paperOpen} /> : <Check size={13} strokeWidth={2.5} />}
          />
        )}
        <button
          onClick={toggle}
          className={`btn-ghost shrink-0 px-1.5 ${collapsed ? 'order-first' : ''}`}
          title={collapsed ? 'Show the question list' : 'Narrow the question list'}
          aria-label={collapsed ? 'Show the question list' : 'Narrow the question list'}
          aria-expanded={!collapsed}
        >
          {collapsed ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}
        </button>
      </div>

      <div ref={list} className="scroll-quiet min-h-0 flex-1 space-y-1 overflow-y-auto p-1.5">
        {shown.map((q) => {
          const missing = q.question === null
          const tone = missing ? 'missing' : q.open ? 'problem' : 'ok'
          const title = missing
            ? `The mark scheme answers Question ${q.number}, but it wasn't found on the question paper`
            : q.open
              ? `Question ${q.number}: ${q.open} thing${q.open === 1 ? '' : 's'} to check`
              : `Question ${q.number}: nothing to check`
          return collapsed ? (
            <Rail
              key={q.number}
              active={view === q.number}
              onClick={() => onView(q.number)}
              tone={tone}
              title={title}
              label={String(q.number)}
            />
          ) : (
            <Row
              key={q.number}
              active={view === q.number}
              onClick={() => onView(q.number)}
              tone={tone}
              title={title}
              label={`Q${q.number}`}
              marks={q.question ? q.question.total_marks : undefined}
              status={
                missing ? (
                  <FileWarning size={13} />
                ) : q.open > 0 ? (
                  <Count n={q.open} />
                ) : (
                  <Check size={13} strokeWidth={2.5} />
                )
              }
            />
          )
        })}
        {shown.length === 0 && !collapsed && <p className="px-1.5 py-2 text-xs text-muted">Nothing left to check.</p>}
      </div>

      {toCheck.length > 0 && (
        <label
          className={`flex cursor-pointer items-center border-t border-line text-xs text-ink-2 ${
            collapsed ? 'justify-center py-2' : 'gap-1.5 px-2.5 py-2'
          }`}
          title={`Hide the questions with nothing to check (${toCheck.length} to check)`}
        >
          <input type="checkbox" checked={onlyToCheck} onChange={(e) => setOnlyToCheck(e.target.checked)} />
          {!collapsed && <span>To check only ({toCheck.length})</span>}
        </label>
      )}
    </nav>
  )
}

function Count({ n }: { n: number }) {
  return (
    <span className="flex items-center gap-0.5 text-xs">
      <AlertTriangle size={12} /> {n}
    </span>
  )
}

type Tone = 'ok' | 'problem' | 'missing'

function toneClass(active: boolean, tone: Tone): string {
  return active
    ? 'border-accent bg-accent-container text-ink'
    : tone === 'ok'
      ? 'border-transparent text-ink-2 hover:border-accent-border hover:bg-card'
      : tone === 'missing'
        ? 'border-dashed border-warning bg-warning-wash text-ink hover:border-warning'
        : 'border-warning/40 bg-warning-wash text-ink hover:border-warning'
}

function Row({
  active,
  tone,
  onClick,
  title,
  label,
  marks,
  status,
}: {
  active: boolean
  tone: Tone
  onClick: () => void
  title: string
  label: string
  marks?: number
  status: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-current={active || undefined}
      className={`flex h-8 w-full min-w-0 items-center gap-1.5 rounded-xl border px-2.5 text-sm font-medium transition-colors ${toneClass(active, tone)}`}
    >
      <span>{label}</span>
      {marks !== undefined && <span className="text-xs font-normal text-muted">{marks}</span>}
      <span className={`ml-auto ${tone === 'ok' ? 'text-success' : 'text-warning'}`}>{status}</span>
    </button>
  )
}

/** a collapsed row: the bare number, its colour saying whether there's something to check */
function Rail({ active, tone, onClick, title, label }: { active: boolean; tone: Tone; onClick: () => void; title: string; label: string }) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-current={active || undefined}
      className={`flex h-8 w-full items-center justify-center rounded-xl border text-sm font-medium transition-colors ${toneClass(active, tone)}`}
    >
      {label}
    </button>
  )
}
