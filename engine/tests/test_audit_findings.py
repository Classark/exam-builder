"""Parser bugs `qbx audit` found over the release library (synthetic PDFs only)."""

import pymupdf

from qbx import anchors, crops
from qbx.anchors import AnchorSpan


def _marker(text, x, y, page=0, tiers=None, follows=False):
    tiers = tiers or (["question"] if text.isdigit() else ["part"])
    return AnchorSpan(
        text=text, x0=x, y0=y, page=page, pattern_tiers=tiers, bbox=(x, y, x + 10, y + 10),
        bold=True, follows_marker=follows,
    )


def test_a_part_letter_opening_its_subpart_s_line_is_a_part():
    # 0625/32 FM16 Q11: "(b)(i)" with the (b) printed 10pt right of the part column
    spans = [_marker("11", 50, 65), _marker("(a)", 72, 65, follows=True)]
    spans += [_marker("(i)", 96, 292, tiers=["part", "subpart"]), _marker("(ii)", 93, 318, tiers=["subpart"])]
    spans += [_marker(str(n), 50, 65, page=n) for n in range(1, 6)]
    spans += [_marker(f"({c})", 72, 100, page=n) for n, c in enumerate("abcde", 1)]
    spans += [_marker("(b)", 82, 65, page=9), _marker("(i)", 96, 65, page=9, tiers=["part", "subpart"], follows=True)]
    results = {r.tier: r for r in anchors.resolve_tiers(spans)}
    assert any(s.page == 9 and s.text == "(b)" for s in results["part"].spans)


def _page():
    doc = pymupdf.open()
    return doc, doc.new_page(width=595, height=842)


def test_a_tall_bar_mid_page_is_not_the_margin_strip():
    # 0610/32 ON22 Fig. 3.1: a grey bar 17pt wide, 425pt tall, at x 222
    doc, page = _page()
    page.draw_rect(pymupdf.Rect(222, 164, 239, 589), fill=(0.8, 0.8, 0.8), color=None)
    assert crops._margin_strip_clip(page)[0] < 100


def test_a_grid_above_the_copyright_block_is_not_its_rule():
    # 0478/23 MJ20 7(b): a grid of 21pt rows ending just above the block
    doc, page = _page()
    for y in range(588, 680, 21):
        page.draw_line((95, y), (543, y), width=0.3)
    page.draw_line((50, 696), (546, 696), width=1)
    page.insert_text((50, 715), "Permission to reproduce items where third-party owned material protected by copyright", fontsize=7)
    assert crops._content_limit(page) > 680


def test_a_line_above_a_marker_isn_t_taken_into_its_region():
    doc, page = _page()
    # (b)(ii)'s sentence, and below it (c) whose fraction's numerator rises into it (0625/52 FM20)
    page.insert_text((93, 355), "(ii) Calculate, and record in Table 3.1, the value", fontsize=11)
    page.insert_text((280, 362), "1", fontsize=11)
    page.insert_text((72, 375), "(c) Plot a graph of l against", fontsize=11)
    top = 364.0
    assert crops._overhang_top(page, top, 0, 595) > 350


def test_a_mark_level_with_the_footer_is_kept():
    # 0610/52 ON16 2(a)(iv): "[2]" at y 788-806, the footer at 796
    doc, page = _page()
    page.insert_text((50, 806), "© UCLES 2016", fontsize=9)
    page.insert_text((270, 806), "0610/52/O/N/16", fontsize=9)
    page.insert_text((533, 801), "[2]", fontsize=11)
    page.insert_text((72, 760), "(iv) State one variable that cannot be controlled.", fontsize=11)
    assert crops._page_bottom(page, anchors.HEADER_FOOTER_MARGIN) > 801
