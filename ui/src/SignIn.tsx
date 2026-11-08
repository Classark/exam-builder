import { useState } from 'react'
import { ExternalLink, Loader2, LogIn } from 'lucide-react'
import { api } from './api'
import { useLive } from './live'
import type { AccountState } from './types'

/** Signing in to a Classark account (engine/qbx/account.py): the browser opens
 * classark.com, the teacher signs in there, and the app hears back on its own. */
export function SignInPanel({ account, onSkip, onAbout }: { account: AccountState; onSkip?: () => void; onAbout: () => void }) {
  const { poke } = useLive()
  const [busy, setBusy] = useState(false)
  const waiting = account.status === 'waiting'

  const signIn = async () => {
    setBusy(true)
    try {
      await api.signIn()
    } finally {
      setBusy(false)
      poke()
    }
  }

  return (
    <div className="flex w-full max-w-md flex-col items-center text-center">
      <img src="/icon.svg" alt="" className="h-14 w-14" />
      <h1 className="mt-4 text-2xl font-semibold">Classark Exam Builder</h1>
      <p className="mt-2 text-sm text-ink-2">
        Sign in with your Classark account. Your browser opens so you can sign in there, then you come straight back here.
      </p>

      {waiting ? (
        <div className="mt-6 w-full space-y-3">
          <div className="flex items-center justify-center gap-2 text-sm">
            <Loader2 size={16} className="animate-spin" />
            Waiting for you to sign in in your browser…
          </div>
          {account.url && (
            <p className="text-xs text-muted">
              Browser didn’t open?{' '}
              <a className="text-accent underline" href={account.url} target="_blank" rel="noreferrer">
                Open the sign-in page
              </a>
            </p>
          )}
          <button
            className="btn-secondary"
            onClick={() => {
              void api.cancelSignIn().then(poke)
            }}
          >
            Cancel
          </button>
        </div>
      ) : (
        <div className="mt-6 w-full space-y-3">
          <button className="btn-primary w-full justify-center" disabled={busy} onClick={() => void signIn()}>
            {busy ? <Loader2 size={16} className="animate-spin" /> : <LogIn size={16} />}
            Sign in with Classark
          </button>
          {account.error && <p className="text-sm text-danger">{account.error}</p>}
          <button className="btn-ghost w-full justify-center" onClick={() => void api.openAccountPage('subscription')}>
            <ExternalLink size={14} />
            No subscription yet? See Classark Exam Builder plans
          </button>
          {onSkip && (
            <button className="text-xs text-muted underline" onClick={onSkip}>
              Continue without signing in (no Claude features)
            </button>
          )}
        </div>
      )}

      <p className="mt-10 text-[11px] leading-relaxed text-faint">
        Classark Exam Builder is free software, licensed under the GNU Affero General Public License v3.0, and comes
        with no warranty. Its complete source code is included.{' '}
        <button className="underline" onClick={onAbout}>
          About &amp; licences
        </button>
      </p>
    </div>
  )
}

/** The whole window, before anything else: what a release build shows until the teacher
 * signs in or chooses to carry on without an account. */
export function SignInScreen(props: { account: AccountState; onSkip: () => void; onAbout: () => void }) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-page p-6">
      <SignInPanel {...props} />
    </div>
  )
}
