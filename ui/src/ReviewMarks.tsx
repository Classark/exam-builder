import { useEffect, useState } from 'react'
import { Check, Loader2, RotateCcw, Sparkles, X } from 'lucide-react'
import { describeIssue, pathLabel } from './issues'
import type { Fix, FixStep, MarkSchemeEntry, Paper, Question, QuestionPart, Report } from './types'

function entriesFor(paper: Paper, part: QuestionPart): MarkSchemeEntry[] {
  const ids = new Set(paper.joins.filter((j) => j.part_id === part.id).map((j) => j.mark_scheme_entry_id))
  return paper.mark_scheme.filter((e) => ids.has(e.id))
}

/** The marking codes at the start of a row's lines (B1, M1, A1, P1, SC1), as Pearson and
 * Cambridge print them - each worth its digit. */
function codes(entry: MarkSchemeEntry): string[] {
  const text = `${entry.guidance_text ?? ""} ${entry.answer_text}`
  // in order, until they make up the row's mark: the notes go on to name alternatives
  // ("B2 ... B1 for cm or mm"), which aren't more marks
  const out: string[] = []
  let sum = 0
  for (const m of text.matchAll(/\b(?:SC|[MABP])(\d)\b/g)) {
    if (sum >= entry.marks) break
    out.push(m[0])
    sum += Number(m[1])
  }
  return out
}

/** Every question's marks at a glance: the printed total against its parts, and each part's
 * mark beside what the mark scheme gives it. Any number can be corrected in place. */
export function MarksView({
  paper,
  selectedPartId,
  onSelect,
  onSetMarks,
  onSetTotal,
}: {
  paper: Paper
  selectedPartId: string | null
  onSelect: (partId: string) => void
  onSetMarks: (part: QuestionPart, marks: number) => Promise<void>
  onSetTotal: (question: Question, total: number) => Promise<void>
}) {
  const [onlyProblems, setOnlyProblems] = useState(false)
  const questions = paper.questions.filter((q) => {
    if (!onlyProblems) return true
    const sum = q.parts.reduce((s, p) => s + p.marks, 0)
    return sum !== q.total_marks || q.parts.some((p) => p.marks === 0 || msDiffers(paper, p))
  })
  const sumAll = paper.questions.reduce((s, q) => s + q.total_marks, 0)
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2 text-xs">
        <span className={`flex-1 ${paper.cover_total_marks != null && sumAll !== paper.cover_total_marks ? 'text-warning' : 'text-muted'}`}>
          Questions add up to {sumAll}
          {paper.cover_total_marks != null && ` of the cover's ${paper.cover_total_marks}`}
        </span>
        <label className="flex items-center gap-1.5 text-muted">
          <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} />
          Only ones that disagree
        </label>
      </div>
      <div className="scroll-quiet min-h-0 flex-1 overflow-auto p-2">
        {questions.length === 0 && <p className="px-3 py-8 text-center text-sm text-muted">Every mark agrees.</p>}
        {questions.map((q) => (
          <QuestionMarks
            key={q.question_number}
            paper={paper}
            question={q}
            selectedPartId={selectedPartId}
            onSelect={onSelect}
            onSetMarks={onSetMarks}
            onSetTotal={onSetTotal}
          />
        ))}
      </div>
    </div>
  )
}

function msDiffers(paper: Paper, part: QuestionPart): boolean {
  const entries = entriesFor(paper, part)
  return entries.length > 0 && entries.reduce((s, e) => s + e.marks, 0) !== part.marks
}

function QuestionMarks({
  paper,
  question,
  selectedPartId,
  onSelect,
  onSetMarks,
  onSetTotal,
}: {
  paper: Paper
  question: Question
  selectedPartId: string | null
  onSelect: (partId: string) => void
  onSetMarks: (part: QuestionPart, marks: number) => Promise<void>
  onSetTotal: (question: Question, total: number) => Promise<void>
}) {
  const sum = question.parts.reduce((s, p) => s + p.marks, 0)
  const agrees = sum === question.total_marks
  return (
    <section className="mb-2 rounded-xl border border-line">
      <header className={`flex items-center gap-2 rounded-t-xl px-3 py-1.5 text-sm ${agrees ? 'bg-hover' : 'bg-accent-wash'}`}>
        <span className="font-semibold">Q{question.question_number}</span>
        <span className={`flex-1 text-xs ${agrees ? 'text-muted' : 'text-warning'}`}>
          parts add up to {sum}
        </span>
        <span className="text-xs text-muted">total</span>
        <NumberCell value={question.total_marks} label={`Q${question.question_number} total`} onSave={(n) => onSetTotal(question, n)} />
      </header>
      <ul>
        {question.parts.map((part) => {
          const entries = entriesFor(paper, part)
          const ms = entries.reduce((s, e) => s + e.marks, 0)
          const rowCodes = entries.flatMap(codes)
          const differs = entries.length > 0 && ms !== part.marks
          return (
            <li
              key={part.id}
              className={`flex items-center gap-2 border-t border-line px-3 py-1 text-sm ${
                part.id === selectedPartId ? 'bg-accent-container' : 'hover:bg-accent-wash'
              }`}
            >
              <button className="w-16 shrink-0 text-left font-medium" onClick={() => onSelect(part.id)}>
                {pathLabel(part.path) || '(whole)'}
              </button>
              <button className="min-w-0 flex-1 truncate text-left text-xs text-muted" onClick={() => onSelect(part.id)}>
                {entries.length === 0 ? (
                  'no mark scheme row'
                ) : (
                  <>
                    scheme {ms}
                    {rowCodes.length > 0 && <span className="ml-1">({rowCodes.join(' ')})</span>}
                  </>
                )}
                {part.marks_printed != null && part.marks_printed !== part.marks && (
                  <span className="ml-1">· paper prints {part.marks_printed || 'none'}</span>
                )}
              </button>
              {differs && (
                <button
                  className="btn-ghost px-1.5 py-0.5 text-xs text-accent"
                  title={`Use the mark scheme's ${ms}`}
                  onClick={() => void onSetMarks(part, ms)}
                >
                  use {ms}
                </button>
              )}
              <NumberCell
                value={part.marks}
                warn={differs || part.marks === 0}
                label={`marks for ${question.question_number}${pathLabel(part.path)}`}
                onSave={(n) => onSetMarks(part, n)}
              />
            </li>
          )
        })}
      </ul>
    </section>
  )
}

function NumberCell({
  value,
  warn,
  label,
  onSave,
}: {
  value: number
  warn?: boolean
  label: string
  onSave: (n: number) => Promise<void>
}) {
  const [text, setText] = useState(String(value))
  useEffect(() => setText(String(value)), [value])
  const commit = () => {
    const n = Number(text)
    if (text.trim() === '' || !Number.isInteger(n) || n < 0) return setText(String(value))
    if (n !== value) void onSave(n)
  }
  return (
    <input
      aria-label={label}
      inputMode="numeric"
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
        if (e.key === 'Escape') setText(String(value))
      }}
      className={`field w-12 shrink-0 px-1.5 py-0.5 text-center text-sm ${warn ? 'border-warning text-warning' : ''}`}
    />
  )
}

// --- Claude's fixes -------------------------------------------------------------------

function stepText(step: FixStep): string {
  const part = (id?: string | null) => (id ? id.replace('.', ' ') : '?')
  switch (step.op) {
    case 'set_marks':
      return `Set ${part(step.part_id)} to ${step.value} marks`
    case 'set_total':
      return `Set Q${step.question_number}'s total to ${step.value}`
    case 'link':
      return `Link ${part(step.part_id)} to row ${step.entry_id}`
    case 'unlink':
      return `Unlink ${part(step.part_id)} from row ${step.entry_id}`
    case 'relabel_entry':
      return `Read row ${step.entry_id} as ${step.path}`
    case 'merge_parts':
      return `Join ${(step.part_ids ?? []).map(part).join(', ')}${step.path ? ` as ${step.path}` : ''}`
    case 'edit_part':
      return `Edit ${part(step.part_id)} (${Object.keys(step.fields ?? {}).join(', ')})`
    case 'split_part':
      return `Split ${part(step.part_id)}${step.path ? ` - the lower half as ${step.path}` : ''}`
    case 'add_part':
      return `Add part ${step.path ?? '?'} to Q${step.question_number}${step.value ? ` (${step.value} marks)` : ''}`
    case 'delete_part':
      return `Delete ${part(step.part_id)}`
    case 'set_entry_marks':
      return `Set row ${step.entry_id} to ${step.value} marks`
    case 'delete_entry':
      return `Delete row ${step.entry_id}`
    case 'set_cover_total':
      return `Set the cover total to ${step.value}`
    case 'renumber_question':
      return `Renumber Q${step.question_number} as Q${step.value}`
    case 'delete_question':
      return `Delete Q${step.question_number}`
    case 'link_region': {
      const at = step.at ?? {}
      return `Link ${part(step.part_id)} to the mark scheme, page ${(at.page ?? 0) + 1} (y ${Math.round(at.y0 ?? 0)}–${Math.round(at.y1 ?? 0)})`
    }
    case 'dismiss':
      return 'The paper itself is inconsistent - mark checked'
    case 'needs_person':
      return 'Needs you'
    default:
      return step.op
  }
}

function issueTitle(report: Report | null, key: string): string {
  const issue = report?.issues.find((i) => i.key === key)
  if (!issue) return key.split('|').slice(1).filter((s) => s && s !== 'None').join(' ')
  const where = [issue.question_number != null ? `Q${issue.question_number}` : '', pathLabel(issue.part_path ?? '')].join('')
  return `${describeIssue(issue).title} ${where}`.trim()
}

/** What Claude (and the teacher) changed on this paper: suggestions to accept or reject,
 * notes on what needs a person, and every applied fix with its undo. */
export function FixesView({
  fixes,
  report,
  busy,
  onAction,
  onFixAll,
  focusedId,
  onFocus,
}: {
  fixes: Fix[]
  report: Report | null
  busy: boolean
  onAction: (fix: Fix, action: 'accept' | 'reject' | 'undo') => Promise<void>
  onFixAll: () => void
  // the fix whose question the panes show, and choosing another
  focusedId: string | null
  onFocus: (fix: Fix) => void
}) {
  const waiting = fixes.filter((f) => f.status === 'suggested')
  const applied = fixes.filter((f) => f.status === 'applied')
  const openIssues = report?.issues.filter((i) => !i.checked).length ?? 0
  return (
    <div className="scroll-quiet min-h-0 flex-1 overflow-auto p-2">
      <div className="mb-2 flex items-center gap-2 rounded-xl bg-hover px-3 py-2 text-xs text-ink-2">
        <Sparkles size={14} className="shrink-0 text-accent" />
        <span className="flex-1">
          {busy ? 'Claude is reviewing this paper…' : openIssues ? `${openIssues} open ${openIssues === 1 ? 'issue' : 'issues'}` : 'No open issues'}
        </span>
        <button className="btn-primary px-3 py-1 text-xs" disabled={busy || openIssues === 0} onClick={onFixAll}>
          {busy ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />} Fix with Claude
        </button>
      </div>
      {fixes.length === 0 && !busy && (
        <p className="px-3 py-6 text-center text-sm text-muted">
          No fixes yet. Claude proposes them; the checks decide which apply on their own (Auto mode, in Settings).
        </p>
      )}
      {waiting.length > 0 && <Heading>Waiting for you</Heading>}
      {waiting.map((f) => (
        <FixCard key={f.id} fix={f} report={report} onAction={onAction} focused={f.id === focusedId} onFocus={onFocus} />
      ))}
      {applied.length > 0 && <Heading>Applied</Heading>}
      {applied.map((f) => (
        <FixCard key={f.id} fix={f} report={report} onAction={onAction} focused={f.id === focusedId} onFocus={onFocus} />
      ))}
    </div>
  )
}

function Heading({ children }: { children: React.ReactNode }) {
  return <h3 className="px-2 pb-1 pt-3 text-xs font-semibold uppercase tracking-wide text-muted">{children}</h3>
}

function FixCard({
  fix,
  report,
  onAction,
  focused,
  onFocus,
}: {
  fix: Fix
  report: Report | null
  onAction: (fix: Fix, action: 'accept' | 'reject' | 'undo') => Promise<void>
  focused: boolean
  onFocus: (fix: Fix) => void
}) {
  const note = fix.steps.every((s) => s.op === 'dismiss' || s.op === 'needs_person')
  const needsPerson = fix.steps.some((s) => s.op === 'needs_person')
  return (
    <div
      className={`mb-1.5 cursor-pointer rounded-xl border px-3 py-2 text-sm ${focused ? 'border-accent bg-hover' : 'border-line hover:bg-hover'}`}
      onClick={() => onFocus(fix)}
      title="Show this fix's question on the papers"
    >
      <div className="mb-1 flex items-center gap-2 text-xs text-muted">
        <span className={fix.source === 'ai' ? 'text-accent' : ''}>{fix.source === 'ai' ? 'Claude' : 'You'}</span>
        {fix.issues.slice(0, 3).map((k) => (
          <span key={k} className="truncate">· {issueTitle(report, k)}</span>
        ))}
      </div>
      <ul className="space-y-0.5">
        {fix.steps.map((s, i) => (
          <li key={i} className="font-medium">{stepText(s)}</li>
        ))}
      </ul>
      {fix.reason && <p className="mt-1 text-xs text-ink-2">{fix.reason}</p>}
      {fix.status === 'suggested' && !note && (
        <p className={`mt-1 text-xs ${fix.raises.length ? 'text-warning' : 'text-muted'}`}>
          Tried: clears {fix.clears.length} {fix.clears.length === 1 ? 'issue' : 'issues'}
          {fix.raises.length > 0 && `, raises ${fix.raises.length}`}
        </p>
      )}
      <div className="mt-2 flex justify-end gap-2" onClick={(e) => e.stopPropagation()}>
        {fix.status === 'suggested' && !needsPerson && (
          <button className="btn-primary px-3 py-1 text-xs" onClick={() => void onAction(fix, 'accept')}>
            <Check size={13} /> {note ? 'Mark checked' : 'Apply'}
          </button>
        )}
        {fix.status === 'suggested' && (
          <button className="btn-ghost px-2 py-1 text-xs" onClick={() => void onAction(fix, 'reject')}>
            <X size={13} /> {needsPerson ? 'Dismiss' : 'Reject'}
          </button>
        )}
        {fix.status === 'applied' && (
          <button className="btn-ghost px-2 py-1 text-xs" onClick={() => void onAction(fix, 'undo')}>
            <RotateCcw size={13} /> Undo
          </button>
        )}
      </div>
    </div>
  )
}
