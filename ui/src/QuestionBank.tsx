import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, ArrowDown, ArrowUp, Download, FileStack, FileText, Loader2, Pencil, Plus, Search, Sparkles, Trash2, X } from 'lucide-react'
import { api } from './api'
import { PagePreview } from './PagePreview'
import { Select } from './Select'
import { paperCode, questionKey, sitting } from './bankFolders'
import { useLive } from './live'
import { Modal } from './Modal'
import { snippet } from './issues'
import { QuestionRow, QuestionTree, questionSource } from './QuestionTree'
import { TopicChips } from './TopicChips'
import type { BankPaper, QuestionDetail, QuizDetail, QuizItemRef, QuizSummary, Topic } from './types'

const LETTERS = 'abcdefghijklmnopqrstuvwxyz'
const letterFor = (index: number) => LETTERS[index] ?? `z${index}`
const SEARCH_DEBOUNCE_MS = 250
const MAX_RESULTS_SHOWN = 300
const FOOTER_NOTE_KEY = 'qbx.footerNote'

/** Downloads a URL the way clicking a link to it would. */
function downloadUrl(url: string) {
  const link = document.createElement('a')
  link.href = url
  link.download = ''
  document.body.appendChild(link)
  link.click()
  link.remove()
}

function romanFor(n: number): string {
  const numerals: [number, string][] = [
    [10, 'x'],
    [9, 'ix'],
    [5, 'v'],
    [4, 'iv'],
    [1, 'i'],
  ]
  let out = ''
  for (const [value, symbol] of numerals) {
    while (n >= value) {
      out += symbol
      n -= value
    }
  }
  return out
}

/** Mirrors quiz_export._group_selection: subparts of one container collapse under a single
 * new letter as (i), (ii)…, while each top-level part gets a letter of its own. Returns the
 * output label each selected path will carry, so the picker shows what the export produces. */
/** A part as the paper prints its label: "a.i" -> "(a)(i)", the whole question -> "Whole". */
function partLabel(path: string): string {
  return path ? path.split('.').map((piece) => `(${piece})`).join('') : 'Whole'
}

/** A part's own words. A subpart's text also carries its container's lead-in ("State which
 * symbol represents: (i) an element…"), so it starts after the part's own marker. */
function partText(text: string, path: string, questionNumber: number): string {
  const clean = snippet(text, questionNumber)
  if (!path.includes('.')) return clean // only a subpart carries its container's lead-in
  const own = path.split('.').pop()!
  const at = clean.indexOf(`(${own})`)
  return at > 0 ? clean.slice(at + own.length + 2).trim() : clean
}

function outputLabels(selected: string[]): Record<string, string> {
  const groups: { container: string | null; paths: string[] }[] = []
  const indexByContainer = new Map<string, number>()
  for (const path of selected) {
    if (path.includes('.')) {
      const container = path.slice(0, path.lastIndexOf('.'))
      let index = indexByContainer.get(container)
      if (index === undefined) {
        index = groups.length
        indexByContainer.set(container, index)
        groups.push({ container, paths: [] })
      }
      groups[index].paths.push(path)
    } else {
      groups.push({ container: null, paths: [path] })
    }
  }
  const labels: Record<string, string> = {}
  groups.forEach((group, groupIndex) => {
    const letter = letterFor(groupIndex)
    group.paths.forEach((path, pathIndex) => {
      labels[path] = group.container === null ? `(${letter})` : `(${letter})(${romanFor(pathIndex + 1)})`
    })
  })
  return labels
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

/** A topic and everything under it - picking "4" means all of 4.x. */
function withSubtopics(topics: Topic[], id: string): Set<string> {
  const out = new Set([id])
  let grew = true
  while (grew) {
    grew = false
    for (const t of topics) {
      if (t.parent && out.has(t.parent) && !out.has(t.id)) {
        out.add(t.id)
        grew = true
      }
    }
  }
  return out
}

/** `openExam`: an exam to show, set when the Claude tab hands one over; `onAskClaude`
 * opens the open exam in the Claude tab. */
export function QuestionBank({ openExam, onAskClaude }: { openExam: string | null; onAskClaude: (name: string) => void }) {
  const { version, poke, ai } = useLive()
  const [bank, setBank] = useState<BankPaper[]>([])
  const [bankLoaded, setBankLoaded] = useState(false)
  const [search, setSearch] = useState('')
  const [hits, setHits] = useState<{ paper_key: string; question_number: number }[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const [detail, setDetail] = useState<QuestionDetail | null>(null)
  // the big preview shows the open question, or the whole exam as it will print
  const [view, setView] = useState<'question' | 'paper'>('question')
  const openQuestion = (key: string) => {
    setActiveKey(key)
  }

  const [quizzes, setQuizzes] = useState<QuizSummary[]>([])
  const [quizName, setQuizName] = useState<string | null>(null)
  const [quiz, setQuiz] = useState<QuizDetail | null>(null)
  // The exam's contents, edited locally and saved on every change, so both previews follow
  // a click straight away. Within an item, part order is click order - the output order.
  const [items, setItems] = useState<QuizItemRef[]>([])
  const [naming, setNaming] = useState(false)
  const [renaming, setRenaming] = useState(false)
  // exam questions ticked in the list, to move or remove together
  const [picked, setPicked] = useState<Set<string>>(new Set())
  // questions waiting on the teacher's say-so before they leave the exam
  const [removing, setRemoving] = useState<Set<string> | null>(null)
  // minutes allowed, printed on the cover as e.g. "1 hour 15 minutes" - left off when unset
  const [duration, setDuration] = useState('')
  // the teacher's own line at the right of every page's foot - remembered on this machine,
  // since it's usually the same school or class name exam after exam
  const [footerNote, setFooterNote] = useState(() => {
    try {
      return localStorage.getItem(FOOTER_NOTE_KEY) ?? ''
    } catch {
      return ''
    }
  })
  const [error, setError] = useState('')
  const saveSeq = useRef(0)
  // bumped to reload the open exam when the server changed it (Claude, in the Claude tab)
  const [quizReload, setQuizReload] = useState(0)
  // every subject's topic list, for chips, the topic filter and tag editing
  const [topicsBySubject, setTopicsBySubject] = useState<Record<string, Topic[]>>({})
  const [topicFilter, setTopicFilter] = useState('') // "subject|topic id"

  useEffect(() => {
    let stale = false
    void api
      .listSubjects()
      .then((subjects) =>
        Promise.all(subjects.filter((s) => s.n_topics > 0).map((s) => api.getTopics(s.subject))),
      )
      .then((lists) => !stale && setTopicsBySubject(Object.fromEntries(lists.map((l) => [l.subject, l.topics]))))
      .catch(() => undefined)
    return () => {
      stale = true
    }
  }, [version])

  // The bank's folders, again whenever the library changes - a scan adds papers live.
  useEffect(() => {
    void api
      .getBank()
      .then((b) => {
        setBank(b.papers)
        setBankLoaded(true)
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
  }, [version])

  useEffect(() => {
    const q = search.trim()
    if (q.length < 2) {
      setHits(null)
      setSearching(false)
      return
    }
    setSearching(true)
    let stale = false
    const timer = window.setTimeout(() => {
      void api
        .search(q)
        .then((result) => !stale && setHits(result))
        .finally(() => !stale && setSearching(false))
    }, SEARCH_DEBOUNCE_MS)
    return () => {
      stale = true
      window.clearTimeout(timer)
    }
  }, [search, version])

  const paperByKey = useMemo(() => new Map(bank.map((p) => [p.paper_key, p])), [bank])

  // the folders, narrowed to one topic when a topic filter is chosen
  const shownBank = useMemo(() => {
    if (!topicFilter) return bank
    const [subject, id] = topicFilter.split('|')
    const wanted = withSubtopics(topicsBySubject[subject] ?? [], id)
    return bank
      .filter((p) => p.subject === subject)
      .map((p) => ({ ...p, questions: p.questions.filter((q) => q.topics?.some((t) => wanted.has(t))) }))
      .filter((p) => p.questions.length > 0)
  }, [bank, topicFilter, topicsBySubject])
  const shownKeys = useMemo(
    () => (topicFilter ? new Set(shownBank.flatMap((p) => p.questions.map((q) => questionKey(p.paper_key, q.n)))) : null),
    [shownBank, topicFilter],
  )

  // the open question's parts, with their text - loaded one question at a time
  useEffect(() => {
    setDetail(null)
    if (!activeKey) return
    const [paperKey, n] = activeKey.split('::')
    let stale = false
    void api.getQuestion(paperKey, Number(n)).then((d) => !stale && setDetail(d)).catch(() => undefined)
    return () => {
      stale = true
    }
  }, [activeKey, version])

  const refreshQuizzes = (selectName?: string) => {
    void api.listQuizzes().then((list) => {
      setQuizzes(list)
      if (selectName) setQuizName(selectName)
      else setQuizName((current) => (current && list.some((q) => q.name === current) ? current : (list[0]?.name ?? null)))
    })
  }

  // on first showing, and whenever the Claude tab hands over an exam it made or changed
  useEffect(() => {
    refreshQuizzes(openExam ?? undefined)
    setQuizReload((n) => n + 1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openExam])

  useEffect(() => {
    if (!quizName) {
      setQuiz(null)
      setItems([])
      return
    }
    void api.getQuiz(quizName).then((d) => {
      setQuiz(d)
      setDuration(d.duration_minutes ? String(d.duration_minutes) : '')
      setItems(
        d.items.map((i) => ({
          paper_key: i.paper_key,
          question_number: i.question_number,
          part_paths: i.parts.map((p) => p.part_path),
        })),
      )
    })
  }, [quizName, quizReload])

  // ticks belong to one exam
  useEffect(() => setPicked(new Set()), [quizName])

  const itemIndexByKey = useMemo(
    () => new Map(items.map((item, index) => [questionKey(item.paper_key, item.question_number), index])),
    [items],
  )
  // The paper prints the written questions first, then the multiple-choice ones on pages of
  // their own - so the list is split the same way and numbered as printed.
  const mcqKeys = useMemo(
    () => new Set((quiz?.items ?? []).filter((i) => i.mcq).map((i) => questionKey(i.paper_key, i.question_number))),
    [quiz],
  )
  const itemKey = (item: QuizItemRef) => questionKey(item.paper_key, item.question_number)
  const writtenItems = items.filter((item) => !mcqKeys.has(itemKey(item)))
  const mcqItems = items.filter((item) => mcqKeys.has(itemKey(item)))
  // each section numbers from 1, as the paper does
  const printedNumber = new Map([
    ...writtenItems.map((item, i) => [itemKey(item), i + 1] as const),
    ...mcqItems.map((item, i) => [itemKey(item), i + 1] as const),
  ])
  // outside the sectioned list a bare "Q2" is ambiguous, so the MCQ section says so
  const printedLabel = (key: string) => `${mcqKeys.has(key) ? 'MCQ ' : 'Q'}${printedNumber.get(key)}`
  const active = detail && activeKey === questionKey(detail.paper_key, detail.question_number) ? detail : null
  const activePaper = activeKey ? (paperByKey.get(activeKey.split('::')[0]) ?? null) : null
  const activeIndex = activeKey ? itemIndexByKey.get(activeKey) : undefined
  const activeSelected = activeIndex !== undefined ? items[activeIndex].part_paths : []
  const activeNumber = activeKey ? printedNumber.get(activeKey) : undefined
  // other saved exams that already set this question - a teacher reusing one should know
  const usedIn = activeKey
    ? quizzes.filter((qz) => qz.name !== quizName && qz.questions.includes(activeKey)).map((qz) => qz.name)
    : []

  // An exam draws on one exam board's papers only: while it holds any question, another
  // board's can't be added (the server refuses it too).
  const boardOf = (paperKey: string) => paperByKey.get(paperKey)?.board || 'Cambridge'
  const examBoard = items.length > 0 ? boardOf(items[0].paper_key) : null
  const activeBoard = activePaper ? activePaper.board || 'Cambridge' : null
  const otherBoard = activeIndex === undefined && examBoard !== null && activeBoard !== null && activeBoard !== examBoard

  const saveItems = (next: QuizItemRef[]) => {
    setItems(next)
    if (!quizName) return
    const seq = ++saveSeq.current
    api
      .updateQuiz(quizName, next)
      .then(() => api.getQuiz(quizName))
      .then((d) => {
        if (seq !== saveSeq.current) return
        setQuiz(d)
        setQuizzes((list) =>
          list.map((qz) =>
            qz.name === quizName
              ? { ...qz, n_items: next.length, questions: next.map((i) => questionKey(i.paper_key, i.question_number)) }
              : qz,
          ),
        )
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
  }

  /** With no exam chosen yet - always so on a fresh install - ticking a part starts one. */
  const startExamWith = async (partPaths: string[]) => {
    if (!active) return
    const taken = new Set(quizzes.map((qz) => qz.name))
    let name = 'My exam'
    for (let n = 2; taken.has(name); n++) name = `My exam ${n}`
    try {
      await api.createQuiz(name)
      await api.updateQuiz(name, [{ paper_key: active.paper_key, question_number: active.question_number, part_paths: partPaths }])
      refreshQuizzes(name)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  /** Ticking a question's first part adds it to the exam; clearing its last removes it. */
  const setActiveParts = (partPaths: string[]) => {
    if (!active) return
    if (!quiz) {
      if (partPaths.length > 0) void startExamWith(partPaths)
      return
    }
    if (activeIndex === undefined) {
      if (partPaths.length === 0 || otherBoard) return
      saveItems([...items, { paper_key: active.paper_key, question_number: active.question_number, part_paths: partPaths }])
    } else if (partPaths.length === 0) {
      // clearing its last part takes the question out of the exam - ask first
      setRemoving(new Set([questionKey(active.paper_key, active.question_number)]))
    } else {
      saveItems(items.map((item, i) => (i === activeIndex ? { ...item, part_paths: partPaths } : item)))
    }
  }

  /** Ticking a part also ticks the earlier parts it can't be set without (its tags). */
  const togglePart = (path: string) => {
    if (activeSelected.includes(path)) {
      setActiveParts(activeSelected.filter((p) => p !== path))
      return
    }
    const needs = active?.parts.find((p) => p.path === path)?.tags?.depends_on ?? []
    const missing = needs.filter((d) => !activeSelected.includes(d))
    setActiveParts([...activeSelected, ...missing, path])
  }

  /** `section` with every question in `keys` moved one place up (-1) or down (+1), as a
   * block: a ticked question only swaps with an unticked neighbour, so a run of ticked
   * questions moves together and one already at the end stays put. */
  const shifted = (section: QuizItemRef[], keys: Set<string>, delta: -1 | 1) => {
    const next = [...section]
    const order = delta < 0 ? next.map((_, i) => i) : next.map((_, i) => next.length - 1 - i)
    for (const i of order) {
      const j = i + delta
      if (j < 0 || j >= next.length) continue
      if (keys.has(itemKey(next[i])) && !keys.has(itemKey(next[j]))) [next[i], next[j]] = [next[j], next[i]]
    }
    return next
  }
  // Each section moves on its own: a question never crosses into the other section, since
  // the paper would put it back where it belongs anyway.
  const moved = (keys: Set<string>, delta: -1 | 1) => [
    ...shifted(writtenItems, keys, delta),
    ...shifted(mcqItems, keys, delta),
  ]
  const canMove = (keys: Set<string>, delta: -1 | 1) =>
    moved(keys, delta).some((item, i) => itemKey(item) !== itemKey([...writtenItems, ...mcqItems][i]))
  const moveItems = (keys: Set<string>, delta: -1 | 1) => {
    if (canMove(keys, delta)) saveItems(moved(keys, delta))
  }
  // a row's own arrows move the whole selection when that row is part of it
  const rowKeys = (key: string) => (picked.has(key) ? picked : new Set([key]))
  const togglePicked = (key: string) =>
    setPicked((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  const confirmRemove = () => {
    if (!removing) return
    saveItems(items.filter((item) => !removing.has(itemKey(item))))
    setPicked((current) => new Set([...current].filter((key) => !removing.has(key))))
    setRemoving(null)
  }

  const createQuiz = async (name: string) => {
    try {
      await api.createQuiz(name)
      setNaming(false)
      refreshQuizzes(name)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const renameQuiz = async (newName: string) => {
    if (!quiz) return
    try {
      await api.renameQuiz(quiz.name, newName)
      setRenaming(false)
      refreshQuizzes(newName)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const deleteQuiz = async () => {
    if (!quiz) return
    await api.deleteQuiz(quiz.name)
    setQuizName(null)
    refreshQuizzes()
  }

  const questionPreview = active
    ? {
        items: [
          {
            paper_key: active.paper_key,
            question_number: active.question_number,
            part_paths: activeSelected.length > 0 ? activeSelected : active.parts.map((p) => p.path),
          },
        ],
        cover: false,
        first_number: activeNumber ?? items.length + 1,
      }
    : null

  // saved with the exam a moment after typing stops, so it's there next time it's opened
  const durationTimer = useRef<number | undefined>(undefined)
  const changeDuration = (value: string) => {
    setDuration(value)
    if (!quizName) return
    const name = quizName
    window.clearTimeout(durationTimer.current)
    durationTimer.current = window.setTimeout(() => {
      const n = Math.round(Number(value))
      api
        .setQuizDuration(name, value.trim() && Number.isFinite(n) && n > 0 ? n : null)
        .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
    }, 500)
  }

  const minutes = Number(duration)
  const examPreview =
    quiz && items.length > 0
      ? {
          items,
          title: quiz.name,
          duration_minutes: duration.trim() && minutes > 0 ? Math.round(minutes) : undefined,
          footer_note: footerNote.trim() || undefined,
        }
      : null

  const labels = outputLabels(activeSelected)
  const selectedMarks = active
    ? active.parts.filter((p) => activeSelected.includes(p.path)).reduce((sum, p) => sum + p.marks, 0)
    : 0
  const canExport = quiz !== null && items.length > 0
  const nQuestions = bank.reduce((s, p) => s + p.questions.length, 0)

  // the open question: its parts to tick - in either view, so a question can be added
  // while the whole paper is in the preview
  const questionPicker = activeKey ? (
      <div className="p-4">
        {activePaper && (
          <div className="truncate text-xs text-muted">
            {activePaper.subject_name} {paperCode(activePaper)} · {sitting(activePaper)}
          </div>
        )}
        <div className="flex items-start gap-2">
          <h2 className="text-xl font-semibold">
            Question {activeKey.split('::')[1]}
            {active && <span className="ml-2 text-base font-normal text-muted">{plural(active.total_marks, 'mark')}</span>}
          </h2>
          {view === 'paper' && (
            <button className="btn-ghost ml-auto px-1.5 py-1" onClick={() => setActiveKey(null)} title="Close this question" aria-label="Close this question">
              <X size={15} />
            </button>
          )}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {activeIndex !== undefined ? (
            <span className="pill bg-accent-container text-accent">
              {printedLabel(activeKey)} in exam · {plural(activeSelected.length, 'part')} · {plural(selectedMarks, 'mark')}
            </span>
          ) : (
            <span className="pill bg-sunken text-muted">Not in exam</span>
          )}
          <div className="ml-auto flex items-center gap-1">
            <button
              className="btn-secondary px-3 py-1.5 text-xs"
              disabled={!active || otherBoard}
              onClick={() => active && setActiveParts(active.parts.map((p) => p.path))}
            >
              Select all
            </button>
            <button className="btn-ghost" disabled={!quiz || activeSelected.length === 0} onClick={() => setActiveParts([])}>
              Clear
            </button>
          </div>
        </div>
        {!quiz && active && (
          <p className="mt-2 text-xs text-muted">Tick parts to add them - an exam called “My exam” is started for you.</p>
        )}
        {otherBoard && (
          <div className="mt-2 flex items-start gap-2 rounded-xl bg-warning-wash px-3 py-2 text-xs text-warning">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>
              This exam uses {examBoard} papers, and this question is from {activeBoard}. An exam can't mix
              exam boards - start another exam for it.
            </span>
          </div>
        )}
        {usedIn.length > 0 && (
          <div className="mt-2 flex items-start gap-2 rounded-xl bg-warning-wash px-3 py-2 text-xs text-warning">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>
              <span className="font-semibold">Used before</span> in {usedIn.map((n) => `“${n}”`).join(', ')}.
              {activeIndex === undefined && ' Students who sat that exam may have seen it.'}
            </span>
          </div>
        )}
        <ul className="mt-3 space-y-0.5">
          {!active
            ? [0, 1, 2].map((i) => <li key={i} className="h-8 animate-pulse rounded-lg bg-sunken" />)
            : active.parts.map((part) => {
                const checked = activeSelected.includes(part.path)
                const text = partText(part.text, part.path, active.question_number)
                return (
                  <li
                    key={part.path || '(whole question)'}
                    className={`rounded-xl px-2.5 py-1.5 transition-colors ${checked ? 'bg-accent-wash' : 'hover:bg-hover'}`}
                  >
                    <label className="flex cursor-pointer items-start gap-2.5">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => togglePart(part.path)}
                        className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]"
                      />
                      <span className="w-10 shrink-0 text-sm font-semibold">{partLabel(part.path)}</span>
                      <span className="min-w-0 flex-1 text-sm text-ink-2">
                        {checked && part.path && labels[part.path] !== partLabel(part.path) && (
                          <span className="pill mr-1.5 bg-accent-container text-accent" title="Its label in your exam">
                            as {labels[part.path]}
                          </span>
                        )}
                        <span className="line-clamp-2" title={text}>
                          {text || 'No text'}
                        </span>
                      </span>
                      <span className="shrink-0 whitespace-nowrap text-right text-xs text-muted">{plural(part.marks, 'mark')}</span>
                    </label>
                    {activePaper && topicsBySubject[activePaper.subject] && (
                      <div className="mt-1 pl-[4.5rem]">
                        <TopicChips
                          paperKey={active.paper_key}
                          questionNumber={active.question_number}
                          path={part.path}
                          earlierPaths={active.parts.slice(0, active.parts.indexOf(part)).map((p) => p.path)}
                          tags={part.tags}
                          topics={topicsBySubject[activePaper.subject]}
                          onSaved={poke}
                        />
                      </div>
                    )}
                  </li>
                )
              })}
        </ul>
      </div>
  ) : null

  return (
    <div className="flex h-full min-h-0 gap-4 p-4">
      {/* ── Left: every paper as folders, or search results ── */}
      <aside className="card flex w-[360px] shrink-0 flex-col overflow-hidden">
        <div className="border-b border-line p-3">
          <div className="relative">
            <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={bankLoaded ? `Search ${nQuestions.toLocaleString()} questions…` : 'Search questions…'}
              className="field pl-9 pr-9"
              aria-label="Search every question"
            />
            {searching ? (
              <Loader2 size={15} className="absolute right-3 top-1/2 -translate-y-1/2 animate-spin text-accent" />
            ) : (
              search && (
                <button
                  onClick={() => setSearch('')}
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-1 text-faint hover:text-accent"
                  aria-label="Clear search"
                >
                  <X size={14} />
                </button>
              )
            )}
          </div>
          {Object.keys(topicsBySubject).length > 0 && (
            <Select
              value={topicFilter}
              onChange={setTopicFilter}
              searchable
              className="mt-2 w-full"
              aria-label="Filter by topic"
              options={[
                { value: '', label: 'All topics' },
                ...Object.entries(topicsBySubject).flatMap(([subject, topics]) =>
                  topics.map((t) => ({ value: `${subject}|${t.id}`, label: `${t.id} ${t.name}`, group: subject, indent: !!t.parent })),
                ),
              ]}
            />
          )}
        </div>
        <div className="scroll-quiet min-h-0 flex-1 overflow-auto">
          {!bankLoaded ? (
            <div className="space-y-2 p-3">
              {[0, 1, 2, 3, 4].map((i) => (
                <div key={i} className="h-8 animate-pulse rounded-lg bg-sunken" />
              ))}
            </div>
          ) : hits ? (
            <ul className="p-2">
              <li className="px-2 pb-2 text-xs text-muted">
                {hits.length === 0
                  ? 'No questions match.'
                  : hits.length > MAX_RESULTS_SHOWN
                    ? `Showing the first ${MAX_RESULTS_SHOWN} of ${hits.length}${hits.length === 500 ? '+' : ''} matches - add a word to narrow them.`
                    : plural(hits.length, 'matching question')}
              </li>
              {hits
                .filter((hit) => !shownKeys || shownKeys.has(questionKey(hit.paper_key, hit.question_number)))
                .slice(0, MAX_RESULTS_SHOWN)
                .map((hit) => {
                const paper = paperByKey.get(hit.paper_key)
                const question = paper?.questions.find((q) => q.n === hit.question_number)
                if (!paper || !question) return null
                const key = questionKey(hit.paper_key, hit.question_number)
                return (
                  <QuestionRow
                    key={key}
                    question={question}
                    paperKey={hit.paper_key}
                    active={key === activeKey}
                    inPaper={itemIndexByKey.get(key)}
                    depth={0}
                    source={questionSource(paper)}
                    onSelect={openQuestion}
                  />
                )
              })}
            </ul>
          ) : (
            topicFilter && shownBank.length === 0 ? (
              <p className="p-4 text-sm text-muted">No tagged questions on this topic yet.</p>
            ) : (
              <QuestionTree papers={shownBank} activeKey={activeKey} itemIndexByKey={itemIndexByKey} onSelect={openQuestion} />
            )
          )}
        </div>
      </aside>

      {/* ── Right: the exam bar, then the question (or the exam) beside a big preview ── */}
      <section className="card flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="flex items-center gap-1.5 border-b border-line px-3 py-2 text-xs">
          <Select
            value={quizName ?? ''}
            onChange={(v) => setQuizName(v || null)}
            placeholder="Choose an exam…"
            minWidth={320}
            size="sm"
            className="w-56 min-w-0 shrink font-medium"
            aria-label="Exam"
            options={quizzes.map((qz) => ({ value: qz.name, label: qz.name, hint: plural(qz.n_items, 'question') }))}
          />
          {quiz && (
            <button className="btn-ghost px-1.5 py-1" onClick={() => setRenaming(true)} title="Rename this exam" aria-label="Rename this exam">
              <Pencil size={13} />
            </button>
          )}
          <button className="btn-ghost px-1.5 py-1" onClick={() => setNaming(true)} title="New exam" aria-label="New exam">
            <Plus size={15} />
          </button>
          {quiz && (
            <>
              {ai && (
                <button className="btn-ghost px-1.5 py-1" onClick={() => onAskClaude(quiz.name)} title="Ask Claude to change this exam" aria-label="Ask Claude">
                  <Sparkles size={14} className="text-accent" />
                </button>
              )}
              <button className="btn-ghost px-1.5 py-1 hover:text-danger" onClick={() => void deleteQuiz()} title="Delete this exam" aria-label="Delete this exam">
                <Trash2 size={14} />
              </button>
              <span className="truncate whitespace-nowrap text-muted">
                {plural(items.length, 'question')} · {plural(quiz.total_marks, 'mark')}
              </span>
            </>
          )}
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            <div className="flex rounded-lg bg-sunken p-0.5" role="tablist" aria-label="Preview">
              {(
                [
                  ['question', 'Question', FileText],
                  ['paper', 'Whole paper', FileStack],
                ] as const
              ).map(([value, label, Icon]) => (
                <button
                  key={value}
                  role="tab"
                  aria-selected={view === value}
                  onClick={() => setView(value)}
                  className={`flex items-center gap-1 rounded-md px-2 py-1 text-xs font-semibold transition-colors ${
                    view === value ? 'bg-accent text-white' : 'text-ink-2 hover:text-accent'
                  }`}
                >
                  <Icon size={13} /> {label}
                </button>
              ))}
            </div>
            {quiz && (
              <>
                <a
                  href={canExport ? api.exportQuizQpPdfUrl(quiz.name, duration, footerNote) : undefined}
                  onClick={() => {
                    // the sources list comes with it: where each question was taken from
                    if (canExport) setTimeout(() => downloadUrl(api.exportQuizSourcesPdfUrl(quiz.name, footerNote)), 400)
                  }}
                  aria-disabled={!canExport}
                  title="Downloads with a sources list - where each question was taken from"
                  className={`btn-primary whitespace-nowrap px-2.5 py-1 text-xs ${canExport ? '' : 'pointer-events-none opacity-50'}`}
                >
                  <Download size={13} /> Question paper
                </a>
                <a
                  href={canExport ? api.exportQuizMsPdfUrl(quiz.name, footerNote) : undefined}
                  aria-disabled={!canExport}
                  className={`btn-secondary whitespace-nowrap px-2.5 py-1 text-xs ${canExport ? '' : 'pointer-events-none opacity-50'}`}
                >
                  <Download size={13} /> Mark scheme
                </a>
              </>
            )}
          </div>
        </div>
        {error && <p className="mx-3 mt-2 rounded-lg bg-danger-wash px-3 py-2 text-xs text-danger">{error}</p>}

        <div className="flex min-h-0 flex-1">
          {/* the question's parts to tick, or the exam's questions in order */}
          <div className="scroll-quiet flex w-[440px] shrink-0 flex-col overflow-auto border-r border-line">
            {view === 'question' ? (
              !activeKey ? (
                <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
                  <FileStack size={36} className="text-accent" />
                  <h2 className="text-lg font-semibold">Choose a question</h2>
                  <p className="max-w-sm text-sm text-muted">
                    Open a paper’s folder on the left, or search every question. Tick the parts you want to add them to your exam.
                  </p>
                </div>
              ) : (
                questionPicker
              )
            ) : !quiz ? (
              <p className="p-6 text-center text-sm text-muted">Choose an exam, or start one with New.</p>
            ) : (
              <>
                {activeKey && <div className="border-b border-line">{questionPicker}</div>}
                <div className="space-y-3 p-4">
                <h2 className="font-semibold leading-snug [overflow-wrap:anywhere]">{quiz.name}</h2>
                {items.length === 0 && (
                  <p className="text-sm text-muted">No questions yet - pick one on the left and tick its parts.</p>
                )}
                {picked.size > 0 && (
                  <div className="flex items-center gap-1 rounded-xl bg-accent-container px-2 py-1 text-xs">
                    <span className="flex-1 font-semibold">{picked.size} selected</span>
                    <button className="btn-ghost px-1" onClick={() => moveItems(picked, -1)} disabled={!canMove(picked, -1)} aria-label="Move selected up" title="Move up">
                      <ArrowUp size={14} />
                    </button>
                    <button className="btn-ghost px-1" onClick={() => moveItems(picked, 1)} disabled={!canMove(picked, 1)} aria-label="Move selected down" title="Move down">
                      <ArrowDown size={14} />
                    </button>
                    <button className="btn-ghost px-1 hover:text-danger" onClick={() => setRemoving(new Set(picked))} title="Remove selected from exam">
                      <X size={14} /> Remove
                    </button>
                    <button className="btn-ghost px-1" onClick={() => setPicked(new Set())}>
                      Clear
                    </button>
                  </div>
                )}
                {(
                  [
                    ['Written questions', writtenItems],
                    ['Multiple choice', mcqItems],
                  ] as const
                ).map(
                  ([heading, section]) =>
                    section.length > 0 && (
                      <section key={heading}>
                        <h3 className="px-2 pb-0.5 text-xs font-semibold uppercase tracking-wide text-muted">
                          {heading} · {section.length}
                        </h3>
                        <ul className="space-y-0.5">
                          {section.map((item, index) => {
                            const key = itemKey(item)
                            const p = paperByKey.get(item.paper_key)
                            const resolved = quiz.items.find((i) => questionKey(i.paper_key, i.question_number) === key)
                            return (
                              <li
                                key={key}
                                className={`flex items-center gap-1 rounded-xl px-2 py-1 ${key === activeKey ? 'bg-accent-wash' : 'hover:bg-hover'}`}
                              >
                                <input
                                  type="checkbox"
                                  checked={picked.has(key)}
                                  onChange={() => togglePicked(key)}
                                  aria-label={`Select ${printedLabel(key)}`}
                                  className="size-4 shrink-0 accent-[var(--accent)]"
                                />
                                <button
                                  onClick={() => openQuestion(key)}
                                  className="min-w-0 flex-1 truncate text-left text-sm"
                                  title="Open this question"
                                >
                                  <span className="font-semibold">Q{printedNumber.get(key)}</span>{' '}
                                  <span className="text-ink-2">
                                    {p ? paperCode(p) : item.paper_key} Q{item.question_number} · {plural(item.part_paths.length, 'part')}
                                  </span>
                                  {resolved?.missing && <span className="text-danger"> · missing from its paper</span>}
                                </button>
                                <button
                                  className="btn-ghost px-1"
                                  onClick={() => moveItems(rowKeys(key), -1)}
                                  disabled={index === 0 || !canMove(rowKeys(key), -1)}
                                  aria-label="Move up"
                                >
                                  <ArrowUp size={14} />
                                </button>
                                <button
                                  className="btn-ghost px-1"
                                  onClick={() => moveItems(rowKeys(key), 1)}
                                  disabled={index === section.length - 1 || !canMove(rowKeys(key), 1)}
                                  aria-label="Move down"
                                >
                                  <ArrowDown size={14} />
                                </button>
                                <button
                                  className="btn-ghost px-1 hover:text-danger"
                                  onClick={() => setRemoving(new Set([key]))}
                                  aria-label="Remove from exam"
                                >
                                  <X size={14} />
                                </button>
                              </li>
                            )
                          })}
                        </ul>
                      </section>
                    ),
                )}
                {items.length > 0 && (
                  <p className="text-xs text-muted">
                    Multiple-choice questions always come last in the paper, after the written questions, and
                    start on a new page with their answer sheet just before them. Tick questions to move or
                    remove several at once; each stays in its own section.
                  </p>
                )}
                <label className="flex items-center gap-2 text-xs text-ink-2">
                  Time allowed
                  <input
                    type="number"
                    min={1}
                    value={duration}
                    onChange={(e) => changeDuration(e.target.value)}
                    placeholder="min"
                    className="field w-20 px-2 py-1.5 text-center"
                  />
                  <span className="text-muted">minutes, printed on the cover and saved with the exam</span>
                </label>
                <label className="flex items-center gap-2 text-xs text-ink-2">
                  Footer text
                  <input
                    type="text"
                    value={footerNote}
                    onChange={(e) => {
                      setFooterNote(e.target.value)
                      try {
                        localStorage.setItem(FOOTER_NOTE_KEY, e.target.value)
                      } catch {
                        // storage blocked - the note still applies to this session
                      }
                    }}
                    maxLength={120}
                    placeholder="e.g. school or class name"
                    className="field min-w-0 flex-1 px-2 py-1.5"
                  />
                </label>
                {canExport && (
                  <p className="text-xs text-muted">
                    The question paper downloads with a{' '}
                    <a className="text-accent underline" href={api.exportQuizSourcesPdfUrl(quiz.name, footerNote)}>
                      sources list
                    </a>{' '}
                    - where each question was taken from.
                  </p>
                )}
              </div>
              </>
            )}
          </div>

          {/* the big preview: this question as it will print, or the whole exam */}
          <div className="scroll-quiet min-h-0 min-w-0 flex-1 overflow-auto bg-sunken p-6">
            <div className="mx-auto max-w-4xl">
              {view === 'question' ? (
                activeKey ? (
                  <>
                    {active && activeSelected.length === 0 && (
                      <p className="mb-3 text-center text-xs text-muted">Nothing ticked - showing the whole question.</p>
                    )}
                    <PagePreview request={questionPreview} empty="" />
                  </>
                ) : (
                  <p className="p-4 text-center text-sm text-muted">The question you pick shows here as it will print.</p>
                )
              ) : !quiz ? (
                <p className="p-4 text-center text-sm text-muted">Choose an exam, or start one with New.</p>
              ) : (
                <PagePreview request={examPreview} empty="Tick parts of a question to add it to this exam." />
              )}
            </div>
          </div>
        </div>
      </section>

      {naming && <NewExam taken={quizzes.map((q) => q.name)} onClose={() => setNaming(false)} onCreate={createQuiz} />}
      {removing && (
        <Modal title={removing.size === 1 ? 'Remove this question?' : `Remove ${removing.size} questions?`} onClose={() => setRemoving(null)} width="max-w-sm">
          <div className="space-y-4">
            <ul className="space-y-0.5 text-sm">
              {items
                .filter((item) => removing.has(itemKey(item)))
                .map((item) => {
                  const p = paperByKey.get(item.paper_key)
                  return (
                    <li key={itemKey(item)}>
                      <span className="font-semibold">{printedLabel(itemKey(item))}</span>{' '}
                      <span className="text-ink-2">
                        {p ? paperCode(p) : item.paper_key} Q{item.question_number}
                      </span>
                    </li>
                  )
                })}
            </ul>
            <p className="text-sm text-ink-2">
              {removing.size === 1 ? 'It' : 'They'} will be taken out of “{quiz?.name}”. You can add{' '}
              {removing.size === 1 ? 'it' : 'them'} back from the bank.
            </p>
            <div className="flex justify-end gap-2">
              <button type="button" className="btn-secondary" onClick={() => setRemoving(null)}>
                Cancel
              </button>
              <button className="btn-primary" autoFocus onClick={confirmRemove}>
                Remove
              </button>
            </div>
          </div>
        </Modal>
      )}
      {renaming && quiz && (
        <NewExam
          current={quiz.name}
          taken={quizzes.map((q) => q.name).filter((n) => n !== quiz.name)}
          onClose={() => setRenaming(false)}
          onCreate={(name) => void renameQuiz(name)}
        />
      )}
    </div>
  )
}

/** Names a new exam, or - given the `current` name - renames that one. */
function NewExam({
  current,
  taken,
  onClose,
  onCreate,
}: {
  current?: string
  taken: string[]
  onClose: () => void
  onCreate: (name: string) => void
}) {
  const [name, setName] = useState(current ?? '')
  const clash = taken.includes(name.trim())
  const unchanged = current !== undefined && name.trim() === current
  return (
    <Modal title={current === undefined ? 'New exam' : 'Rename exam'} onClose={onClose} width="max-w-sm">
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (name.trim() && !clash && !unchanged) onCreate(name.trim())
        }}
        className="space-y-4"
      >
        <label className="block">
          <span className="mb-1.5 block text-sm font-semibold">Name</span>
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Year 10 mock, Paper 4" className="field" />
          {clash && <span className="mt-1 block text-xs text-danger">An exam with this name already exists.</span>}
        </label>
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" disabled={!name.trim() || clash || unchanged}>
            {current === undefined ? 'Create exam' : 'Rename'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
