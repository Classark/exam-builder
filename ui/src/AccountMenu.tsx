import { CreditCard, ExternalLink, LogOut } from 'lucide-react'
import { api } from './api'
import { useLive } from './live'
import { Modal } from './Modal'
import { SignInPanel } from './SignIn'
import type { AccountState } from './types'

/** The account button: who's signed in, the subscription and credit pages, signing out -
 * or, when signed out, the sign-in panel itself. */
export function AccountMenu({
  account,
  onClose,
  onAbout,
  onSignedOut,
}: {
  account: AccountState
  onClose: () => void
  onAbout: () => void
  onSignedOut: () => void
}) {
  const { poke } = useLive()

  if (account.status !== 'signed_in') {
    return (
      <Modal title="Classark account" onClose={onClose}>
        <div className="flex justify-center py-2">
          <SignInPanel account={account} onAbout={onAbout} />
        </div>
      </Modal>
    )
  }

  return (
    <Modal title="Classark account" onClose={onClose}>
      <div className="space-y-4 text-sm">
        <div>
          <div className="font-semibold">{account.name || account.email}</div>
          {account.name && <div className="text-ink-2">{account.email}</div>}
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn-secondary" onClick={() => void api.openAccountPage('subscription')}>
            <ExternalLink size={14} />
            Subscription
          </button>
          <button className="btn-secondary" onClick={() => void api.openAccountPage('credit')}>
            <CreditCard size={14} />
            Add credit
          </button>
          <button
            className="btn-ghost"
            onClick={() => {
              void api.signOut().then(() => {
                onSignedOut()
                poke()
                onClose()
              })
            }}
          >
            <LogOut size={14} />
            Sign out
          </button>
        </div>
      </div>
    </Modal>
  )
}
