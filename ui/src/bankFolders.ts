import type { PaperSummary } from './types'

// The order a year's sittings happen in, with specimen papers last.
const SESSIONS: [string, string][] = [
  ['JAN', 'January'], // Edexcel's sittings: JAN, JUN, NOV
  ['F/M', 'February/March'],
  ['M/J', 'May/June'],
  ['JUN', 'May/June'],
  ['O/N', 'October/November'],
  ['NOV', 'October/November'],
  ['SP', 'Specimen'],
]
export const SESSION_ORDER = new Map(SESSIONS.map(([code], index) => [code, index]))
const SESSION_NAME = new Map(SESSIONS)

export function questionKey(paperKey: string, questionNumber: number): string {
  return `${paperKey}::${questionNumber}`
}

// papers print a two-digit year ("24"); the folders show it in full
export const fullYear = (year: number) => (year < 100 ? 2000 + year : year)
export const sessionName = (session: string) => SESSION_NAME.get(session) ?? session

type PaperMeta = Pick<PaperSummary, 'subject' | 'paper' | 'variant' | 'session' | 'year'>

/** "0620/41" */
export const paperCode = (p: PaperMeta) => `${p.subject}/${p.paper}${p.variant}`
/** "May/June 2024" - and Edexcel's autumn 2020 papers say they're the June 2020 series,
 * postponed (their covers carry the May date) */
export const sitting = (p: PaperMeta) =>
  `${sessionName(p.session)} ${fullYear(p.year)}${p.session === 'NOV' && fullYear(p.year) === 2020 ? ' (June 2020)' : ''}`

/** Subject, newest year first, sittings in calendar order, then paper number. */
export function comparePapers(a: PaperMeta, b: PaperMeta): number {
  return (
    a.subject.localeCompare(b.subject) ||
    fullYear(b.year) - fullYear(a.year) ||
    (SESSION_ORDER.get(a.session) ?? 9) - (SESSION_ORDER.get(b.session) ?? 9) ||
    `${a.paper}${a.variant}`.localeCompare(`${b.paper}${b.variant}`)
  )
}
