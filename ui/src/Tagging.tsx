import { useState } from 'react'
import { Check, Loader2, Pause, Square, Tags } from 'lucide-react'
import { Activity } from './activity'
import { api, errorMessage } from './api'
import { useLive } from './live'
import { Modal } from './Modal'

/** Tagging in the top bar (user-decided: it never starts by itself). With papers whose
 * questions aren't tagged, "Tag n papers" asks the teacher to confirm before anything goes
 * to Claude; while it runs, its progress - a batch's papers answered - and a window with
 * the details. */
export function TaggingControl() {
  const { tagging, untaggedPapers, ai } = useLive()
  const [open, setOpen] = useState(false)
  if (!ai) return null

  let label: string | null = null
  if (tagging.running) {
    label =
      tagging.batch && tagging.stage?.startsWith('Waiting for Claude')
        ? `Claude tagging: ${tagging.batch.done} of ${tagging.batch.total} papers`
        : tagging.stage?.startsWith('Saving')
          ? tagging.stage
          : tagging.total
          ? `Tagging ${tagging.done ?? 0} of ${tagging.total}`
          : 'Tagging…'
  } else if (tagging.paused) {
    label = 'Tagging paused'
  } else if (untaggedPapers > 0) {
    label = `Tag ${untaggedPapers} ${untaggedPapers === 1 ? 'paper' : 'papers'}`
  }

  return (
    <>
      {label && (
        <Activity>
        <button
          onClick={() => setOpen(true)}
          className="flex items-center gap-2 h-8 rounded-xl border border-white/15 bg-white/10 px-3 text-sm font-medium text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
          title="Tagging questions with their syllabus topics"
        >
          {tagging.running ? <Loader2 size={15} className="animate-spin" /> : <Tags size={15} />}
          {label}
        </button>
        </Activity>
      )}
      {open && <TaggingWindow onClose={() => setOpen(false)} />}
    </>
  )
}

function TaggingWindow({ onClose }: { onClose: () => void }) {
  const { tagging, untaggedPapers, startTagging } = useLive()
  const [error, setError] = useState('')
  const [starting, setStarting] = useState(false)
  const showProgress = tagging.running || tagging.paused || tagging.finished_at

  const start = async () => {
    setError('')
    setStarting(true)
    try {
      await startTagging()
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setStarting(false)
    }
  }

  return (
    <Modal title="Tag questions by topic" onClose={onClose}>
      <div className="space-y-4 text-sm">
        {!tagging.running && untaggedPapers > 0 && (
          <>
            <p className="text-ink-2">
              {untaggedPapers} {untaggedPapers === 1 ? 'paper has' : 'papers have'} questions without topic tags. Claude
              reads each question's text and its mark scheme answer, and gives every part its syllabus topics - what topic
              booklets and Claude-built exams choose from.
            </p>
            <ul className="space-y-1 text-xs text-muted">
              <li>Only the extracted text is sent, never the PDFs.</li>
              <li>It uses your Classark credit. Questions already tagged are never sent again.</li>
              <li>Any tag can be corrected in the question bank; your corrections are never replaced.</li>
            </ul>
          </>
        )}
        {showProgress && <TaggingProgress />}
        {error && <p className="rounded-lg bg-danger-wash px-3 py-2 text-xs text-danger">{error}</p>}
        <div className="flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose}>
            {tagging.running ? 'Close' : 'Not now'}
          </button>
          {!tagging.running && untaggedPapers > 0 && (
            <button className="btn-primary" disabled={starting} onClick={() => void start()}>
              {starting && <Loader2 size={14} className="animate-spin" />}
              {tagging.paused ? 'Tag the rest' : 'Start tagging'}
            </button>
          )}
        </div>
      </div>
    </Modal>
  )
}

export function TaggingProgress() {
  const { tagging } = useLive()
  const done = tagging.done ?? 0
  const total = tagging.total ?? 0
  const failed = tagging.failed ?? []
  return (
    <div className="mt-4 rounded-2xl border border-line bg-sunken px-4 py-3">
      <div className="flex items-center gap-3 text-sm">
        {tagging.running ? (
          <Loader2 size={15} className="animate-spin text-accent" />
        ) : tagging.paused ? (
          <Pause size={15} className="text-warning" />
        ) : (
          <Check size={15} className="text-success" />
        )}
        <span className="font-medium">
          {tagging.running
            ? tagging.batch && tagging.stage?.startsWith('Waiting for Claude')
              ? `Claude has answered ${tagging.batch.done.toLocaleString()} of ${tagging.batch.total.toLocaleString()} papers`
              : total
                ? `Tagging ${done.toLocaleString()} of ${total.toLocaleString()} questions`
                : tagging.stage
            : tagging.paused
              ? `Paused after ${done.toLocaleString()} questions`
              : `Tagged ${done.toLocaleString()} questions`}
        </span>
        {tagging.current && <span className="truncate text-xs text-muted">{tagging.current}</span>}
        {tagging.running && (
          <button className="btn-ghost ml-auto" onClick={() => void api.stopTagging()} disabled={tagging.stop}>
            <Square size={12} /> {tagging.stop ? 'Stopping…' : 'Stop'}
          </button>
        )}
      </div>
      {(() => {
        // a batch moves by papers answered, and the tags are saved once it ends
        const batch = tagging.running && tagging.batch && tagging.stage?.startsWith('Waiting for Claude') ? tagging.batch : null
        const [n, of] = batch ? [batch.done, batch.total] : [done, total]
        return of > 0 ? (
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-card">
            <div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${(100 * n) / of}%` }} />
          </div>
        ) : null
      })()}
      {tagging.running && tagging.batch && (
        <p className="mt-2 text-xs text-muted">
          The papers went to Claude together as one batch, at half the price of one at a time. A batch usually takes
          minutes to an hour (at most a day), and the tags appear when it ends. You can close the app meanwhile - it
          collects the batch the next time it opens.
        </p>
      )}
      {tagging.paused && (
        <p className="mt-2 text-xs text-warning">{tagging.paused} Press “Tag the rest” then to carry on.</p>
      )}
      {tagging.error && <p className="mt-2 text-xs text-danger">{tagging.error}</p>}
      {failed.length > 0 && (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-danger">
            {failed.length} paper{failed.length === 1 ? '' : 's'} couldn’t be tagged - start again to retry them
          </summary>
          <ul className="mt-1 space-y-0.5 text-ink-2">
            {failed.slice(0, 20).map((f) => (
              <li key={f.paper_key}>
                {f.paper_key}: {f.error}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

