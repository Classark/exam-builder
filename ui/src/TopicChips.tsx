import { useEffect, useMemo, useRef, useState } from 'react'
import { Link2, Pencil } from 'lucide-react'
import { api } from './api'
import { toLayout } from './pinchZoom'
import type { PartTags, Topic } from './types'

const pathLabel = (path: string) => (path ? path.split('.').map((p) => `(${p})`).join('') : 'the question')

/** A part's topics and what it can't be set without, shown as chips beside it; click the
 * pencil to correct them. A correction is saved as a manual tag, which tagging never replaces. */
export function TopicChips({
  paperKey,
  questionNumber,
  path,
  earlierPaths,
  tags,
  topics,
  onSaved,
}: {
  paperKey: string
  questionNumber: number
  path: string
  earlierPaths: string[]
  tags: PartTags | null
  topics: Topic[]
  onSaved: () => void
}) {
  // where the editor opens: fixed to the viewport, since the part list scrolls and would clip it
  const [open, setOpen] = useState<{ top: number; left: number } | null>(null)
  const names = useMemo(() => new Map(topics.map((t) => [t.id, t.name])), [topics])
  const deps = tags?.depends_on ?? []

  return (
    <span className="relative flex min-w-0 flex-wrap items-center gap-1">
      {tags?.topic_ids.map((id) => (
        <span key={id} className="pill bg-info-wash text-info" title={id}>
          {names.get(id) ?? id}
        </span>
      ))}
      {deps.length > 0 && (
        <span className="pill bg-warning-wash text-warning" title={tags?.reason || 'Can only be set together with these parts'}>
          <Link2 size={11} /> Needs {deps.map(pathLabel).join(', ')}
        </span>
      )}
      {!tags && topics.length > 0 && <span className="text-xs text-faint">Not tagged</span>}
      {topics.length > 0 && (
        <button
          type="button"
          className="rounded-md p-1 text-faint transition-colors hover:bg-accent-wash hover:text-accent"
          onClick={(e) => {
            e.preventDefault()
            // in #root's own coordinates, which differ from the screen's while pinch-magnified
            const rect = e.currentTarget.getBoundingClientRect()
            const r = { ...toLayout(rect.left, rect.top), bottom: toLayout(0, rect.bottom).y }
            const view = { top: toLayout(0, 0), bottom: toLayout(window.innerWidth, window.innerHeight) }
            const height = 420
            const top = r.bottom + height + 8 > view.bottom.y ? Math.max(view.top.y + 8, r.y - height - 4) : r.bottom + 4
            setOpen({ top, left: Math.min(r.x, view.bottom.x - 336) })
          }}
          aria-label="Edit topics"
          title="Edit topics"
        >
          <Pencil size={12} />
        </button>
      )}
      {open && (
        <TagEditor
          at={open}
          topics={topics}
          earlierPaths={earlierPaths}
          initial={tags}
          onClose={() => setOpen(null)}
          onSave={async (topicIds, dependsOn) => {
            await api.savePartTags(paperKey, { question_number: questionNumber, path, topic_ids: topicIds, depends_on: dependsOn })
            setOpen(null)
            onSaved()
          }}
        />
      )}
    </span>
  )
}

function TagEditor({
  at,
  topics,
  earlierPaths,
  initial,
  onClose,
  onSave,
}: {
  at: { top: number; left: number }
  topics: Topic[]
  earlierPaths: string[]
  initial: PartTags | null
  onClose: () => void
  onSave: (topicIds: string[], dependsOn: string[]) => Promise<void>
}) {
  const [chosen, setChosen] = useState<string[]>(initial?.topic_ids ?? [])
  const [deps, setDeps] = useState<string[]>(initial?.depends_on ?? [])
  const [filter, setFilter] = useState('')
  const [saving, setSaving] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onDown = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && onClose()
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const needle = filter.trim().toLowerCase()
  const shown = topics.filter((t) => !needle || `${t.id} ${t.name}`.toLowerCase().includes(needle))
  const toggle = (list: string[], id: string) => (list.includes(id) ? list.filter((x) => x !== id) : [...list, id])

  return (
    <div
      ref={box}
      className="card fixed z-50 w-80 p-3 shadow-float"
      style={{ top: at.top, left: at.left }}
      onClick={(e) => e.preventDefault()}
      role="dialog"
      aria-label="Edit topics"
    >
      <input
        autoFocus
        className="field py-1.5 text-sm"
        placeholder="Find a topic…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <ul className="scroll-quiet mt-2 max-h-56 overflow-auto">
        {shown.map((t) => (
          <li key={t.id}>
            <label
              className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1 text-sm hover:bg-hover"
              style={{ paddingLeft: t.parent ? 22 : 8 }}
            >
              <input
                type="checkbox"
                className="size-3.5 accent-[var(--accent)]"
                checked={chosen.includes(t.id)}
                onChange={() => setChosen((c) => toggle(c, t.id))}
              />
              <span className="w-9 shrink-0 text-xs text-muted">{t.id}</span>
              <span className={t.parent ? '' : 'font-medium'}>{t.name}</span>
            </label>
          </li>
        ))}
      </ul>
      {earlierPaths.length > 0 && (
        <div className="mt-3 border-t border-line pt-2">
          <div className="text-xs text-ink-2">Can’t be set without</div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {earlierPaths.map((p) => (
              <label key={p} className="flex cursor-pointer items-center gap-1 text-sm">
                <input
                  type="checkbox"
                  className="size-3.5 accent-[var(--accent)]"
                  checked={deps.includes(p)}
                  onChange={() => setDeps((d) => toggle(d, p))}
                />
                {pathLabel(p)}
              </label>
            ))}
          </div>
        </div>
      )}
      <div className="mt-3 flex justify-end gap-2">
        <button type="button" className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="btn-primary px-3 py-1.5"
          disabled={saving}
          onClick={async () => {
            setSaving(true)
            try {
              await onSave(chosen, deps)
            } finally {
              setSaving(false)
            }
          }}
        >
          Save
        </button>
      </div>
    </div>
  )
}
