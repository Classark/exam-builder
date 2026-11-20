"""0625 Physics 2016-2018 schemes (user-reported, 22 papers to review)."""

import pymupdf

from qbx import anchors, join
from qbx.markscheme import _entry_ids, _rotated_spans, _tiled_blocks, match_label, parse_mark_scheme
from qbx.pipeline import _unmarked_runs
from qbx.schema import MarkSchemeEntry, Question, QuestionPart


def _scheme(tmp_path, rows: list[tuple[float, float, str]], name: str = "ms.pdf") -> str:
    """A landscape scheme page under a Question | Answer | Marks header; rows are (x, y, text)."""
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((115, 60), "Question")
    page.insert_text((400, 60), "Answer")
    page.insert_text((779, 60), "Marks")
    for x, y, text in rows:
        page.insert_text((x, y), text)
    path = str(tmp_path / name)
    doc.save(path)
    doc.close()
    return path


def test_a_question_number_whose_marks_start_a_line_down(tmp_path):
    # the planning question: "4" alone, then "MP1 ... | 1" 25pt lower (0625/52 MJ17)
    path = _scheme(tmp_path, [
        (101, 90, "3(a)"), (200, 90, "an answer"), (783, 90, "2"),
        (92, 130, "4"),
        (102, 155, "MP1"), (200, 155, "measure the temperature"), (782, 155, "1"),
        (102, 180, "MP2"), (200, 180, "repeat"), (782, 180, "1"),
        (730, 210, "Total:"), (783, 210, "7"),
    ])
    marks = {e.id: e.marks for e in parse_mark_scheme(path)}
    assert marks == {"3.a": 2, "4.stem": 2}


def test_a_question_number_run_into_its_first_marking_point(tmp_path):
    # "4 MP1" as one span (0625/53 MJ17)
    path = _scheme(tmp_path, [
        (101, 90, "3(a)"), (200, 90, "an answer"), (783, 90, "2"),
        (101, 130, "4 MP1"), (200, 130, "measure"), (782, 130, "1"),
        (110, 155, "MP2"), (200, 155, "repeat"), (782, 155, "1"),
    ])
    assert {e.id: e.marks for e in parse_mark_scheme(path)} == {"3.a": 2, "4.stem": 2}


def test_one_row_for_two_parts_answers_both(tmp_path):
    # "2(a),(b)" over a table, its first mark a line down (0625/52 MJ16)
    path = _scheme(tmp_path, [
        (101, 90, "2(a),(b)"), (200, 90, "table:"), (783, 105, "1"), (783, 120, "1"),
        (101, 150, "2(c)"), (200, 150, "an answer"), (783, 150, "1"),
    ])
    entries = parse_mark_scheme(path)
    row = next(e for e in entries if e.id == "2.a")
    assert row.marks == 2 and row.also_paths == ["b"]
    parts = [QuestionPart(id=f"2.{p}", question_id="2", path=p, depth=1, text="", image_path="", marks=1) for p in "abc"]
    question = Question(id="2", paper_key="", board="", subject="", question_number=2, stem_text="", stem_image_path="", total_marks=3, parts=parts)
    joined = {(j.part_id, j.mark_scheme_entry_id) for j in join.join_parts_to_mark_scheme([question], entries)}
    assert {("2.a", "2.a"), ("2.b", "2.a"), ("2.c", "2.c")} <= joined


def test_three_subparts_on_one_row():
    match = match_label("2(b)(i),(ii),(iii)")
    assert match and match["question"] == "2" and match["part"] == "b" and not match["subpart"]


def test_cambridge_label_typos():
    # a doubled bracket (0625/41 MJ16)
    assert match_label("7(a)((i)")["subpart"] == "i"

    # one row in numbered pieces (0625/41 ON18): the same row, never bumped to (ii)
    class Label:
        def __init__(self, text):
            self.text, self.page = text, 0

    ids = _entry_ids([Label("1(a)(i)1"), Label("1(a)(i)2"), Label("1(a)(ii)")])
    assert [entry_id for entry_id, _, _ in ids] == ["1.a.i", "1.a.i", "1.a.ii"]


class _TiledPage:
    """The text layer of 0625/42 MJ17's page 9 as PyMuPDF gives it: each tile its own block
    ("10" then "0(a)", the seam's "0" in both; "Que" then "estion" touching). MuPDF merges
    overlapping text drawn in one file, so a written PDF can't reproduce it."""

    rotation_matrix = pymupdf.Matrix(1, 1)

    @staticmethod
    def _span(text, x0, widths, size=11.0):
        chars, x = [], x0
        for c, w in zip(text, widths):
            chars.append({"c": c, "bbox": (x, 90, x + w, 102), "origin": (x, 100)})
            x += w
        return {"chars": chars, "bbox": (x0, 90, x, 102), "size": size, "flags": 0, "font": "Arial", "origin": (x0, 100)}

    def get_text(self, kind):
        assert kind == "rawdict"
        return {"blocks": [
            {"lines": [{"spans": [self._span("10", 67.0, [6.1, 6.1])]}]},
            # two tiles' copies of a font a rounding error apart in size (0680/11 MJ16)
            {"lines": [{"spans": [self._span("0(a)", 73.1, [6.1, 3.7, 6.1, 3.6], size=10.995)]}]},
            {"lines": [{"spans": [self._span("Que", 300.0, [8.5, 6.1, 6.1])]}]},
            {"lines": [{"spans": [self._span("estion", 314.6, [6.1, 5.5, 3.0, 2.4, 6.1, 6.1])]}]},
        ]}


def test_a_page_drawn_in_overlapping_tiles_reads_whole_words():
    blocks = _tiled_blocks(_TiledPage())
    assert [span["text"] for block in blocks for line in block["lines"] for span in line["spans"]] == [
        "10(a)", "Question",
    ]


def test_an_ordinary_page_is_not_tiled(tmp_path):
    path = _scheme(tmp_path, [(101, 90, "1(a)"), (783, 90, "2")])
    with pymupdf.open(path) as doc:
        assert _tiled_blocks(doc[0]) is None


def test_a_number_is_not_a_run_of_markers():
    # "(b) 176.0 (g)": "176.0" read as markers "17", "6.", "0" made "(g)" part (g)
    # (0625/32 FM16's scheme, set out like its question paper)
    assert not anchors._only_markers("(b) 176.0 ")
    assert anchors._only_markers("12 (a) ")
    assert anchors._only_markers("(a)(i)")


def test_an_answer_in_brackets_is_not_a_cross_reference():
    # "(b) (i) (red), orange, yellow..." is a part and its answer (0625/32 FM16)
    assert not anchors._CROSS_REFERENCE_RE.match("(b) (i) (red), orange, yellow")
    assert anchors._CROSS_REFERENCE_RE.match("(a)(iii).")


def test_a_subpart_row_before_the_part_s_own_row_is_misprinted():
    # "3(b)(ii)" between 3(a)(i) and 3(b) is 3(a)(ii) (0625/53 ON17)
    parts = [
        QuestionPart(id=f"3.{p}", question_id="3", path=p, depth=p.count(".") + 1, text="", image_path="", marks=1)
        for p in ("a.i", "a.ii", "b")
    ]
    question = Question(id="3", paper_key="", board="", subject="", question_number=3, stem_text="", stem_image_path="", total_marks=3, parts=parts)
    entries = [
        MarkSchemeEntry(id=f"3.{p}", paper_key="", question_number=3, part_path=p, answer_text="", image_path="", marks=1)
        for p in ("a.i", "b.ii", "b")
    ]
    joined = {(j.part_id, j.mark_scheme_entry_id) for j in join.join_parts_to_mark_scheme([question], entries)}
    assert ("3.a.ii", "3.b.ii") in joined and ("3.b", "3.b.ii") not in joined


def test_subparts_sharing_one_printed_mark_inside_a_group():
    # (c)(i), (c)(ii) under one "[1]", (c)(iii) its own (0625/51 MJ18)
    parts = [
        QuestionPart(id=f"2.c.{n}", question_id="2", path=f"c.{n}", depth=2, text="", image_path="", marks=m)
        for n, m in (("i", 0), ("ii", 1), ("iii", 1))
    ]
    runs = _unmarked_runs(parts, {(2, "c"), (2, "c.iii")}, 2)
    assert [[p.path for p in run] for run in runs] == [["c.i", "c.ii"]]
    # a subpart with a row of its own isn't merged
    assert _unmarked_runs(parts, {(2, "c"), (2, "c.i"), (2, "c.iii")}, 2) == []
