import { useEffect, useState } from 'react'
import { BookOpen, ClipboardCheck, ExternalLink, Info, Layers, Loader2, Moon, Settings as SettingsIcon, Sparkles, Sun, UserRound } from 'lucide-react'
import { About } from './About'
import { AccountMenu } from './AccountMenu'
import { Classification } from './Classification'
import { ClaudeChat } from './ClaudeChat'
import { useDesktop, WindowControls } from './desktop'
import { QuestionBank } from './QuestionBank'
import { Reviewer } from './Reviewer'
import { ScanControl } from './ScanControl'
import { Settings } from './Settings'
import { SignInScreen } from './SignIn'
import { useLive } from './live'

type Tab = 'reviewer' | 'bank' | 'claude' | 'classified'
type Theme = 'light' | 'dark'

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

function useTheme(): [Theme, () => void] {
  const system = () => (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
  const [theme, setTheme] = useState<Theme>(() => stored('qbx.theme', ['light', 'dark']) ?? system())
  useEffect(() => {
    document.documentElement.dataset.theme = theme
  }, [theme])
  return [
    theme,
    () =>
      setTheme((current) => {
        const next = current === 'dark' ? 'light' : 'dark'
        remember('qbx.theme', next)
        return next
      }),
  ]
}

function App() {
  const [tab, setTab] = useState<Tab>(() => stored('qbx.tab', ['reviewer', 'bank', 'claude', 'classified']) ?? 'reviewer')
  const [theme, toggleTheme] = useTheme()
  const desktop = useDesktop()
  const { papers, tagging, ai, edition, account, ready } = useLive()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [aboutOpen, setAboutOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  // A release build opens on the sign-in screen until the teacher signs in or carries on
  // without an account (remembered here; signing out shows it again).
  const [skipped, setSkipped] = useState(() => stored('qbx.signin', ['skipped']) === 'skipped')
  const gate = ready && edition === 'release' && account.status !== 'signed_in' && !skipped
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
              <span className="hidden lg:inline">{label}</span>
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
                <span className="hidden lg:inline">{label}</span>
              </button>
            ))}
          </div>
        </nav>

        <div className="ml-auto flex min-w-0 items-center gap-2">
          {tagging.running && (
            <button
              onClick={() => setSettingsOpen(true)}
              className="flex items-center gap-2 rounded-xl bg-white/10 px-3 py-2 text-xs text-on-inverse"
              title="Tagging questions with topics"
            >
              <Loader2 size={13} className="animate-spin" />
              {tagging.total ? `Tagging ${tagging.done ?? 0} of ${tagging.total}` : 'Tagging…'}
            </button>
          )}
          {!gate && <ScanControl />}
          {desktop && (
            <button
              onClick={() => void desktop.open_in_browser()}
              className="hidden rounded-xl border border-white/15 bg-white/10 p-2.5 text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30 lg:block"
              aria-label="Open in browser"
              title="Open in your web browser"
            >
              <ExternalLink size={16} />
            </button>
          )}
          {/* Settings (Claude Code, models) are for testing: debug builds only (qbx/edition.py) */}
          {edition === 'debug' && (
            <button
              onClick={() => setSettingsOpen(true)}
              className="rounded-xl border border-white/15 bg-white/10 p-2.5 text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
              aria-label="Settings"
              title="Settings: Claude and topics"
            >
              <SettingsIcon size={16} />
            </button>
          )}
          <button
            onClick={() => setAccountOpen(true)}
            className="rounded-xl border border-white/15 bg-white/10 p-2.5 text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
            aria-label="Classark account"
            title={account.status === 'signed_in' ? `Signed in as ${account.email ?? ''}` : 'Sign in to Classark'}
          >
            <UserRound size={16} />
          </button>
          <button
            onClick={() => setAboutOpen(true)}
            className="rounded-xl border border-white/15 bg-white/10 p-2.5 text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
            aria-label="About and licences"
            title="About & licences"
          >
            <Info size={16} />
          </button>
          <button
            onClick={toggleTheme}
            className="rounded-xl border border-white/15 bg-white/10 p-2.5 text-on-inverse transition-colors hover:border-accent/40 hover:bg-accent/30"
            aria-label={theme === 'dark' ? 'Use light theme' : 'Use dark theme'}
            title={theme === 'dark' ? 'Light theme' : 'Dark theme'}
          >
            {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
          </button>
        </div>
        {desktop && <WindowControls desktop={desktop} />}
      </header>

      {gate ? (
        <SignInScreen
          account={account}
          onSkip={() => {
            remember('qbx.signin', 'skipped')
            setSkipped(true)
          }}
          onAbout={() => setAboutOpen(true)}
        />
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
      {settingsOpen && edition === 'debug' && <Settings onClose={() => setSettingsOpen(false)} />}
      {aboutOpen && <About onClose={() => setAboutOpen(false)} />}
      {accountOpen && (
        <AccountMenu
          account={account}
          onClose={() => setAccountOpen(false)}
          onAbout={() => {
            setAccountOpen(false)
            setAboutOpen(true)
          }}
          onSignedOut={() => {
            remember('qbx.signin', '')
            setSkipped(false)
          }}
        />
      )}
    </div>
  )
}

export default App
