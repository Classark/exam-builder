from qbx.schema import MarkSchemeEntry, Paper, Question, QuestionPart, QuestionPartMarkScheme
from qbx.validate import validate_paper


def _paper(**overrides) -> Paper:
    defaults = dict(
        paper_key="9702_42_MJ_23",
        board="Cambridge",
        level="A Level",
        subject="9702",
        paper="4",
        variant="2",
        session="M/J",
        year=23,
    )
    defaults.update(overrides)
    return Paper(**defaults)


def test_clean_paper_passes_all_checks():
    part = QuestionPart(id="p1a", question_id="q1", path="a", depth=1, text="explain X [3]", image_path="", marks=3)
    question = Question(
        paper_key="9702_42_MJ_23", board="Cambridge", subject="9702", question_number=1,
        stem_text="", stem_image_path="", total_marks=3, parts=[part],
    )
    entry = MarkSchemeEntry(
        id="ms1a", paper_key="9702_42_MJ_23", question_number=1, part_path="a",
        answer_text="X is ...", marks=3,
    )
    paper = _paper(
        cover_total_marks=3,
        questions=[question],
        mark_scheme=[entry],
        joins=[QuestionPartMarkScheme(part_id="p1a", mark_scheme_entry_id="ms1a")],
    )

    report = validate_paper(paper)

    assert report.passed
    assert report.issues == []


def test_missing_marks_is_flagged():
    part = QuestionPart(id="p1a", question_id="q1", path="a", depth=1, text="explain X with no marks", image_path="", marks=0)
    question = Question(
        paper_key="9702_42_MJ_23", board="Cambridge", subject="9702", question_number=1,
        stem_text="", stem_image_path="", total_marks=0, parts=[part],
    )
    report = validate_paper(_paper(questions=[question]))

    assert not report.passed
    assert any(issue.check == "marks_unknown" for issue in report.issues)


def test_marks_known_via_mark_scheme_backfill_is_not_flagged():
    # a part with marks known some way other than its own trailing bracket (e.g. a
    # multiple-choice question, backfilled from the mark scheme by pipeline.py) must not
    # be flagged just because its own text has no "[n]".
    part = QuestionPart(id="p1", question_id="q1", path="", depth=1, text="Which option is correct?", image_path="", marks=1)
    question = Question(
        paper_key="9702_42_MJ_23", board="Cambridge", subject="9702", question_number=1,
        stem_text="", stem_image_path="", total_marks=1, parts=[part],
    )
    report = validate_paper(_paper(questions=[question]))

    assert not any(issue.check == "marks_unknown" for issue in report.issues)


def test_part_sum_mismatch_is_flagged():
    part = QuestionPart(id="p1a", question_id="q1", path="a", depth=1, text="explain X [2]", image_path="", marks=2)
    question = Question(
        paper_key="9702_42_MJ_23", board="Cambridge", subject="9702", question_number=1,
        stem_text="", stem_image_path="", total_marks=5, parts=[part],
    )
    report = validate_paper(_paper(questions=[question]))

    assert any(issue.check == "part_sum_matches_total" for issue in report.issues)


def test_non_contiguous_question_numbers_is_flagged():
    q1 = Question(paper_key="x", board="Cambridge", subject="9702", question_number=1, stem_text="", stem_image_path="", total_marks=0)
    q3 = Question(paper_key="x", board="Cambridge", subject="9702", question_number=3, stem_text="", stem_image_path="", total_marks=0)
    report = validate_paper(_paper(questions=[q1, q3]))

    assert any(issue.check == "question_numbers_contiguous" for issue in report.issues)


def test_orphaned_mark_scheme_entry_is_flagged():
    entry = MarkSchemeEntry(id="ms1a", paper_key="x", question_number=1, part_path="a", answer_text="", marks=1)
    report = validate_paper(_paper(mark_scheme=[entry]))

    assert any(issue.check == "no_orphaned_mark_scheme_entries" for issue in report.issues)


def test_a_question_paper_missing_pages_is_flagged_when_something_is_missing():
    # regression (0620/62 FM24): the cover says 16 pages, the file holds 14, and questions
    # on them are gone - the questions no longer add up to the cover's total
    report = validate_paper(_paper(cover_page_count=16, qp_page_count=14, cover_total_marks=40))
    assert report.issues[0].check == "qp_pages_missing"

    # a download with only its blank pages stripped reconciles in full: nothing to report
    report = validate_paper(_paper(cover_page_count=16, qp_page_count=14))
    assert report.passed
