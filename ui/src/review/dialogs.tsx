import { useEffect, useState } from 'react'
import { FileText } from 'lucide-react'
import { api, errorMessage } from '../api'
import { Modal } from '../Modal'

export function EmptyLibrary() {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="card max-w-md px-10 py-12 text-center">
        <FileText size={36} className="mx-auto mb-4 text-accent" />
        <h2 className="mb-2 text-xl font-semibold">No papers yet</h2>
        <p className="text-sm text-ink-2">
          Copy question papers and their mark schemes into the papers folder (
          <span className="font-medium text-ink">Open papers folder</span> above). They're extracted by themselves, and
          each paper appears here as soon as it's done.
        </p>
      </div>
    </div>
  )
}

/** Confirms deleting a paper, naming the exams that use it first. */
export function DeletePaperDialog({
  paperKey,
  title,
  onClose,
  onDeleted,
}: {
  paperKey: string
  title: string
  onClose: () => void
  onDeleted: () => void
}) {
  const [usedIn, setUsedIn] = useState<string[] | null>(null)
  const [deletePdfs, setDeletePdfs] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    void api.paperUsedIn(paperKey).then((r) => setUsedIn(r.exams)).catch(() => setUsedIn([]))
  }, [paperKey])

  const remove = async () => {
    setBusy(true)
    setError('')
    try {
      await api.deletePaper(paperKey, deletePdfs)
      onDeleted()
    } catch (e) {
      setError(errorMessage(e))
      setBusy(false)
    }
  }

  return (
    <Modal title="Delete this paper?" onClose={onClose} width="max-w-md">
      <div className="space-y-3 text-sm">
        <p>
          <span className="font-semibold">{title}</span> will be removed from the bank - its questions, images, topic tags
          and review notes.
        </p>
        {usedIn && usedIn.length > 0 && (
          <p className="rounded-lg bg-warning-wash px-3 py-2 text-xs text-warning">
            Used in {usedIn.map((n) => `“${n}”`).join(', ')}. Those exams keep their other questions; this paper's will
            show as missing.
          </p>
        )}
        <label className="flex items-start gap-2 text-ink-2">
          <input
            type="checkbox"
            checked={deletePdfs}
            onChange={(e) => setDeletePdfs(e.target.checked)}
            className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]"
          />
          <span>
            Also delete its question paper and mark scheme PDFs from the papers folder.
            <span className="block text-xs text-muted">
              If you keep them, the app won't add this paper again by itself - only “Add one paper” brings it back.
            </span>
          </span>
        </label>
        {error && <p className="rounded-lg bg-danger-wash px-3 py-2 text-xs text-danger">{error}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button className="btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn-primary bg-danger hover:bg-danger" onClick={() => void remove()} disabled={busy || usedIn === null}>
            {busy ? 'Deleting…' : 'Delete paper'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

/** "How reviewing works": the screen, and which control fixes which problem. */
export function ReviewGuide({ onClose }: { onClose: () => void }) {
  const fixes: [string, string][] = [
    ['A part has the wrong marks', 'Type the right number in its marks box. “Use n” takes the mark scheme’s.'],
    [
      'A part isn’t linked to its mark scheme answer',
      'Select the part, then click its answer on the mark scheme. Not boxed there? Press “Draw it” and drag round it.',
    ],
    ['A mark scheme row isn’t linked to any part', 'Pick the part under “Mark scheme rows not linked” and press Link.'],
    ['A part’s box cuts something off or takes in too much', 'Select it and drag the box’s edges, or press Redraw box.'],
    ['Two parts were read as one', 'Select it, press Split, and click the page where the second part starts.'],
    ['One part was read as two', 'Select the first and press Merge with next.'],
    ['A whole question is missing', 'Open it in the question list (marked missing) and draw it on the question paper.'],
    ['The paper itself is like that (a misprint)', 'Nothing to fix - press “looks right”.'],
  ]
  return (
    <Modal title="How reviewing works" onClose={onClose} width="max-w-2xl">
      <div className="space-y-5 text-sm text-ink-2">
        <section className="space-y-1.5">
          <p>
            Every paper is read automatically and checked against itself: do the parts add up to each question’s total,
            and does every part have its answer in the mark scheme? Most papers pass. The ones that don’t are listed as
            “to check”.
          </p>
          <p>
            Work through a paper <b className="text-ink">one question at a time</b>, using the question list on the left (⚠
            marks the ones with something to look at - narrow it to numbers with its top button). The question paper comes next and the mark scheme on the
            right, with every part and every answer boxed and labelled. The middle column puts each part beside its
            answer.
          </p>
        </section>

        <section>
          <h3 className="mb-1.5 font-semibold text-ink">Which control fixes what</h3>
          <table className="w-full text-left">
            <tbody>
              {fixes.map(([problem, fix]) => (
                <tr key={problem} className="border-t border-line align-top">
                  <td className="w-[42%] py-1.5 pr-3 font-medium text-ink">{problem}</td>
                  <td className="py-1.5">{fix}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="space-y-1.5">
          <p>
            When a question is right, press <b className="text-ink">looks right</b> (<kbd className="key">Enter</kbd>) to
            go on to the next one to check.
          </p>
          <p>
            <b className="text-ink">Every change saves straight away</b> and can be undone from the message that
            appears. Changes are kept when a paper is re-processed.
          </p>
          <p className="text-xs text-muted">
            Keys: <kbd className="key">[</kbd>/<kbd className="key">]</kbd> previous/next question ·{' '}
            <kbd className="key">j</kbd>/<kbd className="key">k</kbd> next/previous part · <kbd className="key">Enter</kbd>{' '}
            looks right · <kbd className="key">d</kbd> redraw · <kbd className="key">s</kbd> split ·{' '}
            <kbd className="key">m</kbd> merge · <kbd className="key">f</kbd> widen the question paper ·{' '}
            <kbd className="key">Esc</kbd> cancel · <kbd className="key">{'{'}</kbd>/
            <kbd className="key">{'}'}</kbd> previous/next paper
          </p>
        </section>

        <div className="flex justify-end">
          <button className="btn-primary" onClick={onClose}>
            Got it
          </button>
        </div>
      </div>
    </Modal>
  )
}
