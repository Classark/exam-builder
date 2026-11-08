from qbx.join import join_parts_to_mark_scheme
from qbx.schema import MarkSchemeEntry, Question, QuestionPart


def _question(number, parts):
    return Question(
        paper_key="x", board="Cambridge", subject="0478", question_number=number,
        stem_text="", stem_image_path="", total_marks=0, parts=parts,
    )


def _part(question_number, path):
    return QuestionPart(
        id=f"{question_number}.{path}", question_id=str(question_number), path=path,
        depth=path.count(".") + 1, text="", image_path="", marks=0,
    )


def _entry(question_number, part_path):
    return MarkSchemeEntry(
        id=f"{question_number}.{part_path or 'stem'}", paper_key="x",
        question_number=question_number, part_path=part_path, answer_text="", marks=0,
    )


def test_exact_match():
    questions = [_question(1, [_part(1, "a")])]
    mark_scheme = [_entry(1, "a")]

    joins = join_parts_to_mark_scheme(questions, mark_scheme)

    assert len(joins) == 1
    assert joins[0].part_id == "1.a"
    assert joins[0].mark_scheme_entry_id == "1.a"


def test_mark_scheme_splits_a_qp_part_further():
    # QP has just "3(b)", the mark scheme splits it into (b)(i) and (b)(ii)
    questions = [_question(3, [_part(3, "b")])]
    mark_scheme = [_entry(3, "b.i"), _entry(3, "b.ii")]

    joins = join_parts_to_mark_scheme(questions, mark_scheme)

    assert {j.mark_scheme_entry_id for j in joins} == {"3.b.i", "3.b.ii"}
    assert all(j.part_id == "3.b" for j in joins)


def test_qp_splits_further_than_the_mark_scheme():
    # QP has (b)(i)/(b)(ii), the mark scheme collapses them into one "(b)" row
    questions = [_question(3, [_part(3, "b.i"), _part(3, "b.ii")])]
    mark_scheme = [_entry(3, "b")]

    joins = join_parts_to_mark_scheme(questions, mark_scheme)

    assert {j.part_id for j in joins} == {"3.b.i", "3.b.ii"}
    assert all(j.mark_scheme_entry_id == "3.b" for j in joins)


def test_unrelated_parts_are_not_joined():
    questions = [_question(1, [_part(1, "a")])]
    mark_scheme = [_entry(1, "b")]

    assert join_parts_to_mark_scheme(questions, mark_scheme) == []


def test_different_questions_never_join():
    questions = [_question(1, [_part(1, "a")])]
    mark_scheme = [_entry(2, "a")]

    assert join_parts_to_mark_scheme(questions, mark_scheme) == []


def _pairs(questions, mark_scheme):
    return sorted((j.part_id, j.mark_scheme_entry_id) for j in join_parts_to_mark_scheme(questions, mark_scheme))


def test_a_mislabelled_row_joins_by_its_position():
    # regression (0620/32 ON20): "4(b)(ii)" printed straight after 4(c)(i), meaning 4(c)(ii)
    paths = ["a", "b", "c.i", "c.ii"]
    questions = [_question(4, [_part(4, p) for p in paths])]
    mark_scheme = [_entry(4, p) for p in ["a", "b", "c.i", "b.ii"]]

    assert ("4.c.ii", "4.b.ii") in _pairs(questions, mark_scheme)


def test_a_run_of_mislabelled_rows_joins_one_for_one():
    # regression (0620/41 ON19): "6(b)(iv)", "6(b)(v)" after 6(c)(iii)
    questions = [_question(6, [_part(6, p) for p in ["b", "c.iii", "c.iv", "c.v", "d"]])]
    mark_scheme = [_entry(6, p) for p in ["b", "c.iii", "b.iv", "b.v", "d"]]

    pairs = _pairs(questions, mark_scheme)
    assert ("6.c.iv", "6.b.iv") in pairs and ("6.c.v", "6.b.v") in pairs


def test_unequal_runs_are_left_unjoined():
    # one part, two stray rows: which row is its is a guess - leave it to the report
    questions = [_question(2, [_part(2, p) for p in ["a", "b", "c"]])]
    mark_scheme = [_entry(2, p) for p in ["a", "x", "y", "c"]]

    assert not any(part_id == "2.b" for part_id, _ in _pairs(questions, mark_scheme))


def test_a_corrected_label_that_fits_no_part_falls_back_to_the_printed_one():
    # regression (0620/62 ON17): "3(e)" printed on two rows, read as (e) then (f) - but the
    # QP has no (f), so both rows are (e)'s
    questions = [_question(3, [_part(3, p) for p in ["d", "e"]])]
    second = _entry(3, "f")
    second.printed_path = "e"
    mark_scheme = [_entry(3, "d"), _entry(3, "e"), second]

    assert ("3.e", "3.f") in _pairs(questions, mark_scheme)


def test_a_lone_later_numeral_is_a_mislabelled_row_not_a_subpart():
    # regression (0620/32 MJ24): rows 6(d)(i), "6(e)(ii)", "6(f)" for parts d.i, d.ii, e -
    # QP part 6(e) had taken "6(e)(ii)" as a subpart of its own and left 6(f) orphaned
    questions = [_question(6, [_part(6, p) for p in ["c", "d.i", "d.ii", "e"]])]
    mark_scheme = [_entry(6, p) for p in ["c", "d.i", "e.ii", "f"]]

    pairs = _pairs(questions, mark_scheme)
    assert ("6.d.ii", "6.e.ii") in pairs and ("6.e", "6.f") in pairs
    assert ("6.e", "6.e.ii") not in pairs


def test_a_later_numeral_under_its_parts_own_row_is_still_a_subpart():
    # regression (0620/42 ON19): "1(a)", "1(a)(ii)", "1(a)(iii)" - the (i) printed as the
    # part's own row; (ii) and (iii) still join QP part 1(a)'s subparts
    questions = [_question(1, [_part(1, p) for p in ["a.i", "a.ii", "a.iii", "b"]])]
    mark_scheme = [_entry(1, p) for p in ["a", "a.ii", "a.iii", "b"]]

    pairs = _pairs(questions, mark_scheme)
    assert ("1.a.ii", "1.a.ii") in pairs and ("1.a.iii", "1.a.iii") in pairs


def test_a_question_printed_without_parts_takes_its_rows_whatever_their_label():
    # 4MA0 2FR June 2018 Q12: the paper prints no (a), only "(Total for Question 12 is 2
    # marks)"; the scheme labels its row "12 (a)" anyway.
    questions = [_question(12, [_part(12, "")])]
    mark_scheme = [_entry(12, "a")]

    assert _pairs(questions, mark_scheme) == [("12.", "12.a")]


def test_a_question_with_one_unlettered_part_takes_every_row_of_its_question():
    """4MA0 1FR June 2013 Q5: three answers on one line under one mark; the scheme gives
    rows "5", "(ii)", "(iii)" - the first matched, the others were left orphaned."""
    q = _question(5, [_part(5, "")])
    joins = join_parts_to_mark_scheme([q], [_entry(5, ""), _entry(5, "ii"), _entry(5, "iii")])
    assert len(joins) == 3 and {j.part_id for j in joins} == {q.parts[0].id}


def test_a_question_numbered_two_ways_pairs_in_order():
    """(i), (ii), (iii) on the paper, (a), (b), (c) in the scheme (4MA1 1FR January 2023 Q2):
    the same list counted two ways - paired in order, marks agreeing."""
    q = _question(2, [_part(2, "i"), _part(2, "ii"), _part(2, "iii")])
    rows = [_entry(2, "a"), _entry(2, "b"), _entry(2, "c")]
    joins = join_parts_to_mark_scheme([q], rows)
    assert [(j.part_id, j.mark_scheme_entry_id) for j in joins] == [
        (p.id, e.id) for p, e in zip(q.parts, rows)
    ]
    # but never two lists numbered the same way, or of different lengths
    assert join_parts_to_mark_scheme([_question(2, [_part(2, "a"), _part(2, "b")])], [_entry(2, "c")]) == []


def test_paper_and_scheme_cutting_a_question_differently_are_aligned_by_marks():
    """The paper's (a) worth 2 against rows (a) and (b), 1 each; its (b) against row (c)
    (4MA0 1F January 2018 Q6). The running mark sums meet after (a)|(a)+(b), then (b)|(c)."""
    a, b = _part(6, "a"), _part(6, "b")
    a.marks, b.marks = 2, 1
    rows = [_entry(6, "a"), _entry(6, "b"), _entry(6, "c")]
    for row in rows:
        row.marks = 1
    joins = join_parts_to_mark_scheme([_question(6, [a, b])], rows)
    assert sorted((j.part_id, j.mark_scheme_entry_id) for j in joins) == sorted(
        [(a.id, rows[0].id), (a.id, rows[1].id), (b.id, rows[2].id)]
    )
    # totals that disagree say nothing: the labels' own joins stand
    rows[2].marks = 3
    joins = join_parts_to_mark_scheme([_question(6, [a, b])], rows)
    assert (b.id, rows[2].id) not in {(j.part_id, j.mark_scheme_entry_id) for j in joins}
