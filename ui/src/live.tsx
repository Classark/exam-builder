import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { api } from './api'
import type { AccountState, AppUpdateState, LibraryProgress, PaperSummary, ScanState, TaggingState, UpdateState } from './types'

/** The app's view of the library, kept current without reloading the page.
 *
 * The backend bumps a `version` whenever any paper is extracted, re-extracted or edited
 * (engine/qbx/library.py). This polls that one number - fast while a scan runs, slower when
 * idle - and refetches the paper list only when
 * it changes, so a paper shows up the moment its extraction finishes. */

interface Live {
  version: number
  papers: PaperSummary[]
  loaded: boolean
  scan: ScanState
  /** Whether this build can reach Claude (a release build can't yet): false hides Claude's buttons. */
  ai: boolean
  /** debug: Claude Code and Settings, for testing; release: for teachers. */
  edition: 'debug' | 'release'
  /** the Classark account, and whether the first poll has answered (`ready`) */
  account: AccountState
  ready: boolean
  /** the oldest version Classark still allows; a release build older than it stops */
  update: UpdateState | null
  /** a Mac copy updating itself; null on Windows (the Store updates it) and in debug */
  appUpdate: AppUpdateState | null
  tagging: TaggingState
  /** papers being read into the library in the background (after an update), or null */
  library: LibraryProgress | null
  /** the app's engine hasn't answered for a while: shown, and retried */
  unreachable: boolean
  /** papers with questions not yet tagged; tagging waits for the teacher to start it */
  untaggedPapers: number
  startTagging: (subject?: string) => Promise<void>
  startScan: (reprocess: boolean) => Promise<void>
  /** After this tab changed something itself: check now rather than on the next tick. */
  poke: () => void
}

const LiveContext = createContext<Live | null>(null)

const BUSY_MS = 1000
const IDLE_MS = 3000
const SCAN_REFRESH_MS = 5000
// polls in a row with no answer before the app says it can't reach its engine: one slow
// answer is nothing, three (~10-30s) is worth saying
const UNREACHABLE_AFTER = 3

export function LiveProvider({ children }: { children: ReactNode }) {
  const [version, setVersion] = useState(-1)
  const [papers, setPapers] = useState<PaperSummary[]>([])
  const [loaded, setLoaded] = useState(false)
  const [scan, setScan] = useState<ScanState>({ running: false })
  const [tagging, setTagging] = useState<TaggingState>({ running: false })
  const [untaggedPapers, setUntaggedPapers] = useState(0)
  // unknown until the first poll: hidden, so a release build never flashes Claude's buttons
  const [ai, setAi] = useState(false)
  const [edition, setEdition] = useState<'debug' | 'release'>('release')
  const [account, setAccount] = useState<AccountState>({ status: 'signed_out' })
  const [ready, setReady] = useState(false)
  const [update, setUpdate] = useState<UpdateState | null>(null)
  const [appUpdate, setAppUpdate] = useState<AppUpdateState | null>(null)
  const [library, setLibrary] = useState<LibraryProgress | null>(null)
  const [unreachable, setUnreachable] = useState(false)
  const failures = useRef(0)
  const versionRef = useRef(-1)
  const fetchedAt = useRef(0)
  const scanningRef = useRef(false)
  const timer = useRef<number | undefined>(undefined)

  const tick = useCallback(async () => {
    window.clearTimeout(timer.current)
    try {
      const state = await api.getState()
      // poll fast while waiting on the browser too, so signing in shows at once
      failures.current = 0
      setUnreachable(false)
      setLibrary(state.library ?? null)
      scanningRef.current =
        state.scan.running || !!state.tagging?.running || state.account?.status === 'waiting' || !!state.library
      setScan(state.scan)
      if (state.tagging) setTagging(state.tagging)
      setUntaggedPapers(state.untagged_papers ?? 0)
      setAi(state.ai ?? true)
      setEdition(state.edition ?? 'debug')
      if (state.account) setAccount(state.account)
      setUpdate(state.update ?? null)
      setAppUpdate(state.app_update ?? null)
      setReady(true)
      // While a scan finishes paper after paper, every list re-reading the whole bank each
      // second kept the app busy (user-reported: unbearable while processing) - so then at
      // most every SCAN_REFRESH_MS. The version not taken stays unseen and comes next time.
      const now = Date.now()
      // tagging too: each paper's tags saved moves the version
      // and while the library is read in, which publishes every second
      const throttled =
        (state.scan.running || !!state.tagging?.running || !!state.library) && now - fetchedAt.current < SCAN_REFRESH_MS
      if (state.version !== versionRef.current && !throttled) {
        fetchedAt.current = now
        versionRef.current = state.version
        const list = await api.listPapers()
        setPapers(list)
        setVersion(state.version)
        setLoaded(true)
      }
    } catch {
      // backend restarting, busy or gone: keep what's shown, say so if it lasts, try again
      failures.current += 1
      if (failures.current >= UNREACHABLE_AFTER) setUnreachable(true)
    }
    // Keeps going while the window is hidden - the browser already slows hidden timers,
    // and a minimised window must not come back showing a finished scan as running.
    timer.current = window.setTimeout(() => void tick(), scanningRef.current || failures.current ? BUSY_MS : IDLE_MS)
  }, [])

  useEffect(() => {
    void tick()
    const onVisible = () => document.visibilityState === 'visible' && void tick()
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.clearTimeout(timer.current)
    }
  }, [tick])

  const startScan = useCallback(
    async (reprocess: boolean) => {
      const state = await api.scanPapers(reprocess)
      scanningRef.current = state.running
      setScan(state)
      void tick()
    },
    [tick],
  )

  const startTagging = useCallback(
    async (subject?: string) => {
      const state = await api.startTagging(subject)
      scanningRef.current = state.running
      setTagging(state)
      void tick()
    },
    [tick],
  )

  const poke = useCallback(() => void tick(), [tick])

  return (
    <LiveContext.Provider value={{ version, papers, loaded, scan, ai, edition, account, ready, update, appUpdate, tagging, library, unreachable, untaggedPapers, startTagging, startScan, poke }}>
      {children}
    </LiveContext.Provider>
  )
}

export function useLive(): Live {
  const live = useContext(LiveContext)
  if (!live) throw new Error('useLive outside LiveProvider')
  return live
}
