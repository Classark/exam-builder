import { useMemo, useRef, useState, type ReactNode } from 'react'
import { Check, ChevronDown, Plus, X } from 'lucide-react'
import { Popover, Select } from './Select'
import { useLive } from './live'

/** One subject code the exam draws on ("" in the first row lets the request say), and the
 * papers ticked within it - none ticked: any of its papers. */
interface SourceRow {
  subject: string
  papers: string[]
}

interface SubjectPapers {
  subject: string
  name: string
  papers: [string, string][] // [paper number, title the covers print]
}

export interface PickedSources {
  board: string
  sources: { subject: string; papers: string[] }[]
}

const byNumber = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true })

/** The exam board, subject codes and papers a new exam may draw on - picked before asking,
 * and they win over anything the request says. Returns what's picked and the controls.
 * `initial`: picks to start from (Classification mode remembers the last ones);
 * `requireSubject`: no "Any subject" choice - a subject must be picked. */
export function useSourcePicker(
  disabled: boolean,
  { initial, requireSubject = false }: { initial?: PickedSources; requireSubject?: boolean } = {},
): PickedSources & { picker: ReactNode } {
  const { papers: library } = useLive()
  const [rows, setRows] = useState<SourceRow[]>(() =>
    initial?.sources.length ? initial.sources.map((s) => ({ subject: s.subject, papers: [...s.papers] })) : [{ subject: '', papers: [] }],
  )
  const [pickedBoard, setPickedBoard] = useState(initial?.board ?? '')

  // An exam draws on one exam board's papers only, so the board is chosen first and the
  // subjects below are that board's.
  const boards = useMemo(() => [...new Set(library.map((p) => p.board || 'Cambridge'))].sort(), [library])
  const board = boards.includes(pickedBoard) ? pickedBoard : (boards[0] ?? '')

  // each subject's paper numbers, with the title the covers print ("Paper 4 Theory (Extended)")
  const subjects = useMemo<SubjectPapers[]>(() => {
    const bySubject = new Map<string, { name: string; papers: Map<string, string> }>()
    for (const p of library) {
      if ((p.board || 'Cambridge') !== board) continue
      const row = bySubject.get(p.subject) ?? { name: p.subject_name, papers: new Map() }
      if (!row.papers.has(p.paper) || (!row.papers.get(p.paper) && p.paper_title)) row.papers.set(p.paper, p.paper_title)
      bySubject.set(p.subject, row)
    }
    return [...bySubject.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([subject, row]) => ({
        subject,
        name: row.name,
        papers: [...row.papers.entries()].sort(([a], [b]) => byNumber(a, b)),
      }))
  }, [library, board])

  // with a single code in the bank there's nothing to choose between
  const only = subjects.length === 1 ? subjects[0].subject : ''
  const effective = rows.map((row, i) => (i === 0 && !row.subject && only ? { ...row, subject: only } : row))
  const taken = new Set(effective.map((r) => r.subject).filter(Boolean))
  const canAdd = !disabled && effective.every((r) => r.subject) && subjects.some((s) => !taken.has(s.subject))

  const update = (index: number, next: Partial<SourceRow>) =>
    setRows((current) => current.map((row, i) => (i === index ? { ...effective[i], ...next } : row)))

  const picker = (
    <div className="space-y-1.5">
      {boards.length > 1 && (
        <Select
          value={board}
          disabled={disabled}
          size="sm"
          className="w-full"
          onChange={(v) => {
            setPickedBoard(v)
            setRows([{ subject: '', papers: [] }]) // another board's subjects
          }}
          aria-label="Exam board"
          title="An exam uses one exam board's papers"
          options={boards.map((b) => ({ value: b, label: `${b} papers` }))}
        />
      )}
      {effective.map((row, index) => {
        const choices = subjects.filter((s) => s.subject === row.subject || !taken.has(s.subject))
        const papers = subjects.find((s) => s.subject === row.subject)?.papers ?? []
        return (
          <div key={index} className="flex items-center gap-1.5">
            <Select
              value={row.subject}
              disabled={disabled}
              size="sm"
              minWidth={260}
              className="min-w-0 flex-1"
              placeholder="Choose a subject…"
              // paper numbers belong to one code, so a new subject starts with none ticked
              onChange={(v) => update(index, { subject: v, papers: [] })}
              aria-label={index === 0 ? 'Subject' : `Subject ${index + 1}`}
              options={[
                ...(index === 0 && effective.length === 1 && !only && !requireSubject
                  ? [{ value: '', label: 'Any subject - as you say' }]
                  : []),
                ...choices.map((s) => ({ value: s.subject, label: s.subject, hint: s.name })),
              ]}
            />
            <PapersDropdown
              papers={papers}
              picked={row.papers}
              disabled={disabled || !row.subject}
              onChange={(next) => update(index, { papers: next })}
            />
            {effective.length > 1 && (
              <button
                type="button"
                className="btn-ghost shrink-0 px-1"
                disabled={disabled}
                onClick={() => setRows(effective.filter((_, i) => i !== index))}
                aria-label="Remove this subject"
                title="Remove this subject"
              >
                <X size={14} />
              </button>
            )}
          </div>
        )
      })}
      {subjects.length > 1 && (
        <button
          type="button"
          className="btn-ghost px-1.5 py-1 text-xs"
          disabled={!canAdd}
          onClick={() => setRows([...effective, { subject: '', papers: [] }])}
          title={canAdd ? 'Draw on another subject code too' : 'Choose a subject in each row first'}
        >
          <Plus size={13} /> Add subject
        </button>
      )}
    </div>
  )

  return {
    board,
    // only the board's subjects in the bank: a remembered pick may since have gone
    sources: effective
      .filter((r) => r.subject && subjects.some((s) => s.subject === r.subject))
      .map((r) => ({ subject: r.subject, papers: r.papers })),
    picker,
  }
}

/** A drop-down checklist of one subject's papers. Nothing ticked means any paper. */
function PapersDropdown({
  papers,
  picked,
  disabled,
  onChange,
}: {
  papers: [string, string][]
  picked: string[]
  disabled: boolean
  onChange: (papers: string[]) => void
}) {
  const [open, setOpen] = useState(false)
  const button = useRef<HTMLButtonElement>(null)

  const summary = picked.length === 0 ? 'Any paper' : `Paper ${picked.slice().sort(byNumber).join(', ')}`
  const toggle = (paper: string) =>
    onChange(picked.includes(paper) ? picked.filter((p) => p !== paper) : [...picked, paper])

  return (
    <>
      <button
        ref={button}
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={`field flex min-w-0 flex-1 items-center justify-between gap-1 px-2 py-1.5 text-left text-xs disabled:cursor-not-allowed disabled:opacity-60 ${
          open ? 'border-accent bg-card' : ''
        }`}
      >
        <span className="truncate">{summary}</span>
        <ChevronDown size={13} className={`shrink-0 text-muted transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      <Popover anchor={button} open={open} onClose={() => setOpen(false)} minWidth={280} align="right">
        {() => (
          <div role="listbox" aria-multiselectable="true" className="scroll-quiet min-h-0 overflow-auto">
            <button
              type="button"
              className={`flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-xs hover:bg-hover ${
                picked.length === 0 ? 'font-semibold text-accent' : 'text-ink-2'
              }`}
              onClick={() => onChange([])}
            >
              Any paper
              {picked.length === 0 && <Check size={14} />}
            </button>
            {papers.map(([paper, title]) => (
              <label key={paper} className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs hover:bg-hover">
                <input
                  type="checkbox"
                  checked={picked.includes(paper)}
                  onChange={() => toggle(paper)}
                  className="size-3.5 shrink-0 accent-[var(--accent)]"
                />
                <span className="truncate">{title || `Paper ${paper}`}</span>
              </label>
            ))}
          </div>
        )}
      </Popover>
    </>
  )
}
