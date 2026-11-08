import { useEffect, useRef, useState } from 'react'
import { CheckCheck, ChevronDown, FilePlus2, FolderOpen, FolderSearch, Loader2, RefreshCw, Sparkles, Square } from 'lucide-react'
import { api } from './api'
import { useLive } from './live'
import { Modal } from './Modal'
import type { ScanState } from './types'

/** "Scan papers folder" and its relatives, in the top bar. A scan runs in the background:
 * its progress shows here, and each paper appears in the lists as soon as it's done. */
export function ScanControl() {
  const { scan, startScan, poke, fixing, startFixing, papers, ai } = useLive()
  const withIssues = papers.filter((p) => p.n_open > 0).length
  const fixAll = async (everything = false) => {
    setMenu(false)
    setError('')
    try {
      await startFixing(everything ? { everything: true } : {})
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  const [applying, setApplying] = useState(false)
  const [applied, setApplied] = useState('')
  const applyAll = async () => {
    setMenu(false)
    setError('')
    setApplying(true)
    try {
      const r = await api.acceptAllFixes()
      const parts = [`Applied ${r.applied} ${r.applied === 1 ? 'suggestion' : 'suggestions'}`]
      if (r.didnt_fit) parts.push(`${r.didnt_fit} no longer fit and still wait`)
      if (r.busy.length) parts.push(`${r.busy.length} being reviewed now - apply again after`)
      if (r.notes)
        parts.push(
          `${r.notes} ${r.notes === 1 ? 'is a note' : 'are notes'} for you, not fixes - open the paper's Claude tab`,
        )
      setApplied(parts.join(', '))
      poke()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setApplying(false)
    }
  }
  const [menu, setMenu] = useState(false)
  const [confirmReprocess, setConfirmReprocess] = useState(false)
  const [adding, setAdding] = useState(false)
  const [summary, setSummary] = useState<ScanState | null>(null)
  const [error, setError] = useState('')
  const wasRunning = useRef(false)
  const menuRef = useRef<HTMLDivElement>(null)

  // When a scan this session watched finishes, say what it did.
  useEffect(() => {
    if (wasRunning.current && !scan.running) setSummary(scan)
    wasRunning.current = scan.running
  }, [scan])

  useEffect(() => {
    if (!menu) return
    const close = (e: MouseEvent) => !menuRef.current?.contains(e.target as Node) && setMenu(false)
    window.addEventListener('mousedown', close)
    return () => window.removeEventListener('mousedown', close)
  }, [menu])

  const start = async (reprocess: boolean) => {
    setMenu(false)
    setConfirmReprocess(false)
    setError('')
    try {
      wasRunning.current = true
      await startScan(reprocess)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const total = scan.total ?? 0
  const done = scan.done ?? 0

  return (
    <>
      {fixing.running && !scan.running && (
        <div
          className="flex items-center gap-2.5 rounded-xl border border-white/15 bg-white/10 px-3.5 py-2 text-sm text-on-inverse"
          role="status"
          aria-live="polite"
        >
          <Sparkles size={16} className="text-accent" />
          <span className="font-medium">
            {fixing.stop
              ? 'Stopping…'
              : `Claude: paper ${Math.min((fixing.papers_done ?? 0) + 1, fixing.papers_total ?? 0)} of ${fixing.papers_total ?? 0}`}
          </span>
          <span className="text-white/60">
            {fixing.applied ?? 0} fixed · {fixing.suggested ?? 0} to check
          </span>
          {(fixing.suggested ?? 0) > 0 && <ApplyAllButton busy={applying} onClick={() => void applyAll()} />}
          <button
            className="rounded-md p-1 text-white/70 hover:bg-white/10 hover:text-white"
            onClick={() => void api.stopFixing().then(poke)}
            aria-label="Stop Claude reviewing papers"
            title="Stop after the papers in progress"
          >
            <Square size={13} />
          </button>
        </div>
      )}
      {!fixing.running && fixing.finished_at && fixing.papers_total && !fixing.paper_key ? (
        <span className="text-sm text-white/70" title={fixing.paused ?? fixing.error ?? ''}>
          {fixing.paused ? 'Claude paused at the usage limit: ' : 'Claude finished: '}
          {fixing.applied ?? 0} fixed, {fixing.suggested ?? 0} to check
          {(fixing.failed?.length ?? 0) > 0 && `, ${fixing.failed!.length} failed`}
          {(fixing.suggested ?? 0) > 0 && (
            <span className="ml-2 inline-block align-middle">
              <ApplyAllButton busy={applying} onClick={() => void applyAll()} />
            </span>
          )}
        </span>
      ) : null}
      {applied && (
        <button className="text-sm text-white/70 hover:text-white" onClick={() => setApplied('')} title="Dismiss">
          {applied}
        </button>
      )}
      {scan.running ? (
        <div
          className="flex items-center gap-2.5 rounded-xl border border-white/15 bg-white/10 px-3.5 py-2 text-sm text-on-inverse"
          role="status"
          aria-live="polite"
        >
          <Loader2 size={16} className="animate-spin text-accent" />
          <span className="font-medium">
            {total > 0 ? `Extracting ${Math.min(done + 1, total)} of ${total}` : (scan.stage ?? 'Starting')}
          </span>
          {scan.current && <span className="max-w-44 truncate text-white/60">{scan.current}</span>}
        </div>
      ) : (
        <>
          <button
            onClick={() => api.openPapersFolder().catch((e) => setError(e instanceof Error ? e.message : String(e)))}
            className="flex items-center gap-2 rounded-xl border border-white/15 bg-white/10 px-3.5 py-2 text-sm font-medium text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
            title="Open the folder new papers go in - copy PDFs there, then scan"
            aria-label="Open papers folder"
          >
            <FolderOpen size={16} />
            {/* labels only where the top bar has room for them */}
            <span className="hidden 2xl:inline">Open papers folder</span>
          </button>
          <div ref={menuRef} className="relative flex">
            <button
              onClick={() => void start(false)}
              className="flex items-center gap-2 rounded-l-xl border border-white/15 bg-white/10 px-4 py-2 text-sm font-medium text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
              title="Extract every new question paper and mark scheme pair in the papers folder"
              aria-label="Scan papers folder"
            >
              <FolderSearch size={16} />
              <span className="hidden 2xl:inline">Scan papers folder</span>
            </button>
            <button
              onClick={() => setMenu((m) => !m)}
              className="rounded-r-xl border border-l-0 border-white/15 bg-white/10 px-2 text-on-inverse transition-colors hover:bg-accent/30"
              aria-label="More ways to add papers"
              aria-expanded={menu}
            >
              <ChevronDown size={16} />
            </button>
            {menu && (
              <div className="card absolute right-0 top-full z-40 mt-2 w-80 overflow-hidden whitespace-normal rounded-2xl p-1.5 shadow-float">
                <MenuItem
                  icon={RefreshCw}
                  title="Re-process papers with issues"
                  detail="Extract them again with this version. Clean papers are left alone."
                  onClick={() => {
                    setMenu(false)
                    setConfirmReprocess(true)
                  }}
                />
                {ai && (
                  <>
                  <MenuItem
                    icon={Sparkles}
                    title={fixing.running ? 'Claude is reviewing papers…' : 'Fix all papers with Claude'}
                    detail={`Claude reviews the ${withIssues} ${withIssues === 1 ? 'paper' : 'papers'} with open issues. Settings decides whether checked fixes apply by themselves.`}
                    onClick={() => void fixAll()}
                  />
                  <MenuItem
                    icon={Sparkles}
                    title="Ask Claude again about everything"
                    detail="Every paper with open issues, even those with suggestions or notes already waiting - they're replaced. Fixes you rejected stay rejected."
                    onClick={() => void fixAll(true)}
                  />
                  <MenuItem
                    icon={CheckCheck}
                    title="Apply all Claude's suggestions"
                    detail="Every fix waiting in the Reviewer's Claude tab, in every paper. Each one can be undone there."
                    onClick={() => void applyAll()}
                  />
                  </>
                )}
                <MenuItem
                  icon={FilePlus2}
                  title="Add one paper"
                  detail="Choose a question paper and its mark scheme."
                  onClick={() => {
                    setMenu(false)
                    setAdding(true)
                  }}
                />
              </div>
            )}
          </div>
        </>
      )}

      {/* the progress sweep along the bar's bottom edge */}
      {scan.running && (
        <div className="absolute inset-x-6 bottom-0 h-[3px] overflow-hidden rounded-full bg-white/10">
          {total > 0 ? (
            <div className="h-full bg-accent transition-[width] duration-500" style={{ width: `${(done / total) * 100}%` }} />
          ) : (
            <div className="progress-indeterminate relative h-full" />
          )}
        </div>
      )}

      {error && <span className="text-sm text-[#ffb4a2]">{error}</span>}

      {confirmReprocess && (
        <Modal title="Re-process papers with issues?" onClose={() => setConfirmReprocess(false)}>
          <ul className="mb-5 space-y-2 text-sm text-ink-2">
            <li>Every paper that still has issues is extracted again with this version.</li>
            <li>Clean papers are left alone, along with your corrections to them.</li>
            <li>Corrections you made in Review to the papers that are re-processed are replaced.</li>
          </ul>
          <div className="flex justify-end gap-2">
            <button className="btn-secondary" onClick={() => setConfirmReprocess(false)}>
              Cancel
            </button>
            <button className="btn-primary" onClick={() => void start(true)}>
              Re-process
            </button>
          </div>
        </Modal>
      )}

      {adding && (
        <AddPaper
          onClose={() => setAdding(false)}
          onAdded={() => {
            setAdding(false)
            poke()
          }}
        />
      )}

      {summary && <ScanSummary scan={summary} onClose={() => setSummary(null)} />}
    </>
  )
}

function ApplyAllButton({ busy, onClick }: { busy: boolean; onClick: () => void }) {
  return (
    <button
      className="flex items-center gap-1.5 rounded-md bg-white/10 px-2 py-1 text-xs font-medium text-on-inverse hover:bg-accent/30 disabled:opacity-60"
      onClick={onClick}
      disabled={busy}
      title="Apply every suggestion waiting in the Reviewer's Claude tab. Each one can be undone there."
    >
      {busy ? <Loader2 size={13} className="animate-spin" /> : <CheckCheck size={13} />}
      Apply all
    </button>
  )
}

function MenuItem({
  icon: Icon,
  title,
  detail,
  onClick,
}: {
  icon: typeof RefreshCw
  title: string
  detail: string
  onClick: () => void
}) {
  return (
    <button onClick={onClick} className="flex w-full gap-3 rounded-xl px-3 py-2.5 text-left transition-colors hover:bg-accent-wash">
      <Icon size={18} className="mt-0.5 shrink-0 text-accent" />
      <span>
        <span className="block text-sm font-medium text-ink">{title}</span>
        <span className="block text-xs text-muted">{detail}</span>
      </span>
    </button>
  )
}

function ScanSummary({ scan, onClose }: { scan: ScanState; onClose: () => void }) {
  const processed = scan.processed ?? []
  const failed = scan.failed ?? []
  const unpaired = scan.unpaired ?? []
  const clean = processed.filter((p) => p.passed).length
  const nothing = processed.length + failed.length + unpaired.length === 0

  return (
    <Modal title={nothing ? 'Nothing new to extract' : 'Scan finished'} onClose={onClose}>
      {nothing ? (
        <p className="text-sm text-ink-2">
          Every paper in <span className="break-all text-ink">{scan.folder}</span> is already extracted. Copy new PDFs
          into that folder, then scan again.
        </p>
      ) : (
        <div className="space-y-5 text-sm">
          <div className="grid grid-cols-3 gap-3">
            <Stat value={processed.length} label="extracted" />
            <Stat value={clean} label="clean" tone="success" />
            <Stat value={processed.length - clean} label="to review" tone="accent" />
          </div>
          {scan.error && <p className="rounded-lg bg-danger-wash px-3 py-2 text-danger">{scan.error}</p>}
          {failed.length > 0 && (
            <section>
              <h3 className="mb-1.5 font-medium">Couldn't extract</h3>
              <ul className="space-y-1.5">
                {failed.map((f) => (
                  <li key={f.paper_key} className="rounded-lg bg-danger-wash px-3 py-2">
                    <span className="font-medium text-ink">{f.paper_key}</span>
                    <span className="block text-ink-2">{f.error}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {unpaired.length > 0 && (
            <section>
              <h3 className="mb-1.5 font-medium">Left alone</h3>
              <ul className="space-y-1 text-ink-2">
                {unpaired.map((u) => (
                  <li key={u.file}>
                    <span className="text-ink">{u.file}</span>: {u.reason}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </Modal>
  )
}

function Stat({ value, label, tone }: { value: number; label: string; tone?: 'success' | 'accent' }) {
  const color = tone === 'success' ? 'text-success' : tone === 'accent' ? 'text-accent' : 'text-ink'
  return (
    <div className="rounded-2xl border border-line bg-sunken px-4 py-3">
      <div className={`text-2xl font-semibold ${color}`}>{value}</div>
      <div className="text-xs text-muted">{label}</div>
    </div>
  )
}

function AddPaper({ onClose, onAdded }: { onClose: () => void; onAdded: (key: string) => void }) {
  const [qp, setQp] = useState<File | null>(null)
  const [ms, setMs] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async () => {
    if (!qp || !ms) return
    setBusy(true)
    setError('')
    try {
      const result = await api.uploadPaper(qp, ms)
      onAdded(result.paper_key)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Add one paper" onClose={onClose}>
      <div className="space-y-4">
        <FilePick label="Question paper" file={qp} onPick={setQp} />
        <FilePick label="Mark scheme" file={ms} onPick={setMs} />
        {error && <p className="rounded-lg bg-danger-wash px-3 py-2 text-sm text-danger">{error}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" disabled={!qp || !ms || busy} onClick={() => void submit()}>
            {busy && <Loader2 size={16} className="animate-spin" />}
            {busy ? 'Extracting…' : 'Extract paper'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

function FilePick({ label, file, onPick }: { label: string; file: File | null; onPick: (f: File | null) => void }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-sm font-semibold">{label}</span>
      <span className="flex cursor-pointer items-center gap-3 rounded-xl border-[1.5px] border-dashed border-line-strong bg-sunken px-4 py-3 transition-colors hover:border-accent">
        <FilePlus2 size={18} className="shrink-0 text-accent" />
        <span className={`truncate text-sm ${file ? 'text-ink' : 'text-faint'}`}>{file ? file.name : 'Choose a PDF'}</span>
        <input
          type="file"
          accept="application/pdf"
          className="sr-only"
          onChange={(e) => onPick(e.target.files?.[0] ?? null)}
        />
      </span>
    </label>
  )
}
