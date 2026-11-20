import { useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  ArrowLeftRight,
  Ban,
  CheckCheck,
  CircleCheck,
  Crop,
  GitMerge,
  Link2,
  PenLine,
  Scissors,
  Trash2,
  Unlink,
} from 'lucide-react'
import { pathLabel, snippet } from '../issues'
import { NumberCell } from '../ReviewMarks'
import type { MarkSchemeEntry, QuestionPart } from '../types'
import { codes, likelyPart, partName, rowLabel, type PartRow, type QuestionView, type UnmatchedRow } from './model'

/** What the check column can ask the screen to do - every edit is undoable from its toast. */
export interface CheckActions {
  select: (partId: string) => void
  setMarks: (part: QuestionPart, marks: number) => void
  setTotal: (number: number, total: number) => void
  /** the board withdrew this question ("Question removed") and the parser missed it */
  withdraw: (number: number) => void
  link: (part: QuestionPart, entry: MarkSchemeEntry) => void
  unlink: (part: QuestionPart, entry: MarkSchemeEntry) => void
  deleteEntry: (entry: MarkSchemeEntry) => void
  deletePart: (part: QuestionPart) => void
  rename: (part: QuestionPart, path: string) => void
  merge: (part: QuestionPart) => void
  /** redraw the selected part's box, split it, draw its scheme answer, draw a missing
   * question - or back to adjusting */
  tool: (tool: 'adjust' | 'draw' | 'split' | 'link-draw' | 'add-question') => void
  /** split a part where a scheme row's own part starts inside it: the lower half takes the
   * row's label and marks and is linked to it, all one fix */
  splitFor: (part: QuestionPart, entry: MarkSchemeEntry) => void
  /** mark these issues checked: "looks right", "the paper's own mistake" */
  check: (keys: string[]) => void
  looksRight: () => void
}

export function CheckColumn({
  view,
  selectedId,
  actions,
  emptyPages,
}: {
  view: QuestionView
  selectedId: string | null
  actions: CheckActions
  /** pages (1-based) empty in the question paper PDF - the likely reason a question is missing */
  emptyPages: number[]
}) {
  const q = view.question
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="scroll-quiet min-h-0 flex-1 space-y-3 overflow-auto p-3">
        {q ? <QuestionHeader view={view} actions={actions} /> : <MissingQuestion view={view} actions={actions} emptyPages={emptyPages} />}

        {q && (
          <section aria-label="Parts and their mark scheme answers">
            <div className="mb-1 grid grid-cols-[4.5rem_3rem_1rem_1fr] items-center gap-2 px-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
              <span>Part</span>
              <span>Marks</span>
              <span />
              <span>Mark scheme</span>
            </div>
            <ul className="space-y-1">
              {view.parts.map((row) => (
                <PartLine
                  key={row.part.id}
                  row={row}
                  number={view.number}
                  selected={row.part.id === selectedId}
                  isLast={row === view.parts[view.parts.length - 1]}
                  actions={actions}
                />
              ))}
            </ul>
          </section>
        )}

        {view.unmatched.length > 0 && <Unmatched view={view} actions={actions} />}
      </div>

      <footer className="border-t border-line p-3">
        {view.open > 0 ? (
          <button
            className="btn-primary w-full justify-center"
            onClick={actions.looksRight}
            title="Everything here is right now (or the paper itself is like this): mark it checked and go to the next question to check"
          >
            <CheckCheck size={16} /> Q{view.number} looks right
            <kbd className="key ml-1 border-white/40 bg-white/15 text-inherit">Enter</kbd>
          </button>
        ) : (
          <p className="flex items-center justify-center gap-2 text-sm text-success">
            <CircleCheck size={16} /> Nothing to check on Q{view.number}
          </p>
        )}
      </footer>
    </div>
  )
}

function QuestionHeader({ view, actions }: { view: QuestionView; actions: CheckActions }) {
  const q = view.question!
  const sum = q.parts.reduce((s, p) => s + p.marks, 0)
  const sumIssue = view.questionIssues.find((i) => i.check === 'part_sum_matches_total' && !i.checked)
  return (
    <header className="space-y-2">
      <div className="flex items-center gap-2">
        <h2 className="text-lg font-semibold">Question {view.number}</h2>
        <button
          className="btn-ghost px-2 py-1 text-xs"
          onClick={() => actions.withdraw(view.number)}
          title="The board withdrew this question (its mark scheme says “Question removed”): take it out of the paper and the bank"
        >
          <Ban size={13} /> Removed by the board
        </button>
        <span className="ml-auto flex items-center gap-1.5 text-xs text-muted">
          printed total
          <NumberCell value={q.total_marks} label={`Q${view.number} total`} onSave={async (n) => actions.setTotal(view.number, n)} />
        </span>
      </div>
      {sum === q.total_marks ? (
        <p className="text-xs text-muted">The parts add up to {sum}, the printed total.</p>
      ) : (
        <Problem>
          The parts add up to <b>{sum}</b>, but the paper prints <b>[Total: {q.total_marks}]</b>. Compare each part’s marks
          with the mark scheme column below - or a part may be missing, doubled or wrongly split.
          <Actions>
            <button className="btn-secondary px-2.5 py-1 text-xs" onClick={() => actions.setTotal(view.number, sum)}>
              Set the total to {sum}
            </button>
            {sumIssue && (
              <button className="btn-ghost px-2 py-1 text-xs" onClick={() => actions.check([sumIssue.key])}>
                The paper’s own mistake
              </button>
            )}
          </Actions>
        </Problem>
      )}
    </header>
  )
}

function PartLine({
  row,
  number,
  selected,
  isLast,
  actions,
}: {
  row: PartRow
  number: number
  selected: boolean
  isLast: boolean
  actions: CheckActions
}) {
  const { part, entries, schemeMarks } = row
  const ref = useRef<HTMLLIElement>(null)
  useEffect(() => {
    if (selected) ref.current?.scrollIntoView({ block: 'nearest' })
  }, [selected])
  const open = row.issues.filter((i) => !i.checked)
  const noMark = open.some((i) => i.check === 'marks_unknown')
  const noAnswer = open.some((i) => i.check === 'part_has_mark_scheme_entry')
  const differs = schemeMarks !== null && schemeMarks !== part.marks
  const fine = !open.length && !differs
  const label = part.path ? pathLabel(part.path).trim() : 'whole'

  return (
    <li
      ref={ref}
      className={`rounded-xl border transition-colors ${
        selected ? 'border-accent bg-accent-container' : open.length ? 'border-warning/40 bg-warning-wash' : 'border-line hover:border-accent-border'
      }`}
    >
      <div
        className="grid cursor-pointer grid-cols-[4.5rem_3rem_1rem_1fr] items-center gap-2 px-2 py-1.5"
        onClick={() => actions.select(part.id)}
      >
        <span className="text-sm font-semibold">{label}</span>
        <span onClick={(e) => e.stopPropagation()}>
          <NumberCell
            value={part.marks}
            warn={noMark || differs}
            label={`marks for ${partName(number, part.path)}`}
            onSave={async (n) => actions.setMarks(part, n)}
          />
        </span>
        <ArrowLeftRight size={13} className="text-faint" />
        <span className="flex min-w-0 items-center gap-1.5 text-xs">
          {part.removed ? (
            <span className="text-muted">withdrawn by the board</span>
          ) : entries.length === 0 ? (
            <span className="font-medium text-warning">not linked</span>
          ) : (
            <span className="min-w-0 truncate text-ink-2" title={entries.map((e) => e.answer_text).join('\n\n')}>
              {entries.map(rowLabel).join(', ')} · {schemeMarks} {schemeMarks === 1 ? 'mark' : 'marks'}
              {entries.flatMap(codes).length > 0 && <span className="text-muted"> ({entries.flatMap(codes).join(' ')})</span>}
            </span>
          )}
          {fine ? (
            <CircleCheck size={14} className="ml-auto shrink-0 text-success" />
          ) : (
            <AlertTriangle size={14} className="ml-auto shrink-0 text-warning" />
          )}
        </span>
      </div>

      {(noMark || noAnswer || differs) && (
        <div className="space-y-1.5 px-2 pb-2">
          {noMark && <Problem>No mark was found on the paper for this part. Type it in its marks box.</Problem>}
          {noAnswer && (
            <Problem>
              Not linked to its mark scheme answer. {selected ? 'Click' : 'Select this part, then click'} its answer on the
              mark scheme.
              <Actions>
                <button
                  className="btn-ghost px-2 py-1 text-xs"
                  onClick={() => {
                    actions.select(part.id)
                    actions.tool('link-draw')
                  }}
                >
                  <Crop size={13} /> Not boxed there? Draw it
                </button>
              </Actions>
            </Problem>
          )}
          {differs && !noMark && (
            <Problem>
              The mark scheme gives {schemeMarks}, the paper {part.marks}.
              <Actions>
                <button className="btn-secondary px-2.5 py-1 text-xs" onClick={() => actions.setMarks(part, schemeMarks!)}>
                  Use {schemeMarks}
                </button>
              </Actions>
            </Problem>
          )}
        </div>
      )}

      {selected && <PartTools part={part} entries={entries} isLast={isLast} actions={actions} />}
    </li>
  )
}

/** The selected part's own tools, named for the problem each one fixes. */
function PartTools({
  part,
  entries,
  isLast,
  actions,
}: {
  part: QuestionPart
  entries: MarkSchemeEntry[]
  isLast: boolean
  actions: CheckActions
}) {
  const [renaming, setRenaming] = useState(false)
  const [name, setName] = useState(pathLabel(part.path).trim())
  const save = () => {
    const path = [...name.matchAll(/[a-z]+|[ivx]+/gi)].map((m) => m[0].toLowerCase()).join('.')
    setRenaming(false)
    if (path !== part.path) actions.rename(part, path)
  }
  return (
    <div className="space-y-2 border-t border-accent-border px-2 py-2">
      {entries.length > 0 && (
        <ul className="space-y-1">
          {entries.map((e) => (
            <li key={e.id} className="flex items-center gap-2 text-xs text-ink-2">
              <Link2 size={12} className="shrink-0 text-muted" />
              <span className="min-w-0 flex-1 truncate">
                <b className="text-ink">{rowLabel(e)}</b> {snippet(e.answer_text) || 'no text'}
              </span>
              <button
                className="btn-ghost shrink-0 px-1.5 py-0.5 text-xs"
                onClick={() => actions.unlink(part, e)}
                title="This isn't this part's answer"
              >
                <Unlink size={12} /> Unlink
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap gap-1">
        <Tool icon={Crop} keyName="d" onClick={() => actions.tool('draw')} hint="The box cuts something off or takes in too much">
          Redraw box
        </Tool>
        <Tool icon={Scissors} keyName="s" onClick={() => actions.tool('split')} hint="Two parts were read as one">
          Split
        </Tool>
        {!isLast && (
          <Tool icon={GitMerge} keyName="m" onClick={() => actions.merge(part)} hint="This part and the next are really one">
            Merge with next
          </Tool>
        )}
        <Tool icon={PenLine} onClick={() => setRenaming(true)} hint="The part's label was read wrong">
          Rename
        </Tool>
        <Tool icon={Trash2} onClick={() => actions.deletePart(part)} hint="Something read as a part that isn't one">
          Not a part
        </Tool>
        <Tool icon={Link2} onClick={() => actions.tool('link-draw')} hint="Its answer isn't boxed on the mark scheme">
          Draw its answer
        </Tool>
      </div>
      {renaming && (
        <div className="flex items-center gap-2 text-xs">
          <span className="text-muted">Label</span>
          <input
            autoFocus
            className="field w-28 px-2 py-1 text-xs"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') save()
              if (e.key === 'Escape') setRenaming(false)
            }}
            placeholder="(b)(ii)"
          />
          <button className="btn-primary px-2.5 py-1 text-xs" onClick={save}>
            Save
          </button>
        </div>
      )}
    </div>
  )
}

function Tool({
  icon: Icon,
  keyName,
  onClick,
  hint,
  children,
}: {
  icon: typeof Crop
  keyName?: string
  onClick: () => void
  hint: string
  children: React.ReactNode
}) {
  return (
    <button className="btn-secondary px-2 py-1 text-xs" onClick={onClick} title={keyName ? `${hint} (${keyName})` : hint}>
      <Icon size={13} /> {children}
    </button>
  )
}

/** Scheme rows nobody claimed: link each to its part, or say it isn't a row. */
function Unmatched({ view, actions }: { view: QuestionView; actions: CheckActions }) {
  return (
    <section className="rounded-xl border border-warning/40 p-2.5">
      <h3 className="text-sm font-semibold">Mark scheme rows not linked to a part</h3>
      <p className="mb-2 text-xs text-muted">
        Every answer in the mark scheme should belong to a part{view.question ? '. Pick its part and link it' : ''}. If it
        isn’t a real answer (a heading, a note), say so.
      </p>
      <ul className="space-y-2">
        {view.unmatched.map((u) => (
          <UnmatchedLine key={u.entry.id} row={u} view={view} actions={actions} />
        ))}
      </ul>
    </section>
  )
}

function UnmatchedLine({ row, view, actions }: { row: UnmatchedRow; view: QuestionView; actions: CheckActions }) {
  const { entry, issue } = row
  const [target, setTarget] = useState(() => likelyPart(view, entry)?.id ?? row.near[0]?.id ?? view.parts[0]?.part.id ?? '')
  const near = row.near
  const labelOf = (p: QuestionPart) => pathLabel(p.path).trim() || `Q${view.number}`
  const ownLabel = pathLabel(entry.part_path).trim()
  const part = view.parts.find((p) => p.part.id === target)?.part
  return (
    <li className="rounded-lg bg-card px-2 py-1.5 text-xs">
      <div className="flex items-baseline gap-2">
        <b className="text-sm">{rowLabel(entry)}</b>
        <span className="text-muted">
          {entry.marks} {entry.marks === 1 ? 'mark' : 'marks'}
        </span>
        <span className="min-w-0 flex-1 truncate text-ink-2" title={entry.answer_text}>
          {snippet(entry.answer_text) || 'no text'}
        </span>
      </div>
      {near.length > 0 && (
        <p className="mt-1.5 text-ink">
          The paper has no part {ownLabel}, so it was probably read as part of{' '}
          {near.map((p, i) => (
            <span key={p.id}>
              {i > 0 && ' or '}
              <b>{labelOf(p)}</b>
            </span>
          ))}
          . Look at {near.length > 1 ? 'their boxes' : 'its box'} on the question paper, and split the one holding{' '}
          {ownLabel} where it starts.
        </p>
      )}
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        {near.map((p) => (
          <button
            key={p.id}
            className="btn-secondary px-2.5 py-1 text-xs"
            onClick={() => actions.splitFor(p, entry)}
            title={`Click on the question paper where ${ownLabel} starts inside ${labelOf(p)}`}
          >
            <Scissors size={13} /> Split {labelOf(p)}
          </button>
        ))}
        {view.parts.length > 0 && (
          <>
            <select className="field w-auto px-2 py-1 text-xs" value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Part to link to">
              {view.parts.map((p) => (
                <option key={p.part.id} value={p.part.id}>
                  {p.part.path ? pathLabel(p.part.path).trim() : `Q${view.number}`}
                </option>
              ))}
            </select>
            <button
              className={`${near.length ? 'btn-secondary' : 'btn-primary'} px-2.5 py-1 text-xs`}
              disabled={!part}
              onClick={() => part && actions.link(part, entry)}
            >
              <Link2 size={13} /> Link
            </button>
          </>
        )}
        <button className="btn-ghost px-2 py-1 text-xs" onClick={() => actions.deleteEntry(entry)} title="A heading or note read as an answer row">
          Not a row
        </button>
        {issue && !issue.checked && (
          <button className="btn-ghost px-2 py-1 text-xs" onClick={() => actions.check([issue.key])} title="The mark scheme itself is like this - leave it">
            Leave it
          </button>
        )}
      </div>
    </li>
  )
}

function MissingQuestion({ view, actions, emptyPages }: { view: QuestionView; actions: CheckActions; emptyPages: number[] }) {
  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold">Question {view.number}</h2>
      <Problem>
        The mark scheme has answers for Question {view.number}, but the app didn’t find Question {view.number} on the
        question paper. The whole question paper is shown on the left.
      </Problem>
      <div className="space-y-1 text-xs text-ink-2">
        <p className="font-medium text-ink">Usually it’s one of these:</p>
        {emptyPages.length > 0 && (
          <p>
            • <b>The PDF is damaged:</b> page{emptyPages.length > 1 ? 's' : ''} {emptyPages.join(', ')}{' '}
            {emptyPages.length > 1 ? 'are' : 'is'} empty, so the question isn’t in the file. Download the question paper
            again, replace it in the papers folder and press ↻ (top right).
          </p>
        )}
        <p>
          • <b>Its number wasn’t read</b>, so it was taken as the end of Question {view.number - 1}. Look at the end of Q
          {view.number - 1}: if it’s there, draw it below.
        </p>
        <p>
          • <b>The mark scheme’s label is wrong</b> and these rows belong to another question - check them below.
        </p>
      </div>
      <button className="btn-secondary w-full justify-center" onClick={() => actions.tool('add-question')}>
        <Crop size={15} /> Draw Question {view.number} on the question paper
      </button>
    </section>
  )
}

function Problem({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg bg-warning-wash px-2.5 py-1.5 text-xs leading-relaxed text-ink">
      <AlertTriangle size={12} className="mr-1 inline -translate-y-px text-warning" />
      {children}
    </div>
  )
}

function Actions({ children }: { children: React.ReactNode }) {
  return <div className="mt-1.5 flex flex-wrap gap-1.5">{children}</div>
}
