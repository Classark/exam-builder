import pymupdf

from qbx.anchors import find_anchor_spans, resolve_tiers
from qbx.pages import PageType, classify_document
from qbx.tree import build_tree, collect_text_spans


def _make_synthetic_paper() -> pymupdf.Document:
    """A 2-page synthetic paper (no real exam content) exercising: a leaf part, a part
    with subparts, a subpart continuing across a page break, a dotted answer line that
    must be stripped, and a `[Total: n]` annotation after a question's last part."""
    doc = pymupdf.open()
    page1 = doc.new_page(width=595, height=842)
    lines_page1 = [
        (100, 50, "1"),
        (120, 72, "(a)"),
        (140, 72, "Explain X. [2]"),
        (160, 72, "(b)"),
        (180, 72, "Explain Y."),
        (200, 96, "(i)"),
        (220, 96, "Sub one. [1]"),
    ]
    for y, x, text in lines_page1:
        page1.insert_text((x, y), text)

    page2 = doc.new_page(width=595, height=842)
    lines_page2 = [
        (100, 96, "(ii)"),
        (120, 96, "Sub two."),
        (140, 96, "................"),
        (160, 96, "[1]"),
        (200, 50, "2"),
        (220, 72, "(a)"),
        (240, 72, "Solo leaf. [3]"),
        (260, 50, "[Total: 6]"),
        (300, 50, "3"),
        (320, 50, "An unlettered question with no parts. [4]"),
    ]
    for y, x, text in lines_page2:
        page2.insert_text((x, y), text)

    return doc


def _build(doc):
    page_types = classify_document(doc)
    content_pages = {i for i, pt in enumerate(page_types) if pt == PageType.QUESTION}
    spans = find_anchor_spans(doc, content_pages=content_pages)
    tier_results = resolve_tiers(spans)
    text_spans = collect_text_spans(doc, content_pages=content_pages)
    return build_tree(tier_results, text_spans)


def test_both_pages_classified_as_question():
    doc = _make_synthetic_paper()
    assert classify_document(doc) == [PageType.QUESTION, PageType.QUESTION]


def test_leaf_part_extracted_with_marks():
    doc = _make_synthetic_paper()
    questions = _build(doc)
    q1 = next(q for q in questions if q.question_number == 1)
    part_a = next(p for p in q1.parts if p.path == "a")

    assert "Explain X" in part_a.text
    assert part_a.marks == 2


def test_container_part_has_no_own_entry_but_subparts_do():
    doc = _make_synthetic_paper()
    questions = _build(doc)
    q1 = next(q for q in questions if q.question_number == 1)
    paths = [p.path for p in q1.parts]

    assert "b" not in paths
    assert "b.i" in paths
    assert "b.ii" in paths


def test_container_intro_text_is_prepended_to_first_subpart():
    doc = _make_synthetic_paper()
    questions = _build(doc)
    q1 = next(q for q in questions if q.question_number == 1)
    part_bi = next(p for p in q1.parts if p.path == "b.i")

    assert "Explain Y" in part_bi.text
    assert "Sub one" in part_bi.text


def test_subpart_continues_across_page_break():
    doc = _make_synthetic_paper()
    questions = _build(doc)
    q1 = next(q for q in questions if q.question_number == 1)
    part_bii = next(p for p in q1.parts if p.path == "b.ii")

    assert "Sub two" in part_bii.text
    assert part_bii.marks == 1


def test_dotted_answer_line_is_stripped():
    doc = _make_synthetic_paper()
    questions = _build(doc)
    q1 = next(q for q in questions if q.question_number == 1)
    part_bii = next(p for p in q1.parts if p.path == "b.ii")

    assert "." * 8 not in part_bii.text


def test_total_marks_extracted_and_not_leaked_into_last_leaf():
    doc = _make_synthetic_paper()
    questions = _build(doc)
    q2 = next(q for q in questions if q.question_number == 2)
    part_a = next(p for p in q2.parts if p.path == "a")

    assert q2.total_marks == 6
    assert part_a.marks == 3
    assert "Total" not in part_a.text


def test_unlettered_question_keeps_its_own_stem_and_marks():
    # regression: a question with no (a)/(b) parts at all (e.g. a table/matching item)
    # must not fall through with an empty stem and lost marks.
    doc = _make_synthetic_paper()
    questions = _build(doc)
    q3 = next(q for q in questions if q.question_number == 3)

    assert "unlettered question" in q3.stem_text
    assert q3.total_marks == 4


def test_unlettered_question_gets_a_synthetic_leaf_part_for_joining():
    # so its mark scheme entry (also path "") has something to join to - see join.py
    doc = _make_synthetic_paper()
    questions = _build(doc)
    q3 = next(q for q in questions if q.question_number == 3)

    assert len(q3.parts) == 1
    assert q3.parts[0].path == ""
    assert q3.parts[0].marks == 4


def test_a_state_symbol_line_is_not_a_part():
    # regression (0620 MCQ papers MJ22/ON23, Q7): an equation's state symbols on a line of
    # their own - "(l) (l) (aq)" - started with "(l)" and became part (l) of a question
    # that has no parts, splitting it and losing its mark
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "7")
    page.insert_text((72, 100), "The equation represents the reaction.")
    page.insert_text((72, 120), "MgO + 2HCl -> MgCl2 + H2O")
    page.insert_text((72, 135), "(l) (l) (aq)")
    page.insert_text((50, 200), "8")
    page.insert_text((72, 200), "Which statement is correct? [1]")
    page.insert_text((50, 260), "9")
    page.insert_text((72, 260), "Metals conduct.")
    page.insert_text((72, 280), "(a) Name one metal. [1]")
    page.insert_text((72, 300), "(b) Name another. [1]")

    spans = find_anchor_spans(doc)
    questions = {q.question_number: q for q in build_tree(resolve_tiers(spans), collect_text_spans(doc))}
    doc.close()

    assert [p.path for p in questions[7].parts] == [""]  # one unlettered question, no "(l)"
    assert "(l) (l) (aq)" in questions[7].parts[0].text  # its text kept
    assert [p.path for p in questions[9].parts] == ["a", "b"]  # real parts untouched


def test_a_number_breaking_the_question_sequence_is_not_a_question():
    # regression (0620/52 and 62 FM20): a graph's axis label "40 cm3 of gas / s" starts its
    # line at the question numbers' indent and read as question 40 between 1 and 2
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "1")
    page.insert_text((72, 100), "(a) Plot the results.")
    page.insert_text((48, 150), "40")
    page.insert_text((72, 150), "cm3 of gas / s")
    page.insert_text((400, 170), "[2]")  # after the graph: text after a mark leads in the next part
    page.insert_text((72, 200), "(b) Draw a line. [1]")
    page.insert_text((50, 300), "2")
    page.insert_text((72, 300), "(a) Name a gas. [1]")

    spans = find_anchor_spans(doc)
    questions = {q.question_number: q for q in build_tree(resolve_tiers(spans), collect_text_spans(doc))}
    doc.close()

    assert sorted(questions) == [1, 2]
    assert [p.path for p in questions[1].parts] == ["a", "b"]  # (b) stays with question 1
    assert "cm3 of gas" in questions[1].parts[0].text  # the label kept as text


def test_unlettered_question_with_a_total_still_gets_its_part():
    # regression (0620/41 and 43 MJ21, Q2): an unlettered question printing "[Total: 5]"
    # got no part at all, leaving its mark scheme row nothing to join to
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "2")
    page.insert_text((72, 100), "Complete the table. [5]")
    page.insert_text((400, 130), "[Total: 5]")
    page.insert_text((50, 200), "3")
    page.insert_text((72, 200), "Name a gas. [1]")

    spans = find_anchor_spans(doc)
    questions = {q.question_number: q for q in build_tree(resolve_tiers(spans), collect_text_spans(doc))}
    doc.close()

    assert [(p.path, p.marks) for p in questions[2].parts] == [("", 5)]
    assert questions[2].total_marks == 5


def test_a_part_with_two_mark_allocations_is_worth_both():
    # regression (0620/41 ON21, 5(d)(iii)): "[1]" for an answer line, then a bullet
    # "Draw the structure... [2]" in the same part - worth 3, read as 2
    from qbx.tree import _marks_value

    assert _marks_value("Name it. ........ [1] Draw the structure of it. [2]") == 3
    assert _marks_value("Explain. ........ [2] Working space") == 2
    assert _marks_value("[Total: 8]") == 0  # a question's total is never a part's marks


def test_a_part_label_split_across_two_spans_is_still_a_part():
    # regression (0620/52 MJ21, 1(a)): the label came out as "(a" + ") Complete the table."
    # - a font change on the bracket - so question 1 lost its (a) and its marks
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "1")
    page.insert_text((72, 120), "(a", fontname="hebo")
    x = 72 + pymupdf.get_text_length("(a", fontname="hebo", fontsize=11)
    page.insert_text((x, 120), ") Complete the table. [2]", fontname="helv")
    page.insert_text((72, 160), "(b) Name a gas. [1]")

    spans = find_anchor_spans(doc)
    questions = build_tree(resolve_tiers(spans), collect_text_spans(doc))
    doc.close()

    assert [p.path for p in questions[0].parts] == ["a", "b"]
    assert questions[0].parts[0].marks == 2


def test_text_after_a_parts_mark_leads_in_the_next_part():
    # user-reported (Edexcel 4MA0): "(a) ... (2)  3/10 of a triangle is shaded.  (b) ..." -
    # the line after (a)'s mark is (b)'s lead-in, and was cropped with (a)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "1")
    page.insert_text((72, 100), "Here is a shape.")
    page.insert_text((72, 130), "(a) What fraction is shaded?")
    page.insert_text((400, 180), "[2]")
    page.insert_text((72, 210), "3/10 of a triangle is shaded.")
    page.insert_text((72, 240), "(b) What fraction is not shaded?")
    page.insert_text((400, 280), "[1]")
    page.insert_text((50, 340), "2")
    page.insert_text((72, 340), "Work out 3 + 4. [1]")

    spans = find_anchor_spans(doc)
    boundaries: dict = {}
    questions = {
        q.question_number: q
        for q in build_tree(resolve_tiers(spans), collect_text_spans(doc), boundaries_out=boundaries)
    }
    doc.close()

    a, b = questions[1].parts
    assert "triangle" not in a.text and "triangle" in b.text
    assert boundaries["1.a"][1][1] < 215 and boundaries["1.b"][0][1] < 215
    assert [p.marks for p in (a, b)] == [2, 1]


def test_a_grids_axis_titles_after_the_mark_stay_with_their_part():
    # user-reported (4MA0 3H June 2016 Q11): "(b) On the grid, draw a cumulative frequency
    # graph. (2)", then the grid - its axis titles are four words in all but never more than
    # two on a line, and the whole grid went to (c) as its lead-in
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "1")
    page.insert_text((72, 100), "(a) Complete the table.")
    page.insert_text((400, 120), "[1]")
    page.insert_text((72, 150), "(b) On the grid, draw a cumulative frequency graph.")
    page.insert_text((400, 165), "[2]")
    page.insert_text((42, 250), "Cumulative")
    page.insert_text((50, 264), "frequency")
    for n, x in enumerate(range(117, 500, 55)):
        page.insert_text((x, 400), str(40 + 10 * n))
    page.insert_text((308, 414), "Weight (w kg)")
    page.insert_text((72, 440), "(c) Use your graph to estimate the median.")
    page.insert_text((400, 470), "[2]")

    spans = find_anchor_spans(doc)
    questions = build_tree(resolve_tiers(spans), collect_text_spans(doc))
    doc.close()

    b, c = questions[0].parts[1:]
    assert "Weight" in b.text and "Cumulative" in b.text
    assert c.text.startswith("(c)")


def test_multiple_choice_options_after_the_mark_stay_with_their_part():
    # user-reported (4BI1 2B November 2020): Edexcel prints "(1)" before the options A-D, and
    # they went to (b) as its lead-in
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "1")
    page.insert_text((72, 100), "(a) Which process is affected if kidneys are damaged?")
    page.insert_text((400, 120), "[1]")
    for i, option in enumerate(["A digestion", "B mutation", "C ultrafiltration", "D vaccination"]):
        page.insert_text((90, 140 + 18 * i), option)
    page.insert_text((72, 230), "(b) Suggest three ways to reduce the risk. [3]")

    spans = find_anchor_spans(doc)
    questions = build_tree(resolve_tiers(spans), collect_text_spans(doc))
    doc.close()

    a, b = questions[0].parts
    assert "ultrafiltration" in a.text and "ultrafiltration" not in b.text


def test_an_array_index_in_the_question_is_not_a_mark():
    # user-reported (0478/21 MJ26 Q4): "sort the array Names[50]" read as a [50] mark
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "4")
    page.insert_text((72, 100), "The algorithm sorts the array Names[50]")
    page.insert_text((72, 130), "START")
    page.insert_text((72 + pymupdf.get_text_length("START", fontsize=11), 130), " ")
    page.insert_text((525, 160), "[4]")
    page.insert_text((50, 200), "5")
    page.insert_text((72, 200), "Draw a flowchart. START")
    page.insert_text((525, 230), "[6]")

    spans = find_anchor_spans(doc)
    questions = build_tree(resolve_tiers(spans), collect_text_spans(doc))
    doc.close()

    assert [q.total_marks for q in questions] == [4, 6]
