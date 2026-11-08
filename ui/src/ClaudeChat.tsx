import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, ArrowUp, Download, Loader2, MessageSquarePlus, Pencil, Sparkles } from 'lucide-react'
import { api } from './api'
import { PagePreview } from './PagePreview'
import { useSourcePicker } from './SourcePicker'
import type { ChatMessage, QuizDetail, QuizSummary } from './types'

const SUGGESTIONS = [
  'A 40-mark test on electrolysis, no multiple choice',
  '20 multiple-choice questions from the last three years',
  'A revision paper on rates of reaction and energetics, papers 2 and 4',
]

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`
const paperName = (key: string) => key.replace(/_/g, ' ')

/** The Claude tab: talk to Claude about the exam you want, then about what to change.
 * Claude replies and, when it has enough to go on, makes or changes the exam - always
 * from the bank's own questions, through the same builder as everything else. */
export function ClaudeChat({
  openExam,
  onOpenSettings,
  onEditByHand,
}: {
  openExam: string | null
  onOpenSettings: () => void
  onEditByHand: (name: string) => void
}) {
  const [quizzes, setQuizzes] = useState<QuizSummary[]>([])
  const [quizName, setQuizName] = useState<string | null>(openExam)
  const [quiz, setQuiz] = useState<QuizDetail | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  const { board, sources, picker } = useSourcePicker(busy)
  const thread = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLTextAreaElement>(null)

  const refreshQuizzes = () => void api.listQuizzes().then(setQuizzes).catch(() => undefined)
  useEffect(refreshQuizzes, [])
  useEffect(() => setQuizName(openExam), [openExam])

  useEffect(() => {
    setError(null)
    if (!quizName) {
      setQuiz(null)
      setMessages([])
      return
    }
    let stale = false
    void api
      .getQuiz(quizName)
      .then((d) => {
        if (stale) return
        setQuiz(d)
        setMessages(d.chat ?? [])
      })
      .catch((e: unknown) => !stale && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      stale = true
    }
  }, [quizName, reload])

  // the newest message in view
  useEffect(() => {
    thread.current?.scrollTo({ top: thread.current.scrollHeight, behavior: 'smooth' })
  }, [messages, busy])

  const send = async (text: string) => {
    text = text.trim()
    if (!text || busy) return
    const before = messages
    const next: ChatMessage[] = [...messages, { role: 'teacher', text }]
    setMessages(next)
    setDraft('')
    setBusy(true)
    setError(null)
    try {
      const reply = await api.chat({ messages: next, quiz: quizName, board, sources })
      setMessages([...next, reply.message])
      if (reply.quiz !== quizName) setQuizName(reply.quiz)
      else if (reply.action !== 'none') setReload((n) => n + 1)
      if (reply.action !== 'none') refreshQuizzes()
    } catch (e) {
      // nothing was said: the message goes back in the box to try again
      setMessages(before)
      setDraft(text)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      input.current?.focus()
    }
  }

  const startNew = () => {
    setQuizName(null)
    setDraft('')
    input.current?.focus()
  }

  const talked = quizzes.filter((q) => q.has_chat)
  const others = quizzes.filter((q) => !q.has_chat)

  return (
    <div className="flex h-full min-h-0 gap-4 p-4">
      {/* ── Left: conversations ── */}
      <aside className="card flex w-[260px] shrink-0 flex-col overflow-hidden">
        <div className="border-b border-line p-3">
          <button className="btn-primary w-full justify-center" onClick={startNew} disabled={busy}>
            <MessageSquarePlus size={16} /> New exam with Claude
          </button>
        </div>
        <nav className="scroll-quiet min-h-0 flex-1 space-y-4 overflow-auto p-2" aria-label="Exams">
          {(
            [
              ['Conversations', talked],
              ['Your other exams', others],
            ] as const
          ).map(
            ([heading, list]) =>
              list.length > 0 && (
                <section key={heading}>
                  <h3 className="px-2 pb-1 text-xs font-semibold uppercase tracking-wide text-muted">{heading}</h3>
                  <ul className="space-y-0.5">
                    {list.map((q) => (
                      <li key={q.name}>
                        <button
                          onClick={() => setQuizName(q.name)}
                          disabled={busy}
                          className={`w-full rounded-xl px-2.5 py-1.5 text-left text-sm transition-colors ${
                            q.name === quizName ? 'bg-accent-wash font-semibold' : 'hover:bg-hover'
                          }`}
                        >
                          <div className="truncate">{q.name}</div>
                          <div className="text-xs font-normal text-muted">{plural(q.n_items, 'question')}</div>
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              ),
          )}
          {quizzes.length === 0 && <p className="px-2 text-xs text-muted">Exams you make appear here.</p>}
        </nav>
      </aside>

      {/* ── Middle: the conversation - all the room until there's an exam to show beside it ── */}
      <section className={`card flex min-w-0 flex-col overflow-hidden ${quiz ? 'w-[480px] shrink-0' : 'flex-1'}`}>
        <header className="flex items-center gap-2 border-b border-line px-5 py-3">
          <Sparkles size={16} className="shrink-0 text-accent" />
          <h2 className="min-w-0 flex-1 truncate font-semibold">{quiz ? quiz.name : 'New exam'}</h2>
          {quiz && (
            <span className="shrink-0 text-xs text-muted">
              {plural(quiz.items.length, 'question')} · {plural(quiz.total_marks, 'mark')}
            </span>
          )}
        </header>

        <div ref={thread} className="scroll-quiet min-h-0 flex-1 space-y-4 overflow-auto bg-sunken px-5 py-5">
          {messages.length === 0 && !busy && (
            <div className="mx-auto max-w-lg pt-8 text-center">
              <Sparkles size={28} className="mx-auto text-accent" />
              <h3 className="mt-3 text-lg font-semibold">
                {quiz ? `What should change in “${quiz.name}”?` : 'What exam do you need?'}
              </h3>
              <p className="mt-1 text-sm text-ink-2">
                {quiz
                  ? 'Ask Claude to swap, remove or add questions, change the total, or try different questions.'
                  : 'Describe it in your own words. Claude asks if it needs more, then builds it from your question bank - never with questions already in your other exams.'}
              </p>
              {!quiz && (
                <div className="mt-5 flex flex-col items-center gap-2">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} className="btn-secondary text-sm" onClick={() => void send(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
          {messages.map((m, i) => (
            <Message key={i} message={m} onOpenSettings={onOpenSettings} />
          ))}
          {busy && (
            <div className="flex items-center gap-2 text-sm text-muted">
              <Loader2 size={15} className="animate-spin text-accent" /> Claude is reading that
              {quiz ? '' : ' and may choose questions'}…
            </div>
          )}
        </div>

        <div className="space-y-2 border-t border-line p-3">
          {error && (
            <p className="rounded-lg bg-danger-wash px-3 py-2 text-xs text-danger">
              {error}
              {/Settings/.test(error) && (
                <button className="ml-1 font-medium underline" onClick={onOpenSettings}>
                  Open Settings
                </button>
              )}
            </p>
          )}
          {!quiz && (
            <div>
              <div className="mb-1 text-xs text-muted">Draw on</div>
              {picker}
            </div>
          )}
          <div className="flex items-end gap-2 rounded-2xl border-[1.5px] border-line bg-card py-1.5 pl-4 pr-1.5 transition-colors focus-within:border-accent">
            <textarea
              ref={input}
              rows={1}
              value={draft}
              disabled={busy}
              onChange={(e) => {
                setDraft(e.target.value)
                // grows with what's typed, up to a few lines
                e.target.style.height = 'auto'
                e.target.style.height = `${Math.min(e.target.scrollHeight, 160)}px`
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  void send(draft)
                  e.currentTarget.style.height = 'auto'
                }
              }}
              placeholder={quiz ? 'Ask for a change: “replace Q3 with an electrolysis question”' : 'Describe the exam you want…'}
              className="scroll-quiet max-h-40 min-w-0 flex-1 resize-none bg-transparent py-1.5 text-sm leading-relaxed text-ink outline-none placeholder:text-faint"
              aria-label="Message Claude"
            />
            <button
              className={`flex size-9 shrink-0 items-center justify-center rounded-xl transition-colors ${
                draft.trim() && !busy ? 'bg-accent text-white hover:bg-accent-hover' : 'bg-sunken text-faint'
              }`}
              onClick={() => void send(draft)}
              disabled={!draft.trim() || busy}
              aria-label="Send"
              title="Send (Enter) - Shift+Enter for a new line"
            >
              {busy ? <Loader2 size={17} className="animate-spin" /> : <ArrowUp size={17} />}
            </button>
          </div>
        </div>
      </section>

      {/* ── Right: the exam as it stands ── */}
      {quiz && <ExamPanel quiz={quiz} onEditByHand={() => onEditByHand(quiz.name)} />}
    </div>
  )
}

function Message({ message, onOpenSettings }: { message: ChatMessage; onOpenSettings: () => void }) {
  if (message.role === 'teacher') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[75%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent px-4 py-2 text-sm text-white">
          {message.text}
        </div>
      </div>
    )
  }
  const o = message.outcome
  const changes = o
    ? [
        o.removed && o.removed.length > 0 && `took out ${o.removed.join(', ')}`,
        o.added ? `added ${plural(o.added, 'question')}` : '',
      ].filter(Boolean)
    : []
  return (
    <div className="flex gap-2.5">
      <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-accent-container">
        <Sparkles size={14} className="text-accent" />
      </div>
      <div className="min-w-0 max-w-[80%] space-y-2">
        <div className="whitespace-pre-wrap rounded-2xl rounded-tl-md bg-card px-4 py-2 text-sm shadow-sm">{message.text}</div>
        {o && (
          <div className="rounded-xl border border-line bg-card px-3 py-2 text-xs">
            <div className="font-semibold">{o.name}</div>
            <div className="text-ink-2">
              {changes.length > 0 && <>{changes.join(', ')} · </>}
              {plural(o.n_items, 'question')} · {plural(o.total_marks, 'mark')}
            </div>
            {!o.exact && (
              <div className="mt-1 flex items-start gap-1.5 text-warning">
                <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                Couldn't make exactly {o.target_marks} marks from the tagged questions not already in your other exams.
              </div>
            )}
            {o.missing_topics.length > 0 && (
              <div className="mt-1 flex items-start gap-1.5 text-warning">
                <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                No questions found on: {o.missing_topics.join(', ')}
              </div>
            )}
          </div>
        )}
        {message.error && (
          <div className="rounded-xl bg-danger-wash px-3 py-2 text-xs text-danger">
            {message.error}
            {/Settings/.test(message.error) && (
              <button className="ml-1 font-medium underline" onClick={onOpenSettings}>
                Open Settings
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function ExamPanel({ quiz, onEditByHand }: { quiz: QuizDetail; onEditByHand: () => void }) {
  // numbered as printed: written questions first, then multiple choice, each from 1
  const written = quiz.items.filter((i) => !i.mcq)
  const mcq = quiz.items.filter((i) => i.mcq)
  const canExport = quiz.items.length > 0
  const preview = canExport
    ? {
        items: quiz.items.map((i) => ({
          paper_key: i.paper_key,
          question_number: i.question_number,
          part_paths: i.parts.map((p) => p.part_path),
        })),
        title: quiz.name,
        duration_minutes: quiz.duration_minutes ?? undefined,
      }
    : null
  const marks = (item: QuizDetail['items'][number]) => item.parts.reduce((s, p) => s + (p.marks ?? 0), 0)

  return (
    <aside className="card flex min-w-0 flex-1 overflow-hidden">
      <div className="scroll-quiet w-[340px] shrink-0 space-y-3 overflow-auto border-r border-line p-4">
        <div className="flex gap-2">
          <a
            href={canExport ? api.exportQuizQpPdfUrl(quiz.name) : undefined}
            aria-disabled={!canExport}
            className={`btn-primary flex-1 justify-center whitespace-nowrap ${canExport ? '' : 'pointer-events-none opacity-50'}`}
          >
            <Download size={15} /> Question paper
          </a>
          <a
            href={canExport ? api.exportQuizMsPdfUrl(quiz.name) : undefined}
            aria-disabled={!canExport}
            className={`btn-secondary flex-1 justify-center whitespace-nowrap ${canExport ? '' : 'pointer-events-none opacity-50'}`}
          >
            <Download size={15} /> Mark scheme
          </a>
        </div>
        <button className="btn-ghost w-full justify-center text-sm" onClick={onEditByHand}>
          <Pencil size={14} /> Edit by hand in the question bank
        </button>
        {(
          [
            ['Written questions', written, 'Q'],
            ['Multiple choice', mcq, 'MCQ'],
          ] as const
        ).map(
          ([heading, list, prefix]) =>
            list.length > 0 && (
              <section key={heading}>
                <h3 className="pb-1 text-xs font-semibold uppercase tracking-wide text-muted">
                  {heading} · {list.length}
                </h3>
                <ul className="space-y-0.5 text-sm">
                  {list.map((item, i) => (
                    <li key={`${item.paper_key}::${item.question_number}`} className="flex gap-2">
                      <span className="w-12 shrink-0 font-semibold">
                        {prefix}
                        {i + 1}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-ink-2">
                        {paperName(item.paper_key)} Q{item.question_number}
                        {item.missing && <span className="text-danger"> · missing</span>}
                      </span>
                      <span className="shrink-0 text-xs text-muted">{plural(marks(item), 'mark')}</span>
                    </li>
                  ))}
                </ul>
              </section>
            ),
        )}
      </div>
      <div className="scroll-quiet min-w-0 flex-1 overflow-auto bg-sunken p-4">
        <div className="mx-auto max-w-3xl">
          <PagePreview request={preview} empty="No questions yet." />
        </div>
      </div>
    </aside>
  )
}
