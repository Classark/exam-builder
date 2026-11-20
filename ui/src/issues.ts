import type { Issue } from './types'

/** The one thing in the Reviewer that fixes an issue most often - offered as a button on it. */
export type IssueAction = 'link' | 'marks' | 'marks_tab' | 'parts_tab'

/** What each validator check means to someone reviewing a paper, what to look at, and how
 * to fix it. Keyed by engine/qbx/validate.py's check names. */
const CHECKS: Record<string, { title: string; hint: string; fix: string; action?: IssueAction }> = {
  marks_unknown: {
    title: 'No marks found',
    hint: 'No [n] mark allocation was found for this part, in the question paper or the mark scheme.',
    fix: 'Look at the part on the question paper, find its mark, and type it into Marks below the list.',
    action: 'marks',
  },
  part_sum_matches_total: {
    title: "Parts don't add up to the question's total",
    hint: "The marks of this question's parts differ from its printed [Total: n].",
    fix: 'Open the Marks tab to see every part beside the mark scheme. A wrong mark: type the right one. Two parts read as one: select it and Split. One part read as two: Merge.',
    action: 'marks_tab',
  },
  question_totals_match_cover: {
    title: "Paper total doesn't match the cover",
    hint: 'The questions add up to a different total from the one printed on the cover.',
    fix: 'Open the Marks tab and look for a question whose total is off, or a question missing from the list.',
    action: 'marks_tab',
  },
  question_numbers_contiguous: {
    title: 'Question numbers skip',
    hint: 'Questions should run 1, 2, 3… without gaps.',
    fix: 'A question may have been missed or a stray number read as one. If the paper really skips (a withdrawn question), mark this checked.',
    action: 'parts_tab',
  },
  part_has_mark_scheme_entry: {
    title: 'No mark scheme answer',
    hint: "This part isn't linked to any answer in the mark scheme.",
    fix: 'Press Link mark scheme, find this part’s answer on the mark scheme pages, and drag a box round it.',
    action: 'link',
  },
  no_orphaned_mark_scheme_entries: {
    title: 'Mark scheme row with no question',
    hint: "This mark scheme answer (shown on the right) didn't match any part of the question paper.",
    fix: 'Find its question on the question paper (all of it is shown when nothing is highlighted). If the part is there, choose it in the Parts tab and use Link mark scheme. If the whole question is missing, look for empty or missing pages - then the question paper file needs replacing.',
    action: 'parts_tab',
  },
  qp_pages_empty: {
    title: 'Question paper file is damaged',
    hint: 'Some pages of the question paper PDF are completely empty, so the questions printed on them are missing.',
    fix: 'Nothing here can bring them back. Download the question paper again, replace the file in the papers folder, then press ↻ to re-process this paper. The other issues on this paper probably come from the same missing pages.',
  },
  qp_pages_missing: {
    title: 'Question paper file is incomplete',
    hint: 'The PDF has fewer pages than its cover says.',
    fix: 'Questions on the missing pages can’t be extracted. Replace the file with a complete copy in the papers folder.',
  },
}

export function describeIssue(issue: Issue): { title: string; hint: string; fix: string; action?: IssueAction } {
  return CHECKS[issue.check] ?? { title: issue.check.replace(/_/g, ' '), hint: issue.detail, fix: '' }
}

/** "Q3 (c)(ii)", "Q3", or "Whole paper". */
export function issueWhere(issue: Issue): string {
  if (issue.question_number == null) return 'Whole paper'
  return `Q${issue.question_number}${pathLabel(issue.part_path ?? '')}`
}

/** "c.ii" -> " (c)(ii)"; "" -> "". */
export function pathLabel(path: string): string {
  if (!path) return ''
  return ' ' + path.split('.').map((p) => `(${p})`).join('')
}

/** Extracted text as a one-line snippet: the PDF's control characters (BEL as the gap
 * after a marker, backspace before a mark) become spaces, and the labels already shown
 * beside it are dropped from the front - the question's own number, when it's given, and
 * any part markers. A number is only dropped when it's that question's: a stem can
 * genuinely begin "25 cm3 of…". */
export function snippet(text: string, questionNumber?: number): string {
  let out = text.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim()
  const n = questionNumber === undefined ? '' : String(questionNumber)
  // "7 Aluminium…", "12 (a)…" or just "7" - but not "70 cm3" or "7.5 g"
  if (n && out.startsWith(n) && [' ', '(', ''].includes(out.charAt(n.length))) out = out.slice(n.length).trim()
  return out.replace(/^(?:\([a-z]{1,4}\)\s*)+/, '').trim()
}
