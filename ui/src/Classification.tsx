import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, Download, Layers, Loader2 } from 'lucide-react'
import { api } from './api'
import { Select } from './Select'
import { useSourcePicker, type PickedSources } from './SourcePicker'
import type { ClassifiedPreview, ClassifiedRequest, ClassifiedState, ClassifiedTopic, Topic } from './types'

/** Classification mode: classified (topical) past-paper booklets. Every past question on a
 * topic, gathered into a question booklet and mark scheme per topic, from the topic tags the
 * questions already carry - no Claude, no marks target, and questions used in other exams
 * are fine (engine/qbx/classified.py). */

type Style = ClassifiedRequest['question_style']

interface Remembered extends PickedSources {
  topic_ids: string[]
  year_from: string
  year_to: string
  style: Style
}

const STORE_KEY = 'qbx.classified'
const PREVIEW_DEBOUNCE_MS = 300
const POLL_MS = 1000

function readRemembered(): Remembered | null {
  try {
    const raw = localStorage.getItem(STORE_KEY)
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<Remembered>
    return {
      board: typeof value.board === 'string' ? value.board : '',
      sources: Array.isArray(value.sources) ? value.sources : [],
      topic_ids: Array.isArray(value.topic_ids) ? value.topic_ids : [],
      year_from: typeof value.year_from === 'string' ? value.year_from : '',
      year_to: typeof value.year_to === 'string' ? value.year_to : '',
      style: value.style === 'mcq' || value.style === 'structured' ? value.style : 'any',
    }
  } catch {
    return null
  }
}

function remember(value: Remembered) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(value))
  } catch {
    // remembering the last picks is a convenience only
  }
}

/** Downloads a URL the way clicking a link to it would. */
function downloadUrl(url: string) {
  const link = document.createElement('a')
  link.href = url
  link.download = ''
  document.body.appendChild(link)
  link.click()
  link.remove()
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`
const year = (text: string) => (/^\d{4}$/.test(text.trim()) ? Number(text.trim()) : null)
const itemKey = (topicId: string, paperKey: string, n: number) => `${topicId}|${paperKey}|${n}`

/** How deep a topic sits under its parents, for indenting the list. */
function depths(topics: Topic[]): Map<string, number> {
  const parent = new Map(topics.map((t) => [t.id, t.parent]))
  const out = new Map<string, number>()
  for (const t of topics) {
    let depth = 0
    for (let p = t.parent; p && depth < 6; p = parent.get(p) ?? null) depth += 1
    out.set(t.id, depth)
  }
  return out
}

export function Classification() {
  const remembered = useMemo(readRemembered, [])
  const [job, setJob] = useState<ClassifiedState | null>(null)
  const making = !!job?.running
  const { board, sources, picker } = useSourcePicker(making, {
    initial: remembered ?? undefined,
    requireSubject: true,
  })
  const subject = sources[0]?.subject ?? ''

  const [topics, setTopics] = useState<Topic[]>([])
  const [topicsFor, setTopicsFor] = useState('')
  const [picked, setPicked] = useState<string[]>(remembered?.topic_ids ?? [])
  const [yearFrom, setYearFrom] = useState(remembered?.year_from ?? '')
  const [yearTo, setYearTo] = useState(remembered?.year_to ?? '')
  const [style, setStyle] = useState<Style>(remembered?.style ?? 'any')
  const [preview, setPreview] = useState<ClassifiedPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [excluded, setExcluded] = useState<Set<string>>(new Set())
  const startedId = useRef<number | null>(null)

  // the topic list of the first subject picked - the one topics are chosen from
  useEffect(() => {
    if (!subject) {
      setTopics([])
      setTopicsFor('')
      return
    }
    let live = true
    api
      .getTopics(subject)
      .then((list) => {
        if (!live) return
        setTopics(list.topics)
        setTopicsFor(subject)
      })
      .catch(() => live && setTopics([]))
    return () => {
      live = false
    }
  }, [subject])

  // topics another subject's list doesn't have are dropped once this subject's list is in
  const pickedHere = useMemo(
    () => (topicsFor === subject ? picked.filter((id) => topics.some((t) => t.id === id)) : []),
    [picked, topics, topicsFor, subject],
  )

  const request: ClassifiedRequest = useMemo(
    () => ({
      board,
      sources,
      topic_ids: pickedHere,
      year_from: year(yearFrom),
      year_to: year(yearTo),
      question_style: style,
    }),
    // sources is rebuilt every render: compare it by value
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [board, JSON.stringify(sources), pickedHere, yearFrom, yearTo, style],
  )

  useEffect(() => {
    if (!subject || topicsFor !== subject) return // not yet loaded: keep the remembered topics
    remember({ board, sources, topic_ids: pickedHere, year_from: yearFrom, year_to: yearTo, style })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request])

  // the preview follows the picks, a moment after the last change
  useEffect(() => {
    if (!request.sources.length) {
      setPreview(null)
      return
    }
    let live = true
    const timer = window.setTimeout(() => {
      setLoading(true)
      api
        .classifiedPreview(request)
        .then((p) => {
          if (!live) return
          setPreview(p)
          setError(null)
        })
        .catch((e: Error) => live && setError(e.message))
        .finally(() => live && setLoading(false))
    }, PREVIEW_DEBOUNCE_MS)
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [request])

  // a run already going (started before switching here) shows its progress
  useEffect(() => {
    api
      .getState()
      .then((s) => s.classified?.running && setJob(s.classified))
      .catch(() => undefined)
  }, [])

  // while booklets are being made: follow progress, then download them
  useEffect(() => {
    if (!job?.running) return
    const timer = window.setTimeout(() => {
      api
        .getState()
        .then((s) => {
          const next = s.classified ?? { running: false }
          setJob(next)
          if (!next.running && next.ready && next.id !== undefined && next.id === startedId.current) {
            startedId.current = null
            downloadUrl(api.classifiedDownloadUrl(next.id))
          }
        })
        .catch(() => setJob({ ...job })) // backend busy: try again
    }, POLL_MS)
    return () => window.clearTimeout(timer)
  }, [job])

  const included = (t: ClassifiedTopic) => t.items.filter((i) => !excluded.has(itemKey(t.topic_id, i.paper_key, i.question_number)))
  const toMake = (preview?.topics ?? []).filter((t) => included(t).length > 0)

  const makeBooklets = async () => {
    if (!preview) return
    setError(null)
    const left = preview.topics.flatMap((t) =>
      t.items
        .filter((i) => excluded.has(itemKey(t.topic_id, i.paper_key, i.question_number)))
        .map((i) => ({ topic_id: t.topic_id, paper_key: i.paper_key, question_number: i.question_number })),
    )
    try {
      const state = await api.classifiedExport({ ...request, excluded: left })
      startedId.current = state.id ?? null
      setJob(state)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const toggleItem = (key: string) =>
    setExcluded((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  const toggleTopic = (id: string) =>
    setPicked((current) => (current.includes(id) ? current.filter((t) => t !== id) : [...pickedHere, id]))

  const depth = useMemo(() => depths(topics), [topics])

  return (
    <div className="flex h-full min-h-0 gap-4 p-4">
      {/* ── Left: what the booklets draw on ── */}
      <aside className="card flex w-[320px] shrink-0 flex-col overflow-hidden">
        <header className="flex items-center gap-2 border-b border-line px-4 py-3">
          <Layers size={16} className="shrink-0 text-accent" />
          <h2 className="font-semibold">Topic booklets</h2>
        </header>
        <div className="scroll-quiet min-h-0 flex-1 space-y-4 overflow-auto p-4">
          <div>
            <div className="mb-1 text-xs text-muted">Papers</div>
            {picker}
          </div>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-xs text-muted">
              From year
              <input
                className="field mt-1 px-2 py-1.5 text-xs"
                inputMode="numeric"
                placeholder="Any"
                value={yearFrom}
                disabled={making}
                onChange={(e) => setYearFrom(e.target.value.replace(/\D/g, '').slice(0, 4))}
              />
            </label>
            <label className="text-xs text-muted">
              To year
              <input
                className="field mt-1 px-2 py-1.5 text-xs"
                inputMode="numeric"
                placeholder="Any"
                value={yearTo}
                disabled={making}
                onChange={(e) => setYearTo(e.target.value.replace(/\D/g, '').slice(0, 4))}
              />
            </label>
          </div>
          <div>
            <div className="mb-1 text-xs text-muted">Questions</div>
            <Select
              value={style}
              size="sm"
              className="w-full"
              disabled={making}
              onChange={(v) => setStyle(v as Style)}
              aria-label="Question style"
              options={[
                { value: 'any', label: 'Any question' },
                { value: 'structured', label: 'Written questions only' },
                { value: 'mcq', label: 'Multiple choice only' },
              ]}
            />
          </div>
          <div>
            <div className="mb-1 flex items-baseline justify-between text-xs text-muted">
              <span>Topics - one booklet each</span>
              {pickedHere.length > 0 && (
                <button className="btn-ghost px-1 py-0 text-xs" disabled={making} onClick={() => setPicked([])}>
                  Clear
                </button>
              )}
            </div>
            {!subject ? (
              <p className="text-xs text-faint">Choose a subject first.</p>
            ) : topics.length === 0 ? (
              <p className="text-xs text-faint">
                {subject} has no topic list yet. Add one in Settings, then tag its questions.
              </p>
            ) : (
              <ul className="space-y-0.5" aria-label="Topics">
                {topics.map((t) => (
                  <li key={t.id}>
                    <label
                      className="flex cursor-pointer items-start gap-2 rounded-lg px-2 py-1 text-sm hover:bg-hover"
                      style={{ paddingLeft: `${0.5 + (depth.get(t.id) ?? 0) * 1.1}rem` }}
                      title={t.parent ? undefined : 'Includes all of its subtopics'}
                    >
                      <input
                        type="checkbox"
                        className="mt-0.5 size-3.5 shrink-0 accent-[var(--accent)]"
                        checked={pickedHere.includes(t.id)}
                        disabled={making}
                        onChange={() => toggleTopic(t.id)}
                      />
                      <span className="min-w-0">
                        <span className="mr-1.5 text-xs text-muted">{t.id}</span>
                        {t.name}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </aside>

      {/* ── Right: what each booklet will hold ── */}
      <section className="card flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3">
          <h2 className="min-w-0 flex-1 truncate font-semibold">
            {toMake.length ? `${plural(toMake.length, 'booklet')}, each with its mark scheme` : 'Preview'}
          </h2>
          {loading && <Loader2 size={15} className="animate-spin text-muted" />}
          <button
            className="btn-primary px-3 py-1.5 text-sm"
            disabled={making || toMake.length === 0}
            onClick={() => void makeBooklets()}
            title="A zip of each topic's question booklet and mark scheme"
          >
            <Download size={15} /> Download
          </button>
        </header>

        {making && job && (
          <div className="border-b border-line px-5 py-3 text-sm">
            <div className="flex items-center gap-2 text-ink-2">
              <Loader2 size={14} className="animate-spin text-accent" />
              Making booklets{job.current ? ` - ${job.current}` : '…'}
              <span className="ml-auto text-xs text-muted">
                {job.done ?? 0} of {job.total ?? 0}
              </span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-sunken">
              <div
                className="h-full bg-accent transition-all"
                style={{ width: `${job.total ? Math.round(((job.done ?? 0) / job.total) * 100) : 0}%` }}
              />
            </div>
          </div>
        )}

        <div className="scroll-quiet min-h-0 flex-1 space-y-4 overflow-auto bg-sunken p-5">
          {(error || job?.error) && (
            <p className="rounded-lg bg-danger-wash px-3 py-2 text-xs text-danger">{error || job?.error}</p>
          )}
          {preview && preview.untagged_parts > 0 && (
            <p className="flex items-start gap-2 rounded-lg bg-warning-wash px-3 py-2 text-xs text-warning">
              <AlertTriangle size={14} className="mt-px shrink-0" />
              <span>
                {preview.untagged_parts} of {preview.n_parts} parts in these papers have no topic tags, so they
                can't be put under a topic. Tag them from Settings to include them.
              </span>
            </p>
          )}
          {!preview?.topics.length && !error && (
            <div className="mx-auto max-w-md pt-10 text-center">
              <Layers size={28} className="mx-auto text-accent" />
              <h3 className="mt-3 text-lg font-semibold">Every past question on a topic, in one booklet</h3>
              <p className="mt-1 text-sm text-ink-2">
                Pick the papers and one or more topics. Each topic gets its own question booklet and mark scheme,
                from the topic tags your questions already have. A question reprinted in several papers is
                printed once, from the newest.
              </p>
            </div>
          )}
          {preview?.topics.map((t) => {
            const kept = included(t)
            return (
              <article key={t.topic_id} className="card overflow-hidden">
                <header className="flex items-baseline gap-2 border-b border-line px-4 py-2.5">
                  <span className="text-xs text-muted">{t.topic_id}</span>
                  <h3 className="min-w-0 flex-1 truncate font-semibold">{t.name}</h3>
                  <span className="shrink-0 text-xs text-muted">
                    {plural(kept.length, 'question')} · {plural(kept.reduce((sum, i) => sum + i.marks, 0), 'mark')}
                  </span>
                </header>
                {t.items.length === 0 ? (
                  <p className="px-4 py-3 text-xs text-faint">No tagged questions on this topic in these papers.</p>
                ) : (
                  <ul className="divide-y divide-line">
                    {t.items.map((i) => {
                      const key = itemKey(t.topic_id, i.paper_key, i.question_number)
                      return (
                        <li key={key}>
                          <label className="flex cursor-pointer items-start gap-3 px-4 py-1.5 text-sm hover:bg-hover">
                            <input
                              type="checkbox"
                              className="mt-1 size-3.5 shrink-0 accent-[var(--accent)]"
                              checked={!excluded.has(key)}
                              disabled={making}
                              onChange={() => toggleItem(key)}
                              aria-label={`Include ${i.paper} ${i.sitting} question ${i.question_number}`}
                            />
                            <span className={`min-w-0 flex-1 ${excluded.has(key) ? 'text-faint line-through' : ''}`}>
                              <span className="font-medium">{i.paper}</span>{' '}
                              <span className="text-ink-2">{i.sitting}</span> · Q{i.question_number}
                              {i.parts !== 'All' && <span className="text-ink-2"> {i.parts}</span>}
                              {i.mcq && <span className="pill ml-2 bg-info-wash text-info">MCQ</span>}
                              {i.also_in.length > 0 && (
                                <span className="block text-xs text-muted">Also in {i.also_in.join(', ')}</span>
                              )}
                            </span>
                            <span className="shrink-0 text-xs text-muted">{plural(i.marks, 'mark')}</span>
                          </label>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </article>
            )
          })}
        </div>
      </section>
    </div>
  )
}
