import { useState } from 'react'
import { Download, ExternalLink, Loader2, RefreshCw } from 'lucide-react'
import { api, errorMessage } from './api'
import { useInstallUpdate } from './AppUpdate'
import { useDesktop } from './desktop'
import { useLive } from './live'
import type { UpdateState } from './types'

/** "Please update": this copy is older than the oldest version Classark still allows -
 * config/app's minExamBuilderVersion, the same switch the mobile apps use
 * (engine/qbx/app_version.py). A release build goes no further. A Mac copy that updates
 * itself (engine/qbx/mac_update.py) shows its download and "Restart to update" instead of
 * sending the teacher to a download page; otherwise, the update link. */
export function UpdateScreen({ update }: { update: UpdateState }) {
  const desktop = useDesktop()
  const { appUpdate } = useLive()
  const { install, installing, error: installError } = useInstallUpdate()
  const [error, setError] = useState('')
  const ready = appUpdate?.status === 'ready'
  const downloading = appUpdate?.status === 'downloading'
  const percent = downloading && appUpdate.total ? Math.round((appUpdate.done / appUpdate.total) * 100) : 0
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center bg-page p-6">
      <div className="flex w-full max-w-md flex-col items-center text-center">
        <img src="/icon.svg" alt="" className="h-14 w-14" />
        <h1 className="mt-4 text-2xl font-semibold">Please update Classark Exam Builder</h1>
        <p className="mt-2 text-sm text-ink-2">
          This version ({update.version}) is no longer supported. Install version {update.minimum} or newer to carry on -
          your papers, exams and settings stay where they are.
        </p>
        {downloading && (
          <div className="mt-5 w-full">
            <p className="flex items-center justify-center gap-2 text-sm text-ink-2">
              <Loader2 size={14} className="animate-spin" /> Downloading version {appUpdate.version}… {percent}%
            </p>
            <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-sunken">
              <div className="h-full bg-accent transition-[width]" style={{ width: `${percent}%` }} />
            </div>
          </div>
        )}
        <div className="mt-6 flex gap-2">
          {desktop && (
            <button className="btn-secondary" onClick={() => void desktop.close()}>
              Close the app
            </button>
          )}
          {ready ? (
            <button className="btn-primary" disabled={installing} onClick={install}>
              <RefreshCw size={15} className={installing ? 'animate-spin' : ''} />
              {installing ? 'Restarting…' : 'Restart to update'}
            </button>
          ) : (
            !downloading &&
            update.url && (
              <button
                className="btn-primary"
                onClick={() => api.openUpdate().catch((e: unknown) => setError(errorMessage(e)))}
              >
                <Download size={15} /> Get the update <ExternalLink size={13} />
              </button>
            )
          )}
        </div>
        {appUpdate?.status === 'manual' && appUpdate.error && <p className="mt-3 text-xs text-muted">{appUpdate.error}</p>}
        {(error || installError) && <p className="mt-3 text-xs text-danger">{error || installError}</p>}
      </div>
    </div>
  )
}
