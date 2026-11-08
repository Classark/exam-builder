import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { api } from './api'
import type { AccountState, FixingState, PaperSummary, ScanState, TaggingState } from './types'

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
  tagging: TaggingState
  startTagging: (subject?: string) => Promise<void>
  fixing: FixingState
  /** Claude reviews issues: one paper, one issue on it, or (no argument) every paper. */
  startFixing: (body?: { paper_key?: string; issue_key?: string; everything?: boolean }) => Promise<void>
  startScan: (reprocess: boolean) => Promise<void>
  /** After this tab changed something itself: check now rather than on the next tick. */
  poke: () => void
}

const LiveContext = createContext<Live | null>(null)

const BUSY_MS = 1000
const IDLE_MS = 3000

export function LiveProvider({ children }: { children: ReactNode }) {
  const [version, setVersion] = useState(-1)
  const [papers, setPapers] = useState<PaperSummary[]>([])
  const [loaded, setLoaded] = useState(false)
  const [scan, setScan] = useState<ScanState>({ running: false })
  const [tagging, setTagging] = useState<TaggingState>({ running: false })
  const [fixing, setFixing] = useState<FixingState>({ running: false })
  // unknown until the first poll: hidden, so a release build never flashes Claude's buttons
  const [ai, setAi] = useState(false)
  const [edition, setEdition] = useState<'debug' | 'release'>('release')
  const [account, setAccount] = useState<AccountState>({ status: 'signed_out' })
  const [ready, setReady] = useState(false)
  const versionRef = useRef(-1)
  const scanningRef = useRef(false)
  const timer = useRef<number | undefined>(undefined)

  const tick = useCallback(async () => {
    window.clearTimeout(timer.current)
    try {
      const state = await api.getState()
      // poll fast while waiting on the browser too, so signing in shows at once
      scanningRef.current =
        state.scan.running || !!state.tagging?.running || !!state.fixing?.running || state.account?.status === 'waiting'
      setScan(state.scan)
      if (state.tagging) setTagging(state.tagging)
      if (state.fixing) setFixing(state.fixing)
      setAi(state.ai ?? true)
      setEdition(state.edition ?? 'debug')
      if (state.account) setAccount(state.account)
      setReady(true)
      if (state.version !== versionRef.current) {
        versionRef.current = state.version
        const list = await api.listPapers()
        setPapers(list)
        setVersion(state.version)
        setLoaded(true)
      }
    } catch {
      // backend restarting or gone: keep what's shown and try again
    }
    // Keeps going while the window is hidden - the browser already slows hidden timers,
    // and a minimised window must not come back showing a finished scan as running.
    timer.current = window.setTimeout(() => void tick(), scanningRef.current ? BUSY_MS : IDLE_MS)
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

  const startFixing = useCallback(
    async (body: { paper_key?: string; issue_key?: string; everything?: boolean } = {}) => {
      const state = await api.startFixing(body)
      scanningRef.current = state.running
      setFixing(state)
      void tick()
    },
    [tick],
  )

  const poke = useCallback(() => void tick(), [tick])

  return (
    <LiveContext.Provider value={{ version, papers, loaded, scan, ai, edition, account, ready, tagging, startTagging, fixing, startFixing, startScan, poke }}>
      {children}
    </LiveContext.Provider>
  )
}

export function useLive(): Live {
  const live = useContext(LiveContext)
  if (!live) throw new Error('useLive outside LiveProvider')
  return live
}
