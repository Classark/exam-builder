import pymupdf

from qbx.markscheme import parse_mark_scheme, parse_mark_scheme_layout


def _write_ms_page(page, rows, rotation: int):
    """rows: list of (label, answer_lines, marks). On an unrotated page a row is a normal
    left-to-right line (shared y, increasing x). On a 90-rotated page - matching the real
    Cambridge layout in markscheme.py's module docstring - a visual row is instead a raw
    *column*: same raw x for the whole row, with raw y *decreasing* as reading position
    moves left-to-right after rotation (rotated_y = raw_x, rotated_x = pagewidth - raw_y)."""
    if rotation:
        row_x = 80
        for label, answer_lines, marks in rows:
            y = 700
            page.insert_text((row_x, y), label)
            for line in answer_lines:
                y -= 40
                page.insert_text((row_x, y), line)
            page.insert_text((row_x, 60), str(marks))
            row_x += 80
    else:
        y = 100
        for label, answer_lines, marks in rows:
            page.insert_text((50, y), label)
            page.insert_text((500, y), str(marks))
            for line in answer_lines:
                y += 20
                page.insert_text((150, y), line)
            y += 30


def _make_ms_pdf(tmp_path, rotation: int) -> str:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    if rotation:
        page.set_rotation(rotation)
    _write_ms_page(
        page,
        [
            ("1(a)", ["Any one from:", "Item one", "Item two"], 2),
            ("1(b)", ["Single line answer"], 1),
            ("2", ["No lettered parts here"], 4),
        ],
        rotation=rotation,
    )
    path = str(tmp_path / f"ms_{rotation}.pdf")
    doc.save(path)
    doc.close()
    return path


def test_parses_a_non_rotated_mark_scheme(tmp_path):
    path = _make_ms_pdf(tmp_path, rotation=0)
    entries = parse_mark_scheme(path)
    by_label = {f"{e.question_number}{e.part_path}": e for e in entries}

    assert by_label["1a"].marks == 2
    assert by_label["1b"].marks == 1
    assert by_label["2"].marks == 4
    assert "Item one" in by_label["1a"].answer_text


def test_parses_a_rotated_mark_scheme(tmp_path):
    # real Cambridge mark schemes are sometimes laid out on a rotated page (see
    # markscheme.py's module docstring) - the parser must give the same result either way.
    path = _make_ms_pdf(tmp_path, rotation=90)
    entries = parse_mark_scheme(path)
    by_label = {f"{e.question_number}{e.part_path}": e for e in entries}

    assert by_label["1a"].marks == 2
    assert by_label["1b"].marks == 1
    assert by_label["2"].marks == 4


def test_marks_value_not_mistaken_for_next_rows_label(tmp_path):
    # regression: a bare marks value ("2", "4", ...) also matches the row-label pattern
    # (the part/subpart groups are optional) and must not be mistaken for the next row.
    path = _make_ms_pdf(tmp_path, rotation=0)
    entries = parse_mark_scheme(path)
    entry_1a = next(e for e in entries if e.question_number == 1 and e.part_path == "a")

    assert "Item one" in entry_1a.answer_text
    assert "Item two" in entry_1a.answer_text


def test_numbered_preamble_heading_is_not_mistaken_for_a_question(tmp_path):
    # regression (real 0620 mark scheme): a numbered generic-guidance heading like
    # "6 Calculation specific guidance" also matches the bare-digit row-label pattern and
    # can land at the same x as the real Question column - but it has no accompanying
    # marks value on its row, unlike every genuine entry.
    doc = pymupdf.open()
    preamble_page = doc.new_page(width=595, height=842)
    preamble_page.insert_text((68, 55), "6")
    preamble_page.insert_text((90, 55), "Calculation specific guidance")
    preamble_page.insert_text((90, 80), "Some general marking prose with no mark value.")

    table_page = doc.new_page(width=595, height=842)
    table_page.insert_text((68, 100), "1(a)")
    table_page.insert_text((150, 100), "Real answer")
    table_page.insert_text((500, 100), "3")

    path = str(tmp_path / "ms_with_preamble.pdf")
    doc.save(path)
    doc.close()

    entries = parse_mark_scheme(path)

    assert len(entries) == 1
    assert entries[0].question_number == 1
    assert entries[0].part_path == "a"
    assert entries[0].marks == 3


def test_no_row_labels_returns_empty_rather_than_crashing(tmp_path):
    # not every mark scheme is a table at all (prose-style mark schemes exist too) -
    # the parser must degrade gracefully rather than error out.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "This mark scheme has no tabular row structure.")
    path = str(tmp_path / "prose_ms.pdf")
    doc.save(path)
    doc.close()

    assert parse_mark_scheme(path) == []


def test_max_n_marks_value_still_starts_its_own_row(tmp_path):
    # regression (0620 w18 MS 41): "any three from:" rows print "max 3" in the Marks
    # column, not a bare digit - the label must still be recognised and the row not merged
    # into the one above.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    _write_ms_page(
        page,
        [
            ("3(d)(i)", ["Cu(OH)2"], 1),
            ("3(d)(ii)", ["Any three from:", "zinc more reactive than copper"], "max 3"),
            ("3(d)(iii)", ["sodium hydroxide"], 2),
        ],
        rotation=0,
    )
    path = str(tmp_path / "ms_max.pdf")
    doc.save(path)
    doc.close()

    by_label = {f"{e.question_number}{e.part_path}": e for e in parse_mark_scheme(path)}

    assert by_label["3d.ii"].marks == 3
    assert "zinc" in by_label["3d.ii"].answer_text
    assert "zinc" not in by_label["3d.i"].answer_text


def test_per_marking_point_values_are_summed(tmp_path):
    # regression (0620 w18 MS 41): each marking point M1..Mn prints its own "1" in the
    # Marks column; the row is worth their sum, not just the last one.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "4(a)")
    for i, y in enumerate((100, 120, 140), start=1):
        page.insert_text((150, y), f"M{i} point {i}")
        page.insert_text((500, y), "1")
    page.insert_text((50, 180), "4(b)")
    page.insert_text((150, 180), "single answer")
    page.insert_text((500, 180), "2")
    path = str(tmp_path / "ms_points.pdf")
    doc.save(path)
    doc.close()

    by_label = {f"{e.question_number}{e.part_path}": e for e in parse_mark_scheme(path)}

    assert by_label["4a"].marks == 3
    assert by_label["4b"].marks == 2


def test_layout_locates_each_row_between_its_rules(tmp_path):
    from qbx.markscheme import parse_mark_scheme_layout
    from tests.ms_fixtures import write_ruled_ms

    path = tmp_path / "ms.pdf"
    write_ruled_ms(path, [("1(a)", "first answer", 2), ("1(b)", "second answer", 1)])
    layout = parse_mark_scheme_layout(str(path))

    assert set(layout.rows) == {"1.a", "1.b"}
    first, second = layout.rows["1.a"].segments[0], layout.rows["1.b"].segments[0]
    # a row runs from the rule above it to the rule below, which it shares with the next
    assert first.page == 2 and round(first.y0) == 85 and round(first.y1) == 111
    assert round(second.y0) == 110
    assert layout.rows["1.a"].label.x0 < 90  # the label cell, for relabelling
    assert layout.header is not None and round(layout.header.y0) == 60
    # the principles page, without its running header or footer
    assert [b.page for b in layout.preamble] == [1]
    assert layout.preamble[0].y0 > 30 and layout.preamble[0].y1 < 560


def test_marks_glued_to_the_first_partial_mark_code_are_split(tmp_path):
    # regression (0580 m25 MS 22): a 4-column maths scheme's mark and first partial-mark
    # code came out as one span, "2 B1" / "2 FT", which no marks pattern matched
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for y, label, cell in ((100, "5", "2 B1 for three in order"), (140, "14(b)(i)", "2 FT 180 - 45"), (180, "6", "1")):
        page.insert_text((50, y), label)
        page.insert_text((150, y), "answer")
        page.insert_text((360, y), cell)
    path = tmp_path / "ms.pdf"
    doc.save(str(path))
    doc.close()

    by_id = {e.id: e for e in parse_mark_scheme(str(path))}
    assert by_id["5.stem"].marks == 2
    assert by_id["14.b.i"].marks == 2
    assert by_id["6.stem"].marks == 1


def test_marks_column_holding_only_codes_counts_their_digits(tmp_path):
    # regression (0580 m25 MS 22, 18(a)): "M1" above "A1" in the Marks column, no number
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    number_right = 360 + pymupdf.get_text_length("2", fontsize=11)
    page.insert_text((50, 100), "17")
    page.insert_text((150, 100), "answer")
    page.insert_text((360, 100), "2")
    page.insert_text((50, 160), "18(a)")
    page.insert_text((150, 160), "answer")
    for y, code in ((160, "M1"), (190, "A1")):
        page.insert_text((number_right - pymupdf.get_text_length(code, fontsize=11), y), code)
    page.insert_text((420, 160), "B1 for something")  # a partial-marks code: not marks
    path = tmp_path / "ms.pdf"
    doc.save(str(path))
    doc.close()

    by_id = {e.id: e for e in parse_mark_scheme(str(path))}
    assert by_id["17.stem"].marks == 2
    assert by_id["18.a"].marks == 2


def test_rule_between_marking_points_does_not_end_the_row(tmp_path):
    # regression (0620 s25 MS 61): a rule between one row's marking points crosses Answer
    # and Marks but not the Question cell merged over them - it isn't a row break
    from qbx.markscheme import parse_mark_scheme_layout
    from tests.ms_fixtures import HAIRLINE, write_ruled_ms

    path = tmp_path / "ms.pdf"
    write_ruled_ms(path, [("1(d)", "M1 methyl orange", 1), ("1(e)", "swirl", 1)])
    doc = pymupdf.open(str(path))
    page = doc[2]
    # split 1(d)'s row with an inner rule that skips the Question column, then add M2
    page.draw_rect(pymupdf.Rect(122, 106, 780, 106 + HAIRLINE), color=None, fill=(0, 0, 0))
    doc.save(str(tmp_path / "ms2.pdf"))
    doc.close()

    layout = parse_mark_scheme_layout(str(tmp_path / "ms2.pdf"))
    first = layout.rows["1.d"].segments[0]
    assert round(first.y1) == 111  # still ends at the rule under the whole row, not at 106


def test_marks_glued_to_guidance_words_are_split(tmp_path):
    # regression (0620 MJ26 schemes): a fourth Guidance column, with the mark running into
    # it - "1 ALLOW ...", "1 Allow ...", and the one-letter "1 A 'sodium'"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    rows = ((100, "1(a)", "1 ALLOW calcium oxide"), (140, "1(b)", "1 Allow liquid"), (180, "1(c)", "1 A 'sodium'"), (220, "1(d)", "2"))
    for y, label, cell in rows:
        page.insert_text((50, y), label)
        page.insert_text((150, y), "answer")
        page.insert_text((360, y), cell)
    path = tmp_path / "ms.pdf"
    doc.save(str(path))
    doc.close()

    by_id = {e.id: e.marks for e in parse_mark_scheme(str(path))}
    assert by_id == {"1.a": 1, "1.b": 1, "1.c": 1, "1.d": 2}


def test_malformed_labels_in_the_question_column_still_count():
    # Cambridge's own typos, found calibrating ~200 papers: "1f" (0620/41 ON22) and
    # "3(b(i)" (0620/42 MJ23) each lost their row
    from qbx.markscheme import _entry_id, match_label

    assert _entry_id("1f")[0] == "1.f"
    assert _entry_id("3(b(i)")[0] == "3.b.i"
    assert _entry_id("2(a)(ii)")[0] == "2.a.ii"  # well-formed ones as before
    for not_a_label in ("B", "1 A", "Question", "3(ab)", "Answer"):
        assert match_label(not_a_label) is None


def test_a_label_repeated_on_the_next_row_is_the_next_in_sequence():
    # Cambridge typo (0620/32 ON24): "3(d)(i)" on two consecutive rows, the second meaning
    # 3(d)(ii) - it collided with the first and left 3(d)(ii) unjoined
    from qbx.markscheme import MSSpan, _entry_ids

    labels = [MSSpan(text=t, page=0, x=80, y=100 + 25 * i) for i, t in enumerate(["3(d)(i)", "3(d)(i)", "3(e)", "3(e)"])]
    assert [entry_id for entry_id, _, _ in _entry_ids(labels)] == ["3.d.i", "3.d.ii", "3.e", "3.f"]


def test_repeated_and_split_labels_are_read_by_their_sequence():
    from qbx.markscheme import MSSpan, _entry_ids

    def ids(*labels):
        spans = [MSSpan(text=text, page=page, x=80, y=100 + 25 * i) for i, (text, page) in enumerate(labels)]
        return [entry_id for entry_id, _, _ in _entry_ids(spans)]

    # the numerals vi-viii printed split in two after (a)(v) (0620/42 FM21)
    assert ids(("1(a)(v)", 0), ("1(v)(i)", 0), ("1(v)(ii)", 0), ("1(b)", 0)) == ["1.a.v", "1.a.vi", "1.a.vii", "1.b"]
    # ...but a genuine part (i)'s subparts are left alone (0620/52 MJ22)
    assert ids(("1(i)(i)", 0), ("1(i)(ii)", 0)) == ["1.i.i", "1.i.ii"]
    # (i), (iii), (iii) on one page: the first "(iii)" is the typo (0620/63 ON23)
    assert ids(("2(e)(i)", 0), ("2(e)(iii)", 0), ("2(e)(iii)", 0)) == ["2.e.i", "2.e.ii", "2.e.iii"]
    # a label printed again at the top of the next page is the same row carried over
    assert ids(("3(a)(ii)", 8), ("3(a)(ii)", 9), ("3(a)(iii)", 9)) == ["3.a.ii", "3.a.ii", "3.a.iii"]
    # an earlier row's label printed again further down: "2(b)" after 2(e) is 2(f) (0620/33 MJ24)
    assert ids(("2(b)", 0), ("2(c)", 0), ("2(e)", 0), ("2(b)", 0), ("3(a)", 0)) == ["2.b", "2.c", "2.e", "2.f", "3.a"]
    # the question number left off: "(a)(ii)" under "2(a)(i)" (0620/33 MJ25)
    assert ids(("2(a)(i)", 0), ("(a)(ii)", 0), ("(a)(iii)", 0), ("2(b)", 0)) == ["2.a.i", "2.a.ii", "2.a.iii", "2.b"]
    # a capital in the numeral, and a stray "i" after the bracket (0620/31 ON21, 32 ON19, 42 MJ23)
    assert ids(("7(c)(i)", 0), ("7(c)(Ii)", 0)) == ["7.c.i", "7.c.ii"]
    assert ids(("6(a)(i)i", 0), ("6(a)(ii)", 0)) == ["6.a.i", "6.a.ii"]
    assert ids(("4(f)(ii)", 0), ("4(f)(iii)i", 0), ("4(f)(iv)", 0)) == ["4.f.ii", "4.f.iii", "4.f.iv"]


def test_a_row_with_a_blank_marks_cell_still_counts(tmp_path):
    # regression (0620/32 FM21, 7(e)(ii)): Cambridge left the row's Marks cell empty
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for y, label, marks in ((100, "7(e)(i)", "1"), (140, "7(e)(ii)", None), (180, "7(e)(iii)", "1")):
        page.insert_text((50, y), label)
        page.insert_text((150, y), "an answer")
        if marks:
            page.insert_text((500, y), marks)
    page.insert_text((50, 220), "3")  # a bare number, no mark: still not a row
    path = tmp_path / "ms.pdf"
    doc.save(str(path))
    doc.close()

    assert [e.id for e in parse_mark_scheme(str(path))] == ["7.e.i", "7.e.ii", "7.e.iii"]


def test_two_up_answer_key_scheme_reads_each_item_as_one_mark(tmp_path):
    # regression (0620/11 MJ16): older multiple-choice schemes are an answer key printed
    # two-up - "Question Number | Key | Question Number | Key", rows "1 D 21 A" - with no
    # Marks column. The table parser read 21 as question 1's marks and never found 21-40.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for x, text in ((150, "Key"), (400, "Key")):
        page.insert_text((x, 100), text)
    answers = "DCCBCBACBCADDCADDCBDACCCCDCDDABBDCDBDCBA"
    for row in range(20):
        y = 130 + row * 18
        page.insert_text((100, y), str(row + 1))
        page.insert_text((155, y), answers[row])
        page.insert_text((350, y), str(row + 21))
        page.insert_text((405, y), answers[row + 20])
    path = str(tmp_path / "answer_key.pdf")
    doc.save(path)
    doc.close()

    entries = parse_mark_scheme(path)

    assert [e.question_number for e in entries] == list(range(1, 41))
    assert all(e.marks == 1 and e.part_path == "" for e in entries)
    assert entries[0].answer_text == "D"
    assert entries[20].answer_text == "A"
    # no table rows to copy - a composed scheme falls back to the extracted letters
    assert parse_mark_scheme_layout(path).rows == {}


def test_a_numeric_answer_beside_its_label_is_not_a_row_of_its_own(tmp_path):
    # regression (0620/33 ON19): an answer that is just a number - "5" beside 2(d)(ii) -
    # sat within chaining distance of the Question column's labels (spread 86-105pt, the
    # answer at 117) and became row "5" of its own
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    rows = [("1(a)", 86, 180, "sodium chloride"), ("2(d)", 92, 117, "5"), ("3(a)", 98, 180, "neutral"), ("3(b)", 104, 180, "acidic")]
    for i, (label, x, answer_x, answer) in enumerate(rows):
        y = 100 + i * 40
        page.insert_text((x, y), label)
        page.insert_text((answer_x, y), answer)
        page.insert_text((795, y), "1")
    path = str(tmp_path / "numeric_answer.pdf")
    doc.save(path)
    doc.close()

    entries = parse_mark_scheme(path)

    assert [e.id for e in entries] == ["1.a", "2.d", "3.a", "3.b"]
    assert entries[1].answer_text == "5"


def _codes_only_ms(tmp_path, rows) -> str:
    """A 0625-style scheme: Question | Answer | Mark, the Mark column holding codes only.
    `rows` is (y, label, answer, [(x, code), ...])."""
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    for x, text in ((50, "Question"), (200, "Answer"), (700, "Mark")):
        page.insert_text((x, 60), text)
    for y, label, answer, codes in rows:
        if label:
            page.insert_text((50, y), label)
        page.insert_text((150, y), answer)
        for x, code in codes:
            page.insert_text((x, y), code)
    path = tmp_path / "ms.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


def test_a_mark_column_of_codes_alone_is_read_by_its_codes(tmp_path):
    # regression (0625 Physics papers 3/4): no number anywhere in the Mark column - a 70-row
    # scheme read as 3 rows. A bracketed "(C2)" is part credit, not a code; a stray number in
    # an answer ("cm 2") is never a mark.
    path = _codes_only_ms(tmp_path, [
        (100, "1(a)", "acceleration", [(704, "B1")]),
        (130, "1(b)", "60 (m)", [(704, "A3")]),
        (150, "", "1/2 x 6 x 20", [(690, "(C2)")]),
        (180, "1(c)", "any three from:", [(704, "B3")]),
        (200, "", "difference in volumes", [(704, "B1")]),
        (230, "1(d)", "6 g / cm 2", [(704, "B1")]),
    ])
    by_id = {e.id: e.marks for e in parse_mark_scheme(path)}
    assert by_id == {"1.a": 1, "1.b": 3, "1.c": 4, "1.d": 1}


def test_codes_glued_to_guidance_and_compensation_marks(tmp_path):
    # 0625 MJ26: "B1 accept ...", "B1 A ..."; 0625/41 MJ21: a bare "C1" under "A2" is part
    # credit for it, while "C1", "C1", "A1" (0625/31 MJ21) are three marks; 0625/41 ON22: a
    # "B2" total beside its "B1", "B1" breakdown is 2, but "M1" beside "A1" is 2 marks too
    path = _codes_only_ms(tmp_path, [
        (100, "1(a)", "4.5 m/s", [(704, "B1 accept 4.4 m/s")]),
        (130, "1(b)", "constant", [(704, "B1 A moving")]),
        (160, "1(c)", "3800 N", [(704, "A2")]),
        (180, "", "F = ma", [(704, "C1")]),
        (210, "1(d)", "d / t", [(704, "C1")]),
        (230, "", "710 / 87", [(704, "C1")]),
        (250, "", "8.2 m/s", [(704, "A1")]),
        (280, "1(e)", "magnitude", [(720, "B2"), (704, "B1")]),
        (300, "", "direction", [(704, "B1")]),
        (330, "1(f)", "ray", [(720, "M1")]),
        (350, "", "image", [(704, "A1")]),
    ])
    by_id = {e.id: e.marks for e in parse_mark_scheme(path)}
    assert by_id == {"1.a": 1, "1.b": 1, "1.c": 2, "1.d": 3, "1.e": 2, "1.f": 2}


def test_split_and_combined_labels(tmp_path):
    # 0625/61 MJ26: "3" then "(a)" as two spans; 0625/42 FM21: "5(c)(i),(ii)", one row for
    # both subparts, read as the part they share
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    for x, text in ((50, "Question Answer"), (700, "Mark")):
        page.insert_text((x, 60), text)
    page.insert_text((50, 100), "3")
    page.insert_text((57, 100), "(a)")
    page.insert_text((150, 100), "1.4 cm")
    page.insert_text((704, 100), "B1")
    page.insert_text((50, 130), "3(b)(i),(ii)")
    page.insert_text((150, 130), "wavelength labelled")
    page.insert_text((704, 130), "B1")
    page.insert_text((704, 150), "B1")
    path = tmp_path / "ms.pdf"
    doc.save(str(path))
    doc.close()

    by_id = {e.id: e.marks for e in parse_mark_scheme(str(path))}
    assert by_id == {"3.a": 1, "3.b": 2}


def test_marks_wandering_under_their_header_still_count(tmp_path):
    # regression (0610/32 FM22, MJ24): a row's "1" set at x 525, 536 or 544 under a "Marks"
    # header at 521 - the column's median took 525, and 544 fell outside it
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    for x, text in ((50, "Question"), (200, "Answer"), (521, "Marks")):
        page.insert_text((x, 60), text)
    for i, (label, x) in enumerate((("1(a)", 525), ("1(b)", 525), ("1(c)", 536), ("1(d)", 544), ("1(e)", 525))):
        page.insert_text((50, 100 + 30 * i), label)
        page.insert_text((150, 100 + 30 * i), "osmosis")
        page.insert_text((x, 100 + 30 * i), "1")
    path = tmp_path / "ms.pdf"
    doc.save(str(path))
    doc.close()
    by_id = {e.id: e.marks for e in parse_mark_scheme(str(path))}
    assert by_id == {"1.a": 1, "1.b": 1, "1.c": 1, "1.d": 1, "1.e": 1}


def test_a_mark_glued_to_a_notes_heading(tmp_path):
    # 0417/11 MJ26 Q1: "4 Remember:" - the mark run into the Notes column's heading
    from qbx.markscheme import _MARKS_THEN_CODE_RE

    match = _MARKS_THEN_CODE_RE.match("4 Remember:")
    assert match and match.group("marks") == "4"
    assert not _MARKS_THEN_CODE_RE.match("4 Remember the units")
