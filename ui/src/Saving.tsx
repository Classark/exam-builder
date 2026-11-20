import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react'
import { Check, Download, ExternalLink, FileText, FolderOpen, FolderInput, History, Loader2, X } from 'lucide-react'
import { api, errorMessage } from './api'
import { Modal } from './Modal'
import { AboutSection, AppearanceSection } from './Appearance'
import type { SavedFile, SaveResult, SavingState } from './types'

/* Saving what a teacher makes into their own folder (qbx/saving.py, user-decided): no
 * browser download bar. A save shows a notice with Open and Show in folder; the top bar's
 * Saved files button lists the latest; the folder is chosen once after sign-in. */

// -- the notice after a save, shown by <SaveNotice /> wherever the save came from --

type Notice = { result: SaveResult } | { error: string }
let notice: Notice | null = null
const listeners = new Set<() => void>()
const tell = (next: Notice | null) => {
  notice = next
  listeners.forEach((l) => l())
}
const subscribe = (l: () => void) => {
  listeners.add(l)
  return () => void listeners.delete(l)
}

/** Runs a save and tells the teacher where it went (or why it didn't). */
export async function runSave(save: () => Promise<SaveResult>): Promise<SaveResult | null> {
  try {
    const result = await save()
    tell({ result })
    return result
  } catch (e) {
    tell({ error: errorMessage(e) })
    return null
  }
}

const lastPart = (path: string) => path.split(/[\\/]/).filter(Boolean).pop() ?? path

export function SaveNotice() {
  const current = useSyncExternalStore(subscribe, () => notice)
  useEffect(() => {
    if (!current) return
    const t = setTimeout(() => tell(null), 'error' in current ? 12000 : 9000)
    return () => clearTimeout(t)
  }, [current])
  if (!current) return null
  return (
    <div role="status" className="fixed right-4 bottom-4 z-50 w-[380px] max-w-[calc(100vw-2rem)] border border-line bg-card p-3 text-sm shadow-lg">
      <button className="absolute top-2 right-2 text-muted hover:text-ink" aria-label="Close" onClick={() => tell(null)}>
        <X size={14} />
      </button>
      {'error' in current ? (
        <p className="pr-5 text-danger">{current.error}</p>
      ) : (
        <>
          <p className="flex items-center gap-2 pr-5 font-medium">
            <Check size={15} className="text-success" />
            Saved {current.result.files.length === 1 ? current.result.files[0].name : `${current.result.files.length} files`}
          </p>
          <p className="mt-1 text-xs break-all text-muted">in {current.result.folder}</p>
          {current.result.archived && (
            <p className="mt-1 flex items-start gap-1 text-xs text-ink-2">
              <History size={13} className="mt-px shrink-0" />
              The last version moved to Old versions\{current.result.archived}
            </p>
          )}
          <div className="mt-2 flex gap-2">
            {current.result.files.length === 1 && (
              <button className="btn-secondary px-2.5 py-1 text-xs" onClick={() => void api.openSaved(current.result.files[0].path)}>
                <FileText size={13} /> Open
              </button>
            )}
            <button
              className="btn-secondary px-2.5 py-1 text-xs"
              onClick={() => void api.showSaved(current.result.files[0]?.path ?? current.result.folder)}
            >
              <FolderOpen size={13} /> Show in folder
            </button>
          </div>
        </>
      )}
    </div>
  )
}

/** A button that saves (instead of a download link): spinner while it runs. */
export function SaveButton({
  save,
  disabled,
  className,
  title,
  children,
  iconSize = 13,
}: {
  save: () => Promise<SaveResult>
  disabled?: boolean
  className: string
  title?: string
  children: ReactNode
  iconSize?: number
}) {
  const [busy, setBusy] = useState(false)
  return (
    <button
      className={className}
      disabled={disabled || busy}
      title={title}
      onClick={async () => {
        setBusy(true)
        await runSave(save)
        setBusy(false)
      }}
    >
      {busy ? <Loader2 size={iconSize} className="animate-spin" /> : <Download size={iconSize} />}
      {children}
    </button>
  )
}

// -- the save folder ----------------------------------------------------------------------

/** The folder, with Change… (the window's own folder dialog, or a typed location in a
 * browser) and Open folder. `onChosen` after a change is saved. */
export function SaveFolderField({ onChosen, autoFocus }: { onChosen?: (s: SavingState) => void; autoFocus?: boolean }) {
  const [state, setState] = useState<SavingState | null>(null)
  const [typed, setTyped] = useState('')
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    void api.getSaving().then((s) => {
      setState(s)
      setTyped(s.folder)
    })
  }, [])

  const choose = async (folder: string) => {
    setError(null)
    try {
      const next = await api.setSaveFolder(folder)
      setState(next)
      setTyped(next.folder)
      onChosen?.(next)
    } catch (e) {
      setError(errorMessage(e))
    }
  }

  if (!state) return <Loader2 size={16} className="animate-spin text-muted" />
  return (
    <div className="space-y-2">
      {state.can_pick ? (
        <div className="field flex items-center gap-2 px-3 py-2 text-sm break-all">
          <FolderInput size={15} className="shrink-0 text-accent" />
          {state.folder}
        </div>
      ) : (
        <input
          className="field w-full px-3 py-2 text-sm"
          value={typed}
          autoFocus={autoFocus}
          onChange={(e) => setTyped(e.target.value)}
          aria-label="Save folder"
        />
      )}
      <div className="flex flex-wrap gap-2">
        {state.can_pick ? (
          <button
            className="btn-secondary px-3 py-1.5 text-xs"
            onClick={async () => {
              const picked = await api.pickSaveFolder()
              if (picked.folder) await choose(picked.folder)
            }}
          >
            <FolderInput size={13} /> Change…
          </button>
        ) : (
          typed.trim() !== state.folder && (
            <button className="btn-secondary px-3 py-1.5 text-xs" onClick={() => void choose(typed)}>
              <Check size={13} /> Use this folder
            </button>
          )
        )}
        <button className="btn-ghost px-3 py-1.5 text-xs" onClick={() => void api.showSaved(state.folder).catch((e: unknown) => setError(errorMessage(e)))}>
          <FolderOpen size={13} /> Open folder
        </button>
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  )
}

/** Asked once, after sign-in (user-decided): where exams are saved. */
export function SaveFolderPrompt({ onDone }: { onDone: () => void }) {
  const [state, setState] = useState<SavingState | null>(null)
  useEffect(() => void api.getSaving().then(setState), [])
  const confirm = async () => {
    if (state) await api.setSaveFolder(state.folder).catch(() => {})
    onDone()
  }
  return (
    <Modal title="Where should your exams be saved?" onClose={() => void confirm()}>
      <div className="space-y-4 text-sm">
        <p className="text-ink-2">
          Each exam gets its own folder here, with its question paper, mark scheme and sources list. When you change an exam
          and save it again, the earlier version is kept in an Old versions folder. You can change this later in Settings.
        </p>
        <SaveFolderField autoFocus onChosen={setState} />
        <div className="flex justify-end">
          <button className="btn-primary" onClick={() => void confirm()}>
            <Check size={15} /> Continue
          </button>
        </div>
      </div>
    </Modal>
  )
}

/** Release builds' Settings: the save folder is the only setting (user-decided). */
export function SaveFolderSettings({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="Settings" onClose={onClose}>
      <div className="space-y-7">
        <AppearanceSection />
        <section className="space-y-2">
          <h3 className="font-semibold">Save folder</h3>
          <p className="text-sm text-ink-2">Exams, mark schemes and topic booklets are saved here, each exam in its own folder.</p>
          <SaveFolderField />
        </section>
        <AboutSection />
      </div>
    </Modal>
  )
}

// -- the top bar's Saved files list -------------------------------------------------------

const when = (seconds: number) =>
  new Date(seconds * 1000).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })

export function SavedFiles({ onClose }: { onClose: () => void }) {
  const [files, setFiles] = useState<SavedFile[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => void api.savedFiles().then(setFiles, (e: unknown) => setError(errorMessage(e))), [])
  const act = (run: () => Promise<unknown>) => void run().catch((e: unknown) => setError(errorMessage(e)))
  return (
    <Modal title="Saved files" onClose={onClose} width="max-w-2xl">
      <div className="space-y-3 text-sm">
        {error && <p className="text-danger">{error}</p>}
        {files === null ? (
          <Loader2 size={16} className="animate-spin text-muted" />
        ) : files.length === 0 ? (
          <p className="text-muted">Nothing saved yet. Exams you save appear here.</p>
        ) : (
          <ul className="divide-y divide-line border border-line">
            {files.map((f) => (
              <li key={f.path} className="flex items-center gap-3 px-3 py-2">
                <FileText size={16} className={`shrink-0 ${f.exists ? 'text-accent' : 'text-muted'}`} />
                <div className="min-w-0 flex-1">
                  <div className={`truncate font-medium ${f.exists ? '' : 'text-muted line-through'}`} title={f.path}>
                    {f.name}
                  </div>
                  <div className="truncate text-xs text-muted">
                    {f.exists ? `${lastPart(f.path.slice(0, -f.name.length))} · ${when(f.saved_at)}` : 'Moved or deleted'}
                  </div>
                </div>
                {f.exists && (
                  <>
                    <button className="btn-ghost px-2 py-1 text-xs" onClick={() => act(() => api.openSaved(f.path))}>
                      <ExternalLink size={13} /> Open
                    </button>
                    <button className="btn-ghost px-2 py-1 text-xs" onClick={() => act(() => api.showSaved(f.path))}>
                      <FolderOpen size={13} /> Show in folder
                    </button>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Modal>
  )
}
