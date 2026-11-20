import { useEffect, useState } from 'react'
import { Loader2, RotateCcw, TriangleAlert, WifiOff, X } from 'lucide-react'
import { Activity } from './activity'
import { UpdateNotice } from './AppUpdate'
import { useDesktop } from './desktop'
import { useLive } from './live'

// how long the engine may stay silent before the strip offers to restart the app: long
// enough that a busy moment never asks, short enough that a dead engine isn't a mystery
const RESTART_AFTER_MS = 30_000

const line = 'flex items-center gap-2 h-8 text-sm font-medium text-on-inverse'
const stripButton =
  'flex items-center gap-1.5 rounded-lg border border-on-inverse/30 px-2.5 py-1 text-xs font-medium text-on-inverse hover:bg-on-inverse/10'

/** What the app is waiting on, said rather than left blank (user-reported: the paper list
 * stayed empty with nothing to say why, and it looked like a connection problem).
 *
 * - Reading papers into the library after an update changed its format: every paper is read
 *   once, in the background, and the lists fill in as it goes.
 * - The engine not answering: the app keeps what it shows and tries again every second;
 *   after RESTART_AFTER_MS it offers to restart the app (user-asked). */
export function LibraryStatus() {
  const { library, unreachable } = useLive()
  const desktop = useDesktop()
  const [since, setSince] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!unreachable) {
      setSince(null)
      return
    }
    setSince((s) => s ?? Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [unreachable])

  if (unreachable) {
    const stuck = since !== null && now - since >= RESTART_AFTER_MS
    return (
      <Activity>
        <span className={line}>
          <WifiOff size={15} />
          {stuck ? "The app's engine has stopped answering." : "The app's engine isn't answering - trying again…"}
        </span>
        {stuck &&
          (desktop?.restart ? (
            <button className={stripButton} onClick={() => void desktop.restart?.()}>
              <RotateCcw size={13} /> Restart the app
            </button>
          ) : (
            <span className="text-sm text-on-inverse/80">Close the app and open it again.</span>
          ))}
      </Activity>
    )
  }
  return (
    <>
      {library && (
        <Activity>
          <span className={line}>
            <Loader2 size={15} className="animate-spin" />
            Loading your papers: {library.done} of {library.total}
          </span>
        </Activity>
      )}
      <JobProblems />
      <UpdateNotice />
    </>
  )
}

/** A background job that stopped on an error - a scan, or tagging - said in the activity
 * strip with Retry, rather than the job just disappearing (user-asked). Each failure shows
 * once; closing it hides it until the next one. */
function JobProblems() {
  const { scan, tagging, startScan, startTagging } = useLive()
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set())
  const [retrying, setRetrying] = useState(false)

  const problems = [
    !scan.running && scan.error
      ? { id: `scan:${scan.finished_at ?? ''}`, text: scan.error, retry: () => startScan(!!scan.reprocess) }
      : null,
    !tagging.running && tagging.error
      ? { id: `tag:${tagging.finished_at ?? ''}`, text: tagging.error, retry: () => startTagging(tagging.subject ?? undefined) }
      : null,
  ].filter((p): p is NonNullable<typeof p> => !!p && !dismissed.has(p.id))

  return (
    <>
      {problems.map((p) => (
        <Activity key={p.id}>
          <span className={`${line} min-w-0`}>
            <TriangleAlert size={15} className="shrink-0 text-[#ffb4a2]" />
            <span className="truncate" title={p.text}>
              {p.text}
            </span>
          </span>
          <button
            className={stripButton}
            disabled={retrying}
            onClick={() => {
              setRetrying(true)
              setDismissed((d) => new Set(d).add(p.id))
              void p
                .retry()
                .catch(() =>
                  // it couldn't start: show the problem again rather than nothing
                  setDismissed((d) => {
                    const next = new Set(d)
                    next.delete(p.id)
                    return next
                  }),
                )
                .finally(() => setRetrying(false))
            }}
          >
            <RotateCcw size={13} /> Try again
          </button>
          <button
            className="rounded-lg p-1 text-on-inverse/70 hover:text-on-inverse"
            aria-label="Dismiss"
            onClick={() => setDismissed((d) => new Set(d).add(p.id))}
          >
            <X size={14} />
          </button>
        </Activity>
      ))}
    </>
  )
}
