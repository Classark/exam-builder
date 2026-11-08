import { memo, useEffect, useMemo, useState } from 'react'
import { ChevronRight, Folder, FolderOpen } from 'lucide-react'
import { comparePapers, fullYear, paperCode, questionKey, sessionName, sitting } from './bankFolders'
import { snippet } from './issues'
import type { BankPaper, BankQuestionBrief } from './types'

const OPEN_STORAGE_KEY = 'qbx.questionBank.openFolders'

interface YearFolder {
  year: number
  sessions: { session: string; papers: BankPaper[] }[]
}

interface SubjectFolder {
  subject: string
  name: string
  years: YearFolder[]
}

/** Subject -> year -> session -> paper, in the order a teacher looks for them. */
function buildTree(papers: BankPaper[]): SubjectFolder[] {
  const subjects = new Map<string, SubjectFolder>()
  for (const paper of [...papers].sort(comparePapers)) {
    let subject = subjects.get(paper.subject)
    if (!subject) {
      subject = { subject: paper.subject, name: paper.subject_name, years: [] }
      subjects.set(paper.subject, subject)
    }
    subject.name ||= paper.subject_name
    let year = subject.years.find((y) => y.year === fullYear(paper.year))
    if (!year) {
      year = { year: fullYear(paper.year), sessions: [] }
      subject.years.push(year)
    }
    let session = year.sessions.find((s) => s.session === paper.session)
    if (!session) {
      session = { session: paper.session, papers: [] }
      year.sessions.push(session)
    }
    session.papers.push(paper)
  }
  return [...subjects.values()]
}

function loadOpen(): Set<string> {
  try {
    const saved = localStorage.getItem(OPEN_STORAGE_KEY)
    return new Set(saved ? (JSON.parse(saved) as string[]) : [])
  } catch {
    return new Set()
  }
}

function FolderRow({
  label,
  detail,
  count,
  open,
  depth,
  onToggle,
  children,
}: {
  label: string
  detail?: string
  count: number
  open: boolean
  depth: number
  onToggle: () => void
  children: React.ReactNode
}) {
  const Icon = open ? FolderOpen : Folder
  return (
    <li>
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-2 rounded-lg py-1.5 pr-2 text-left transition-colors hover:bg-accent-wash"
        style={{ paddingLeft: 6 + depth * 16 }}
        aria-expanded={open}
      >
        <ChevronRight size={14} className={`shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`} />
        <Icon size={15} className="shrink-0 text-accent" />
        <span className="shrink-0 text-sm font-medium">{label}</span>
        {detail && <span className="truncate text-xs text-muted">{detail}</span>}
        <span className="ml-auto shrink-0 text-xs text-faint">{count}</span>
      </button>
      {open && <ul>{children}</ul>}
    </li>
  )
}

export const QuestionRow = memo(function QuestionRow({
  question,
  paperKey,
  active,
  inPaper,
  depth,
  source,
  onSelect,
}: {
  question: BankQuestionBrief
  paperKey: string
  active: boolean
  inPaper: number | undefined
  depth: number
  source?: string
  onSelect: (key: string) => void
}) {
  return (
    <li>
      <button
        onClick={() => onSelect(questionKey(paperKey, question.n))}
        aria-current={active || undefined}
        className={`my-0.5 block w-full rounded-xl py-2 pr-3 text-left transition-colors ${
          active ? 'bg-accent-container' : 'hover:bg-accent-wash'
        }`}
        style={{ paddingLeft: 10 + depth * 16 }}
      >
        <div className="flex items-center gap-2 text-xs text-muted">
          <span className="flex-1 truncate">
            {source && <span className="text-ink-2">{source} · </span>}
            <span className="font-medium text-ink">Question {question.n}</span> · {question.marks}{' '}
            {question.marks === 1 ? 'mark' : 'marks'}
          </span>
          {inPaper !== undefined && <span className="pill bg-accent text-white">Q{inPaper + 1} in exam</span>}
        </div>
        <div className="mt-0.5 line-clamp-2 text-sm text-ink-2">{snippet(question.stem, question.n) || 'No text'}</div>
      </button>
    </li>
  )
})

export function QuestionTree({
  papers,
  activeKey,
  itemIndexByKey,
  onSelect,
}: {
  papers: BankPaper[]
  activeKey: string | null
  itemIndexByKey: Map<string, number>
  onSelect: (key: string) => void
}) {
  const tree = useMemo(() => buildTree(papers), [papers])
  const [open, setOpen] = useState<Set<string>>(loadOpen)

  const persist = (next: Set<string>) => {
    try {
      localStorage.setItem(OPEN_STORAGE_KEY, JSON.stringify([...next]))
    } catch {
      // a remembered folder is a convenience only
    }
  }

  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      persist(next)
      return next
    })

  // Opening a question from elsewhere (the exam's list, a search) reveals its folder.
  useEffect(() => {
    if (!activeKey) return
    const [paperKey] = activeKey.split('::')
    const p = papers.find((x) => x.paper_key === paperKey)
    if (!p) return
    const path = [`s:${p.subject}`, `y:${p.subject}|${fullYear(p.year)}`, `ss:${p.subject}|${fullYear(p.year)}|${p.session}`, `p:${p.paper_key}`]
    setOpen((prev) => {
      if (path.every((key) => prev.has(key))) return prev
      const next = new Set([...prev, ...path])
      persist(next)
      return next
    })
  }, [activeKey, papers])

  if (tree.length === 0) return <p className="p-4 text-sm text-muted">No papers extracted yet.</p>

  return (
    <ul className="p-2">
      {tree.map((subject) => {
        const subjectKey = `s:${subject.subject}`
        const nPapers = subject.years.reduce((s, y) => s + y.sessions.reduce((t, x) => t + x.papers.length, 0), 0)
        return (
          <FolderRow
            key={subjectKey}
            label={subject.subject}
            detail={subject.name}
            count={nPapers}
            open={open.has(subjectKey)}
            depth={0}
            onToggle={() => toggle(subjectKey)}
          >
            {subject.years.map((year) => {
              const yearKey = `y:${subject.subject}|${year.year}`
              return (
                <FolderRow
                  key={yearKey}
                  label={String(year.year)}
                  count={year.sessions.reduce((s, x) => s + x.papers.length, 0)}
                  open={open.has(yearKey)}
                  depth={1}
                  onToggle={() => toggle(yearKey)}
                >
                  {year.sessions.map((session) => {
                    const sessionKey = `ss:${subject.subject}|${year.year}|${session.session}`
                    return (
                      <FolderRow
                        key={sessionKey}
                        label={`${sessionName(session.session)}${session.session === 'NOV' && year.year === 2020 ? ' (June 2020)' : ''}`}
                        count={session.papers.length}
                        open={open.has(sessionKey)}
                        depth={2}
                        onToggle={() => toggle(sessionKey)}
                      >
                        {session.papers.map((paper) => {
                          const paperFolder = `p:${paper.paper_key}`
                          return (
                            <FolderRow
                              key={paperFolder}
                              label={`${paper.paper}${paper.variant}`}
                              detail={paper.paper_title}
                              count={paper.questions.length}
                              open={open.has(paperFolder)}
                              depth={3}
                              onToggle={() => toggle(paperFolder)}
                            >
                              {paper.questions.map((q) => {
                                const key = questionKey(paper.paper_key, q.n)
                                return (
                                  <QuestionRow
                                    key={key}
                                    question={q}
                                    paperKey={paper.paper_key}
                                    active={key === activeKey}
                                    inPaper={itemIndexByKey.get(key)}
                                    depth={4}
                                    onSelect={onSelect}
                                  />
                                )
                              })}
                            </FolderRow>
                          )
                        })}
                      </FolderRow>
                    )
                  })}
                </FolderRow>
              )
            })}
          </FolderRow>
        )
      })}
    </ul>
  )
}

/** "0620/41 · May/June 2024", for a search result shown outside its folder. */
export const questionSource = (p: BankPaper) => `${paperCode(p)} · ${sitting(p)}`
