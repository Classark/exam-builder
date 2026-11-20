"""0580 Mathematics (user-reported, 65 papers to review)."""

import pymupdf

from qbx import anchors, join
from qbx.markscheme import MARKS_VALUE_RE, MSSpan, _entry_ids, _is_column_code, match_label, parse_mark_scheme
from qbx.schema import MarkSchemeEntry, Question, QuestionPart


class _Label:
    def __init__(self, text):
        self.text, self.page = text, 0


def test_a_subpart_s_lettered_items_are_one_row():
    # "6(a)(i)(a)", "6(a)(i)(b)": the paper marks 6(a)(i) once (0580/33 MJ19)
    assert match_label("6(a)(i)(b)")["item"] == "b"
    assert match_label("6(a)(b)") is None
    ids = _entry_ids([_Label("6(a)(i)(a)"), _Label("6(a)(i)(b)"), _Label("6(a)(ii)")])
    assert [entry_id for entry_id, _, _ in ids] == ["6.a.i", "6.a.i", "6.a.ii"]


def test_a_table_s_rows_are_items_of_the_question_s_row():
    # "11A", "11B", "11C" for sequences A, B, C (0580/43 ON20)
    ids = _entry_ids([_Label("10(d)"), _Label("11A"), _Label("11B"), _Label("11C")])
    assert [entry_id for entry_id, _, _ in ids] == ["10.d", "11.stem", "11.stem", "11.stem"]


def test_a_mark_with_follow_through_glued_on():
    assert MARKS_VALUE_RE.match("1FT")["marks"] == "1"
    assert MARKS_VALUE_RE.match("max 3")


def test_a_code_s_edge_is_its_text_s_not_its_box_s():
    # "M1" with a box 29pt wide, its ink ending at 345 under numbers ending at 345.5
    # (0580/11 MJ24 Q22)
    code = MSSpan(text="M1", page=0, x=329.0, y=370, bbox=(329.0, 370.0, 358.5, 382.0), size=11.0)
    assert _is_column_code(code, 345.5)


def test_the_next_question_number_glued_to_any_words():
    pattern = anchors._GLUED_NEXT_NUMBER_RE
    for text in ("12 300 students choose", "13 y is inversely", "19 % = {1, 2, 3}", "15 m is inversely"):
        assert pattern.match(text), text
    for text in ("15 cm", "24 dm3 of", "3 m", "3 m.", "7 s", "6 m2", "9 m 12 m"):
        assert not pattern.match(text), text


def test_a_glued_number_counts_only_after_the_one_before(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "11 A company sells boxes.")
    page.insert_text((50, 200), "12 300 students choose a sport.")
    page.insert_text((50, 300), "40 y is not a question number.")
    numbers = [a.text for a in anchors.find_anchor_spans(doc) if a.pattern_tiers == ["question"]]
    doc.close()
    assert numbers == ["11", "12"]


def test_a_taller_line_on_the_same_baseline_is_the_same_row():
    # "19" and "(a) Find the inverse of" a matrix, the (a) line 7pt taller (0580/22 ON16)
    # MuPDF merges text on one baseline written to one file, so a fake page gives the lines
    # as that paper's text layer does
    def line(text, x, top, base):
        return {"dir": (1.0, 0.0), "bbox": (x, top, x + 100, base + 3), "spans": [{"text": text, "origin": (x, base)}]}

    class Page:
        @staticmethod
        def get_text(kind):
            return {"blocks": [
                {"lines": [line("19 ", 49.6, 70.0, 79.0)]},
                {"lines": [line("(a) Find the inverse of", 72.3, 63.0, 79.0)]},
                {"lines": [line("2 3", 180.0, 63.0, 72.0)]},  # the matrix's top row, its own baseline
                {"lines": [line("(b) The matrix", 72.3, 231.0, 240.0)]},
            ]}

    rows = anchors.line_top_map(Page())
    assert rows[70.0] == rows[63.0] == 63.0
    assert rows[231.0] == 231.0


def _scheme(tmp_path, rows: list[tuple[float, float, str]]) -> str:
    """A portrait scheme page under a Question | Answer | Mark header; rows are (x, y, text)."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((48, 83), "Question")
    page.insert_text((166, 83), "Answer")
    page.insert_text((272, 83), "Mark")
    for x, y, text in rows:
        page.insert_text((x, y), text)
    path = str(tmp_path / "ms.pdf")
    doc.save(path)
    doc.close()
    return path


def test_rows_printing_only_their_part_cell(tmp_path):
    # "14 (a)", then "(b)" set in under the number; "15 (a) (i)", then "(ii)" (0580/12 MJ16)
    path = _scheme(tmp_path, [
        (37, 107, "14 (a)"), (108, 107, "44"), (282, 107, "1"),
        (58, 131, "(b)"), (108, 131, "180 to 184"), (282, 131, "2"),
        (37, 165, "15 (a) (i)"), (108, 165, "12"), (282, 165, "1"),
        (77, 211, "(ii)"), (108, 211, "7"), (282, 211, "1"),
        (58, 247, "(b)"), (108, 247, "in correct position"), (282, 247, "1FT"),
    ])
    marks = {e.id: e.marks for e in parse_mark_scheme(path)}
    assert marks == {"14.a": 1, "14.b": 2, "15.a.i": 1, "15.a.ii": 1, "15.b": 1}


def test_a_label_centred_below_its_row_s_first_line(tmp_path):
    # "19" at y 441 in a two-line cell, its "2" on the first line at 435 (0580/11 MJ20)
    path = _scheme(tmp_path, [
        (37, 400, "18"), (108, 400, "66"), (282, 400, "1"),
        (282, 435, "2"), (108, 435, "29.65"),
        (37, 441, "19"),
        (108, 447, "29.75"),
        (37, 480, "20"), (108, 480, "7"), (282, 480, "1"),
    ])
    entries = {e.id: e for e in parse_mark_scheme(path)}
    assert entries["19.stem"].marks == 2
    assert "29.65" in entries["19.stem"].answer_text
    assert "29.65" not in entries["18.stem"].answer_text


def test_an_unsplit_part_s_own_row_keeps_it_from_later_subpart_rows():
    # "4(e)", then "4(e)(ii)" twice for 4(f)(i), 4(f)(ii) (0580/33 MJ17)
    parts = [
        QuestionPart(id=f"4.{p}", question_id="4", path=p, depth=p.count(".") + 1, text="", image_path="", marks=m)
        for p, m in (("d", 3), ("e", 2), ("f.i", 1), ("f.ii", 1))
    ]
    question = Question(
        id="4", paper_key="", board="", subject="", question_number=4, stem_text="", stem_image_path="",
        total_marks=7, parts=parts,
    )
    entries = [
        MarkSchemeEntry(id=f"4.{p}", paper_key="", question_number=4, part_path=p, answer_text="", image_path="", marks=m)
        for p, m in (("d", 3), ("e", 2), ("e.ii", 1), ("e.iii", 1))
    ]
    joined = {(j.part_id, j.mark_scheme_entry_id) for j in join.join_parts_to_mark_scheme([question], entries)}
    assert joined == {("4.d", "4.d"), ("4.e", "4.e"), ("4.f.i", "4.e.ii"), ("4.f.ii", "4.e.iii")}


def test_a_code_boxed_over_spaces_before_it():
    # "M1" boxed 316-351, its ink at the box's right (0580/11 MJ25 Q17)
    code = MSSpan(text="M1", page=0, x=316.0, y=430, bbox=(316.0, 430.0, 351.0, 443.0), size=11.0)
    assert _is_column_code(code, 345.5)


def test_rows_join_by_baseline_but_never_split():
    # "(iii) ..." at top 539.8 and its "[2]" at 540 are one row before baselines are looked
    # at; a bracket at 533.3 on (nearly) the same baseline joins the whole row, the "[2]"
    # never on its own (0580/41 MJ24)
    def line(text, x, top, base):
        return {"dir": (1.0, 0.0), "bbox": (x, top, x + 50, base + 3), "spans": [{"text": text, "origin": (x, base)}]}

    class Page:
        @staticmethod
        def get_text(kind):
            return {"blocks": [{"lines": [
                line("(iii) triangle T", 89.0, 539.8, 551.6),
                line("[2]", 532.0, 540.0, 551.6),
                line("`", 409.0, 533.3, 551.1),
            ]}]}

    rows = anchors.line_top_map(Page())
    assert rows[539.8] == rows[540.0] == rows[533.3]


def test_a_superscript_reads_in_its_place_in_the_row(tmp_path):
    # "K⁺" a point above "2(d)(ii)"'s line read "+ M2 potassium" (0620/52 MJ26)
    path = _scheme(tmp_path, [
        (37, 200, "2(d)(i)"), (108, 200, "M1 chromium"), (282, 200, "1"),
        (37, 240, "2(d)(ii)"), (108, 240, "M2 potassium / K"), (190, 239, "+"), (282, 240, "1"),
    ])
    entries = {e.id: e for e in parse_mark_scheme(path)}
    assert entries["2.d.ii"].answer_text.startswith("M2")


def test_a_cover_set_in_the_shifted_font_is_read():
    # 0620/23 June 2026: "&+(0,675<" is CHEMISTRY, "0$5.\x03 6&+(0(" MARK SCHEME
    class Page:
        @staticmethod
        def get_text(kind):
            return "&+(0,675<\x03\nMay/June 2026\n7KLV\x03GRFXPHQW\x03KDV\x03\x14\x19\x03SDJHV\x11\x03"

    text = anchors.page_text(Page())
    assert text.splitlines() == ["CHEMISTRY ", "May/June 2026", "This document has 16 pages. "]
