import { useEffect, useRef, useState } from 'react'
import { ChevronDown, ClipboardList, FilePlus2, FolderOpen, FolderSearch, Loader2, RefreshCw, Tags } from 'lucide-react'
import { Activity } from './activity'
import { api, errorMessage } from './api'
import { useLive } from './live'
import { Modal } from './Modal'
import type { ScanState } from './types'

/** The papers folder and its relatives, in the top bar. The app scans the folder by itself
 * (main._watch_papers); a scan's progress shows here, and each paper appears in the lists
 * as soon as it's done. */
export function ScanControl() {
  const { scan, startScan, poke } = useLive()
  const [menu, setMenu] = useState(false)
  const [confirmReprocess, setConfirmReprocess] = useState(false)
  const [adding, setAdding] = useState(false)
  const [summary, setSummary] = useState<ScanState | null>(null)
  const [error, setError] = useState('')
  const wasRunning = useRef(false)
  const asked = useRef(false) // a scan started from the menu: report it even if it found nothing
  const menuRef = useRef<HTMLDivElement>(null)

  // When a scan this session watched finishes, say what it did.
  // Scans start by themselves, so one says nothing unless it extracted or failed something.
  useEffect(() => {
    if (wasRunning.current && !scan.running) {
      if (asked.current || (scan.processed?.length ?? 0) + (scan.failed?.length ?? 0) > 0) setSummary(scan)
      asked.current = false
    }
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
      asked.current = true
      await startScan(reprocess)
    } catch (e) {
      setError(errorMessage(e))
    }
  }

  const total = scan.total ?? 0
  const done = scan.done ?? 0

  return (
    <>
      <Activity>
        {scan.running && (
          <div
            className="flex items-center gap-2.5 h-8 rounded-xl border border-white/15 bg-white/10 px-3 text-sm text-on-inverse"
          >
            <Loader2 size={16} className="animate-spin text-accent" />
            <span className="font-medium">
              {total > 0 ? `Extracting ${Math.min(done + 1, total)} of ${total}` : (scan.stage ?? 'Starting')}
            </span>
            {scan.current && <span className="max-w-44 truncate text-white/60">{scan.current}</span>}
          </div>
        )}
        {error && <span className="text-sm text-[#ffb4a2]">{error}</span>}
      </Activity>
      {/* no Scan button (user-asked): the app watches the papers folder and scans by itself
          when PDFs are added or moved (main._watch_papers) */}
      <div ref={menuRef} className="relative flex">
        <button
          onClick={() => api.openPapersFolder().catch((e) => setError(errorMessage(e)))}
          className="flex items-center gap-2 rounded-l-xl border border-white/15 bg-white/10 px-3.5 py-2 text-sm font-medium text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
          title="Open the papers folder - PDFs copied in are extracted by themselves"
          aria-label="Open papers folder"
        >
          <FolderOpen size={16} />
          {/* labels only where the top bar has room for them */}
          <span className="hidden 2xl:inline">Open papers folder</span>
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
            <MenuItem
              icon={FilePlus2}
              title="Add one paper"
              detail="Choose a question paper and its mark scheme."
              onClick={() => {
                setMenu(false)
                setAdding(true)
              }}
            />
            <MenuItem
              icon={ClipboardList}
              title="Last scan's results"
              detail="What the last scan extracted, what failed, and the files that couldn't be paired."
              onClick={() => {
                setMenu(false)
                api
                  .lastScan()
                  .then(setSummary)
                  .catch((e) => setError(errorMessage(e)))
              }}
            />
            <MenuItem
              icon={FolderSearch}
              title="Check the papers folder now"
              detail="It's checked by itself every few seconds - this lists what's in it and what couldn't be paired."
              onClick={() => void start(false)}
            />
          </div>
        )}
      </div>

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
  const { ai, untaggedPapers, tagging, startTagging, papers } = useLive()
  const missingInserts = papers.filter((p) => p.needs_insert && !p.has_insert)
  const [tagError, setTagError] = useState('')
  const processed = scan.processed ?? []
  const failed = scan.failed ?? []
  const unpaired = scan.unpaired ?? []
  const clean = processed.filter((p) => p.passed).length
  const nothing = processed.length + failed.length + unpaired.length === 0

  return (
    <Modal title={nothing ? 'Nothing new to extract' : 'Scan finished'} onClose={onClose}>
      {scan.finished_at && (
        <p className="-mt-2 mb-3 text-xs text-muted">
          {nothing ? 'Checked' : 'Finished'} {new Date(scan.finished_at * 1000).toLocaleString()}
          {scan.checked_at && scan.checked_at - scan.finished_at > 60
            ? ` · folder last checked ${new Date(scan.checked_at * 1000).toLocaleString()}`
            : ''}
        </p>
      )}
      {nothing ? (
        <p className="text-sm text-ink-2">
          Every paper in <span className="break-all text-ink">{scan.folder}</span> is already extracted. Copy new PDFs
          into that folder and they're extracted by themselves.
        </p>
      ) : (
        <div className="space-y-5 text-sm">
          <div className="grid grid-cols-3 gap-3">
            <Stat value={processed.length} label="extracted" />
            <Stat value={clean} label="clean" tone="success" />
            <Stat value={processed.length - clean} label="to review" tone="accent" />
          </div>
          {scan.error && <p className="rounded-lg bg-danger-wash px-3 py-2 text-danger">{scan.error}</p>}
          {/* tagging waits for the teacher (user-decided): offered here, right after processing */}
          {ai && untaggedPapers > 0 && !tagging.running && processed.length > 0 && (
            <section className="rounded-2xl border border-accent/30 bg-accent-wash px-4 py-3">
              <h3 className="flex items-center gap-2 font-medium text-ink">
                <Tags size={15} className="text-accent" />
                {untaggedPapers} {untaggedPapers === 1 ? 'paper needs' : 'papers need'} topic tags
              </h3>
              <p className="mt-1 text-xs text-ink-2">
                Claude reads each question's text and mark scheme answer and gives every part its syllabus topics, for
                topic booklets and Claude-built exams. Only the text is sent, never the PDFs, and it uses your Classark
                credit.
              </p>
              {tagError && <p className="mt-1 text-xs text-danger">{tagError}</p>}
              <div className="mt-2 flex justify-end gap-2">
                <button className="btn-secondary px-3 py-1 text-xs" onClick={onClose}>
                  Not now
                </button>
                <button
                  className="btn-primary px-3 py-1 text-xs"
                  onClick={() => {
                    setTagError('')
                    startTagging()
                      .then(onClose)
                      .catch((e: unknown) => setTagError(errorMessage(e)))
                  }}
                >
                  Tag them now
                </button>
              </div>
            </section>
          )}
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
          {missingInserts.length > 0 && (
            <section className="rounded-2xl border border-warning-border bg-warning-wash px-4 py-3">
              <h3 className="font-medium text-ink">
                {missingInserts.length} {missingInserts.length === 1 ? 'paper is' : 'papers are'} missing an insert
              </h3>
              <p className="mt-1 text-xs text-ink-2">
                Their covers say they come with an insert (the case study the questions are about). Copy the insert PDFs
                into the papers folder. Until then, these papers' questions print without it.
              </p>
              <p className="mt-1.5 break-words text-xs text-ink">{missingInserts.map((p) => p.paper_key).join(', ')}</p>
            </section>
          )}
          {unpaired.length > 0 && <LeftAlone files={unpaired} />}
        </div>
      )}
    </Modal>
  )
}

/** Why a file was left alone, in groups, each with what to do about it. Matched on
 * scan.py's reasons; anything unrecognised falls into "couldn't identify". */
const LEFT_ALONE_GROUPS: { id: string; test: RegExp; title: string; explain: string }[] = [
  {
    id: 'partner',
    test: /isn't in the folder/,
    title: 'Waiting for its other half',
    explain:
      'A question paper needs its mark scheme, and the other way round; an insert needs its question paper. Copy the missing file into the papers folder and they are paired by themselves - any file name will do.',
  },
  {
    id: 'copy',
    test: /second copy/,
    title: 'Duplicates',
    explain: 'The same paper is in the folder twice. The other copy is used, so you can delete this one or leave it.',
  },
  {
    id: 'other',
    test: /examiner report/,
    title: 'Not a question paper or mark scheme',
    explain: 'Examiner reports aren’t needed. They’re skipped on purpose - nothing to do.',
  },
  {
    id: 'scanned',
    test: /scanned PDF/,
    title: 'Scanned copies',
    explain:
      'These are pictures of pages, with no text the app can read. Find a digital copy (one where you can select the text) and put it in the folder instead.',
  },
  {
    id: 'open',
    test: /couldn't be opened/,
    title: 'Couldn’t be opened',
    explain: 'The file may be damaged, password-protected or still downloading. Try downloading it again.',
  },
  {
    id: 'unknown',
    test: /.*/,
    title: 'Couldn’t tell what it is',
    explain:
      'The app couldn’t read from its cover which paper it is, or whether it’s a question paper or a mark scheme. If it is one, use “Add one paper” and choose the question paper and mark scheme yourself.',
  },
]

function LeftAlone({ files }: { files: { file: string; reason: string }[] }) {
  const groups = LEFT_ALONE_GROUPS.map((g) => ({
    ...g,
    files: files.filter((f) => LEFT_ALONE_GROUPS.find((x) => x.test.test(f.reason)) === g),
  })).filter((g) => g.files.length > 0)
  return (
    <section>
      <h3 className="font-medium">
        {files.length} {files.length === 1 ? 'file' : 'files'} not extracted
      </h3>
      <p className="mb-2 text-xs text-muted">
        The app reads each PDF’s cover to find which paper it is and pair it with its other half. These couldn’t be
        paired, so they were left as they are - nothing was changed or deleted.
      </p>
      <div className="space-y-2">
        {groups.map((g) => (
          <details key={g.id} className="rounded-xl border border-line bg-sunken px-3 py-2" open={groups.length === 1}>
            <summary className="cursor-pointer font-medium text-ink">
              {g.title} <span className="font-normal text-muted">({g.files.length})</span>
            </summary>
            <p className="mt-1 text-xs text-ink-2">{g.explain}</p>
            <ul className="mt-1.5 space-y-0.5 text-xs">
              {g.files.map((u) => (
                <li key={u.file}>
                  <span className="break-all text-ink">{u.file}</span>
                  <span className="text-muted"> - {u.reason}</span>
                </li>
              ))}
            </ul>
          </details>
        ))}
      </div>
    </section>
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
  const [insert, setInsert] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async () => {
    if (!qp || !ms) return
    setBusy(true)
    setError('')
    try {
      const result = await api.uploadPaper(qp, ms, insert)
      onAdded(result.paper_key)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Add one paper" onClose={onClose}>
      <div className="space-y-4">
        <FilePick label="Question paper" file={qp} onPick={setQp} />
        <FilePick label="Mark scheme" file={ms} onPick={setMs} />
        <FilePick label="Insert (optional)" file={insert} onPick={setInsert} />
        <p className="-mt-2 text-xs text-muted">
          Only for papers that come with one, such as a Business Studies case study. It prints before the paper's
          questions in an exam.
        </p>
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
