import pymupdf

from qbx.anchors import AnchorSpan, check_tier_ordering, find_anchor_spans, resolve_tiers
from qbx.tree import collect_text_spans


def _span(text, x0, y0=100.0, page=0):
    from qbx.anchors import _match_anchor

    marker, tiers = _match_anchor(text)
    return AnchorSpan(
        text=marker, x0=x0, y0=y0, page=page, pattern_tiers=tiers,
        bbox=(x0, y0, x0 + len(marker) * 6.0, y0 + 10.0),
    )


def test_anchor_y0_matches_collect_text_spans_line_y_even_with_mixed_font_sizes():
    # regression (real 0620 QP): a bold marker in a larger font glued to smaller-font text
    # on the same line ("(e) Calculate the volume...") can have a *span* bbox y0 a few pt
    # off from the *line* bbox y0. anchors.py and tree.py must agree on which one they use,
    # or the boundary comparison misattributes that line's content to the wrong part.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 200), "(e)", fontsize=14, fontname="hebo")
    page.insert_text((95, 200), "Calculate the volume.", fontsize=10)

    anchor_spans = find_anchor_spans(doc)
    text_spans = collect_text_spans(doc)

    anchor = next(s for s in anchor_spans if s.text == "(e)")
    content = next(s for s in text_spans if "Calculate" in s.text)

    assert anchor.y0 == content.y0


def test_vertical_margin_sidebar_is_excluded():
    # regression: "DO NOT WRITE IN THIS MARGIN", repeated top-to-bottom in a narrow strip
    # along the page edge (left or right, board-dependent), is set as vertical text
    # (line["dir"] != (1,0)). It must never leak into anchors or extracted content,
    # regardless of where its bbox happens to fall relative to the header/footer margin.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((580, 800), "DO NOT WRITE IN THIS MARGIN", rotate=90)
    page.insert_text((72, 200), "1")
    page.insert_text((72, 220), "Explain X. [2]")

    anchor_spans = find_anchor_spans(doc)
    text_spans = collect_text_spans(doc)

    assert not any("MARGIN" in s.text.upper() for s in anchor_spans)
    assert not any("MARGIN" in s.text.upper() for s in text_spans)
    assert any(s.text == "1" for s in anchor_spans)


def test_real_anchors_cluster_by_tier():
    spans = [
        _span("1", 50), _span("2", 50), _span("3", 50),
        _span("(a)", 72), _span("(b)", 72),
        _span("(ii)", 96),
    ]
    results = {r.tier: r for r in resolve_tiers(spans)}

    assert [s.text for s in results["question"].spans] == ["1", "2", "3"]
    assert [s.text for s in results["part"].spans] == ["(a)", "(b)"]
    assert [s.text for s in results["subpart"].spans] == ["(ii)"]
    assert check_tier_ordering(list(results.values()))


def test_bare_digit_noise_is_rejected_from_question_tier():
    # regression: a binary truth table's bare 0/1 digits also match the question pattern,
    # but sit at a very different x0 from the real question numbers.
    real_questions = [_span(str(n), 50) for n in range(1, 6)]
    binary_noise = [_span(d, 150) for d in ["0", "1"] * 4]

    result = next(r for r in resolve_tiers(real_questions + binary_noise) if r.tier == "question")

    assert len(result.spans) == 5
    assert all(s.x0 == 50 for s in result.spans)
    assert len(result.rejected) == 8


def test_i_and_v_are_disambiguated_by_position_not_regex_order():
    # regression: "(i)" matches both PART_RE (letter i) and SUBPART_RE (roman numeral i);
    # it must be assigned to whichever tier's established x0 it's actually closer to.
    spans = [
        _span("(a)", 72), _span("(b)", 72), _span("(c)", 72),  # establish part x0 = 72
        _span("(ii)", 96),  # establish subpart x0 = 96 (unambiguous roman numeral)
        _span("(i)", 96),  # ambiguous, but sits with the subparts
    ]
    results = {r.tier: r for r in resolve_tiers(spans)}

    assert "(i)" in [s.text for s in results["subpart"].spans]
    assert "(i)" not in [s.text for s in results["part"].spans]
    assert results["subpart"].resolved_by_position[0].text == "(i)"


def test_marker_glued_to_following_word_is_still_detected():
    # regression: a bold run can glue a marker to the next word into one span, e.g.
    # "(c) Four" when "(c)" and "Four" share formatting. The closing paren is a safe
    # boundary regardless of what follows in the same span.
    span = _span("(c) Four", 72)
    assert span.text == "(c)"
    assert span.pattern_tiers == ["part"]


def test_check_tier_ordering_flags_reversed_indentation():
    spans = [_span("1", 96), _span("(a)", 72), _span("(ii)", 50)]
    results = resolve_tiers(spans)
    assert not check_tier_ordering(results)


def test_question_number_and_first_part_on_one_line_keep_their_order():
    # real-paper finding (0620 s24): a question opening "5  (a)  Lead(II) bromide..." puts
    # its number and first part on one printed line, but they come back as two lines a
    # fraction of a point apart. Ordered by raw y, the "(a)" sorted above its own question
    # number - it was attributed to the previous question, and this question's subparts,
    # left with no container, were promoted to top-level parts.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((49.7, 100.0), "5", fontname="hebo")
    page.insert_text((71.0, 99.2), "(a)", fontname="hebo")  # same row, 0.8pt higher
    page.insert_text((91.8, 130.0), "(i)", fontname="hebo")

    spans = find_anchor_spans(doc, content_pages={0})
    ordered = sorted(spans, key=lambda s: (s.page, s.y0, s.x0))

    assert [s.text for s in ordered] == ["5", "(a)", "(i)"]


def test_subpart_sharing_a_line_with_its_part_is_still_an_anchor():
    # real-paper finding (0620 s24, Q7): "(a)  (i) Organic compound A has..." - requiring an
    # anchor to start its own line dropped the "(i)" and orphaned the subpart.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((71.0, 100.0), "(a)", fontname="hebo")
    page.insert_text((91.8, 100.0), "(i) Organic compound A has this composition.")

    spans = find_anchor_spans(doc, content_pages={0})

    assert sorted(s.text for s in spans) == ["(a)", "(i)"]


def test_inline_state_symbols_are_still_rejected():
    # the flip side: the "leads its line" rule is what stops chemistry state symbols being
    # read as part labels, and a marker preceded by a formula does not lead its line.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((71.0, 100.0), "SF4(g)  +  2H2O(g)")

    spans = find_anchor_spans(doc, content_pages={0})

    assert [s.text for s in spans] == []


def test_question_number_glued_to_its_text_is_still_an_anchor():
    # regression (0580 m25 qp 22): "12 (a) Write as..." came out as one span, so neither the
    # question number nor its first part was found, and their marks went missing
    from qbx.anchors import find_anchor_spans

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "11 A company sells items.")
    page.insert_text((50, 200), "12 (a) Write as a single fraction.")
    page.insert_text((72, 300), "(b) (i) Factorise.")
    page.insert_text((50, 400), "15 cm")  # a measurement label, not a question
    page.insert_text((50, 450), "1 ..............................")  # answer-line numbering

    found = [(s.text, round(s.x0)) for s in find_anchor_spans(doc)]
    doc.close()

    assert ("11", 50) in found and ("12", 50) in found
    # the markers after the first are placed by their own characters, not the span's start
    assert any(text == "(a)" and x > 55 for text, x in found)
    assert ("(b)", 72) in found and any(text == "(i)" and x > 80 for text, x in found)
    assert "15" not in [t for t, _ in found] and "1" not in [t for t, _ in found]


def test_family_and_weight_are_measured_when_the_font_name_says_nothing():
    # regression (0580 m25 qp 22): one merged font "AllAndNone" holds every glyph, so a
    # redrawn label came out in regular Arial beside the paper's bold Times ones
    from qbx.anchors import _family_vote, _width_suggests_bold

    span = {"font": "AllAndNone", "size": 11.5}

    def box(marker, fontname):
        return (0.0, 0.0, pymupdf.get_text_length(marker, fontsize=11.5, fontname=fontname), 10.0)

    assert _family_vote("12", box("12", "tibo"), span) is True
    assert _family_vote("12", box("12", "hebo"), span) is False
    assert _family_vote("(i)", box("(i)", "tibo"), span) is None  # same width either way
    # a named font needs no measuring
    assert _family_vote("12", box("12", "tibo"), {"font": "Arial-BoldMT", "size": 11.5}) is None
    # bold, judged against Times widths once the label is known to be Times
    assert _width_suggests_bold("(a)", box("(a)", "tibo"), span, times=True) is True
    assert _width_suggests_bold("(a)", box("(a)", "tiro"), span, times=True) is False


def test_markers_listed_side_by_side_on_one_row_are_not_parts():
    """4MA0 1FR January 2016 Q1(a): "(i) (ii) (iii)" under three shapes, and the answer
    line "(i) .... (ii) .... (iii) ....", were read as subparts - two parts both (a)(i)."""
    from qbx.anchors import _listed_markers

    assert _listed_markers("(i)  (ii)  (iii)") == {"i", "ii", "iii"}
    assert _listed_markers("(i) ........ (ii) ........ (iii) ........") == {"i", "ii", "iii"}
    assert _listed_markers("(a) (i) Find the value of x.") == set()
    # part (h) and its first subpart, not h then i (4BI1 2B November 2020)
    assert _listed_markers("(h)	 (i)	Suggest what is given to the control group.") == set()
    assert _listed_markers("(b) Work out CO2(g) and (s)") == set()


def test_a_control_character_is_a_gap_between_markers():
    """4MA0 3H June 2018 Q8 prints "8\x03 (a) Solve": the font's own space, a control
    character - read as part of the number, every such question lost its (a)."""
    from qbx.anchors import GLUED_QUESTION_NUMBER_RE, _MARKER_RUN_RE

    assert _MARKER_RUN_RE.match("8\x03 ")
    assert GLUED_QUESTION_NUMBER_RE.match("8\x03 (a) Solve")


def test_a_question_number_with_a_full_stop_is_its_number():
    """International A Level prints "1.", "11." - the number is the digits."""
    from qbx.anchors import _match_anchor

    assert _match_anchor("11.") == ("11", ["question"])


def test_letters_inside_numeral_parts_are_their_subparts():
    """Further Pure (WFM01 January 2025): "7. (i)", its (a), (b) an indent further in than
    the paper's own part letters, then "(ii)" and its (a). The letters were a second column
    of part letters, rejected as noise, and each (i) swallowed its (a) and (b)."""
    spans = [
        _span("1", 43, 60, 0), _span("(a)", 60, 100, 0), _span("(b)", 60, 200, 0),
        _span("2", 43, 60, 1), _span("(a)", 60, 100, 1), _span("(b)", 60, 200, 1),
        _span("7", 43, 60, 2), _span("(i)", 60, 60, 2), _span("(a)", 77, 120, 2),
        _span("(b)", 77, 200, 2), _span("(ii)", 60, 300, 2), _span("(a)", 77, 360, 2),
        _span("8", 43, 60, 3), _span("(a)", 60, 100, 3),
    ]
    results = {r.tier: r for r in resolve_tiers(spans)}
    subparts = [(s.page, s.text) for s in results["subpart"].spans]
    assert subparts == [(2, "(a)"), (2, "(b)"), (2, "(a)")]
    assert (3, "(a)") in [(s.page, s.text) for s in results["part"].spans]


def test_plain_markers_among_bold_ones_are_text():
    # regression (0610/33 ON22): a dichotomous key's plain "(a)", "(b)" lines sit at the part
    # indent among the paper's bold part letters, and split the question at every key line
    from qbx.anchors import resolve_tiers as resolve

    bold = [_span(m, 72, y0=100 + 40 * i) for i, m in enumerate(["(a)", "(b)", "(c)", "(d)", "(e)", "(f)"])]
    key = [_span("(a)", 72, y0=400), _span("(b)", 72, y0=420)]
    for s in key:
        s.bold = False
    part = next(r for r in resolve([_span("1", 50)] + bold + key) if r.tier == "part")
    assert [s.y0 for s in part.spans] == [s.y0 for s in bold]


def test_the_footer_barcode_is_not_text_but_a_drawn_line_is():
    from qbx.anchors import is_machine_readable_mark

    assert is_machine_readable_mark("ĬÍĊ®Ġ³íÅùĊ")  # 0610/32 MJ24, beside the footer
    assert not is_machine_readable_mark("¯¯¯¯¯¯¯¯¯¯¯")  # a rule set as macrons
    assert not is_machine_readable_mark("Température")


def test_a_low_footer_moves_the_page_bottom_down():
    # regression (0610/32 MJ24, user-reported): footer at y 806, and the "[2]" at 794 fell in
    # the 48pt margin - the part read no mark and its crop stopped above it
    from qbx.anchors import bottom_limit

    footer = [("© UCLES 2024", (50, 806, 120, 816)), ("0610/32/M/J/24", (270, 806, 330, 816))]
    assert bottom_limit(footer, 842, 48) == 805
    assert bottom_limit([("[2]", (530, 794, 545, 805))], 842, 48) == 794
    # a footer higher up than the margin never moves it up
    assert bottom_limit([("© UCLES 2024", (50, 785, 120, 795))], 842, 48) == 794


def test_plain_numbers_in_a_table_are_not_bold_question_numbers():
    # 0417/11 ON21 Q10(b): a spreadsheet's row numbers down column A, at the question indent,
    # outnumber the paper's bold question numbers - but sit on one page
    real = [_span(str(n), 50, y0=60, page=n) for n in range(1, 11)]
    table = [_span(str(n), 50, y0=100 + 13 * n, page=11) for n in range(1, 20)]
    for s in table:
        s.bold = False
    later = [_span(str(n), 50, y0=60, page=n + 1) for n in range(11, 14)]
    question = next(r for r in resolve_tiers(real + table + later) if r.tier == "question")
    assert [s.text for s in question.spans] == [str(n) for n in range(1, 14)]
