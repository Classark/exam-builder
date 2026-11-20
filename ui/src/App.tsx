import { useEffect, useState } from 'react'
import { BookOpen, ClipboardCheck, FolderOpen, Layers, Settings as SettingsIcon, Sparkles, UserRound } from 'lucide-react'
import { AccountMenu } from './AccountMenu'
import { ActivityBar, ActivityProvider } from './activity'
import { api } from './api'
import { Classification } from './Classification'
import { ClaudeChat } from './ClaudeChat'
import { useDesktop, WindowControls } from './desktop'
import { QuestionBank } from './QuestionBank'
import { Reviewer } from './Reviewer'
import { ScanControl } from './ScanControl'
import { LibraryStatus } from './LibraryStatus'
import { Settings } from './Settings'
import { TaggingControl } from './Tagging'
import { SavedFiles, SaveFolderPrompt, SaveFolderSettings, SaveNotice } from './Saving'
import { SignInScreen } from './SignIn'
import { TermsPrompt } from './Terms'
import { UpdateScreen } from './UpdateScreen'
import { useLive } from './live'

type Tab = 'reviewer' | 'bank' | 'claude' | 'classified'

function stored<T extends string>(key: string, allowed: T[]): T | null {
  try {
    const value = localStorage.getItem(key)
    return allowed.includes(value as T) ? (value as T) : null
  } catch {
    return null
  }
}

function remember(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // a remembered choice is a convenience only
  }
}

function App() {
  const [tab, setTab] = useState<Tab>(() => stored('qbx.tab', ['reviewer', 'bank', 'claude', 'classified']) ?? 'reviewer')
  const desktop = useDesktop()
  const { papers, ai, edition, account, ready, update } = useLive()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  // A release build shows only the sign-in screen until the teacher signs in (user-decided:
  // no carrying on without an account); signing out shows it again.
  // The terms come before anything else, sign-in included (user-asked; qbx/terms.py):
  // null until known, then the version to agree to, or '' once agreed.
  const [termsToAgree, setTermsToAgree] = useState<string | null>(null)
  useEffect(() => {
    void api.getTerms().then((t) => setTermsToAgree(t.accepted ? '' : t.version)).catch(() => setTermsToAgree(''))
  }, [])
  // Before everything, terms included: a release build older than Classark's minimum
  // version (config/app, qbx/app_version.py) shows "Please update" and nothing else.
  const updateGate = ready && edition === 'release' && !!update?.required
  const termsDue = termsToAgree !== ''
  const signInGate = ready && edition === 'release' && account.status !== 'signed_in'
  const gate = updateGate || signInGate || termsDue
  const [savedOpen, setSavedOpen] = useState(false)
  // Asked once, straight after sign-in (user-decided): where exams are saved.
  const [askFolder, setAskFolder] = useState(false)
  useEffect(() => {
    if (!ready || gate) return
    let live = true
    void api.getSaving().then((s) => live && setAskFolder(!s.chosen)).catch(() => {})
    return () => {
      live = false
    }
  }, [ready, gate])
  // an exam handed between the two modes, opened when the other one shows
  const [handedExam, setHandedExam] = useState<string | null>(null)
  const open = papers.reduce((sum, p) => sum + (p.n_open > 0 ? 1 : 0), 0)

  const go = (next: Tab) => {
    setTab(next)
    remember('qbx.tab', next)
  }
  const openIn = (next: Tab, exam: string) => {
    setHandedExam(exam)
    go(next)
  }

  const tabs: { id: Tab; label: string; icon: typeof BookOpen; badge?: number }[] = [
    { id: 'reviewer', label: 'Review', icon: ClipboardCheck, badge: open || undefined },
  ]
  // building an exam: by hand from the question bank, or by talking to Claude
  type Mode = { id: Tab; label: string; icon: typeof BookOpen; title: string }
  const modes = (
    [
    { id: 'bank', label: 'Manual', icon: BookOpen, title: 'Build exams by hand from the question bank' },
    { id: 'claude', label: 'AI', icon: Sparkles, title: 'Talk to Claude about the exam you want' },
    // not an exam: every past question on a topic, a booklet per topic
    { id: 'classified', label: 'Classification', icon: Layers, title: 'Topic booklets: every past question on a topic' },
    ] as Mode[]
  ).filter((m) => ai || m.id !== 'claude') // a build without Claude (release, for now) has no AI mode
  // the AI tab remembered from a build that had it shows the question bank instead
  const shown: Tab = !ai && tab === 'claude' ? 'bank' : tab

  return (
    <ActivityProvider>
    <div className="flex h-full flex-col">
      {/* Classark's top bar: navy, square-cornered, two-weight wordmark */}
      <header className="no-print relative z-30 flex shrink-0 items-center gap-3 bg-inverse px-4 py-3 whitespace-nowrap lg:gap-6 lg:px-6 shadow-[0_4px_20px_rgba(45,48,71,0.15)] [app-region:drag] [&_:is(button,input,select,textarea,a,.absolute,.fixed)]:[app-region:no-drag]">
        <div className="flex shrink-0 items-center gap-2.5 text-on-inverse">
          <img src="/icon.svg" alt="" className="h-9 w-9" draggable={false} />
          <span className="text-[1.35rem] tracking-tight" aria-label="Classark">
            <span className="font-light">Class</span>
            <span className="font-semibold">ark</span>
          </span>
        </div>

        <nav className={`flex shrink-0 items-center gap-2 ${gate ? 'invisible' : ''}`} aria-label="Sections">
          {tabs.map(({ id, label, icon: Icon, badge }) => (
            <button
              key={id}
              onClick={() => go(id)}
              aria-current={tab === id ? 'page' : undefined}
              title={label}
              className={`flex items-center gap-2 rounded-xl border px-4 py-2 text-sm font-medium text-on-inverse transition-all ${
                tab === id
                  ? 'border-accent bg-accent shadow-[0_4px_12px_rgba(224,122,95,0.3)]'
                  : 'border-white/15 bg-white/10 hover:border-accent/40 hover:bg-accent/30'
              }`}
            >
              <Icon size={16} strokeWidth={2} />
              {/* icon only when the window is too narrow for the whole bar */}
              <span className="hidden xl:inline">{label}</span>
              {badge !== undefined && (
                <span
                  className={`rounded-full px-1.5 text-xs leading-5 ${tab === id ? 'bg-white/25' : 'bg-accent'}`}
                  title={`${badge} papers need review`}
                >
                  {badge}
                </span>
              )}
            </button>
          ))}
          <div className="flex rounded-xl border border-white/15 bg-white/10 p-0.5" role="group" aria-label="Exam builder mode">
            {modes.map(({ id, label, icon: Icon, title }) => (
              <button
                key={id}
                onClick={() => go(id)}
                aria-current={shown === id ? 'page' : undefined}
                title={title}
                className={`flex items-center gap-2 rounded-[10px] px-4 py-1.5 text-sm font-medium text-on-inverse transition-all ${
                  shown === id ? 'bg-accent shadow-[0_4px_12px_rgba(224,122,95,0.3)]' : 'hover:bg-accent/30'
                }`}
              >
                <Icon size={16} strokeWidth={2} />
                <span className="hidden xl:inline">{label}</span>
              </button>
            ))}
          </div>
        </nav>

        <div className="ml-auto flex min-w-0 items-center gap-2">
          {!gate && <TaggingControl />}
          {!gate && <ScanControl />}
          {!gate && <LibraryStatus />}
          {!gate && (
            <button
              onClick={() => setSavedOpen(true)}
              className="rounded-xl border border-white/15 bg-white/10 p-2.5 text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
              aria-label="Saved files"
              title="Saved files"
            >
              <FolderOpen size={16} />
            </button>
          )}
          {/* Every build: theme, save folder, About & licences; debug builds add Claude Code,
              models and topics (qbx/edition.py) */}
          <button
            onClick={() => setSettingsOpen(true)}
            className="rounded-xl border border-white/15 bg-white/10 p-2.5 text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
            aria-label="Settings"
            title={edition === 'debug' ? 'Settings: appearance, save folder, Claude, topics, about' : 'Settings: appearance, save folder, about'}
          >
            <SettingsIcon size={16} />
          </button>
          <button
            onClick={() => setAccountOpen(true)}
            className="rounded-xl border border-white/15 bg-white/10 p-2.5 text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
            aria-label="Classark account"
            title={account.status === 'signed_in' ? `Signed in as ${account.email ?? ''}` : 'Sign in to Classark'}
          >
            <UserRound size={16} />
          </button>
        </div>
        {desktop && <WindowControls desktop={desktop} />}
      </header>
      {!gate && <ActivityBar />}

      {updateGate && update ? (
        <UpdateScreen update={update} />
      ) : signInGate ? (
        <SignInScreen account={account} />
      ) : termsDue ? (
        <div className="min-h-0 flex-1 bg-page" />
      ) : (
      <main className="min-h-0 flex-1">
        {shown === 'reviewer' ? (
          <Reviewer />
        ) : shown === 'bank' ? (
          <QuestionBank openExam={handedExam} onAskClaude={(name) => openIn('claude', name)} />
        ) : shown === 'classified' ? (
          <Classification />
        ) : (
          <ClaudeChat
            openExam={handedExam}
            onOpenSettings={() => setSettingsOpen(true)}
            onEditByHand={(name) => openIn('bank', name)}
          />
        )}
      </main>
      )}
      {settingsOpen &&
        (edition === 'debug' ? (
          <Settings onClose={() => setSettingsOpen(false)} />
        ) : (
          <SaveFolderSettings onClose={() => setSettingsOpen(false)} />
        ))}
      {termsToAgree && !updateGate && <TermsPrompt version={termsToAgree} onAccepted={() => setTermsToAgree('')} />}
      {savedOpen && <SavedFiles onClose={() => setSavedOpen(false)} />}
      {askFolder && !gate && <SaveFolderPrompt onDone={() => setAskFolder(false)} />}
      <SaveNotice />
      {accountOpen && (
        <AccountMenu
          account={account}
          onClose={() => setAccountOpen(false)}
        />
      )}
    </div>
    </ActivityProvider>
  )
}

export default App
