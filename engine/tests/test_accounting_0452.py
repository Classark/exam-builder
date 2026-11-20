"""0452 Accounting (user-reported in the Reviewer, 7 papers)."""

import pymupdf

from qbx import anchors, markscheme_paper
from qbx.anchors import AnchorSpan


def _span(text, x, y, page=0, tiers=None, follows=False, bold=True):
    tiers = tiers or (["question"] if text.isdigit() else ["part"])
    return AnchorSpan(
        text=text, x0=x, y0=y, page=page, pattern_tiers=tiers, bbox=(x, y, x + 10, y + 10),
        bold=bold, follows_marker=follows,
    )


def test_a_question_number_set_at_the_part_indent():
    # "3" at x 72 with its (a), (b) at 72 as usual (0452/22 ON18)
    spans = [
        _span("1", 50, 59, 1), _span("(a)", 72, 100, 1),
        _span("2", 50, 59, 4), _span("(a)", 72, 100, 4),
        _span("3", 72, 59, 9), _span("(a)", 72, 85, 10),
        _span("4", 50, 59, 13),
    ]
    anchors.place_moved_numbers(spans)
    assert spans[4].x0 == 50


def test_a_table_s_number_isn_t_moved_when_the_column_prints_it():
    # a bold "2" in Q1's table, the real "2" printed later (0610/41 ON25)
    spans = [_span("1", 50, 64, 1), _span("2", 75, 223, 2), _span("2", 50, 64, 5), _span("3", 50, 64, 9)]
    anchors.place_moved_numbers(spans)
    assert spans[1].x0 == 75


def test_markers_on_a_turned_page_go_to_their_columns():
    # 0452/22 FM16: the petty cash book's page is turned, "1 (a)" 12pt right of the other pages'
    spans = [
        _span("1", 62, 76, 1), _span("(a)", 84, 76, 1), _span("4", 186, 196, 1), _span("7", 186, 214, 1),
        _span("(b)", 71, 76, 2), _span("(i)", 93, 76, 2, ["part", "subpart"], True),
        _span("(ii)", 90, 101, 2, ["subpart"]), _span("(c)", 71, 201, 2),
        _span("2", 50, 454, 2), _span("(a)", 71, 454, 2), _span("3", 50, 100, 3),
    ]
    out = markscheme_paper._placed_markers(spans, {1})
    placed = {(s.page, s.text): s.x0 for s in out}
    assert placed[(1, "1")] == 50 and placed[(1, "(a)")] == 71
    assert (1, "4") not in placed and (1, "7") not in placed  # the table's numbers


def test_a_scheme_s_marker_off_its_indent():
    # "(e)" at the number's indent, "(ii)" 9pt in from the other (ii) (0452/22 MJ16, 0452/23 ON16)
    spans = [
        _span("3", 50, 76, 1), _span("(a)", 71, 76, 1), _span("(b)", 71, 200, 1),
        _span("(d)", 71, 300, 1), _span("(i)", 93, 300, 1, ["part", "subpart"], True),
        _span("(e)", 50, 76, 2), _span("(i)", 93, 76, 2, ["part", "subpart"], True),
        _span("(ii)", 99, 238, 2, ["subpart"]), _span("(ii)", 90, 400, 2, ["subpart"]),
        _span("(f)", 71, 567, 2),
    ]
    out = markscheme_paper._placed_markers(spans, set())
    assert out[5].x0 == 71
    assert out[7].x0 == 93


def test_a_turned_page_is_straightened(tmp_path):
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "1 (a) Petty cash book")
    page.set_rotation(90)
    upright = markscheme_paper._upright(doc)
    assert upright[1].rotation == 0
    assert "Petty cash book" in upright[1].get_text()


def test_white_padding_before_a_marker_is_where_its_line_starts():
    # "  " in white just before "(ii)", the line's other text further left (0452/12 ON18 2(f)(ii))
    white = {"text": "  ", "bbox": (88.0, 300, 92.6, 312), "color": 16777215}
    marker = {"text": "(ii)", "bbox": (93.5, 300, 110, 312), "color": 0}
    assert anchors._padded_start([white, marker], 1, 93.5) == 88.0
    printed = {"text": "x", "bbox": (88.0, 300, 92.6, 312), "color": 0}
    assert anchors._padded_start([printed, marker], 1, 93.5) == 93.5


def _sideways_table_page(doc):
    """A page whose table is printed sideways, as 0452/21 MJ26's petty cash book: its title
    near the left edge, its "[11]" at the right margin, and the margin strip."""
    page = doc.new_page(width=595, height=842)
    page.insert_text((10, 800), "DO NOT WRITE IN THIS MARGIN", rotate=90, fontsize=8)
    page.insert_text((58, 440), "Reggie", rotate=90, fontsize=11)
    page.insert_text((71, 460), "Petty cash book", rotate=90, fontsize=11)
    page.insert_text((300, 500), "details", rotate=90, fontsize=11)
    page.insert_text((545, 82), "[11]", rotate=90, fontsize=11)
    return page


def test_a_sideways_page_s_mark_is_read():
    from qbx import tree

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 410), "(a) Prepare the petty cash book.", fontsize=11)
    _sideways_table_page(doc)
    texts = [s.text for s in tree.collect_text_spans(doc)]
    assert "[11]" in texts
    assert not any("details" in t or "WRITE" in t for t in texts)


def test_sideways_content_near_the_edge_is_not_the_margin_strip():
    from qbx import crops

    doc = pymupdf.open()
    page = _sideways_table_page(doc)
    x0, x1 = crops._margin_strip_clip(page)
    assert x0 < 45  # clipped past the strip only, not at the table's title
    box = crops._content_bbox(page, pymupdf.Rect(x0, 50, x1, 780))
    assert box.x0 < 50 and box.x1 > 540  # the title and the "[11]" are ink


def test_a_sideways_mark_short_of_the_mark_column_is_still_a_mark():
    # 0452/23 MJ25: the sideways "[11]" ends at x 497, the page's marks at 546
    from qbx import tree

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 410), "(a) Prepare the journal entry.", fontsize=11)
    page.insert_text((533, 430), "[4]", fontsize=11)
    page.insert_text((72, 460), "(b) Prepare the petty cash book.", fontsize=11)
    page = doc.new_page(width=595, height=842)
    page.insert_text((496, 83), "[11]", rotate=90, fontsize=11)
    texts = [s.text for s in tree.collect_text_spans(doc)]
    assert "[11]" in texts
