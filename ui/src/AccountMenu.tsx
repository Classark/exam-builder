import { CreditCard, ExternalLink, LogOut } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api, errorMessage } from './api'
import { useLive } from './live'
import { Modal } from './Modal'
import { SignInPanel } from './SignIn'
import type { AccountState, Balance } from './types'

const dollars = (micros: number) => `$${(micros / 1_000_000).toFixed(2)}`

/** The account button: who's signed in, the subscription and credit pages, signing out -
 * or, when signed out, the sign-in panel itself. */
export function AccountMenu({
  account,
  onClose,
}: {
  account: AccountState
  onClose: () => void
}) {
  const { poke } = useLive()
  const [balance, setBalance] = useState<Balance | null>(null)
  const [balanceError, setBalanceError] = useState<string | null>(null)
  const signedIn = account.status === 'signed_in'
  useEffect(() => {
    if (!signedIn) return
    api.getBalance().then(setBalance, (e: unknown) => setBalanceError(errorMessage(e)))
  }, [signedIn])

  if (account.status !== 'signed_in') {
    return (
      <Modal title="Classark account" onClose={onClose}>
        <div className="flex justify-center py-2">
          <SignInPanel account={account} />
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
        <div className="border border-line px-3 py-2">
          <div className="text-ink-2">Claude credit</div>
          {balance ? (
            <>
              <div className="text-lg font-semibold">{dollars(balance.available_micros)} available</div>
              {balance.allowance && (
                <div className="text-xs text-muted">
                  {dollars(balance.allowance.remaining_micros)} of this month’s{' '}
                  {dollars(balance.allowance.monthly_micros)} allowance left, refills{' '}
                  {new Date(balance.allowance.resets_at).toLocaleDateString()}
                  {balance.topup && balance.topup.balance_micros > 0 && (
                    <>, plus {dollars(balance.topup.balance_micros)} added credit</>
                  )}
                </div>
              )}
            </>
          ) : (
            <div className="text-xs text-muted">{balanceError ?? 'Loading…'}</div>
          )}
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
