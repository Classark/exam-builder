import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  Check,
  CheckCheck,
  CircleCheck,
  Crop,
  FileText,
  GitMerge,
  ListChecks,
  Move,
  NotebookPen,
  Scissors,
  Sparkles,
  Loader2,
  RefreshCw,
  Scale,
  Trash2,
} from 'lucide-react'
import { api } from './api'
import { comparePapers, paperCode, sitting } from './bankFolders'
import { describeIssue, issueWhere, pathLabel, snippet } from './issues'
import { useLive } from './live'
import { PagePane, type PaneEditing } from './PagePane'
import { Modal } from './Modal'
import { PaperPicker, StatusPill } from './PaperPicker'
import { FixesView, MarksView } from './ReviewMarks'
import type { BBox, Fix, Issue, MarkSchemeEntry, Paper, Question, QuestionPart, Report } from './types'

/** One thing to look at: a report issue, or (in "All parts") any part of the paper. */
interface Item {
  id: string
  issue: Issue | null
  question: Question | null
  part: QuestionPart | null
  entries: MarkSchemeEntry[]
}

type Mode = 'issues' | 'marks' | 'parts' | 'fixes'
type Tool = PaneEditing['mode']

const PAPER_KEY = 'qbx.review.paper'
const SNAP_KEY = 'qbx.review.snap'

function isTyping(target: EventTarget | null) {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement
}

function entriesFor(paper: Paper, part: QuestionPart): MarkSchemeEntry[] {
  const ids = new Set(paper.joins.filter((j) => j.part_id === part.id).map((j) => j.mark_scheme_entry_id))
  return paper.mark_scheme.filter((e) => ids.has(e.id))
}

function buildItems(paper: Paper, report: Report, mode: Mode): Item[] {
  if (mode !== 'issues') {
    return paper.questions.flatMap((question) =>
      question.parts.map((part) => ({ id: part.id, issue: null, question, part, entries: entriesFor(paper, part) })),
    )
  }
  return report.issues.map((issue) => {
    const question = paper.questions.find((q) => q.question_number === issue.question_number) ?? null
    const part = question?.parts.find((p) => p.path === (issue.part_path ?? '')) ?? null
    let entries: MarkSchemeEntry[] = []
    if (part) entries = entriesFor(paper, part)
    else if (issue.check === 'no_orphaned_mark_scheme_entries')
      entries = paper.mark_scheme.filter(
        (e) => e.question_number === issue.question_number && e.part_path === (issue.part_path ?? ''),
      )
    else if (question) entries = paper.mark_scheme.filter((e) => e.question_number === question.question_number)
    return { id: issue.key, issue, question, part, entries }
  })
}

/** The part a fix is about: the first part one of its steps names, else the part (or the
 *  question's first part) its first issue names - a part since merged or renamed falls
 *  back to its question. */
function partForFix(paper: Paper, fix: Fix): string | null {
  const parts = paper.questions.flatMap((q) => q.parts)
  for (const step of fix.steps) {
    for (const id of [step.part_id, ...(step.part_ids ?? [])]) {
      if (id && parts.some((p) => p.id === id)) return id
    }
  }
  const named = [...fix.steps.flatMap((s) => [s.part_id, ...(s.part_ids ?? [])]), ...fix.issues]
  for (const ref of named) {
    if (!ref) continue
    // "check|question|path" for an issue key, "question.path" for a part id
    const [question, path] = ref.includes('|') ? ref.split('|').slice(1) : [ref.split('.')[0], ref.split('.').slice(1).join('.')]
    const q = paper.questions.find((x) => String(x.question_number) === question)
    if (!q) continue
    const part = q.parts.find((p) => p.path === (path === 'None' ? '' : path)) ?? q.parts[0]
    if (part) return part.id
  }
  const asked = fix.steps.find((s) => s.question_number != null)?.question_number
  const q = paper.questions.find((x) => x.question_number === asked)
  return q?.parts[0]?.id ?? null
}

function qpSegments(item: Item | null): BBox[] {
  if (!item) return []
  if (item.part) return item.part.segments
  if (item.question) return [...item.question.stem_segments, ...item.question.parts.flatMap((p) => p.segments)]
  return []
}

export function Reviewer() {
  const { papers, loaded, version, poke, fixing, startFixing, ai } = useLive()
  const [paperKey, setPaperKey] = useState<string | null>(() => {
    try {
      return localStorage.getItem(PAPER_KEY)
    } catch {
      return null
    }
  })
  const [paper, setPaper] = useState<Paper | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [msRows, setMsRows] = useState<Record<string, BBox[]>>({})
  const [msPageCount, setMsPageCount] = useState(0)
  // linking a part to its mark scheme answer by hand: the boxes drawn so far, or null
  const [linkDraft, setLinkDraft] = useState<BBox[] | null>(null)
  const [mode, setMode] = useState<Mode>('issues')
  const [showChecked, setShowChecked] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // in the Claude tab: the fix whose question the papers show
  const [focusedFixId, setFocusedFixId] = useState<string | null>(null)
  const [tool, setTool] = useState<Tool>('adjust')
  const [notice, setNotice] = useState('')
  const [fixes, setFixes] = useState<Fix[]>([])
  // regions drawn or dragged on the question paper snap to the question's own lines
  const [snap, setSnapState] = useState(() => {
    try {
      return localStorage.getItem(SNAP_KEY) !== 'off'
    } catch {
      return true
    }
  })
  const setSnap = (on: boolean) => {
    setSnapState(on)
    try {
      localStorage.setItem(SNAP_KEY, on ? 'on' : 'off')
    } catch {
      // a convenience only
    }
  }

  // A paper to show: the remembered one, or the first still needing review.
  const sorted = useMemo(() => [...papers].sort(comparePapers), [papers])
  useEffect(() => {
    if (!loaded || sorted.length === 0) return
    if (paperKey && sorted.some((p) => p.paper_key === paperKey)) return
    setPaperKey((sorted.find((p) => p.n_open > 0) ?? sorted[0]).paper_key)
  }, [loaded, sorted, paperKey])

  const pick = (key: string) => {
    setPaperKey(key)
    setSelectedId(null)
    try {
      localStorage.setItem(PAPER_KEY, key)
    } catch {
      // a convenience only
    }
  }

  // The paper and its report - again whenever anything changes (a re-extraction, an edit).
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
    setMsRows({})
    if (paperKey)
      void api
        .getMsRows(paperKey)
        .then((r) => {
          setMsRows(r.rows)
          setMsPageCount(r.page_count)
        })
        .catch(() => undefined)
  }, [paperKey, version])

  useEffect(() => {
    setFixes([])
    if (paperKey) void api.listFixes(paperKey).then(setFixes).catch(() => undefined)
  }, [paperKey, version, fixing.running])

  const fixingThis = fixing.running && (!fixing.paper_key || fixing.paper_key === paperKey)
  const suggestionsByIssue = useMemo(() => {
    const map = new Map<string, Fix[]>()
    for (const f of fixes) if (f.status === 'suggested') for (const k of f.issues) map.set(k, [...(map.get(k) ?? []), f])
    return map
  }, [fixes])

  const askClaude = async (issueKey?: string) => {
    if (!paperKey) return
    try {
      await startFixing({ paper_key: paperKey, issue_key: issueKey })
      flash(issueKey ? 'Claude is looking at this issue…' : 'Claude is reviewing this paper…')
    } catch (e) {
      flash((e as Error).message)
    }
  }

  const fixAction = async (fix: Fix, action: 'accept' | 'reject' | 'undo') => {
    if (!paperKey) return
    try {
      await api.fixAction(paperKey, fix.id, action)
      flash(action === 'accept' ? 'Fix applied' : action === 'undo' ? 'Fix undone' : 'Suggestion rejected')
      setFixes(await api.listFixes(paperKey))
      await after()
    } catch (e) {
      flash((e as Error).message)
    }
  }

  const setPartMarks = async (part: QuestionPart, marks: number) => {
    if (!paperKey) return
    await api.updatePart(paperKey, part.id, { marks })
    flash(`Marks set to ${marks}`)
    await after()
  }

  const setTotal = async (question: Question, total: number) => {
    if (!paperKey) return
    try {
      await api.setQuestionTotal(paperKey, question.question_number, total)
      flash(`Q${question.question_number} total set to ${total}`)
      await after()
    } catch (e) {
      flash((e as Error).message)
    }
  }

  const summary = papers.find((p) => p.paper_key === paperKey) ?? null
  const [deleting, setDeleting] = useState(false)
  // re-extracting this one paper: null idle, '' running, else what it came to
  const [reprocessing, setReprocessing] = useState<string | null>(null)
  useEffect(() => setReprocessing(null), [paperKey])
  const reprocess = async () => {
    if (!paperKey) return
    setReprocessing('')
    try {
      const r = await api.reprocessPaper(paperKey)
      setReprocessing(r.passed ? 'Re-processed: no issues' : `Re-processed: ${r.n_issues} issue${r.n_issues === 1 ? '' : 's'}`)
      await reload(paperKey)
      poke()
    } catch (e) {
      setReprocessing(e instanceof Error ? e.message : String(e))
    }
  }
  const ready = paper && report && paper.paper_key === paperKey

  const allItems = useMemo(() => (ready ? buildItems(paper, report, mode) : []), [ready, paper, report, mode])
  const items = mode === 'issues' && !showChecked ? allItems.filter((i) => !i.issue?.checked) : allItems
  const nChecked = allItems.filter((i) => i.issue?.checked).length
  const openKeysByPart = useMemo(() => {
    const map = new Map<string, number>()
    for (const issue of report?.issues ?? []) {
      if (issue.checked) continue
      const key = `${issue.question_number}|${issue.part_path ?? ''}`
      map.set(key, (map.get(key) ?? 0) + 1)
    }
    return map
  }, [report])

  const selectedIndex = Math.max(0, items.findIndex((i) => i.id === selectedId))
  const current = items[selectedIndex] ?? null

  const focusFix = useCallback(
    (fix: Fix) => {
      setFocusedFixId(fix.id)
      if (!paper) return
      const partId = partForFix(paper, fix)
      if (partId) setSelectedId(partId)
    },
    [paper],
  )
  // opening the Claude tab shows the first waiting fix's question, not the last part looked at
  useEffect(() => {
    if (mode !== 'fixes' || !ready) return
    if (focusedFixId && fixes.some((f) => f.id === focusedFixId)) return
    const first = fixes.find((f) => f.status === 'suggested') ?? fixes.find((f) => f.status === 'applied')
    if (first) focusFix(first)
  }, [mode, ready, fixes, focusedFixId, focusFix])
  useEffect(() => setFocusedFixId(null), [paperKey])

  // keep a selection whenever there is something to select
  useEffect(() => {
    if (items.length > 0 && !items.some((i) => i.id === selectedId)) setSelectedId(items[0].id)
  }, [items, selectedId])

  useEffect(() => {
    setTool('adjust')
    setLinkDraft(null)
  }, [current?.id])

  const saveLink = async () => {
    if (!paperKey || !current?.part || !linkDraft?.length) return
    try {
      await api.linkMarkScheme(paperKey, current.part.id, linkDraft)
      setLinkDraft(null)
      await reload(paperKey)
      flash('Mark scheme linked')
    } catch (e) {
      flash((e as Error).message)
    }
  }
  const removeLink = async () => {
    if (!paperKey || !current?.part) return
    try {
      await api.unlinkMarkScheme(paperKey, current.part.id)
      await reload(paperKey)
      flash('Link removed')
    } catch (e) {
      flash((e as Error).message)
    }
  }

  const flash = (text: string) => {
    setNotice(text)
    window.setTimeout(() => setNotice((n) => (n === text ? '' : n)), 2500)
  }

  const after = async () => {
    if (paperKey) await reload(paperKey)
    poke()
  }

  const setChecked = async (keys: string[], checked: boolean) => {
    if (!paperKey || keys.length === 0) return
    // move on first, so the list doesn't jump under the cursor
    if (checked && !showChecked && current && keys.includes(current.id)) {
      const next = items[selectedIndex + 1] ?? items[selectedIndex - 1]
      setSelectedId(next ? next.id : null)
    }
    await api.reviewIssues(paperKey, keys, checked)
    await after()
  }

  const savePart = async (updates: Record<string, unknown>, message: string) => {
    if (!paperKey || !current?.part) return
    await api.updatePart(paperKey, current.part.id, updates)
    flash(message)
    await after()
  }

  const saveRegion = async (segments: BBox[]) => {
    if (!paperKey) return
    let boxes = segments
    if (snap) {
      try {
        boxes = (await api.snapRegion(paperKey, segments)).segments
      } catch {
        // keep the box as drawn
      }
    }
    await savePart({ segments: boxes }, snap ? 'Region snapped to the question' : 'Region saved')
  }

  const merge = async () => {
    if (!paperKey || !current?.part) return
    try {
      await api.mergePart(paperKey, current.part.id)
      flash(`Merged ${pathLabel(current.part.path).trim()} with the next part`)
      await after()
    } catch {
      flash('This is the last part of its question - nothing to merge with')
    }
  }

  const split = async (page: number, y: number) => {
    if (!paperKey || !current?.part) return
    await api.splitPart(paperKey, current.part.id, { page, y })
    setTool('adjust')
    flash('Split into two parts - set the new part’s marks')
    await after()
  }

  const move = (by: number) => {
    const next = items[Math.max(0, Math.min(items.length - 1, selectedIndex + by))]
    if (next) setSelectedId(next.id)
  }

  const movePaper = (by: number) => {
    const queue = sorted.filter((p) => p.n_open > 0 || p.paper_key === paperKey)
    const index = queue.findIndex((p) => p.paper_key === paperKey)
    const next = queue[index + by]
    if (next) pick(next.paper_key)
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey) return
      if (document.querySelector('[role="dialog"]')) return
      const k = e.key
      if (k === 'j' || k === 'ArrowDown') move(1)
      else if (k === 'k' || k === 'ArrowUp') move(-1)
      else if ((k === 'Enter' || k === 'c') && current?.issue) void setChecked([current.issue.key], !current.issue.checked)
      else if (k === 'm' && current?.part) void merge()
      else if (k === 'd' && current?.part) setTool((t) => (t === 'draw' ? 'adjust' : 'draw'))
      else if (k === 's' && current?.part) setTool((t) => (t === 'split' ? 'adjust' : 'split'))
      else if (k === 'Escape') setTool('adjust')
      else if (k === ']') movePaper(1)
      else if (k === '[') movePaper(-1)
      else return
      e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  if (loaded && papers.length === 0) return <EmptyLibrary />

  // In the Claude tab, everything the focused fix touches: every part it names on the
  // question paper (a join of (i) and (ii) shows both, not just the first) and their rows
  // (not a hook: the component can return early above, for an empty library)
  const focusedFix = mode === 'fixes' && paper ? fixes.find((f) => f.id === focusedFixId) : undefined
  const fixPartIds = new Set(focusedFix?.steps.flatMap((s) => [s.part_id, ...(s.part_ids ?? [])]).filter(Boolean))
  const fixParts = focusedFix && paper ? paper.questions.flatMap((q) => q.parts).filter((p) => fixPartIds.has(p.id)) : []
  const spansParts = fixParts.length > 1
  const qp = spansParts ? fixParts.flatMap((p) => p.segments) : qpSegments(current)
  const msSegments = spansParts
    ? fixParts.flatMap((p) => entriesFor(paper!, p)).flatMap((e) => msRows[e.id] ?? [])
    : current
      ? current.entries.flatMap((e) => msRows[e.id] ?? [])
      : []
  const openCount = report ? report.issues.filter((i) => !i.checked).length : 0
  const marksFound = paper ? paper.questions.reduce((s, q) => s + q.total_marks, 0) : 0

  return (
    <div className="flex h-full min-h-0 gap-4 p-4">
      {deleting && summary && (
        <DeletePaperDialog
          paperKey={summary.paper_key}
          title={`${paperCode(summary)} · ${sitting(summary)}`}
          onClose={() => setDeleting(false)}
          onDeleted={() => {
            setDeleting(false)
            setPaperKey(null) // the next paper is picked once the list refreshes
            poke()
          }}
        />
      )}
      {/* ── Left: the paper, its issues, and the selected part's editor ── */}
      <aside className="flex w-[380px] shrink-0 flex-col gap-3">
        <PaperPicker papers={papers} current={summary} onPick={pick} />

        {summary && paper && (
          <div className="card flex items-center gap-4 px-4 py-3">
            <Figure value={paper.questions.length} label="questions" />
            <Figure
              value={paper.cover_total_marks ? `${marksFound}/${paper.cover_total_marks}` : marksFound}
              label={paper.cover_total_marks ? 'marks of cover total' : 'marks'}
              warn={paper.cover_total_marks !== null && marksFound !== paper.cover_total_marks}
            />
            <div className="ml-auto flex flex-col items-end gap-1.5">
              <div className="flex items-center gap-1">
                <StatusPill paper={summary} />
                <button
                  className="btn-ghost px-1 hover:text-accent disabled:opacity-60"
                  onClick={() => void reprocess()}
                  disabled={reprocessing === ''}
                  title="Re-process this paper: extract it again from its PDFs (your fixes and links are kept)"
                  aria-label="Re-process this paper"
                >
                  <RefreshCw size={14} className={reprocessing === '' ? 'animate-spin' : ''} />
                </button>
                <button
                  className="btn-ghost px-1 hover:text-danger"
                  onClick={() => setDeleting(true)}
                  title="Delete this paper from the bank"
                  aria-label="Delete this paper"
                >
                  <Trash2 size={14} />
                </button>
              </div>
              {reprocessing !== null && (
                <span className="text-xs text-muted" role="status">
                  {reprocessing === '' ? 'Re-processing…' : reprocessing}
                </span>
              )}
              {openCount > 0 && ai && (
                <button
                  className="flex items-center gap-1 text-xs font-medium text-accent hover:underline disabled:opacity-60"
                  disabled={fixingThis}
                  onClick={() => {
                    setMode('fixes')
                    void askClaude()
                  }}
                  title="Claude reviews this paper's open issues and proposes fixes"
                >
                  {fixingThis ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
                  {fixingThis ? 'Claude is reviewing…' : 'Fix with Claude'}
                </button>
              )}
              {openCount > 0 && (
                <button
                  className="text-xs font-medium text-accent hover:underline"
                  onClick={() => void setChecked(report!.issues.filter((i) => !i.checked).map((i) => i.key), true)}
                  title="Mark every issue on this paper as checked"
                >
                  Mark all checked
                </button>
              )}
            </div>
          </div>
        )}

        <div className="card flex min-h-0 flex-1 flex-col overflow-hidden">
          <div className="flex items-center gap-1 border-b border-line p-2" role="tablist" aria-label="What to review">
            <ModeTab active={mode === 'issues'} onClick={() => setMode('issues')} icon={AlertTriangle}>
              Issues {openCount > 0 && <span className="text-accent">{openCount}</span>}
            </ModeTab>
            <ModeTab active={mode === 'marks'} onClick={() => setMode('marks')} icon={Scale}>
              Marks
            </ModeTab>
            <ModeTab active={mode === 'parts'} onClick={() => setMode('parts')} icon={ListChecks}>
              Parts
            </ModeTab>
            {ai && (
              <ModeTab active={mode === 'fixes'} onClick={() => setMode('fixes')} icon={Sparkles}>
                Claude
                {fixes.some((f) => f.status === 'suggested') && (
                  <span className="text-accent">{fixes.filter((f) => f.status === 'suggested').length}</span>
                )}
              </ModeTab>
            )}
          </div>

          {ready && mode === 'marks' ? (
            <MarksView
              paper={paper}
              selectedPartId={current?.part?.id ?? null}
              onSelect={(id) => setSelectedId(id)}
              onSetMarks={setPartMarks}
              onSetTotal={setTotal}
            />
          ) : ready && mode === 'fixes' && ai ? (
            <FixesView
              fixes={fixes}
              report={report}
              busy={fixingThis}
              onAction={fixAction}
              onFixAll={() => void askClaude()}
              focusedId={focusedFixId}
              onFocus={focusFix}
            />
          ) : (
          <ul className="scroll-quiet min-h-0 flex-1 overflow-auto p-2" aria-label={mode === 'issues' ? 'Issues' : 'Parts'}>
            {!ready ? (
              <ListSkeleton />
            ) : items.length === 0 ? (
              <li className="flex flex-col items-center gap-2 px-6 py-10 text-center">
                <CircleCheck size={32} className="text-success" />
                <span className="font-medium">{report.issues.length === 0 ? 'This paper is clean' : 'Every issue is checked'}</span>
                <span className="text-sm text-muted">
                  Nothing to fix here. Press <kbd className="key">]</kbd> for the next paper to review.
                </span>
              </li>
            ) : (
              items.map((item, index) =>
                mode === 'issues' && item.issue ? (
                  <IssueRow
                    key={item.id}
                    issue={item.issue}
                    suggested={suggestionsByIssue.get(item.issue.key)?.length ?? 0}
                    selected={index === selectedIndex}
                    onSelect={() => setSelectedId(item.id)}
                    onToggle={() => void setChecked([item.issue!.key], !item.issue!.checked)}
                  />
                ) : (
                  <PartRow
                    key={item.id}
                    item={item}
                    flagged={openKeysByPart.get(`${item.question?.question_number}|${item.part?.path ?? ''}`) ?? 0}
                    selected={index === selectedIndex}
                    onSelect={() => setSelectedId(item.id)}
                  />
                ),
              )
            )}
          </ul>
          )}

          {mode === 'issues' && nChecked > 0 && (
            <button
              onClick={() => setShowChecked((s) => !s)}
              className="border-t border-line px-4 py-2 text-left text-xs text-muted hover:text-accent"
            >
              {showChecked ? 'Hide checked issues' : `Show ${nChecked} checked ${nChecked === 1 ? 'issue' : 'issues'}`}
            </button>
          )}
        </div>

        {current?.part && (
          <PartEditor
            key={current.part.id + ':' + current.part.marks}
            item={current}
            tool={tool}
            onTool={setTool}
            onSave={savePart}
            onMerge={() => void merge()}
            snap={snap}
            onSnap={setSnap}
          />
        )}
      </aside>

      {/* ── Right: the question paper and mark scheme, side by side ── */}
      <div className="card relative flex min-w-0 flex-1 overflow-hidden">
        {current?.issue && (
          <IssueBanner
            item={current}
            busy={fixingThis}
            suggestions={suggestionsByIssue.get(current.issue.key) ?? []}
            canFix={ai}
            onFix={() => void askClaude(current.issue!.key)}
            onShowFixes={() => setMode('fixes')}
            onToggle={() => void setChecked([current.issue!.key], !current.issue!.checked)}
          />
        )}
        <div className={`flex min-h-0 min-w-0 flex-1 ${current?.issue ? 'pt-[76px]' : ''}`}>
          <PagePane
            title="Question paper"
            icon={<FileText size={16} />}
            zoomKey="qbx.zoom.qp"
            pageUrl={(page) => api.pageImageUrl(paperKey!, page)}
            segments={qp}
            editing={
              // dragging saves the selected part's region: never while several parts show
              current?.part && !spansParts
                ? {
                    mode: tool,
                    onChange: (segments) => void saveRegion(segments),
                    onSplit: (page, y) => void split(page, y),
                  }
                : undefined
            }
            empty={
              !current
                ? 'Pick an issue or a part on the left to see it here.'
                : current.issue && current.issue.question_number == null
                ? 'This issue is about the whole paper.'
                : 'No region was recorded for this part. Choose Draw region, then drag over the page.'
            }
          />
          <div className="w-px shrink-0 bg-line" />
          <PagePane
            title="Mark scheme"
            icon={<NotebookPen size={16} />}
            zoomKey="qbx.zoom.ms"
            pageUrl={(page) => api.msPageImageUrl(paperKey!, page)}
            segments={linkDraft ?? msSegments}
            allPages={linkDraft ? msPageCount : undefined}
            editing={
              linkDraft
                ? { mode: 'draw', onChange: (segments) => setLinkDraft(segments), onSplit: () => undefined }
                : undefined
            }
            aside={
              <>
                {current?.part && msPageCount > 0 && (
                  <LinkBar
                    linking={linkDraft !== null}
                    drawn={linkDraft?.length ?? 0}
                    linked={current.entries.some((e) => e.manual)}
                    unjoined={current.entries.length === 0}
                    onStart={() => setLinkDraft([])}
                    onSave={() => void saveLink()}
                    onCancel={() => setLinkDraft(null)}
                    onRemove={() => void removeLink()}
                  />
                )}
                {!linkDraft && current && current.entries.length > 0 && msSegments.length === 0 && (
                  <AnswerText entries={current.entries} />
                )}
              </>
            }
            empty={
              !current
                ? ''
                : current.issue && current.issue.question_number == null
                  ? 'This issue is about the whole paper.'
                  : current.entries.length === 0
                  ? 'No mark scheme row is joined to this. Compare the labels on both papers.'
                  : 'This mark scheme has no ruled rows to highlight - its answers are listed above.'
            }
          />
        </div>
        {notice && (
          <div className="absolute bottom-4 left-1/2 z-20 -translate-x-1/2 rounded-xl bg-inverse px-4 py-2 text-sm text-on-inverse shadow-float" role="status">
            {notice}
          </div>
        )}
      </div>
    </div>
  )
}

/** Link a part to its answer on the mark scheme by hand: every page shows, and a box drawn
 * round the answer becomes the part's mark scheme row. The same for every board. */
function LinkBar({
  linking,
  drawn,
  linked,
  unjoined,
  onStart,
  onSave,
  onCancel,
  onRemove,
}: {
  linking: boolean
  drawn: number
  linked: boolean
  unjoined: boolean
  onStart: () => void
  onSave: () => void
  onCancel: () => void
  onRemove: () => void
}) {
  if (linking) {
    return (
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-accent-wash px-4 py-2 text-xs">
        <span className="min-w-0 flex-1 text-ink">
          {drawn === 0
            ? 'Scroll to the answer and drag a box round it (one box per page if it runs over a page break).'
            : `${drawn} box${drawn === 1 ? '' : 'es'} drawn - save to link it to this part.`}
        </span>
        <button className="btn-primary px-3 py-1 text-xs" disabled={drawn === 0} onClick={onSave}>
          Save link
        </button>
        <button className="btn-ghost px-2 py-1 text-xs" onClick={onCancel}>
          Cancel
        </button>
      </div>
    )
  }
  return (
    <div className="flex items-center gap-2 border-b border-line px-4 py-2 text-xs">
      <span className="min-w-0 flex-1 text-muted">
        {unjoined ? 'No mark scheme row was found for this part.' : linked ? 'Linked by hand.' : 'Wrong row? Link the right one.'}
      </span>
      {linked && (
        <button className="btn-ghost px-2 py-1 text-xs" onClick={onRemove}>
          Remove link
        </button>
      )}
      <button className={`${unjoined ? 'btn-primary' : 'btn-secondary'} px-3 py-1 text-xs`} onClick={onStart}>
        {linked ? 'Link again' : 'Link mark scheme'}
      </button>
    </div>
  )
}

function Figure({ value, label, warn }: { value: number | string; label: string; warn?: boolean }) {
  return (
    <div>
      <div className={`text-xl font-semibold leading-tight ${warn ? 'text-warning' : 'text-ink'}`}>{value}</div>
      <div className="text-xs text-muted">{label}</div>
    </div>
  )
}

function ModeTab({
  active,
  onClick,
  icon: Icon,
  children,
}: {
  active: boolean
  onClick: () => void
  icon: typeof Check
  children: React.ReactNode
}) {
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`flex flex-1 items-center justify-center gap-2 rounded-xl px-3 py-2 text-sm font-medium transition-colors ${
        active ? 'bg-accent-wash-strong text-accent' : 'text-ink-2 hover:bg-hover'
      }`}
    >
      <Icon size={15} />
      {children}
    </button>
  )
}

function IssueRow({
  issue,
  suggested,
  selected,
  onSelect,
  onToggle,
}: {
  issue: Issue
  suggested: number
  selected: boolean
  onSelect: () => void
  onToggle: () => void
}) {
  const { title } = describeIssue(issue)
  const ref = useRef<HTMLLIElement>(null)
  useEffect(() => {
    if (selected) ref.current?.scrollIntoView({ block: 'nearest' })
  }, [selected])
  return (
    <li ref={ref} className="mb-1">
      <div
        className={`group flex items-start gap-3 rounded-xl border px-3 py-2.5 transition-colors ${
          selected ? 'border-accent-border bg-accent-container' : 'border-transparent hover:bg-accent-wash'
        } ${issue.checked ? 'opacity-60' : ''}`}
      >
        <button
          onClick={onToggle}
          aria-label={issue.checked ? 'Mark as not checked' : 'Mark as checked'}
          title={issue.checked ? 'Checked - click to reopen' : 'Mark as checked'}
          className={`mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-md border-[1.5px] transition-colors ${
            issue.checked ? 'border-success bg-success text-white' : 'border-line-strong hover:border-accent'
          }`}
        >
          {issue.checked && <Check size={13} strokeWidth={3} />}
        </button>
        <button onClick={onSelect} className="min-w-0 flex-1 text-left" aria-current={selected || undefined}>
          <span className="block text-sm font-medium leading-snug">{title}</span>
          <span className="mt-0.5 block text-xs text-muted">
            {issueWhere(issue)}
            {suggested > 0 && <span className="ml-2 text-accent">Claude suggests a fix</span>}
          </span>
        </button>
      </div>
    </li>
  )
}

function PartRow({
  item,
  flagged,
  selected,
  onSelect,
}: {
  item: Item
  flagged: number
  selected: boolean
  onSelect: () => void
}) {
  const ref = useRef<HTMLLIElement>(null)
  useEffect(() => {
    if (selected) ref.current?.scrollIntoView({ block: 'nearest' })
  }, [selected])
  const part = item.part!
  const msMarks = item.entries.reduce((s, e) => s + e.marks, 0)
  return (
    <li ref={ref}>
      <button
        onClick={onSelect}
        aria-current={selected || undefined}
        className={`flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left transition-colors ${
          selected ? 'bg-accent-container' : 'hover:bg-accent-wash'
        }`}
      >
        <span className="w-20 shrink-0 text-sm font-medium">
          Q{item.question?.question_number}
          <span className="text-ink-2">{pathLabel(part.path)}</span>
        </span>
        <span className="min-w-0 flex-1 truncate text-xs text-muted">{snippet(part.text, item.question?.question_number) || 'No text'}</span>
        {flagged > 0 && <span className="size-2 shrink-0 rounded-full bg-accent" title={`${flagged} open issue`} />}
        {part.removed ? (
          <span className="pill shrink-0 bg-line text-ink-2" title="The board withdrew this question - it's never set in an exam">
            Removed by board
          </span>
        ) : (
          <span
            className={`shrink-0 text-xs ${item.entries.length > 0 && msMarks !== part.marks ? 'text-warning' : 'text-muted'}`}
            title={item.entries.length > 0 ? `Mark scheme gives ${msMarks}` : 'No mark scheme row'}
          >
            {part.marks} {part.marks === 1 ? 'mark' : 'marks'}
          </span>
        )}
      </button>
    </li>
  )
}

/** The selected issue, spelled out, across the top of the page panes. */
function IssueBanner({
  item,
  busy,
  suggestions,
  onFix,
  onShowFixes,
  onToggle,
  canFix,
}: {
  item: Item
  busy: boolean
  suggestions: Fix[]
  /** whether this build can reach Claude: without it there's no "Fix this" */
  canFix: boolean
  onFix: () => void
  onShowFixes: () => void
  onToggle: () => void
}) {
  const issue = item.issue!
  const { title, hint } = describeIssue(issue)
  return (
    <div className="absolute inset-x-0 top-0 z-10 flex h-[76px] items-center gap-4 border-b border-accent-border bg-accent-container px-5">
      <AlertTriangle size={22} className="shrink-0 text-accent" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="font-semibold">{title}</span>
          <span className="text-sm text-ink-2">{issueWhere(issue)}</span>
        </div>
        <p className="truncate text-sm text-ink-2" title={`${hint}\n\n${issue.detail}`}>
          {hint} <span className="text-muted">({issue.detail})</span>
        </p>
      </div>
      {!issue.checked &&
        canFix &&
        (suggestions.length > 0 ? (
          <button className="btn-secondary" onClick={onShowFixes} title={suggestions[0].reason}>
            <Sparkles size={16} /> See Claude's fix
          </button>
        ) : (
          <button className="btn-secondary" onClick={onFix} disabled={busy} title="Claude proposes a fix for this issue">
            {busy ? <Loader2 size={16} className="animate-spin" /> : <Sparkles size={16} />} Fix this
          </button>
        ))}
      <button className={issue.checked ? 'btn-secondary' : 'btn-primary'} onClick={onToggle}>
        {issue.checked ? (
          'Reopen'
        ) : (
          <>
            <CheckCheck size={16} /> Mark checked
          </>
        )}
        <kbd className="key ml-1 border-white/40 bg-white/15 text-inherit">Enter</kbd>
      </button>
    </div>
  )
}

function AnswerText({ entries }: { entries: MarkSchemeEntry[] }) {
  return (
    <div className="space-y-2 border-b border-line bg-card px-4 py-3">
      {entries.map((e) => (
        <div key={e.id} className="flex items-baseline gap-3 text-sm">
          <span className="w-16 shrink-0 font-medium">
            {e.question_number}
            {pathLabel(e.part_path)}
          </span>
          <span className="min-w-0 flex-1 text-ink-2">{e.answer_text || 'No answer text'}</span>
          <span className="shrink-0 text-xs text-muted">
            {e.marks} {e.marks === 1 ? 'mark' : 'marks'}
          </span>
        </div>
      ))}
    </div>
  )
}

function PartEditor({
  item,
  tool,
  onTool,
  onSave,
  onMerge,
  snap,
  onSnap,
}: {
  item: Item
  tool: Tool
  onTool: (t: Tool) => void
  onSave: (updates: Record<string, unknown>, message: string) => Promise<void>
  onMerge: () => void
  snap: boolean
  onSnap: (on: boolean) => void
}) {
  const part = item.part!
  const [marks, setMarks] = useState(String(part.marks))
  const [text, setText] = useState(part.text)
  const msMarks = item.entries.reduce((s, e) => s + e.marks, 0)

  const saveMarks = () => {
    const n = Number(marks)
    if (marks.trim() !== '' && Number.isInteger(n) && n >= 0 && n !== part.marks) void onSave({ marks: n }, `Marks set to ${n}`)
  }

  const tools: { id: Tool; label: string; icon: typeof Move; key: string; help: string }[] = [
    { id: 'adjust', label: 'Adjust', icon: Move, key: 'Esc', help: 'Drag the highlight’s edges on the question paper.' },
    { id: 'draw', label: 'Draw region', icon: Crop, key: 'd', help: 'Drag over the page to set this part’s region there.' },
    { id: 'split', label: 'Split', icon: Scissors, key: 's', help: 'Click the page where the next part should start.' },
  ]

  return (
    <div className="card space-y-3 px-4 py-3.5">
      <div className="flex items-center gap-3">
        <span className="font-semibold">
          Q{item.question?.question_number}
          {pathLabel(part.path)}
        </span>
        <label className="ml-auto flex items-center gap-2 text-sm text-ink-2">
          Marks
          <input
            value={marks}
            onChange={(e) => setMarks(e.target.value.replace(/[^0-9]/g, ''))}
            onBlur={saveMarks}
            onKeyDown={(e) => e.key === 'Enter' && (e.currentTarget as HTMLInputElement).blur()}
            inputMode="numeric"
            className="field w-16 px-2 py-1.5 text-center"
            aria-label="Marks for this part"
          />
        </label>
      </div>
      {item.entries.length > 0 && msMarks !== part.marks && (
        <p className="rounded-lg bg-warning-wash px-3 py-1.5 text-xs text-warning">
          The mark scheme gives {msMarks} {msMarks === 1 ? 'mark' : 'marks'} for this.
        </p>
      )}

      <div>
        <div className="flex rounded-xl bg-sunken p-1" role="radiogroup" aria-label="Region tool">
          {tools.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              role="radio"
              aria-checked={tool === id}
              onClick={() => onTool(id)}
              className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-medium transition-colors ${
                tool === id ? 'bg-card text-accent shadow-card' : 'text-ink-2 hover:text-accent'
              }`}
            >
              <Icon size={14} />
              {label}
            </button>
          ))}
        </div>
        <div className="mt-1.5 flex items-start gap-2 text-xs text-muted">
          <p className="flex-1">
            {tools.find((t) => t.id === tool)!.help}{' '}
            <kbd className="key">{tools.find((t) => t.id === tool)!.key}</kbd>
          </p>
          <label
            className="flex shrink-0 items-center gap-1.5"
            title="Move the region's edges to the question's own lines: its label at the top, its mark at the bottom, its text and figures at the sides"
          >
            <input type="checkbox" checked={snap} onChange={(e) => onSnap(e.target.checked)} />
            Snap to question
          </label>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button className="btn-secondary px-3 py-1.5 text-xs" onClick={onMerge} title="Merge with the next part (m)">
          <GitMerge size={14} /> Merge with next
        </button>
        <details className="group ml-auto">
          <summary className="cursor-pointer list-none text-xs text-muted hover:text-accent">Extracted text</summary>
          <div className="card absolute z-30 mt-2 w-[360px] -translate-x-[260px] p-3 shadow-float">
            <p className="mb-2 text-xs text-muted">Used for search only - never shown in exported papers.</p>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              onBlur={() => text !== part.text && void onSave({ text }, 'Text saved')}
              rows={8}
              className="field resize-y text-xs"
            />
          </div>
        </details>
      </div>
    </div>
  )
}

function ListSkeleton() {
  return (
    <>
      {[0, 1, 2, 3].map((i) => (
        <li key={i} className="mb-2 h-12 animate-pulse rounded-xl bg-sunken" />
      ))}
    </>
  )
}

function EmptyLibrary() {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="card max-w-md px-10 py-12 text-center">
        <FileText size={36} className="mx-auto mb-4 text-accent" />
        <h2 className="mb-2 text-xl font-semibold">No papers yet</h2>
        <p className="text-sm text-ink-2">
          Copy question papers and their mark schemes into the papers folder, then choose{' '}
          <span className="font-medium text-ink">Scan papers folder</span> above. Each paper appears here as soon as it’s
          extracted.
        </p>
      </div>
    </div>
  )
}

/** Confirms deleting a paper, naming the exams that use it first. */
function DeletePaperDialog({
  paperKey,
  title,
  onClose,
  onDeleted,
}: {
  paperKey: string
  title: string
  onClose: () => void
  onDeleted: () => void
}) {
  const [usedIn, setUsedIn] = useState<string[] | null>(null)
  const [deletePdfs, setDeletePdfs] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    void api.paperUsedIn(paperKey).then((r) => setUsedIn(r.exams)).catch(() => setUsedIn([]))
  }, [paperKey])

  const remove = async () => {
    setBusy(true)
    setError('')
    try {
      await api.deletePaper(paperKey, deletePdfs)
      onDeleted()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  return (
    <Modal title="Delete this paper?" onClose={onClose} width="max-w-md">
      <div className="space-y-3 text-sm">
        <p>
          <span className="font-semibold">{title}</span> will be removed from the bank - its questions, images, topic tags
          and review notes.
        </p>
        {usedIn && usedIn.length > 0 && (
          <p className="rounded-lg bg-warning-wash px-3 py-2 text-xs text-warning">
            Used in {usedIn.map((n) => `“${n}”`).join(', ')}. Those exams keep their other questions; this paper's will
            show as missing.
          </p>
        )}
        <label className="flex items-start gap-2 text-ink-2">
          <input
            type="checkbox"
            checked={deletePdfs}
            onChange={(e) => setDeletePdfs(e.target.checked)}
            className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]"
          />
          <span>
            Also delete its question paper and mark scheme PDFs from the papers folder.
            <span className="block text-xs text-muted">
              Leave this on unless you want “Scan papers folder” to add it again.
            </span>
          </span>
        </label>
        {error && <p className="rounded-lg bg-danger-wash px-3 py-2 text-xs text-danger">{error}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button className="btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn-primary bg-danger hover:bg-danger" onClick={() => void remove()} disabled={busy || usedIn === null}>
            {busy ? 'Deleting…' : 'Delete paper'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
