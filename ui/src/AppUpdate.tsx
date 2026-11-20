import { useState } from 'react'
import { Download, ExternalLink, RefreshCw, X } from 'lucide-react'
import { Activity } from './activity'
import { api, errorMessage } from './api'
import { useLive } from './live'

const line = 'flex items-center gap-2 h-8 text-sm font-medium text-on-inverse'
const stripButton =
  'flex items-center gap-1.5 rounded-lg border border-on-inverse/30 px-2.5 py-1 text-xs font-medium text-on-inverse hover:bg-on-inverse/10'

/** Restarting into a downloaded update (Mac): the app closes, swaps itself for the new
 * version and opens again (engine/qbx/mac_update.py). */
export function useInstallUpdate() {
  const [installing, setInstalling] = useState(false)
  const [error, setError] = useState('')
  const install = () => {
    setInstalling(true)
    setError('')
    api.installUpdate().catch((e: unknown) => {
      setError(errorMessage(e))
      setInstalling(false)
    })
  }
  return { install, installing, error }
}

/** A Mac copy's next version, in the activity strip: "ready - restart to update" once it's
 * downloaded and checked, or where it can't replace itself, why and the download link.
 * Nothing while it downloads, or when a check fails (it's tried again within the hour).
 * Windows never shows this: the Microsoft Store updates it. */
export function UpdateNotice() {
  const { appUpdate, update } = useLive()
  const { install, installing, error } = useInstallUpdate()
  const [hidden, setHidden] = useState<string | null>(null)
  if (!appUpdate?.version || hidden === `${appUpdate.status}:${appUpdate.version}`) return null
  const close = (
    <button
      className="rounded-lg p-1 text-on-inverse/70 hover:text-on-inverse"
      aria-label="Not now"
      title="Not now"
      onClick={() => setHidden(`${appUpdate.status}:${appUpdate.version}`)}
    >
      <X size={14} />
    </button>
  )

  if (appUpdate.status === 'ready') {
    return (
      <Activity>
        <span className={line}>
          <RefreshCw size={15} />
          Version {appUpdate.version} is ready - your papers and exams stay as they are.
        </span>
        <button className={stripButton} disabled={installing} onClick={install}>
          <RefreshCw size={13} className={installing ? 'animate-spin' : ''} />
          {installing ? 'Restarting…' : 'Restart to update'}
        </button>
        {error && <span className="text-xs text-[#ffb4a2]">{error}</span>}
        {close}
      </Activity>
    )
  }
  if (appUpdate.status === 'manual') {
    return (
      <Activity>
        <span className={`${line} min-w-0`}>
          <Download size={15} className="shrink-0" />
          <span className="truncate" title={appUpdate.error ?? undefined}>
            Version {appUpdate.version} is out. {appUpdate.error}
          </span>
        </span>
        {update?.url && (
          <button className={stripButton} onClick={() => void api.openUpdate().catch(() => undefined)}>
            Get the update <ExternalLink size={12} />
          </button>
        )}
        {close}
      </Activity>
    )
  }
  return null
}
