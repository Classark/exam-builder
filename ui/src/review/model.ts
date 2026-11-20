import { pathLabel } from '../issues'
import type { BBox, Issue, MarkSchemeEntry, Paper, Question, QuestionPart, Report } from '../types'

/** The Reviewer, question by question: everything the screen shows is derived here from
 * the paper, its report and where its mark scheme rows sit, so the rules live in one place.
 * Each report issue lands on the thing it's about - a part's row, a scheme row nobody
 * claimed, the question's header, or the paper as a whole. */

export interface PartRow {
  part: QuestionPart
  /** the mark scheme rows linked to it */
  entries: MarkSchemeEntry[]
  /** what those rows add up to, or null with none */
  schemeMarks: number | null
  /** its open (and checked) issues: no mark found, no scheme answer */
  issues: Issue[]
}

export interface UnmatchedRow {
  entry: MarkSchemeEntry
  issue: Issue | null
  /** the parts either side of it in the scheme, when the paper has no part with its label:
   * the paper's own part was most likely read as part of one of them. Which one isn't
   * guessable from marks - 0452/12 ON18's (f)(ii) sat inside (f)(i), while (f)(iii) was the
   * one whose marks were "over", from a group mark shared out - so both are offered and the
   * boxes on the page show which. */
  near: QuestionPart[]
}

export interface QuestionView {
  number: number
  /** null: the scheme has rows for this number, but the paper as read has no such question */
  question: Question | null
  parts: PartRow[]
  unmatched: UnmatchedRow[]
  /** about the question as a whole: its parts don't add up to its printed total */
  questionIssues: Issue[]
  /** every issue about this question, wherever it shows */
  issues: Issue[]
  open: number
}

export interface PaperModel {
  questions: QuestionView[]
  /** about the paper as a whole: cover total, numbering, damaged file */
  paperIssues: Issue[]
  paperOpen: number
  /** questions with something open */
  toCheck: number
}

const PART_CHECKS = new Set(['marks_unknown', 'part_has_mark_scheme_entry'])
export const ORPHAN = 'no_orphaned_mark_scheme_entries'

export function entriesFor(paper: Paper, part: QuestionPart): MarkSchemeEntry[] {
  const ids = new Set(paper.joins.filter((j) => j.part_id === part.id).map((j) => j.mark_scheme_entry_id))
  return paper.mark_scheme.filter((e) => ids.has(e.id))
}

export function buildModel(paper: Paper, report: Report): PaperModel {
  const joined = new Set(paper.joins.map((j) => j.mark_scheme_entry_id))
  const issues = report.issues
  const numbers = new Set<number>(paper.questions.map((q) => q.question_number))
  for (const e of paper.mark_scheme) if (!joined.has(e.id)) numbers.add(e.question_number)
  for (const i of issues) if (i.question_number != null) numbers.add(i.question_number)

  const questions = [...numbers]
    .sort((a, b) => a - b)
    .map((number): QuestionView => {
      const question = paper.questions.find((q) => q.question_number === number) ?? null
      const mine = issues.filter((i) => i.question_number === number)
      const parts: PartRow[] = (question?.parts ?? []).map((part) => {
        const entries = entriesFor(paper, part)
        return {
          part,
          entries,
          schemeMarks: entries.length ? entries.reduce((s, e) => s + e.marks, 0) : null,
          issues: mine.filter((i) => PART_CHECKS.has(i.check) && (i.part_path ?? '') === part.path),
        }
      })
      const unmatched: UnmatchedRow[] = paper.mark_scheme
        .filter((e) => e.question_number === number && !joined.has(e.id))
        .map((entry) => ({
          entry,
          issue: mine.find((i) => i.check === ORPHAN && (i.part_path ?? '') === entry.part_path) ?? null,
          near: nearParts(paper, parts, entry),
        }))
      const placed = new Set([...parts.flatMap((p) => p.issues), ...unmatched.flatMap((u) => (u.issue ? [u.issue] : []))])
      const questionIssues = mine.filter((i) => !placed.has(i))
      return {
        number,
        question,
        parts,
        unmatched,
        questionIssues,
        issues: mine,
        open: mine.filter((i) => !i.checked).length,
      }
    })

  const paperIssues = issues.filter((i) => i.question_number == null)
  const paperOpen = paperIssues.filter((i) => !i.checked).length
  return { questions, paperIssues, paperOpen, toCheck: questions.filter((q) => q.open > 0).length + (paperOpen ? 1 : 0) }
}

/** The parts an unlinked row sits between in the scheme (see UnmatchedRow.near). */
function nearParts(paper: Paper, parts: PartRow[], entry: MarkSchemeEntry): QuestionPart[] {
  if (parts.some((p) => p.part.path === entry.part_path)) return [] // its own part exists
  const rows = paper.mark_scheme.filter((e) => e.question_number === entry.question_number)
  const at = rows.findIndex((e) => e.id === entry.id)
  const owner = (row: MarkSchemeEntry | undefined) => (row ? parts.find((p) => p.entries.some((e) => e.id === row.id))?.part : undefined)
  const found = [owner(rows[at - 1]), owner(rows[at + 1])].filter((p): p is QuestionPart => !!p)
  return found.filter((p, i) => found.indexOf(p) === i)
}

/** "Q2 (f)(ii)" */
export function partName(number: number, path: string): string {
  return `Q${number}${pathLabel(path)}`
}

/** A scheme row's label as the scheme prints it: "2(f)(ii)". */
export function rowLabel(entry: MarkSchemeEntry): string {
  return `${entry.question_number}${pathLabel(entry.part_path).replace(/ /g, '')}`
}

/** The part a scheme row most likely belongs to: the one with its label, else none. */
export function likelyPart(view: QuestionView, entry: MarkSchemeEntry): QuestionPart | null {
  return view.parts.find((p) => p.part.path === entry.part_path)?.part ?? null
}

/** The marking codes at the start of a row's lines (B1, M1, A1, P1, SC1), each worth its
 * digit, in order until they make up the row's mark - the notes go on to name
 * alternatives ("B2 ... B1 for cm or mm"), which aren't more marks. */
export function codes(entry: MarkSchemeEntry): string[] {
  const text = `${entry.guidance_text ?? ''} ${entry.answer_text}`
  const out: string[] = []
  let sum = 0
  for (const m of text.matchAll(/\b(?:SC|[MABP])(\d)\b/g)) {
    if (sum >= entry.marks) break
    out.push(m[0])
    sum += Number(m[1])
  }
  return out
}

/** Every region a question covers on the question paper: its stem and its parts. */
export function questionSegments(question: Question | null): BBox[] {
  if (!question) return []
  return [...question.stem_segments, ...question.parts.flatMap((p) => p.segments)]
}

/** The question paper's page count, from the paper or (older extractions) its regions. */
export function qpPageCount(paper: Paper): number {
  if (paper.qp_page_count) return paper.qp_page_count
  const pages = paper.questions.flatMap((q) => questionSegments(q)).map((s) => s.page + 1)
  return pages.length ? Math.max(...pages) : 0
}

/** The question to open a paper on: the first with something open, else the first. */
export function firstToCheck(model: PaperModel): number | 'paper' | null {
  if (model.paperOpen) return 'paper'
  const open = model.questions.find((q) => q.open > 0)
  return (open ?? model.questions[0])?.number ?? null
}
