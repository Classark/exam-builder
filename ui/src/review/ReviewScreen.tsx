import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { FileText, HelpCircle, NotebookPen, RefreshCw, Trash2, Undo2, X } from 'lucide-react'
import { api, errorMessage } from '../api'
import { comparePapers, paperCode, sitting } from '../bankFolders'
import { pathLabel } from '../issues'
import { useLive } from '../live'
import { PagePane, type PaneBox, type PaneEditing } from '../PagePane'
import { PaperPicker } from '../PaperPicker'
import type { BBox, FixStep, MarkSchemeEntry, Paper, QuestionPart, Report } from '../types'
import { CheckColumn, type CheckActions } from './CheckColumn'
import { DeletePaperDialog, EmptyLibrary, ReviewGuide } from './dialogs'
import { buildModel, firstToCheck, partName, qpPageCount, rowLabel, type QuestionView } from './model'
import { PaperView } from './PaperView'
import { QuestionList, type View } from './QuestionStrip'

type Tool = 'adjust' | 'draw' | 'split' | 'link-draw' | 'add-question'

const PAPER_KEY = 'qbx.review.paper'
const SNAP_KEY = 'qbx.review.snap'
const GUIDE_KEY = 'qbx.review.guideSeen'

function stored(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}
function store(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // a convenience only
  }
}

function isTyping(target: EventTarget | null) {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement
}

interface Toast {
  text: string
  undo?: () => Promise<unknown>
  error?: boolean
}

/** The Reviewer, question by question (user-chosen): a collapsible list of questions, the question
 * paper and mark scheme with every part and row boxed and labelled, and between them each
 * part beside its mark scheme answer, problems written where they are. */
export function ReviewScreen() {
  const { papers, loaded, version, poke } = useLive()
  const [paperKey, setPaperKey] = useState<string | null>(() => stored(PAPER_KEY))
  const [paper, setPaper] = useState<Paper | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [msRows, setMsRows] = useState<Record<string, BBox[]>>({})
  const [msPageCount, setMsPageCount] = useState(0)
  const [view, setView] = useState<View | null>(null)
  // one document widened over the other, for redrawing regions on a page that's easier to see
  const [expanded, setExpanded] = useState<'qp' | 'ms' | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [tool, setTool] = useState<Tool>('adjust')
  // a tool that works on the hidden document brings it forward
  useEffect(() => {
    if (tool === 'link-draw') setExpanded((x) => (x === 'qp' ? 'ms' : x))
    else if (tool !== 'adjust') setExpanded((x) => (x === 'ms' ? 'qp' : x))
  }, [tool])
  // boxes being drawn: round a part's answer on the scheme, or round a missing question
  const [draft, setDraft] = useState<BBox[]>([])
  // a scheme row clicked: what to do with it
  const [rowMenu, setRowMenu] = useState<string | null>(null)
  const [toast, setToast] = useState<Toast | null>(null)
  const [guide, setGuide] = useState(() => stored(GUIDE_KEY) !== 'yes')
  const [deleting, setDeleting] = useState(false)
  const [reprocessing, setReprocessing] = useState<string | null>(null)
  const [snap, setSnap] = useState(() => stored(SNAP_KEY) !== 'off')

  // ── which paper ──────────────────────────────────────────────────────────────
  const sorted = useMemo(() => [...papers].sort(comparePapers), [papers])
  useEffect(() => {
    if (!loaded || sorted.length === 0) return
    if (paperKey && sorted.some((p) => p.paper_key === paperKey)) return
    setPaperKey((sorted.find((p) => p.n_open > 0) ?? sorted[0]).paper_key)
  }, [loaded, sorted, paperKey])

  const pick = (key: string) => {
    setPaperKey(key)
    store(PAPER_KEY, key)
  }
  const movePaper = (by: number) => {
    const queue = sorted.filter((p) => p.n_open > 0 || p.paper_key === paperKey)
    const next = queue[queue.findIndex((p) => p.paper_key === paperKey) + by]
    if (next) pick(next.paper_key)
  }
  useEffect(() => {
    setView(null)
    setSelectedId(null)
    setReprocessing(null)
    setTool('adjust')
    setRowMenu(null)
  }, [paperKey])

  // ── its data, again whenever anything changes ────────────────────────────────
  const request = useRef(0)
  const reload = useCallback(async (key: string) => {
    const seq = ++request.current
    const [p, r] = await Promise.all([api.getPaper(key), api.getReport(key)])
    if (seq !== request.current) return
    setPaper(p)
    setReport(r)
  }, [])
  useEffect(() => {
    if (paperKey) void reload(paperKey).catch(() => setPaper(null))
  }, [paperKey, version, reload])
  useEffect(() => {
    if (!paperKey) return
    void api
      .getMsRows(paperKey)
      .then((r) => {
        setMsRows(r.rows)
        setMsPageCount(r.page_count)
      })
      .catch(() => undefined)
  }, [paperKey, version])

  const ready = !!paper && !!report && paper.paper_key === paperKey
  const model = useMemo(() => (ready ? buildModel(paper, report) : null), [ready, paper, report])

  // ── which question, which part ───────────────────────────────────────────────
  useEffect(() => {
    if (!model) return
    if (view === 'paper' || (view !== null && model.questions.some((q) => q.number === view))) return
    setView(firstToCheck(model))
  }, [model, view])
  const current: QuestionView | null = model && typeof view === 'number' ? (model.questions.find((q) => q.number === view) ?? null) : null
  useEffect(() => {
    if (!current) return
    if (current.parts.some((p) => p.part.id === selectedId)) return
    // the part to look at first: one with a problem, else the one an unlinked row was run into
    const flagged = current.parts.find((p) => p.issues.some((i) => !i.checked))?.part
    const near = current.unmatched.find((u) => u.issue && !u.issue.checked && u.near.length)?.near[0]
    setSelectedId((flagged ?? near ?? current.parts[0]?.part)?.id ?? null)
  }, [current, selectedId])
  const selected = current?.parts.find((p) => p.part.id === selectedId) ?? null
  // a tool chosen together with a part (Split (f)(i) from an unlinked row) survives the
  // reset that selecting a part does
  const nextTool = useRef<Tool | null>(null)
  // the unlinked row a split is for: the lower half becomes its part
  const [splitEntry, setSplitEntry] = useState<MarkSchemeEntry | null>(null)
  useEffect(() => {
    setTool(nextTool.current ?? 'adjust')
    if (!nextTool.current) setSplitEntry(null)
    nextTool.current = null
    setDraft([])
    setRowMenu(null)
  }, [view, selectedId])

  const openView = (v: View) => {
    setView(v)
  }

  // ── edits: each one saved, then offered back as Undo ─────────────────────────
  const after = async () => {
    if (paperKey) await reload(paperKey)
    poke()
  }
  const run = async (work: () => Promise<unknown>, text: string, undo?: (result: unknown) => (() => Promise<unknown>) | undefined) => {
    try {
      const result = await work()
      setToast({ text, undo: undo?.(result) })
      await after()
    } catch (e) {
      setToast({ text: errorMessage(e), error: true })
    }
  }
  // the fix an edit made, taken back out
  const undoFix = (result: unknown) => {
    const id = (result as { fix_id?: string } | null)?.fix_id
    return id && paperKey ? () => api.fixAction(paperKey, id, 'undo') : undefined
  }
  const edit = (steps: FixStep[], text: string) => run(() => api.editPaper(paperKey!, steps), text, undoFix)

  const check = (keys: string[]) =>
    run(
      () => api.reviewIssues(paperKey!, keys, true),
      keys.length === 1 ? 'Marked checked' : `${keys.length} things marked checked`,
      () => () => api.reviewIssues(paperKey!, keys, false),
    )

  const name = (part: QuestionPart) => partName(Number(part.question_id), part.path)

  const actions: CheckActions = {
    select: (id) => setSelectedId(id),
    setMarks: (part, marks) =>
      void run(() => api.updatePart(paperKey!, part.id, { marks }), `${name(part)} set to ${marks} ${marks === 1 ? 'mark' : 'marks'}`, undoFix),
    setTotal: (number, total) =>
      void run(() => api.setQuestionTotal(paperKey!, number, total), `Q${number}'s total set to ${total}`, undoFix),
    link: (part, entry) => void edit([{ op: 'link', part_id: part.id, entry_id: entry.id }], `Linked ${rowLabel(entry)} to ${name(part)}`),
    unlink: (part, entry) =>
      void (entry.manual
        ? run(() => api.unlinkMarkScheme(paperKey!, part.id), `Removed the answer drawn for ${name(part)}`)
        : edit([{ op: 'unlink', part_id: part.id, entry_id: entry.id }], `Unlinked ${rowLabel(entry)} from ${name(part)}`)),
    deleteEntry: (entry) => void edit([{ op: 'delete_entry', entry_id: entry.id }], `${rowLabel(entry)} is no longer a mark scheme row`),
    deletePart: (part) => void edit([{ op: 'delete_part', part_id: part.id }], `${name(part)} is no longer a part`),
    withdraw: (number) =>
      void edit([{ op: 'withdraw_question', question_number: number }], `Q${number} marked as removed by the board`),
    rename: (part, path) =>
      void run(() => api.updatePart(paperKey!, part.id, { path }), `Renamed to ${partName(Number(part.question_id), path)}`, (r) => {
        setSelectedId(`${part.question_id}.${path}`)
        return undoFix(r)
      }),
    merge: (part) => void run(() => api.mergePart(paperKey!, part.id), `Merged ${name(part)} with the next part`, undoFix),
    tool: (t) => {
      setDraft([])
      setRowMenu(null)
      setSplitEntry(null)
      setTool(t)
    },
    splitFor: (part, entry) => {
      setSplitEntry(entry)
      if (part.id === selectedId) setTool('split')
      else {
        nextTool.current = 'split'
        setSelectedId(part.id)
      }
    },
    check: (keys) => void check(keys),
    looksRight: () => {
      if (!current || !model) return
      const keys = current.issues.filter((i) => !i.checked).map((i) => i.key)
      const next = model.questions.find((q) => q.number > current.number && q.open > 0) ?? model.questions.find((q) => q.open > 0 && q !== current)
      void check(keys).then(() => {
        if (next) setView(next.number)
        else if (model.paperOpen) setView('paper')
      })
    },
  }

  const saveRegion = async (segments: BBox[]) => {
    if (!selected) return
    let boxes = segments
    if (snap) boxes = (await api.snapRegion(paperKey!, segments).catch(() => ({ segments }))).segments
    setTool('adjust')
    await run(() => api.updatePart(paperKey!, selected.part.id, { segments: boxes }), `${name(selected.part)}'s box saved`, undoFix)
  }
  const split = (page: number, y: number) => {
    if (!selected) return
    setTool('adjust')
    const entry = splitEntry
    setSplitEntry(null)
    if (entry) {
      const own = partName(Number(selected.part.question_id), entry.part_path)
      void edit(
        [
          { op: 'split_part', part_id: selected.part.id, at: { page, y }, path: entry.part_path, value: entry.marks },
          { op: 'link', part_id: `${selected.part.id}-split`, entry_id: entry.id },
        ],
        `Split ${own} out of ${name(selected.part)} and linked it to ${rowLabel(entry)}`,
      )
      return
    }
    void run(() => api.splitPart(paperKey!, selected.part.id, { page, y }), `Split ${name(selected.part)} in two - check both parts' marks`, undoFix)
  }
  const saveLinkDraw = () => {
    if (!selected || !draft.length) return
    const part = selected.part
    setTool('adjust')
    void run(() => api.linkMarkScheme(paperKey!, part.id, draft), `Linked the drawn answer to ${name(part)}`, () => () =>
      api.unlinkMarkScheme(paperKey!, part.id),
    )
  }
  const saveAddQuestion = () => {
    if (!current || !draft.length) return
    const number = current.number
    const marks = current.unmatched.reduce((s, u) => s + u.entry.marks, 0)
    setTool('adjust')
    void edit(
      [
        { op: 'add_question', question_number: number, value: marks, fields: { segments: draft } },
        ...current.unmatched.map((u) => ({ op: 'link', part_id: `${number}.`, entry_id: u.entry.id })),
      ],
      `Added Question ${number}, linked to its ${current.unmatched.length} mark scheme ${current.unmatched.length === 1 ? 'row' : 'rows'}`,
    )
  }

  const reprocess = async () => {
    if (!paperKey) return
    setReprocessing('')
    try {
      const r = await api.reprocessPaper(paperKey)
      setReprocessing(r.passed ? 'Re-processed: nothing to check' : 'Re-processed')
      await after()
    } catch (e) {
      setReprocessing(errorMessage(e))
    }
  }


  // ── keys ──────────────────────────────────────────────────────────────────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey) return
      if (document.querySelector('[role="dialog"]')) return
      const views: View[] = model ? ['paper', ...model.questions.map((q) => q.number)] : []
      const at = views.indexOf(view ?? 'paper')
      const parts = current?.parts ?? []
      const partAt = parts.findIndex((p) => p.part.id === selectedId)
      const k = e.key
      const nextPart = parts[partAt + (k === 'k' || k === 'ArrowUp' ? -1 : 1)]
      const nextView = views[at + (k === '[' ? -1 : 1)]
      if (k === 'j' || k === 'ArrowDown' || k === 'k' || k === 'ArrowUp') {
        if (nextPart) setSelectedId(nextPart.part.id)
      } else if (k === ']' || k === '[') {
        if (nextView !== undefined) openView(nextView)
      } else if (k === '}') movePaper(1)
      else if (k === '{') movePaper(-1)
      else if (k === 'Enter' && current && current.open > 0 && tool === 'adjust') actions.looksRight()
      else if (k === 'd' && selected) setTool((t) => (t === 'draw' ? 'adjust' : 'draw'))
      else if (k === 's' && selected) setTool((t) => (t === 'split' ? 'adjust' : 'split'))
      else if (k === 'm' && selected && partAt < parts.length - 1) actions.merge(selected.part)
      else if (k === 'f') setExpanded((x) => (x ? null : 'qp'))
      else if (k === 'Escape') {
        if (tool === 'adjust' && !rowMenu && draft.length === 0) setExpanded(null)
        setTool('adjust')
        setDraft([])
        setRowMenu(null)
      } else return
      e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  useEffect(() => {
    if (!toast || toast.undo) return
    const t = window.setTimeout(() => setToast((x) => (x === toast ? null : x)), 3000)
    return () => window.clearTimeout(t)
  }, [toast])
  // an Undo stays offered 15s, and as long as the pointer is on it
  const [toastHeld, setToastHeld] = useState(false)
  useEffect(() => {
    if (!toast) setToastHeld(false)
  }, [toast])
  useEffect(() => {
    if (!toast?.undo || toastHeld) return
    const t = window.setTimeout(() => setToast((x) => (x === toast ? null : x)), 15000)
    return () => window.clearTimeout(t)
  }, [toast, toastHeld])

  if (loaded && papers.length === 0) return <EmptyLibrary />
  const summary = papers.find((p) => p.paper_key === paperKey) ?? null

  // ── what the two papers show ─────────────────────────────────────────────────
  const n = current?.number
  const q = current?.question ?? null
  const qpPages = paper ? qpPageCount(paper) : 0
  const qpBoxes: PaneBox[] = q
    ? [
        ...(q.stem_segments.length ? [{ id: 'stem', label: `Q${n}`, tone: 'plain' as const, segments: q.stem_segments }] : []),
        ...current!.parts
          .filter((p) => p.part.id !== selectedId && p.part.segments.length)
          .map((p) => ({
            id: p.part.id,
            label: p.part.path ? pathLabel(p.part.path).trim() : `Q${n}`,
            tone: p.issues.some((i) => !i.checked) ? ('problem' as const) : ('plain' as const),
            segments: p.part.segments,
            title: `${partName(n!, p.part.path)} - click to select it`,
          })),
      ]
    : []
  const addingQuestion = tool === 'add-question'
  const qpSegments = addingQuestion ? draft : (selected?.part.segments ?? [])
  const qpWhole = view === 'paper' || !q || addingQuestion || tool === 'draw' || (qpSegments.length === 0 && qpBoxes.length === 0)
  const qpEditing: PaneEditing | undefined = addingQuestion
    ? { mode: 'draw', onChange: setDraft, onSplit: () => undefined }
    : selected
      ? { mode: tool === 'draw' ? 'draw' : tool === 'split' ? 'split' : 'adjust', onChange: (s) => void saveRegion(s), onSplit: split }
      : undefined
  const selectedName = selected ? (selected.part.path ? pathLabel(selected.part.path).trim() : `Q${n}`) : undefined

  const questionRows: MarkSchemeEntry[] = current
    ? [...current.parts.flatMap((p) => p.entries), ...current.unmatched.map((u) => u.entry)].filter(
        (e, i, all) => all.findIndex((x) => x.id === e.id) === i,
      )
    : []
  const unmatchedIds = new Set(current?.unmatched.map((u) => u.entry.id))
  const selectedRowIds = new Set(selected?.entries.map((e) => e.id))
  const linkedTo = (entry: MarkSchemeEntry) =>
    paper ? paper.joins.filter((j) => j.mark_scheme_entry_id === entry.id).map((j) => j.part_id) : []
  const msBoxes: PaneBox[] = questionRows
    .filter((e) => !selectedRowIds.has(e.id) && (msRows[e.id]?.length ?? 0) > 0)
    .map((e) => ({
      id: e.id,
      label: rowLabel(e),
      tone: unmatchedIds.has(e.id) ? ('problem' as const) : ('plain' as const),
      segments: msRows[e.id],
      title: unmatchedIds.has(e.id) ? `${rowLabel(e)}: not linked to any part - click to link it` : `${rowLabel(e)} - click to link or unlink`,
    }))
  const drawingAnswer = tool === 'link-draw'
  const msSegments = drawingAnswer ? draft : (selected?.entries.flatMap((e) => msRows[e.id] ?? []) ?? [])
  const msWhole = view === 'paper' || drawingAnswer || (msSegments.length === 0 && msBoxes.length === 0)
  const menuEntry = rowMenu && paper ? (paper.mark_scheme.find((e) => e.id === rowMenu) ?? null) : null

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 p-4">
      {guide && (
        <ReviewGuide
          onClose={() => {
            setGuide(false)
            store(GUIDE_KEY, 'yes')
          }}
        />
      )}
      {deleting && summary && (
        <DeletePaperDialog
          paperKey={summary.paper_key}
          title={`${paperCode(summary)} · ${sitting(summary)}`}
          onClose={() => setDeleting(false)}
          onDeleted={() => {
            setDeleting(false)
            setPaperKey(null)
            poke()
          }}
        />
      )}

      {/* ── the paper, and what's left to check on it ── */}
      <header className="flex flex-wrap items-center gap-3">
        <div className="w-[340px]">
          <PaperPicker papers={papers} current={summary} onPick={pick} />
        </div>
        {model && (
          <p className="text-sm text-ink-2">
            {model.toCheck === 0 ? (
              <span className="text-success">Nothing to check on this paper.</span>
            ) : (
              <>
                <b className="text-ink">{model.toCheck}</b> {model.toCheck === 1 ? 'place' : 'places'} to check - work through
                the ⚠ questions on the left.
              </>
            )}
          </p>
        )}
        {reprocessing !== null && (
          <span className="text-xs text-muted" role="status">
            {reprocessing === '' ? 'Re-processing…' : reprocessing}
          </span>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          <IconButton onClick={() => setGuide(true)} label="How reviewing works">
            <HelpCircle size={16} />
          </IconButton>
          <IconButton
            onClick={() => void reprocess()}
            disabled={reprocessing === ''}
            label="Re-process this paper: read it again from its PDFs (your changes are kept)"
          >
            <RefreshCw size={16} className={reprocessing === '' ? 'animate-spin' : ''} />
          </IconButton>
          <IconButton onClick={() => setDeleting(true)} label="Delete this paper from the bank" danger>
            <Trash2 size={16} />
          </IconButton>
        </div>
      </header>

      <div className="flex min-h-0 flex-1 gap-3">
        {/* ── the paper's questions ── */}
        {model && <QuestionList model={model} view={view ?? 'paper'} onView={openView} />}

        {/* ── question paper ── */}
        <div className={`card min-w-0 flex-1 overflow-hidden ${expanded === 'ms' ? 'hidden' : 'flex'}`}>
          {paperKey && (
            <PagePane
              title="Question paper"
              expanded={expanded === 'qp'}
              onExpand={() => setExpanded((x) => (x === 'qp' ? null : 'qp'))}
              icon={<FileText size={16} />}
              zoomKey="qbx.zoom.qp"
              pageUrl={(page) => api.pageImageUrl(paperKey, page)}
              segments={qpSegments}
              selectedLabel={addingQuestion ? `Q${n}` : selectedName}
              boxes={addingQuestion ? [] : qpBoxes}
              onBoxClick={(id) => id !== 'stem' && setSelectedId(id)}
              allPages={qpWhole ? qpPages : undefined}
              editing={qpEditing}
              tool={
                addingQuestion ? (
                  <DrawStrip
                    text={`Drag a box round Question ${n} - one on each page if it runs over a page break.`}
                    drawn={draft.length}
                    save={`Add Question ${n}`}
                    onSave={saveAddQuestion}
                    onCancel={() => actions.tool('adjust')}
                  />
                ) : tool === 'draw' && selected ? (
                  <span className="flex items-center gap-3">
                    <span className="flex-1">Drag a new box round {selectedName} - it replaces its box on that page.</span>
                    <label className="flex items-center gap-1.5 font-normal" title="Move the box's edges to the question's own lines">
                      <input
                        type="checkbox"
                        checked={snap}
                        onChange={(e) => {
                          setSnap(e.target.checked)
                          store(SNAP_KEY, e.target.checked ? 'on' : 'off')
                        }}
                      />
                      Snap to the text
                    </label>
                    <CancelButton onClick={() => setTool('adjust')} />
                  </span>
                ) : tool === 'split' && selected ? (
                  <span className="flex items-center gap-3">
                    <span className="flex-1">
                      {splitEntry
                        ? `Click the page just above where ${pathLabel(splitEntry.part_path).trim()} starts - it becomes its own part, linked to ${rowLabel(splitEntry)}.`
                        : 'Click the page where the second part starts.'}
                    </span>
                    <CancelButton onClick={() => setTool('adjust')} />
                  </span>
                ) : undefined
              }
              empty={view === 'paper' ? 'The question paper has no pages to show.' : 'Choose a question above.'}
            />
          )}
        </div>

        {/* ── each part beside its answer ── */}
        <div className="card flex w-[400px] shrink-0 flex-col overflow-hidden">
          {!model ? (
            <div className="space-y-2 p-3">
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="h-12 animate-pulse rounded-xl bg-sunken" />
              ))}
            </div>
          ) : view === 'paper' ? (
            <PaperView
              paper={paper!}
              model={model}
              onOpen={openView}
              onCheck={(keys) => void check(keys)}
              onCoverTotal={(value) => void edit([{ op: 'set_cover_total', value }], `The cover total set to ${value}`)}
            />
          ) : current ? (
            <CheckColumn
              view={current}
              selectedId={selectedId}
              actions={actions}
              emptyPages={(paper?.qp_empty_pages ?? []).map((p) => p + 1)}
            />
          ) : null}
        </div>

        {/* ── mark scheme ── */}
        <div className={`card min-w-0 flex-1 overflow-hidden ${expanded === 'qp' ? 'hidden' : 'flex'}`}>
          {paperKey && (
            <PagePane
              title="Mark scheme"
              expanded={expanded === 'ms'}
              onExpand={() => setExpanded((x) => (x === 'ms' ? null : 'ms'))}
              icon={<NotebookPen size={16} />}
              zoomKey="qbx.zoom.ms"
              pageUrl={(page) => api.msPageImageUrl(paperKey, page)}
              segments={msSegments}
              selectedLabel={drawingAnswer ? selectedName : selected?.entries.map(rowLabel).join(', ')}
              boxes={drawingAnswer ? [] : msBoxes}
              onBoxClick={(id) => setRowMenu(id)}
              allPages={msWhole ? msPageCount : undefined}
              editing={drawingAnswer ? { mode: 'draw', onChange: setDraft, onSplit: () => undefined } : undefined}
              tool={
                drawingAnswer && selected ? (
                  <DrawStrip
                    text={`Find ${selectedName}'s answer (every page is shown) and drag a box round it - its label, answer and marks. One box on each page if it runs over.`}
                    drawn={draft.length}
                    save="Link it"
                    onSave={saveLinkDraw}
                    onCancel={() => actions.tool('adjust')}
                  />
                ) : menuEntry ? (
                  <RowMenu
                    entry={menuEntry}
                    selected={selected?.part ?? null}
                    selectedName={selectedName}
                    linkedTo={linkedTo(menuEntry)}
                    onLink={(alsoUnlink) => {
                      if (!selected) return
                      setRowMenu(null)
                      void edit(
                        [
                          ...alsoUnlink.map((partId) => ({ op: 'unlink', part_id: partId, entry_id: menuEntry.id })),
                          { op: 'link', part_id: selected.part.id, entry_id: menuEntry.id },
                        ],
                        `Linked ${rowLabel(menuEntry)} to ${name(selected.part)}`,
                      )
                    }}
                    onUnlink={() => {
                      if (!selected) return
                      setRowMenu(null)
                      actions.unlink(selected.part, menuEntry)
                    }}
                    onClose={() => setRowMenu(null)}
                  />
                ) : undefined
              }
              aside={
                selected && selected.entries.length > 0 && msSegments.length === 0 && !drawingAnswer ? (
                  <div className="space-y-1 border-b border-line px-4 py-2 text-xs">
                    {selected.entries.map((e) => (
                      <p key={e.id}>
                        <b>{rowLabel(e)}</b> <span className="text-ink-2">{e.answer_text || 'no text'}</span>{' '}
                        <span className="text-muted">({e.marks})</span>
                      </p>
                    ))}
                  </div>
                ) : undefined
              }
              empty="This mark scheme has no pages to show."
            />
          )}
        </div>
      </div>

      {toast && (
        <div
          className={`fixed bottom-6 left-1/2 z-40 flex -translate-x-1/2 items-center gap-3 rounded-xl px-4 py-2 text-sm shadow-float ${
            toast.error ? 'bg-danger text-white' : 'bg-inverse text-on-inverse'
          }`}
          role="status"
          onMouseEnter={() => setToastHeld(true)}
          onMouseLeave={() => setToastHeld(false)}
        >
          {toast.text}
          {toast.undo && (
            <button
              className="flex items-center gap-1 font-semibold underline-offset-2 hover:underline"
              onClick={() => {
                const undo = toast.undo!
                setToast(null)
                void run(undo, 'Undone')
              }}
            >
              <Undo2 size={14} /> Undo
            </button>
          )}
          <button onClick={() => setToast(null)} aria-label="Dismiss" className="opacity-70 hover:opacity-100">
            <X size={14} />
          </button>
        </div>
      )}
    </div>
  )
}

/** A mark scheme row clicked: link it to the selected part, or unlink it. */
function RowMenu({
  entry,
  selected,
  selectedName,
  linkedTo,
  onLink,
  onUnlink,
  onClose,
}: {
  entry: MarkSchemeEntry
  selected: QuestionPart | null
  selectedName?: string
  linkedTo: string[]
  onLink: (alsoUnlink: string[]) => void
  onUnlink: () => void
  onClose: () => void
}) {
  const label = rowLabel(entry)
  const others = linkedTo.filter((id) => id !== selected?.id)
  const otherNames = others.map((id) => pathLabel(id.split('.').slice(1).join('.')).trim() || 'the question').join(', ')
  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className="flex-1">
        <b>{label}</b> ({entry.marks} {entry.marks === 1 ? 'mark' : 'marks'}){' '}
        {!selected
          ? '- select a part in the middle column first.'
          : linkedTo.includes(selected.id)
            ? `is ${selectedName}'s answer.`
            : others.length
              ? `is linked to ${otherNames}.`
              : 'is linked to no part.'}
      </span>
      {selected && linkedTo.includes(selected.id) && (
        <button className="btn-secondary px-2.5 py-1 text-xs" onClick={onUnlink}>
          Unlink from {selectedName}
        </button>
      )}
      {selected && !linkedTo.includes(selected.id) && (
        <>
          <button className="btn-primary px-2.5 py-1 text-xs" onClick={() => onLink(others)}>
            {others.length ? `Move it to ${selectedName}` : `Link it to ${selectedName}`}
          </button>
          {others.length > 0 && (
            <button className="btn-secondary px-2.5 py-1 text-xs" onClick={() => onLink([])} title="One row answering two parts">
              Also link to {selectedName}
            </button>
          )}
        </>
      )}
      <CancelButton onClick={onClose} label="Close" />
    </span>
  )
}

function DrawStrip({
  text,
  drawn,
  save,
  onSave,
  onCancel,
}: {
  text: string
  drawn: number
  save: string
  onSave: () => void
  onCancel: () => void
}) {
  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className="flex-1">
        {text}
        {drawn > 0 && <span className="font-normal text-muted"> {drawn} drawn - drag again on a page to redraw it there.</span>}
      </span>
      <CancelButton onClick={onCancel} />
      <button className="btn-primary px-2.5 py-1 text-xs" disabled={drawn === 0} onClick={onSave}>
        {save}
      </button>
    </span>
  )
}

function CancelButton({ onClick, label = 'Cancel' }: { onClick: () => void; label?: string }) {
  return (
    <button className="btn-ghost px-2 py-1 text-xs font-normal" onClick={onClick} title="Esc">
      {label}
    </button>
  )
}

function IconButton({
  onClick,
  label,
  disabled,
  danger,
  children,
}: {
  onClick: () => void
  label: string
  disabled?: boolean
  danger?: boolean
  children: React.ReactNode
}) {
  return (
    <button
      className={`btn-ghost px-2 py-1.5 disabled:opacity-60 ${danger ? 'hover:text-danger' : 'hover:text-accent'}`}
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
    >
      {children}
    </button>
  )
}
