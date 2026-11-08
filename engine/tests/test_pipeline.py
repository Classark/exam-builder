from qbx.pipeline import _backfill_marks_from_mark_scheme
from qbx.schema import MarkSchemeEntry, Question, QuestionPart, QuestionPartMarkScheme


def _question(number, parts, total_marks=None):
    return Question(
        paper_key="x", board="Cambridge", subject="0620", question_number=number,
        stem_text="", stem_image_path="",
        total_marks=total_marks if total_marks is not None else sum(p.marks for p in parts),
        parts=parts,
    )


def _part(qid, path, marks):
    return QuestionPart(id=f"{qid}.{path}", question_id=str(qid), path=path, depth=1, text="", image_path="", marks=marks)


def test_backfills_marks_for_a_part_with_none_of_its_own():
    # real case: a multiple-choice question, no [n] bracket anywhere in the QP.
    part = _part(1, "", 0)
    question = _question(1, [part])
    entry = MarkSchemeEntry(id="ms1", paper_key="x", question_number=1, part_path="", answer_text="A", marks=1)
    joins = [QuestionPartMarkScheme(part_id=part.id, mark_scheme_entry_id="ms1")]

    _backfill_marks_from_mark_scheme([question], [entry], joins)

    assert part.marks == 1
    assert question.total_marks == 1


def test_does_not_touch_a_part_that_already_has_marks():
    part = _part(1, "a", 3)
    question = _question(1, [part])
    entry = MarkSchemeEntry(id="ms1", paper_key="x", question_number=1, part_path="a", answer_text="", marks=99)
    joins = [QuestionPartMarkScheme(part_id=part.id, mark_scheme_entry_id="ms1")]

    _backfill_marks_from_mark_scheme([question], [entry], joins)

    assert part.marks == 3  # not overwritten by the (deliberately different) MS value


def test_ambiguous_join_is_not_backfilled():
    # a part joined to more than one mark scheme entry isn't unambiguous enough to guess.
    part = _part(1, "b", 0)
    question = _question(1, [part])
    entries = [
        MarkSchemeEntry(id="ms1", paper_key="x", question_number=1, part_path="b.i", answer_text="", marks=1),
        MarkSchemeEntry(id="ms2", paper_key="x", question_number=1, part_path="b.ii", answer_text="", marks=1),
    ]
    joins = [
        QuestionPartMarkScheme(part_id=part.id, mark_scheme_entry_id="ms1"),
        QuestionPartMarkScheme(part_id=part.id, mark_scheme_entry_id="ms2"),
    ]

    _backfill_marks_from_mark_scheme([question], entries, joins)

    assert part.marks == 0


def test_explicit_total_is_not_clobbered_by_backfill():
    # tree.py sets total_marks from an explicit "[Total: n]" annotation sometimes - that
    # must survive even if a sibling part gets backfilled from 0 to something nonzero.
    part_a = _part(1, "a", 5)
    part_b = _part(1, "b", 0)  # will be backfilled to 1
    question = _question(1, [part_a, part_b], total_marks=10)  # explicit total, not 5+0=5
    entry = MarkSchemeEntry(id="ms1", paper_key="x", question_number=1, part_path="b", answer_text="", marks=1)
    joins = [QuestionPartMarkScheme(part_id=part_b.id, mark_scheme_entry_id="ms1")]

    _backfill_marks_from_mark_scheme([question], [entry], joins)

    assert part_b.marks == 1
    assert question.total_marks == 10  # untouched


def test_cover_total_of_an_older_multiple_choice_paper_is_questions_times_one_mark():
    # regression (0620 MCQ covers 2016-2019): no "total mark for this paper is n" at all
    from qbx.pipeline import read_cover_total

    cover = (
        "There are forty questions on this paper. Answer all questions.\n"
        "Each correct answer will score one mark. A mark will not be deducted for a wrong answer."
    )
    assert read_cover_total(cover) == 40
    assert read_cover_total("The total mark for this paper is 80.") == 80
    assert read_cover_total("There are forty questions on this paper.") is None


def _entry(id_, number, path, marks, text=""):
    return MarkSchemeEntry(id=id_, paper_key="x", question_number=number, part_path=path, answer_text=text, marks=marks)


def test_a_stray_question_row_in_a_settled_question_is_dropped():
    # WMA11 June 2023 Q6: the previous question's notes read under "6", joining nothing
    from qbx.pipeline import _without_stray_question_rows

    a, b = _part(6, "a", 2), _part(6, "b", 5)
    question = _question(6, [a, b], total_marks=7)
    rows = [_entry("s", 6, "", 20, "Condone work in n"), _entry("ra", 6, "a", 2), _entry("rb", 6, "b", 5)]
    joins = [QuestionPartMarkScheme(part_id=a.id, mark_scheme_entry_id="ra"),
             QuestionPartMarkScheme(part_id=b.id, mark_scheme_entry_id="rb")]
    assert [e.id for e in _without_stray_question_rows([question], rows, joins)] == ["ra", "rb"]
    # not while the question is short: the row may be what's missing
    b.marks = 3
    assert len(_without_stray_question_rows([question], rows, joins)) == 3


def test_a_group_marked_once_on_its_last_subpart_is_one_part():
    # WMA11 June 2025 Q9: "[6]" after (b)(ii) for (i) and (ii); the scheme answers each in a
    # row but marks only the last
    from qbx.pipeline import _join_unsplit_groups

    question = _question(9, [_part(9, "a", 4), _part(9, "b.i", 0), _part(9, "b.ii", 6)], total_marks=10)
    rows = [_entry("a", 9, "a", 4), _entry("bi", 9, "b.i", 0), _entry("bii", 9, "b.ii", 6)]
    _join_unsplit_groups([question], rows, {})
    assert [(p.path, p.marks) for p in question.parts] == [("a", 4), ("b", 6)]
    # the scheme marking (i) itself keeps them apart
    question = _question(9, [_part(9, "b.i", 0), _part(9, "b.ii", 6)], total_marks=6)
    _join_unsplit_groups([question], [_entry("bi", 9, "b.i", 2), _entry("bii", 9, "b.ii", 4)], {})
    assert [p.path for p in question.parts] == ["b.i", "b.ii"]


def test_questions_the_board_withdrew_are_marked_removed():
    from qbx.pipeline import _mark_removed

    notice = "Due to an issue with Question 5, the question has been removed from the question paper."
    q4 = _question(4, [_part(4, "b.ii", 3)])
    q5 = _question(5, [_part(5, "", 0)])
    q5.parts[0].text = "5 " + notice
    q10 = _question(10, [_part(10, "a", 7), _part(10, "b", 8)])
    q11 = _question(11, [_part(11, "a", 3), _part(11, "b", 2)])
    rows = [
        _entry("r4", 4, "b.ii", 3, "use of iteration " + notice),  # Q5's notice run on into 4(b)(ii)
        _entry("r10", 10, "", 15, "Due to an issue with this question, the question has been removed from the question paper."),
        _entry("r11", 11, "a", 0, "Due to an issue with question 11(a) , the question has been removed from the question paper."),
    ]
    _mark_removed([q4, q5, q10, q11], rows, [])
    assert [(q.question_number, p.path) for q in (q4, q5, q10, q11) for p in q.parts if p.removed] == [
        (5, ""), (10, "a"), (10, "b"), (11, "a")
    ]
    assert [q.removed for q in (q4, q5, q10, q11)] == [False, True, True, False]
