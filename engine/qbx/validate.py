"""The six validation checks that produce report.json (CLAUDE.md section 7).

A paper passing all six is almost certainly correct and needs no review.
"""

import re

from pydantic import BaseModel

from qbx.schema import Paper

_REMOVED_RE = re.compile(
    r"has been removed from (?:the|this) (?:question paper|paper|mark scheme|examination)", re.IGNORECASE
)


# the question it names: "Question 5", "question 5(a)", "question 14" - or "this question"
_NAMED_RE = re.compile(r"issue with question\s*(\d{1,2})\s*((?:\([a-z]+\))*)", re.IGNORECASE)


def removed_target(text: str) -> tuple[int, str] | None:
    """(question, part path) a removal notice names - path "" for the whole question -
    or None when it says "this question"."""
    named = _NAMED_RE.search(text or "")
    if not named:
        return None
    return int(named.group(1)), ".".join(re.findall(r"\(([a-z]+)\)", named.group(2)))


def is_removed_notice(text: str) -> bool:
    """The board's notice that a question was withdrawn after printing: "Due to an issue
    with Question 5, the question has been removed from the question paper." """
    return bool(_REMOVED_RE.search(text or ""))


class Issue(BaseModel):
    check: str
    question_number: int | None = None
    part_path: str | None = None
    detail: str


class Report(BaseModel):
    paper_key: str
    passed: bool
    issues: list[Issue] = []


def validate_paper(paper: Paper) -> Report:
    issues: list[Issue] = []
    issues += _check_marks_known(paper)
    issues += _check_part_sums_to_question_total(paper)
    issues += _check_question_totals_sum_to_cover(paper)
    issues += _check_question_numbers_contiguous(paper)
    issues += _check_every_part_joined(paper)
    issues += _check_no_orphaned_mark_scheme_entries(paper)
    if issues:
        # Only as an explanation of something else: a download with its blank pages stripped
        # is short of its cover's count too (0620/41 MJ24: printed pages 14-15, both blank),
        # and a paper that reconciles in full has lost nothing.
        issues = _check_no_missing_pages(paper) + issues
    return Report(paper_key=paper.paper_key, passed=not issues, issues=issues)


def _check_no_missing_pages(paper: Paper) -> list[Issue]:
    """Real-paper finding (0620/62 FM24): a QP file with pages missing - the cover says 16,
    the file holds 14, printed pages 3-4 gone with 1(d) and 1(e) on them. Every other
    check then blamed the mark scheme ("orphaned entries") for what was the file's fault;
    this says so first. Blank pages are counted in both, so no allowance is needed."""
    if paper.cover_page_count is None or paper.qp_page_count is None:
        return []
    if paper.qp_page_count < paper.cover_page_count:
        return [
            Issue(
                check="qp_pages_missing",
                detail=(
                    f"the question paper's cover says it has {paper.cover_page_count} pages, "
                    f"but the PDF has {paper.qp_page_count} - the file is incomplete, and "
                    "questions on the missing pages can't be extracted"
                ),
            )
        ]
    return []


def _check_marks_known(paper: Paper) -> list[Issue]:
    """Flags a part whose mark count is unknown by any means - not found as a `[n]` bracket
    in its own text (tree.py's charitable last-bracket-anywhere search, not just a strict
    trailing match - see tree.py's `_marks_value`) and not backfilled from a confirmed
    mark scheme join (pipeline.py's `_backfill_marks_from_mark_scheme`, for formats with no
    marks in the QP at all, e.g. multiple choice). A part with marks > 0 by either route is
    not flagged: re-deriving "does this look like a bracket" here too, with a different
    regex than tree.py's, was itself a source of drift (see the "Working space" case, where
    marks were correctly known but a stricter end-anchored check flagged it anyway).
    """
    issues = []
    for question in paper.questions:
        for part in question.parts:
            if part.marks <= 0 and not part.removed:
                issues.append(
                    Issue(
                        check="marks_unknown",
                        question_number=question.question_number,
                        part_path=part.path,
                        detail="no mark allocation found in the QP text or the mark scheme",
                    )
                )
    return issues


def _check_part_sums_to_question_total(paper: Paper) -> list[Issue]:
    issues = []
    for question in paper.questions:
        part_sum = sum(part.marks for part in question.parts)
        if question.parts and part_sum != question.total_marks:
            issues.append(
                Issue(
                    check="part_sum_matches_total",
                    question_number=question.question_number,
                    detail=f"parts sum to {part_sum}, question total is {question.total_marks}",
                )
            )
    return issues


def _check_question_totals_sum_to_cover(paper: Paper) -> list[Issue]:
    if paper.cover_total_marks is None:
        return []
    total = sum(q.total_marks for q in paper.questions)
    # a question withdrawn after printing still counts on the cover, at marks nothing prints
    # now (0478/22 MJ23: Q5's notice, 70 of the cover's 75)
    unknown = any(q.removed and not q.total_marks for q in paper.questions)
    if total != paper.cover_total_marks and not (unknown and total < paper.cover_total_marks):
        return [
            Issue(
                check="question_totals_match_cover",
                detail=f"questions sum to {total}, cover page total is {paper.cover_total_marks}",
            )
        ]
    return []


def _check_question_numbers_contiguous(paper: Paper) -> list[Issue]:
    numbers = sorted(q.question_number for q in paper.questions)
    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        return [
            Issue(
                check="question_numbers_contiguous",
                detail=f"expected {expected}, got {numbers}",
            )
        ]
    return []


def _check_every_part_joined(paper: Paper) -> list[Issue]:
    joined_part_ids = {join.part_id for join in paper.joins}
    issues = []
    for question in paper.questions:
        for part in question.parts:
            if part.id not in joined_part_ids and not part.removed:
                issues.append(
                    Issue(
                        check="part_has_mark_scheme_entry",
                        question_number=question.question_number,
                        part_path=part.path,
                        detail="no mark scheme entry joined to this part",
                    )
                )
    return issues


def _check_no_orphaned_mark_scheme_entries(paper: Paper) -> list[Issue]:
    joined_entry_ids = {join.mark_scheme_entry_id for join in paper.joins}
    issues = []
    for entry in paper.mark_scheme:
        if entry.id not in joined_entry_ids and not is_removed_notice(entry.answer_text):
            issues.append(
                Issue(
                    check="no_orphaned_mark_scheme_entries",
                    question_number=entry.question_number,
                    part_path=entry.part_path,
                    detail="mark scheme entry has no matching question part",
                )
            )
    return issues
