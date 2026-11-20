import { AlertTriangle, Check, CircleCheck, FileWarning } from 'lucide-react'
import { describeIssue } from '../issues'
import { NumberCell } from '../ReviewMarks'
import type { Paper } from '../types'
import type { PaperModel } from './model'

/** The paper as a whole: its own problems (cover total, numbering, a damaged file) and
 * every question's marks at a glance - a row opens that question. */
export function PaperView({
  paper,
  model,
  onOpen,
  onCheck,
  onCoverTotal,
}: {
  paper: Paper
  model: PaperModel
  onOpen: (number: number) => void
  onCheck: (keys: string[]) => void
  onCoverTotal: (total: number) => void
}) {
  const sum = paper.questions.reduce((s, q) => s + q.total_marks, 0)
  const open = model.paperIssues.filter((i) => !i.checked)
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="scroll-quiet min-h-0 flex-1 space-y-4 overflow-auto p-3">
        <h2 className="text-lg font-semibold">The whole paper</h2>

        {open.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-success">
            <CircleCheck size={16} /> Nothing wrong with the paper as a whole.
          </p>
        ) : (
          <ul className="space-y-2">
            {open.map((issue) => {
              const { title, hint, fix } = describeIssue(issue)
              return (
                <li key={issue.key} className="rounded-xl border border-warning/40 bg-warning-wash p-2.5 text-xs">
                  <p className="flex items-center gap-1.5 text-sm font-semibold text-ink">
                    <AlertTriangle size={14} className="text-warning" /> {title}
                  </p>
                  <p className="mt-1 text-ink-2">
                    {hint} <span className="text-muted">({issue.detail})</span>
                  </p>
                  {fix && <p className="mt-1 text-ink">{fix}</p>}
                  <button className="btn-ghost mt-1.5 px-2 py-1 text-xs" onClick={() => onCheck([issue.key])}>
                    <Check size={13} /> The paper really is like this
                  </button>
                </li>
              )
            })}
          </ul>
        )}

        <section>
          <div className="mb-1.5 flex items-center gap-2 text-sm">
            <span className="font-semibold">Marks</span>
            <span className={`ml-auto text-xs ${paper.cover_total_marks != null && sum !== paper.cover_total_marks ? 'text-warning' : 'text-muted'}`}>
              questions add up to {sum}
              {paper.cover_total_marks != null ? ', the cover prints' : ' · cover total'}
            </span>
            <NumberCell
              value={paper.cover_total_marks ?? 0}
              warn={paper.cover_total_marks != null && sum !== paper.cover_total_marks}
              label="The cover's total"
              onSave={async (n) => onCoverTotal(n)}
            />
          </div>
          <table className="w-full text-left text-sm">
            <thead className="text-[11px] uppercase tracking-wide text-muted">
              <tr>
                <th className="py-1 font-semibold">Question</th>
                <th className="py-1 font-semibold">Printed total</th>
                <th className="py-1 font-semibold">Parts</th>
                <th className="py-1" />
              </tr>
            </thead>
            <tbody>
              {model.questions.map((v) => {
                const q = v.question
                const parts = q ? q.parts.reduce((s, p) => s + p.marks, 0) : null
                return (
                  <tr key={v.number} className="cursor-pointer border-t border-line hover:bg-accent-wash" onClick={() => onOpen(v.number)}>
                    <td className="py-1.5 font-medium">Q{v.number}</td>
                    <td className="py-1.5">{q ? q.total_marks : '-'}</td>
                    <td className={`py-1.5 ${q && parts !== q.total_marks ? 'text-warning' : ''}`}>{parts ?? '-'}</td>
                    <td className="py-1.5 text-right text-xs">
                      {!q ? (
                        <span className="inline-flex items-center gap-1 text-warning">
                          <FileWarning size={13} /> missing
                        </span>
                      ) : v.open ? (
                        <span className="inline-flex items-center gap-1 text-warning">
                          <AlertTriangle size={13} /> {v.open} to check
                        </span>
                      ) : (
                        <CircleCheck size={14} className="ml-auto text-success" />
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  )
}
