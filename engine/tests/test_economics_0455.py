"""0455 Economics (user-reported): a withdrawn question in an answer key, a section heading
taken into the part before it, a cover counting only the questions answered, and the
Reviewer's "Removed by the board" for a withdrawal the parser didn't see."""

import pymupdf

from qbx import crops, fixes
from qbx.fixes import Fix, Step
from qbx.markscheme import parse_mark_scheme
from qbx.pipeline import read_questions_answered
from qbx.schema import MarkSchemeEntry, Paper, Question, QuestionPart, QuestionPartMarkScheme
from qbx.validate import validate_paper


def test_question_removed_in_the_answer_key_withdraws_the_question(tmp_path):
    """0455/12 ON16 prints "Question" over "Removed" in Q15's Key cell, centred on the row."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((160, 100), "Key")
    for row in range(1, 16):
        y = 130 + row * 16
        page.insert_text((165, y), str(row))
        if row == 15:
            page.insert_text((219, y - 5), "Question")
            page.insert_text((219, y + 6), "Removed")
        else:
            page.insert_text((230, y), "ABCD"[row % 4])
    path = str(tmp_path / "ms.pdf")
    doc.save(path)
    doc.close()

    entries = parse_mark_scheme(path)
    assert [e.question_number for e in entries] == list(range(1, 16))
    assert entries[-1].answer_text == "Question removed"
    from qbx.validate import is_removed_notice

    assert is_removed_notice(entries[-1].answer_text) and not is_removed_notice(entries[0].answer_text)


def test_a_section_heading_ends_the_part_before_it():
    """0455/21 MJ26: 1(h) ran on to Q2's number, past "Section B" and its instructions at the
    top of the next page."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((272, 72), "Section B")
    page.insert_text((227, 98), "Answer any three questions.")
    page.insert_text((50, 163), "2  In 2023, Argentina had a high inflation rate.")
    assert crops._question_total_top(page, 42, 150) < 72 - 9
    # "Section B" inside a sentence is not a heading
    other = doc.new_page(width=595, height=842)
    other.insert_text((50, 100), "Section B of the insert shows the data.")
    assert crops._question_total_top(other, 42, 150) == 150


def test_the_cover_says_how_many_questions_are_answered():
    cover = "INSTRUCTIONS\n● Answer four questions in total:\nSection A: answer Question 1.\nSection B: answer three questions."
    assert read_questions_answered(cover) == 4
    assert read_questions_answered("Answer any three questions.") == 3
    assert read_questions_answered("Answer all questions.") is None
    # with sections, "answer any three" is one section's share, not the paper's
    assert read_questions_answered("Section A: answer all questions. Section B: answer any two questions.") is None


def _paper(totals: list[int], cover: int, answered: int | None = None) -> Paper:
    questions, rows, joins = [], [], []
    for n, total in enumerate(totals, start=1):
        part = QuestionPart(id=f"{n}.", question_id=str(n), path="", depth=0, text="", image_path="", marks=total)
        questions.append(Question(paper_key="E", board="Cambridge", subject="0455", question_number=n,
                                  stem_text="", stem_image_path="", total_marks=total, parts=[part]))
        rows.append(MarkSchemeEntry(id=f"m{n}", paper_key="E", question_number=n, part_path="", answer_text="x", marks=total))
        joins.append(QuestionPartMarkScheme(part_id=f"{n}.", mark_scheme_entry_id=f"m{n}"))
    return Paper(paper_key="E", board="Cambridge", level="IGCSE", subject="0455", paper="2", variant="1",
                 session="MJ", year=26, cover_total_marks=cover, questions_answered=answered,
                 questions=questions, mark_scheme=rows, joins=joins)


def test_a_choice_of_questions_matches_the_cover():
    """Q1 (30) and any three of Q2-5 (20 each): 90 on the cover, 110 printed."""
    assert validate_paper(_paper([30, 20, 20, 20, 20], 90, answered=4)).passed
    assert not validate_paper(_paper([30, 20, 20, 20, 20], 90)).passed
    assert not validate_paper(_paper([30, 20, 20, 20, 20], 95, answered=4)).passed


def test_a_question_is_marked_removed_by_the_board_in_the_reviewer():
    paper = fixes.apply_fix(_paper([1, 1, 1], 2), Fix(steps=[Step(op="withdraw_question", question_number=2)]))
    assert [q.question_number for q in paper.questions] == [1, 3]
    assert paper.withdrawn == ["2"] and [e.id for e in paper.mark_scheme] == ["m1", "m3"]
    assert validate_paper(paper).passed  # numbering and the cover allow for it


def test_question_discounted_is_a_withdrawal_row():
    """0455/12 ON21's scheme prints "Question Discounted" in Q22's Answer cell."""
    from qbx.validate import is_removed_notice

    assert is_removed_notice("Question Discounted")
    assert not is_removed_notice("Discounted cash flow")


def test_a_removal_notice_for_another_question_ends_the_part_above_it():
    """0455/12 ON21: Q22's notice sits where Q22 was, under Q21's options, with no number of
    its own - it's no part of Q21's text or crop."""
    from qbx.tree import TextSpan, _before_other_removal_notice

    spans = [
        TextSpan(text="How is the rate of inflation measured?", page=8, y0=62, x0=70),
        TextSpan(text="D by calculating the total value of exports", page=8, y0=170, x0=70),
        TextSpan(text="Due to an issue with Question 22, the question has been removed from the question paper.", page=8, y0=189, x0=70),
    ]
    assert _before_other_removal_notice(spans, (8, 62, 45), (8, 240, 45), 21) == (8, 189, 70)
    # its own notice ("5 Due to an issue with Question 5...") stays its content
    assert _before_other_removal_notice(spans, (8, 62, 45), (8, 240, 45), 22) == (8, 240, 45)


def test_a_withdrawn_question_with_no_number_left_is_withdrawn_not_missing():
    from qbx.pipeline import _drop_withdrawn

    def q(n):
        return Question(id=str(n), paper_key="p", board="b", subject="s", question_number=n, total_marks=1, stem_text="", stem_image_path="",
                        parts=[QuestionPart(id=f"{n}.", question_id=str(n), path="", depth=1, text="x", image_path="", marks=1)])

    rows = [MarkSchemeEntry(id="22.stem", paper_key="p", question_number=22, part_path="", answer_text="Question Discounted", marks=1)]
    kept, kept_rows, _, withdrawn = _drop_withdrawn([q(21), q(23)], rows, [], {(22, "")})
    assert withdrawn == ["22"] and kept_rows == []


def test_parts_before_the_first_number_are_the_question_the_instruction_names():
    """0455/22 ON21 prints "Answer all parts of Question 1." over (a)-(h), and no "1"."""
    from qbx.anchors import AnchorSpan, TierResult
    from qbx.pipeline import _number_unnumbered_first_question
    from qbx.tree import TextSpan

    def span(text, x, y, page, tier):
        return AnchorSpan(text=text, x0=x, y0=y, page=page, pattern_tiers=[tier], bbox=(x, y, x + 12, y + 11), origin=(x, y + 9))

    numbers = TierResult(tier="question", x0_center=50, spans=[span("2", 50, 61, 3, "question")], rejected=[], resolved_by_position=[])
    parts = TierResult(tier="part", x0_center=72, spans=[span("(a)", 72, 87, 2, "part"), span("(b)", 72, 113, 2, "part"), span("(a)", 72, 90, 3, "part")], rejected=[], resolved_by_position=[])
    text = [TextSpan(text="Answer all parts of Question 1. Refer to the source material in your answers.", page=2, y0=61, x0=50)]
    _number_unnumbered_first_question([numbers, parts], text)
    added = [s for s in numbers.spans if s.text == "1"]
    assert len(added) == 1 and (added[0].page, added[0].y0, added[0].x0) == (2, 87, 50)
    # nothing before the first number: nothing added
    _number_unnumbered_first_question([numbers, parts], text)
    assert len([s for s in numbers.spans if s.text == "1"]) == 1


def test_a_question_number_misprinted_with_a_digit_too_many():
    """0455/12 FM20's key prints "113" between "12" and "14"."""
    from qbx.markscheme import MSSpan, _overtyped_numbers

    def span(text, y, x=300):
        return MSSpan(text=text, page=1, x=x, y=y, bbox=(x, y, x + 6 * len(text), y + 10))

    spans = [span("12", 100, 303), span("113", 120), span("14", 140, 303), span("150", 400, 300)]
    assert [s.text for s in _overtyped_numbers(spans)] == ["12", "13", "14", "150"]


def test_a_cover_with_no_candidate_block_is_a_cover():
    """0455/23 ON18 comes with an answer booklet: its cover has no candidate boxes or
    INFORMATION block, only the instructions - read as a question page, the paper code was
    looked for in the cover's foot and the paper couldn't be extracted."""
    from qbx.pages import PageType, classify_page

    cover = "ECONOMICS\n0455/23\nREAD THESE INSTRUCTIONS FIRST\nAn Answer Booklet is provided inside this Question Paper.\n"
    assert classify_page(cover, 1, 8) == PageType.COVER
    assert classify_page("This document consists of 5 printed pages.\n" + cover, 1, 8) == PageType.COVER
