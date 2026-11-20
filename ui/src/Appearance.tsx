import { useEffect, useState } from 'react'
import { Info, Monitor, Moon, Sun } from 'lucide-react'
import { About } from './About'
import { api } from './api'

// Settings sections every edition has: the theme and About & licences (user-asked: both
// moved off the top bar into Settings).

export type ThemeChoice = 'auto' | 'light' | 'dark'

// A new key, so everyone starts on Auto (user-asked): the old top-bar toggle saved light or
// dark under 'qbx.theme' on any click, which would have kept those computers off Auto.
const THEME_KEY = 'qbx.theme-choice'

function storedTheme(): ThemeChoice {
  try {
    localStorage.removeItem('qbx.theme')
    const value = localStorage.getItem(THEME_KEY)
    return value === 'light' || value === 'dark' ? value : 'auto'
  } catch {
    return 'auto'
  }
}

/** Auto leaves data-theme off, and index.css follows the computer's light or dark mode,
 * live, as Windows or macOS switches it. */
function applyTheme(choice: ThemeChoice) {
  if (choice === 'auto') delete document.documentElement.dataset.theme
  else document.documentElement.dataset.theme = choice
}

/** Called once before the first render, so the app never flashes the wrong theme. */
export function applyStoredTheme() {
  applyTheme(storedTheme())
}

function saveTheme(choice: ThemeChoice) {
  applyTheme(choice)
  try {
    if (choice === 'auto') localStorage.removeItem(THEME_KEY)
    else localStorage.setItem(THEME_KEY, choice)
  } catch {
    // a remembered choice is a convenience only
  }
}

const THEMES: { id: ThemeChoice; label: string; icon: typeof Sun; title: string }[] = [
  { id: 'auto', label: 'Auto', icon: Monitor, title: "Follow this computer's light or dark mode" },
  { id: 'light', label: 'Light', icon: Sun, title: 'Always light' },
  { id: 'dark', label: 'Dark', icon: Moon, title: 'Always dark' },
]

export function AppearanceSection() {
  const [theme, setTheme] = useState<ThemeChoice>(storedTheme)
  const choose = (choice: ThemeChoice) => {
    saveTheme(choice)
    setTheme(choice)
  }
  return (
    <section className="space-y-2">
      <h3 className="font-semibold">Appearance</h3>
      <div className="inline-flex rounded-xl bg-sunken p-1" role="radiogroup" aria-label="Theme">
        {THEMES.map(({ id, label, icon: Icon, title }) => (
          <button
            key={id}
            role="radio"
            aria-checked={theme === id}
            title={title}
            onClick={() => choose(id)}
            // the Reviewer's Region tool switch, the app's own segmented control
            className={`flex items-center justify-center gap-1.5 rounded-lg px-4 py-1.5 text-sm font-medium transition-colors ${
              theme === id ? 'bg-card text-accent shadow-card' : 'text-ink-2 hover:text-accent'
            }`}
          >
            <Icon size={14} />
            {label}
          </button>
        ))}
      </div>
    </section>
  )
}

export function AboutSection() {
  const [version, setVersion] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  useEffect(() => void api.getAbout().then((a) => setVersion(a.version)).catch(() => {}), [])
  return (
    <section className="space-y-2">
      <h3 className="font-semibold">About</h3>
      <div className="flex flex-wrap items-center gap-3 text-sm text-ink-2">
        <span>Classark Exam Builder{version ? ` ${version}` : ''}</span>
        <button className="btn-secondary" onClick={() => setOpen(true)}>
          <Info size={14} />
          About & licences
        </button>
      </div>
      {open && <About onClose={() => setOpen(false)} />}
    </section>
  )
}
