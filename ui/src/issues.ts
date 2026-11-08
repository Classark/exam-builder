import type { Issue } from './types'

/** What each validator check means to someone reviewing a paper - and what to look at.
 * Keyed by engine/qbx/validate.py's check names. */
const CHECKS: Record<string, { title: string; hint: string }> = {
  marks_unknown: {
    title: 'No marks found',
    hint: 'No [n] mark allocation was found for this part, in the question paper or the mark scheme. Enter its marks.',
  },
  part_sum_matches_total: {
    title: "Parts don't add up to the question's total",
    hint: 'The marks of this question\'s parts differ from its printed [Total: n]. A part may be missing, merged or split wrongly.',
  },
  question_totals_match_cover: {
    title: "Paper total doesn't match the cover",
    hint: 'The questions add up to a different total from the one printed on the cover. Look for a missed or doubled question.',
  },
  question_numbers_contiguous: {
    title: 'Question numbers skip',
    hint: 'Questions should run 1, 2, 3… without gaps. A question may have been missed or a stray number read as one.',
  },
  part_has_mark_scheme_entry: {
    title: 'No mark scheme answer',
    hint: 'This part matched no row in the mark scheme. Compare the two: the scheme may label it differently, or leave it out.',
  },
  no_orphaned_mark_scheme_entries: {
    title: 'Mark scheme row with no question',
    hint: "This mark scheme row matched no part of the question paper. Its label may differ from the paper's, or the part may be missing.",
  },
  qp_pages_missing: {
    title: 'Question paper file is incomplete',
    hint: 'The PDF has fewer pages than its cover says. Questions on the missing pages can’t be extracted - use a complete copy.',
  },
}

export function describeIssue(issue: Issue): { title: string; hint: string } {
  return CHECKS[issue.check] ?? { title: issue.check.replace(/_/g, ' '), hint: issue.detail }
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
