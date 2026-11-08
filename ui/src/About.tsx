import { useEffect, useState } from 'react'
import { Download, FileText, Scale } from 'lucide-react'
import { api } from './api'
import { Modal } from './Modal'

interface AboutInfo {
  product: string
  version: string
  edition: string
  copyright: string
  license: string
  notice: string
}

/** About & licences: the AGPL notice, the licence texts and this version's source code
 * (engine/qbx/legal.py - the app is AGPL-3.0 because PyMuPDF is). */
export function About({ onClose }: { onClose: () => void }) {
  const [info, setInfo] = useState<AboutInfo | null>(null)
  const [text, setText] = useState<{ title: string; body: string } | null>(null)

  useEffect(() => void api.getAbout().then(setInfo).catch(() => {}), [])

  const show = async (title: string, load: () => Promise<string>) => {
    try {
      setText({ title, body: await load() })
    } catch (e) {
      setText({ title, body: (e as Error).message })
    }
  }

  return (
    <Modal title="About & licences" onClose={onClose} width="max-w-2xl">
      {info && (
        <div className="space-y-4 text-sm">
          <div>
            <div className="text-base font-semibold">{info.product}</div>
            <div className="text-ink-2">
              Version {info.version}
              {info.edition === 'debug' ? ' (testing build)' : ''}
            </div>
            <div className="text-ink-2">{info.copyright}</div>
          </div>

          <div className="space-y-2 whitespace-pre-line text-ink-2">{info.notice}</div>

          <div className="flex flex-wrap gap-2">
            <a className="btn-primary" href="/api/about/source.zip" download>
              <Download size={14} />
              Download source code
            </a>
            <button className="btn-secondary" onClick={() => void show(info.license, api.getLicense)}>
              <Scale size={14} />
              Licence
            </button>
            <button className="btn-secondary" onClick={() => void show('Third-party licences', api.getThirdParty)}>
              <FileText size={14} />
              Third-party licences
            </button>
          </div>

          {text && (
            <div>
              <div className="mb-1 font-medium">{text.title}</div>
              <pre className="scroll-quiet max-h-72 overflow-auto whitespace-pre-wrap rounded-xl border border-line bg-sunken p-3 text-xs leading-relaxed text-ink-2">
                {text.body}
              </pre>
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
