"""Mark schemes printed like a question paper (0478 2016) - see qbx/markscheme_paper.py."""

import pymupdf

from qbx.anchors import furniture_rows
from qbx.markscheme import parse_mark_scheme, parse_mark_scheme_layout


def _paper_layout_scheme(tmp_path, glued: bool = False) -> str:
    doc = pymupdf.open()
    cover = doc.new_page(width=595, height=842)
    cover.insert_text((50, 150), "COMPUTER SCIENCE 0478/11")
    cover.insert_text((50, 180), "MARK SCHEME")
    cover.insert_text((50, 200), "Maximum Mark: 7")
    page = doc.new_page(width=595, height=842)
    # the running header, set as three lines on one row
    page.insert_text((176, 60), "Cambridge IGCSE - May/June 2016")
    page.insert_text((453, 60), "0478")
    page.insert_text((513, 60), "11")
    y = 100
    if glued:
        page.insert_text((50, y), "1 1 mark for each correct answer")
    else:
        page.insert_text((50, y), "1")
        page.insert_text((71, y), "1 mark for each correct answer")
    page.insert_text((71, y + 20), "compiler, assembler")
    page.insert_text((533, y + 20), "[2]")
    y = 200
    page.insert_text((50, y), "2")
    page.insert_text((71, y), "(a) media access control")
    page.insert_text((533, y), "[1]")
    page.insert_text((71, y + 40), "(b) Any three from:")
    page.insert_text((92, y + 60), "hardware address")
    page.insert_text((92, y + 80), "unique to a device")
    page.insert_text((533, y + 80), "[3]")
    page.insert_text((71, y + 120), "(c) parity check")
    page.insert_text((533, y + 120), "[1]")
    path = str(tmp_path / ("glued.pdf" if glued else "paper_layout.pdf"))
    doc.save(path)
    doc.close()
    return path


def test_a_scheme_set_out_like_its_question_paper_reads_by_its_markers(tmp_path):
    # 0478 2016: no Question | Answer | Marks table - numbers and "(a)" at the margin, "[n]"
    # closing each answer. The table parser read two rows for the whole paper
    entries = {e.id: e for e in parse_mark_scheme(_paper_layout_scheme(tmp_path))}
    assert {k: e.marks for k, e in entries.items()} == {"1.stem": 2, "2.a": 1, "2.b": 3, "2.c": 1}
    assert "hardware address" in entries["2.b"].answer_text
    # the running header isn't an answer
    assert not any("0478" in e.answer_text or "May/June" in e.answer_text for e in entries.values())


def test_its_rows_have_regions_to_copy(tmp_path):
    layout = parse_mark_scheme_layout(_paper_layout_scheme(tmp_path))
    assert set(layout.rows) == {"1.stem", "2.a", "2.b", "2.c"}
    assert layout.header is None and not layout.landscape
    two_b = layout.rows["2.b"].segments[0]
    assert two_b.y0 < 245 and two_b.y1 > 275  # "(b)" down to its "[3]"


def test_a_question_number_run_into_how_it_is_marked(tmp_path):
    # "10 1 mark for each correct storage device" (0478/12 MJ16): "10 1" is one span
    entries = {e.id: e.marks for e in parse_mark_scheme(_paper_layout_scheme(tmp_path, glued=True))}
    assert entries["1.stem"] == 2


def test_a_running_header_in_pieces_is_furniture():
    lines = [
        ("Cambridge IGCSE � May/June 2016 ", (176, 51, 380, 62)),
        ("0478 ", (453, 51, 480, 62)),
        ("11 ", (513, 51.5, 525, 62)),
        ("1 ", (50, 76, 56, 87)),
        ("11 ", (513, 76, 525, 87)),  # an answer's "11" lower down is not
    ]
    assert furniture_rows(lines) == {(176, 51, 380, 62), (453, 51, 480, 62), (513, 51.5, 525, 62)}


def test_a_table_scheme_with_a_header_is_still_read_as_a_table(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((50, 60), "Question")
    page.insert_text((150, 60), "Answer")
    page.insert_text((700, 60), "Marks")
    page.insert_text((50, 100), "1(a)")
    page.insert_text((150, 100), "an answer closed [2]")
    page.insert_text((700, 100), "2")
    page.insert_text((50, 140), "1(b)")
    page.insert_text((150, 140), "another [1] and [3]")
    page.insert_text((700, 140), "1")
    path = str(tmp_path / "table.pdf")
    doc.save(path)
    doc.close()
    assert {e.id: e.marks for e in parse_mark_scheme(path)} == {"1.a": 2, "1.b": 1}
