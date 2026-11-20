import { useEffect, useState } from 'react'
import { Trash2 } from 'lucide-react'
import { api, errorMessage } from './api'
import { Modal } from './Modal'

/** Settings' "Delete all my data" (engine/qbx/wipe.py): papers, parsed papers, exams, tags,
 * settings and the sign-in, then the app closes. Saved PDFs stay. Only in the packaged app
 * (`can_delete_data`) - running from source never deletes anything. */
export function DeleteDataSection() {
  const [available, setAvailable] = useState(false)
  const [asking, setAsking] = useState(false)
  useEffect(() => void api.getState().then((s) => setAvailable(!!s.can_delete_data)).catch(() => {}), [])
  if (!available) return null
  return (
    <section className="space-y-2">
      <h3 className="font-semibold">Your data</h3>
      <p className="text-sm text-ink-2">
        Remove everything this app keeps on this computer - for handing the computer on, or before removing the app.
      </p>
      <button className="btn-secondary !text-danger hover:!border-danger" onClick={() => setAsking(true)}>
        <Trash2 size={14} />
        Delete all my data
      </button>
      {asking && <ConfirmDelete onClose={() => setAsking(false)} />}
    </section>
  )
}

function ConfirmDelete({ onClose }: { onClose: () => void }) {
  const [typed, setTyped] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState('')
  const ready = typed.trim().toLowerCase() === 'delete'

  const remove = () => {
    setDeleting(true)
    setError('')
    api.deleteAllData(typed).catch((e: unknown) => {
      setError(errorMessage(e))
      setDeleting(false)
    })
  }

  return (
    <Modal title="Delete all your data?" onClose={deleting ? () => {} : onClose} dismissable={!deleting}>
      <div className="space-y-4 text-sm">
        <div>
          <p className="font-medium">This permanently deletes, from this computer:</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-ink-2">
            <li>every paper you've added, and everything read from them</li>
            <li>your exams, topic tags and fixes</li>
            <li>your settings, and your sign-in on this computer</li>
          </ul>
        </div>
        <p className="text-ink-2">
          Question papers and mark schemes you've saved as PDFs stay where they are. The app closes once it's done. This
          can't be undone.
        </p>
        <label className="block">
          <span className="text-ink-2">
            Type <strong className="text-ink">delete</strong> to confirm
          </span>
          <input
            className="field mt-1 w-full"
            value={typed}
            autoFocus
            disabled={deleting}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && ready && !deleting && remove()}
          />
        </label>
        {error && <p className="text-xs text-danger">{error}</p>}
        <div className="flex justify-end gap-2">
          <button className="btn-secondary" disabled={deleting} onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary !bg-danger hover:!bg-danger disabled:opacity-45"
            disabled={!ready || deleting}
            onClick={remove}
          >
            <Trash2 size={14} />
            {deleting ? 'Deleting…' : 'Delete everything'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
