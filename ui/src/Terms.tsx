import { useState } from 'react'
import { Check, ExternalLink, Loader2 } from 'lucide-react'
import { api, errorMessage } from './api'
import { useDesktop } from './desktop'
import { Modal } from './Modal'

type Page = 'terms' | 'privacy' | 'refunds' | 'licence'

/** Asked before anything else, and again when the terms change (engine/qbx/terms.py,
 * user-asked). The documents are classark.com's, opened in the browser; nothing goes on
 * until the teacher agrees. */
export function TermsPrompt({ version, onAccepted }: { version: string; onAccepted: () => void }) {
  const desktop = useDesktop()
  const [agreed, setAgreed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const link = (page: Page, label: string) => (
    <button className="inline-flex items-center gap-1 text-accent underline" onClick={() => void api.openAccountPage(page)}>
      {label}
      <ExternalLink size={12} />
    </button>
  )

  const accept = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.acceptTerms(version)
      onAccepted()
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Before you start" onClose={() => {}} dismissable={false}>
      <div className="space-y-4 text-sm">
        <p className="text-ink-2">
          To use Classark Exam Builder, please read and agree to its {link('terms', 'Terms of Service')} and{' '}
          {link('privacy', 'Privacy Policy')}. Payments and cancellations follow the {link('refunds', 'Refund Policy')}.
        </p>
        <p className="text-xs text-muted">
          The app itself is open-source software under the GNU Affero General Public License v3.0 ({link('licence', 'licence')}
          ). These terms cover your Classark account and the service, and take away none of the rights that licence gives
          you. Terms last updated {version}.
        </p>
        <label className="flex cursor-pointer items-start gap-2.5">
          <input
            type="checkbox"
            className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]"
            checked={agreed}
            onChange={(e) => setAgreed(e.target.checked)}
          />
          <span>I have read and agree to the Terms of Service and Privacy Policy.</span>
        </label>
        {error && <p className="text-xs text-danger">{error}</p>}
        <div className="flex justify-end gap-2">
          {desktop && (
            <button className="btn-ghost" onClick={() => void desktop.close()}>
              Close the app
            </button>
          )}
          <button className="btn-primary" disabled={!agreed || busy} onClick={() => void accept()}>
            {busy ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />} Agree and continue
          </button>
        </div>
      </div>
    </Modal>
  )
}
