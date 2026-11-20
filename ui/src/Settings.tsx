import { useCallback, useEffect, useRef, useState } from 'react'
import { Bot, FileUp, Loader2, Play, Tags } from 'lucide-react'
import { api, errorMessage } from './api'
import { AboutSection, AppearanceSection } from './Appearance'
import { DeleteDataSection } from './DeleteData'
import { TaggingProgress } from './Tagging'
import { useLive } from './live'
import { Modal } from './Modal'
import { SaveFolderField } from './Saving'
import { Select } from './Select'
import type { Settings as SettingsData, SubjectInfo, Topic } from './types'

/** Topics as editable lines, "4.1  Electrolysis": the syllabus's own numbering first. */
function topicsToText(topics: Topic[]): string {
  return topics.map((t) => `${t.id}  ${t.name}`).join('\n')
}

/** Back from lines; a topic's parent is its id up to the last dot ("4.1" under "4"). */
function textToTopics(text: string): Topic[] {
  const rows = text
    .split('\n')
    .map((line) => line.trim().match(/^(\S+)\s+(.+)$/))
    .filter((m): m is RegExpMatchArray => m !== null)
    .map((m) => ({ id: m[1].replace(/[.:]$/, ''), name: m[2].trim() }))
  const ids = new Set(rows.map((r) => r.id))
  return rows.map((r) => {
    const cut = r.id.lastIndexOf('.')
    const parent = cut > 0 ? r.id.slice(0, cut) : null
    return { ...r, parent: parent && ids.has(parent) ? parent : null }
  })
}

export function Settings({ onClose }: { onClose: () => void }) {
  const { tagging, startTagging, version } = useLive()
  const [settings, setSettings] = useState<SettingsData | null>(null)
  const [subjects, setSubjects] = useState<SubjectInfo[]>([])
  const [editing, setEditing] = useState<string | null>(null)

  const loadSubjects = useCallback(() => void api.listSubjects().then(setSubjects).catch(() => {}), [])
  useEffect(() => void api.getSettings().then(setSettings), [])
  useEffect(loadSubjects, [loadSubjects, version])

  return (
    <Modal title="Settings" onClose={onClose} width="max-w-3xl">
      <div className="space-y-7">
        <AppearanceSection />

        <section className="space-y-2">
          <h3 className="font-semibold">Save folder</h3>
          <p className="text-sm text-ink-2">Exams, mark schemes and topic booklets are saved here, each exam in its own folder.</p>
          <SaveFolderField />
        </section>

        <ClaudeSection settings={settings} onChange={setSettings} />

        <section>
          <h3 className="font-semibold">Topics</h3>
          <p className="mt-1 text-sm text-ink-2">
            Each subject needs its syllabus topic list before its questions can be tagged. Tagged questions are what
            “Ask for an exam” chooses from.
          </p>

          {subjects.length === 0 ? (
            <p className="mt-3 text-sm text-muted">No papers in the bank yet.</p>
          ) : (
            <ul className="mt-3 divide-y divide-line rounded-2xl border border-line">
              {subjects.map((s) => (
                <SubjectRow
                  key={s.subject}
                  subject={s}
                  canTag={!!settings?.enabled && s.n_topics > 0 && !tagging.running}
                  onEdit={() => setEditing(s.subject)}
                  onTag={() => void startTagging(s.subject)}
                />
              ))}
            </ul>
          )}

          {(tagging.running || tagging.finished_at) && <TaggingProgress />}
        </section>

        <AboutSection />
        <DeleteDataSection />
      </div>

      {editing && (
        <TopicEditor
          subject={subjects.find((s) => s.subject === editing)!}
          keySet={!!settings?.enabled}
          onClose={() => {
            setEditing(null)
            loadSubjects()
          }}
        />
      )}
    </Modal>
  )
}

const MODEL_NAMES: Record<string, string> = {
  'claude-haiku-5-5': 'Haiku 5.5 - fastest and cheapest',
  'claude-sonnet-5-5': 'Sonnet 5.5 - balanced',
  'claude-opus-5-5': 'Opus 5.5 - most capable, costs the most',
}

function ClaudeSection({ settings, onChange }: { settings: SettingsData | null; onChange: (s: SettingsData) => void }) {
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const check = async () => {
    setBusy(true)
    setStatus(null)
    try {
      const result = await api.checkClaude()
      setStatus({ ok: true, text: `Connected - Claude answered in ${result.seconds} s.` })
    } catch (e) {
      setStatus({ ok: false, text: errorMessage(e) })
    } finally {
      setBusy(false)
    }
  }

  const save = async (changes: Partial<SettingsData>) => onChange(await api.saveSettings(changes))
  const onApi = settings?.provider === 'api'

  return (
    <section>
      <h3 className="flex items-center gap-2 font-semibold">
        <Bot size={16} className="text-accent" /> Claude
      </h3>
      <p className="mt-1 text-sm text-ink-2">
        Claude tags questions with syllabus topics, reads your exam requests, and reviews papers with problems. It never
        writes questions: its fixes only move, join and number what was extracted, and the paper's own checks test each
        one.
      </p>

      {settings && (
        <div className="mt-3 space-y-3 rounded-2xl border border-line px-4 py-3">
          {/* No API keys in the app (user-decided): Claude Code here for testing; teachers will sign in
              with their Classark account, whose server reaches Claude by identity federation. */}
          <fieldset className="space-y-2 text-sm">
            {[
              {
                value: 'claude_code',
                name: 'Claude Code on this computer',
                text: 'Runs through the Claude Code install here, on the Claude subscription it’s logged in with, and counts against that plan’s usage limits.',
              },
              {
                value: 'classark',
                name: 'Classark account',
                text: 'What teachers use: through the Classark server, on the signed-in account’s credit (sign in from the account button in the top bar). Topic lists come from the server.',
              },
            ].map((p) => (
              <label key={p.value} className="flex cursor-pointer items-start gap-2">
                <input
                  type="radio"
                  name="provider"
                  className="mt-1"
                  checked={settings.provider === p.value || (p.value === 'claude_code' && onApi)}
                  onChange={() => {
                    setStatus(null)
                    void save({ provider: p.value as SettingsData['provider'] })
                  }}
                />
                <span>
                  <span className="font-medium">{p.name}</span>
                  <span className="block text-xs text-muted">{p.text}</span>
                </span>
              </label>
            ))}
          </fieldset>

          <div className="flex items-center gap-2">
            <button className="btn-secondary px-3 py-1.5 text-xs" disabled={busy} onClick={() => void check()}>
              {busy && <Loader2 size={13} className="animate-spin" />} Test connection
            </button>
            {status && <span className={`text-sm ${status.ok ? 'text-success' : 'text-danger'}`}>{status.text}</span>}
          </div>
        </div>
      )}

      <label className="mt-3 flex cursor-pointer items-center gap-3">
        <input
          type="checkbox"
          className="size-4 accent-[var(--accent)]"
          checked={!!settings?.enabled}
          disabled={!settings}
          onChange={(e) => {
            setStatus(null)
            void save({ enabled: e.target.checked })
          }}
        />
        <span className="text-sm font-medium">Use Claude</span>
      </label>

      {settings?.enabled && (
        <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
          <label className="flex flex-col gap-1">
            <span className="text-ink-2">Model for tagging</span>
<Select
              value={settings.tag_model}
              options={settings.models.map((m) => ({ value: m, label: MODEL_NAMES[m] ?? m }))}
              onChange={(v) => void save({ tag_model: v })}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-ink-2">Model for exam requests and syllabuses</span>
<Select
              value={settings.request_model}
              options={settings.models.map((m) => ({ value: m, label: MODEL_NAMES[m] ?? m }))}
              onChange={(v) => void save({ request_model: v })}
            />
          </label>
        </div>
      )}

      <p className="mt-3 rounded-xl bg-info-wash px-3 py-2 text-xs leading-relaxed text-info">
        What leaves this computer: to tag, the extracted text of a paper’s questions and their mark scheme answers goes
        to Claude{onApi ? ', through the Anthropic API' : ''}. To read a request, only what you typed and the
        topic names are sent. Your PDFs are never uploaded.{' '}
        {onApi
          ? 'If tagging reaches the API’s rate limit it pauses, and “Tag the rest” carries on later.'
          : 'Use counts against your Claude plan’s usage limits; if tagging reaches the limit it pauses, and “Tag the rest” carries on once it resets.'}
      </p>
    </section>
  )
}

function SubjectRow({
  subject: s,
  canTag,
  onEdit,
  onTag,
}: {
  subject: SubjectInfo
  canTag: boolean
  onEdit: () => void
  onTag: () => void
}) {
  const pct = s.n_parts ? Math.round((100 * s.n_tagged) / s.n_parts) : 0
  return (
    <li className="flex items-center gap-4 px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="text-sm">
          <span className="font-medium">{s.subject}</span> <span className="text-ink-2">{s.name}</span>
        </div>
        <div className="mt-1 flex items-center gap-2 text-xs text-muted">
          <span>{s.n_topics ? `${s.n_topics} topics` : 'No topic list'}</span>
          <span aria-hidden>·</span>
          <span>
            {s.n_tagged.toLocaleString()} of {s.n_parts.toLocaleString()} parts tagged
          </span>
          <span className="h-1.5 w-24 overflow-hidden rounded-full bg-sunken">
            <span className="block h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
          </span>
        </div>
      </div>
      <button className="btn-secondary" onClick={onEdit}>
        <Tags size={14} /> {s.n_topics ? 'Edit topics' : 'Add topics'}
      </button>
      <button
        className="btn-primary"
        disabled={!canTag || s.n_tagged >= s.n_parts}
        onClick={onTag}
        title={s.n_topics ? undefined : 'Add a topic list first'}
      >
        <Play size={14} /> {s.n_tagged ? 'Tag the rest' : 'Tag questions'}
      </button>
    </li>
  )
}

function TopicEditor({ subject, keySet, onClose }: { subject: SubjectInfo; keySet: boolean; onClose: () => void }) {
  const [text, setText] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState<'reading' | 'saving' | null>(null)
  const [error, setError] = useState<string | null>(null)
  // learning outcomes by topic id: the text box holds only ids and names, so they travel
  // beside it and go back with whichever ids are still there on save
  const [outcomes, setOutcomes] = useState<Record<string, string[]>>({})
  const fileInput = useRef<HTMLInputElement>(null)

  const show = (list: Topic[]) => {
    setText(topicsToText(list))
    setOutcomes(Object.fromEntries(list.filter((t) => t.outcomes?.length).map((t) => [t.id, t.outcomes ?? []])))
  }

  useEffect(() => {
    void api.getTopics(subject.subject).then((t) => {
      show(t.topics)
      setLoaded(true)
    })
  }, [subject.subject])

  const readSyllabus = async (file: File) => {
    setBusy('reading')
    setError(null)
    try {
      const result = await api.topicsFromSyllabus(subject.subject, file)
      show(result.topics)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  const topics = textToTopics(text).map((t) => ({ ...t, outcomes: outcomes[t.id] ?? [] }))
  const withOutcomes = topics.filter((t) => t.outcomes.length > 0)
  const save = async () => {
    setBusy('saving')
    try {
      await api.saveTopics(subject.subject, topics)
      onClose()
    } catch (e) {
      setError(errorMessage(e))
      setBusy(null)
    }
  }

  return (
    <Modal title={`${subject.subject} ${subject.name} topics`} onClose={onClose} width="max-w-2xl">
      <p className="text-sm text-ink-2">
        Upload the syllabus PDF and Claude lists its topics and their learning outcomes for you to check, or type them:
        one per line, its number then its name. A number like 4.1 goes under 4. Tagging matches questions against the
        learning outcomes, so a list with them tags better than headings alone.
      </p>

      <div className="mt-3 flex items-center gap-2">
        <input
          ref={fileInput}
          type="file"
          accept="application/pdf"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) void readSyllabus(file)
            e.target.value = ''
          }}
        />
        <button className="btn-secondary" disabled={!keySet || busy !== null} onClick={() => fileInput.current?.click()}>
          {busy === 'reading' ? <Loader2 size={14} className="animate-spin" /> : <FileUp size={14} />}
          {busy === 'reading' ? 'Reading the syllabus… (a minute or two)' : 'Upload syllabus'}
        </button>
        {!keySet && <span className="text-xs text-muted">Switch on Claude above to read a syllabus.</span>}
        <span className="ml-auto text-xs text-muted">
          {topics.length} topics{withOutcomes.length > 0 && `, ${withOutcomes.length} with learning outcomes`}
        </span>
      </div>

      <textarea
        className="field scroll-quiet mt-3 h-80 resize-y font-[inherit] leading-relaxed"
        value={text}
        disabled={!loaded}
        spellCheck={false}
        placeholder={'1  States of matter\n1.1  Solids, liquids and gases\n2  Atoms, elements and compounds'}
        onChange={(e) => setText(e.target.value)}
      />
      {withOutcomes.length > 0 && (
        <details className="mt-2 text-sm">
          <summary className="cursor-pointer text-ink-2">Learning outcomes</summary>
          <div className="scroll-quiet mt-1 max-h-64 space-y-2 overflow-y-auto rounded-xl border border-line p-3">
            {withOutcomes.map((t) => (
              <div key={t.id}>
                <div className="font-medium">
                  {t.id} {t.name}
                </div>
                <ul className="ml-4 list-disc text-ink-2">
                  {t.outcomes.map((o, i) => (
                    <li key={i}>{o}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </details>
      )}
      {error && <p className="mt-2 text-sm text-danger">{error}</p>}
      <p className="mt-2 text-xs text-muted">
        Changing the list doesn’t re-tag questions already tagged. Questions tagged with a topic you remove keep its id
        until tagged again.
      </p>

      <div className="mt-4 flex justify-end gap-2">
        <button className="btn-secondary" onClick={onClose}>
          Cancel
        </button>
        <button className="btn-primary" disabled={busy !== null || !loaded} onClick={() => void save()}>
          Save topics
        </button>
      </div>
    </Modal>
  )
}
