from pathlib import Path

import pymupdf

from qbx.crops import _margin_strip_clip, crop_to_svg, is_noise_line, page_segments

# What a Cambridge per-page barcode extracts as: a run in a barcode font whose glyphs are
# control codes, so the line carries no letters or digits at all.
BARCODE = ",\x01\x01\x01\x01\t\x01\x01\x01\x01\x01\x01\x01\x02,"


def _two_page_doc():
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 400), "Some question content.")
    return doc


def test_single_page_region_writes_one_svg(tmp_path):
    doc = _two_page_doc()
    start = (0, 100.0, 72.0)
    end = (0, 300.0, 72.0)

    written = crop_to_svg(doc, start, end, tmp_path / "part.svg")

    assert written == [tmp_path / "part.svg"]
    assert written[0].read_text(encoding="utf-8").startswith("<svg")


def test_region_spanning_a_page_break_writes_one_svg_per_page(tmp_path):
    doc = _two_page_doc()
    start = (0, 700.0, 72.0)
    end = (1, 200.0, 72.0)

    written = crop_to_svg(doc, start, end, tmp_path / "part.svg")

    assert [p.name for p in written] == ["part_p1.svg", "part_p2.svg"]
    assert all(p.exists() for p in written)


def test_open_ended_region_crops_to_bottom_margin_of_its_own_page(tmp_path):
    doc = _two_page_doc()
    start = (1, 700.0, 72.0)

    written = crop_to_svg(doc, start, None, tmp_path / "last_part.svg")

    assert [p.name for p in written] == ["last_part.svg"]


def test_cropbox_is_restored_after_cropping(tmp_path):
    doc = _two_page_doc()
    original = doc[0].cropbox
    crop_to_svg(doc, (0, 100.0, 72.0), (0, 300.0, 72.0), tmp_path / "part.svg")

    assert doc[0].cropbox == original


def test_margin_strip_on_right_edge_is_excluded():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((580, 800), "DO NOT WRITE IN THIS MARGIN", rotate=90)
    page.insert_text((72, 400), "Some question content.")

    x0, x1 = _margin_strip_clip(page)

    assert x0 == 0.0
    assert x1 < 580  # stops before the sidebar, not at the raw page width


def test_margin_strip_on_left_edge_is_excluded():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((15, 800), "DO NOT WRITE IN THIS MARGIN", rotate=90)
    page.insert_text((200, 400), "Some question content.")

    x0, x1 = _margin_strip_clip(page)

    assert x0 > 15
    assert x1 == page.rect.width


def test_no_margin_strip_keeps_full_page_width():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 400), "Some question content.")

    assert _margin_strip_clip(page) == (0.0, page.rect.width)


def test_wide_diagram_near_edge_is_not_mistaken_for_a_margin_strip():
    # a real diagram/table can legitimately run close to the page edge - only a genuinely
    # *vertical* line should ever narrow the clip, never ordinary horizontal content.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((520, 400), "Wide table column right at the edge")

    assert _margin_strip_clip(page) == (0.0, page.rect.width)


def test_crop_rect_excludes_margin_strip():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((580, 800), "DO NOT WRITE IN THIS MARGIN", rotate=90)
    page.insert_text((72, 400), "Some question content. [2]")

    segments = page_segments(doc, (0, 300.0, 72.0), (0, 500.0, 72.0), margin=60.0)

    assert len(segments) == 1
    _, rect = segments[0]
    assert rect.x1 < 580


def test_decorative_fill_strip_with_no_text_label_is_excluded():
    # real-paper finding (0620 w25 qp 41): the sidebar is sometimes just a tinted fill
    # rectangle with no rotated "DO NOT WRITE" text at all.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(pymupdf.Rect(0, 0, 21, 842), fill=(0.93, 0.93, 0.93), color=None)
    page.insert_text((200, 400), "Some question content.")

    x0, x1 = _margin_strip_clip(page)

    assert x0 >= 21


def test_segment_is_tightly_cropped_to_real_content_not_full_column_width():
    # user-reported issue: a short part left a wide band of blank space on either side of
    # its own crop because the x-range came from the page column, not the content itself.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 400), "(a) Short answer. [1]")

    segments = page_segments(doc, (0, 380.0, 72.0), (0, 420.0, 72.0), margin=60.0)

    assert len(segments) == 1
    _, rect = segments[0]
    assert rect.x0 < 72  # padding still allowed
    assert rect.x1 < 300  # nowhere near the 595pt page width


def test_page_break_continuation_with_only_boilerplate_text_is_dropped():
    # real-paper finding (0620 w25 qp 41, Q4->Q5): a part's blank answer space running onto
    # the next page can contain nothing but Cambridge's own "Question N starts on the next
    # page." navigational line - not real content, and shouldn't produce a filler page.
    doc = pymupdf.open()
    page0 = doc.new_page(width=595, height=842)
    page0.insert_text((72, 100), "(a) Real content on page one. [1]")
    page1 = doc.new_page(width=595, height=842)
    page1.insert_text((72, 100), "Question 2 starts on the next page.")

    segments = page_segments(doc, (0, 80.0, 72.0), (1, 700.0, 72.0), margin=60.0)

    assert [page_index for page_index, _ in segments] == [0]


def test_page_break_continuation_with_tiny_leftover_sliver_is_dropped():
    # real-paper finding: the leftover gap after a page break can be just a few points tall
    # (an 8px-at-150dpi sliver on a real paper) - too short to hold meaningful content even
    # if something technically clips into it.
    doc = pymupdf.open()
    page0 = doc.new_page(width=595, height=842)
    page0.insert_text((72, 100), "(a) Real content on page one. [1]")
    doc.new_page(width=595, height=842)

    segments = page_segments(doc, (0, 80.0, 72.0), (1, 63.0, 72.0), margin=60.0)

    assert [page_index for page_index, _ in segments] == [0]


def test_page_break_continuation_with_real_content_is_kept():
    # a genuine multi-page part must still produce a segment per page - only content-free
    # continuations get dropped.
    doc = pymupdf.open()
    page0 = doc.new_page(width=595, height=842)
    page0.insert_text((72, 100), "(a) Content on page one, continues below.")
    page1 = doc.new_page(width=595, height=842)
    page1.insert_text((72, 100), "...and finishes here on page two. [3]")

    segments = page_segments(doc, (0, 80.0, 72.0), (1, 200.0, 72.0), margin=60.0)

    assert [page_index for page_index, _ in segments] == [0, 1]


def test_trailing_blank_space_is_trimmed_from_a_segment():
    # user-reported: a short part whose boundary ran to an anchor on the *next* page
    # inherited all the empty space down to its own page's bottom margin, so the composed
    # paper showed a big unexplained gap under it.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "(a) A short question. [1]")

    # boundary runs to the bottom margin, far below the last line of real content
    segments = page_segments(doc, (0, 88.0, 72.0), (0, 780.0, 72.0), margin=60.0)

    assert len(segments) == 1
    _, rect = segments[0]
    assert rect.y1 < 120  # hugs the content, not the 780pt boundary


def test_blank_space_between_content_is_kept():
    # the flip side: a part with working space between its prompt and its trailing mark
    # allocation must keep that room to write in - only the run *after* the last mark
    # gets trimmed.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "(a) Calculate the energy needed.")
    page.insert_text((72, 400), "............ kJ [4]")

    segments = page_segments(doc, (0, 88.0, 72.0), (0, 780.0, 72.0), margin=60.0)

    assert len(segments) == 1
    _, rect = segments[0]
    assert rect.y0 < 95
    assert rect.y1 > 395  # the interior working space survives
    assert rect.y1 < 450  # but the dead space below it does not


def test_closing_copyright_block_is_cut_out_of_the_last_question():
    # real-paper finding (0972/11): the acknowledgements don't always get a page to
    # themselves - that paper runs its last questions down the top of the final page and
    # tucks the copyright notice underneath. The last question's region runs to the bottom
    # margin, so it swallowed the whole notice.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "40 What is the approximate diameter of the Milky Way? [1]")
    page.draw_line((48, 698), (547, 698))
    page.insert_text(
        (48, 712),
        "Permission to reproduce items where third-party owned material is included",
        fontsize=8,
    )
    page.insert_text(
        (48, 740), "Cambridge Assessment International Education is part of Cambridge", fontsize=8
    )

    segments = page_segments(doc, (0, 88.0, 72.0), None, margin=60.0)

    assert len(segments) == 1
    _, rect = segments[0]
    assert rect.y1 < 690  # stops above the rule and the notice beneath it


def test_per_page_barcode_is_not_treated_as_content():
    # real-paper finding (0620 w25 qp 41): every page carries a barcode just under the
    # header, at y=54-59 - below HEADER_FOOTER_MARGIN and inside the band where body text
    # starts, so it can't be excluded by position. Left in, it appeared in the composed
    # paper as a stray barcode floating above a question.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((66, 58), BARCODE, fontsize=5)
    page.insert_text((72, 200), "(a) The real question. [2]")

    segments = page_segments(doc, (0, 50.0, 72.0), (0, 400.0, 72.0), margin=48.0)

    assert len(segments) == 1
    _, rect = segments[0]
    assert rect.y0 > 59  # starts below the barcode, at the real content


def test_segment_containing_only_a_barcode_is_dropped():
    # the same barcode on a page-break continuation that holds nothing else: the segment is
    # not content-free by the old test (there *is* ink), but a barcode alone is not a part.
    doc = pymupdf.open()
    page0 = doc.new_page(width=595, height=842)
    page0.insert_text((72, 100), "(a) Real content on page one. [1]")
    page1 = doc.new_page(width=595, height=842)
    page1.insert_text((66, 58), BARCODE, fontsize=5)

    segments = page_segments(doc, (0, 80.0, 72.0), (1, 700.0, 72.0), margin=48.0)

    assert [page_index for page_index, _ in segments] == [0]


def test_prose_containing_control_characters_is_not_mistaken_for_a_barcode():
    # the flip side: these papers use control characters inside ordinary text - BEL as the
    # gap after a marker, backspace before a mark allocation - so "has control characters"
    # alone can't be the test. Real prose always carries letters and digits too.
    assert is_noise_line(BARCODE)
    assert not is_noise_line("\x07Give the symbol of the element that: \x08 [1]")
    assert not is_noise_line("." * 40)  # a dotted answer line has no control characters


def test_pages_without_the_copyright_block_are_unaffected():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "(a) An ordinary question. [1]")
    page.insert_text((72, 600), "...with content lower down the page. [2]")

    segments = page_segments(doc, (0, 88.0, 72.0), None, margin=60.0)

    assert len(segments) == 1
    _, rect = segments[0]
    assert rect.y1 > 595  # the lower content is still included


def test_thin_stroked_rules_count_but_thick_corner_marks_do_not():
    # regression (0620 s25 qp 61): a table drawn in stroked lines lost its bottom border -
    # a line's bbox has no area - but counting every stroke pulled in the 2.8pt corner
    # registration marks and stretched crops out to the page corners
    from qbx.crops import _content_bbox

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((150, 100), "final burette reading")
    page.draw_line((140, 90), (450, 90), width=0.5)
    page.draw_line((140, 110), (450, 110), width=0.5)  # the table's bottom rule
    page.draw_line((37, 791), (37, 811), width=2.8)  # a corner registration mark
    page.draw_line((38, 811), (58, 811), width=2.8)

    table = _content_bbox(page, pymupdf.Rect(0, 60, 575, 130))
    assert table.y1 >= 110  # the bottom border is inside the crop

    near_corner = _content_bbox(page, pymupdf.Rect(0, 60, 575, 794))
    assert near_corner.x0 > 100 and near_corner.y1 < 130  # the marks are not
    doc.close()


def test_a_region_never_runs_onto_a_blank_page():
    # regression (0620 s25 qp 61): Q1(f) ran up to Q2's number over a BLANK PAGE in
    # between, and took that page as its continuation - "BLANK PAGE" appeared in its crop
    from qbx.crops import page_segments

    doc = pymupdf.open()
    for text in ("1 (f) Describe what the student should do. [3]", "BLANK PAGE", "2 A student investigates..."):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), text)

    segments = page_segments(doc, (0, 90, 72), (2, 90, 72), margin=48)
    assert [page for page, _ in segments] == [0]
    doc.close()


def test_a_region_starts_at_the_top_of_what_stands_on_its_first_line():
    """A matrix beside "7. (i)" is centred on that line and stands above it - cut at the
    marker, the region lost its top rows (WFM01 January 2025, user-reported). Its bracket's
    stacked pieces touch; the previous question's last line, a gap above, stays out."""
    import pymupdf

    from qbx.crops import _overhang_top

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 40), "the end of the question before.", fontsize=11)
    for y in (72, 83, 94, 105, 116):  # a tall bracket, its pieces touching
        page.insert_text((274, y), "|", fontsize=14)
    page.insert_text((43, 98), "7. (i) A =", fontsize=11)
    top = page.search_for("7. (i)")[0].y0
    assert _overhang_top(page, top, 0, 595) < 70
    assert _overhang_top(page, top, 0, 595) > 45
    doc.close()


def test_cambridge_2026_footer_is_not_content():
    """The June 2026 template moved the footer up to y 782-793, inside the bottom margin, and
    a region running to the page's foot cropped the whole page (0625/32 June 2026 Q1(b))."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 300), "(b) In a different race, the student runs 400 m.", fontsize=11)
    page.insert_text((50, 790), "© Cambridge University Press & Assessment 2026", fontsize=8)
    page.insert_text((270, 790), "0625/32/M/J/26", fontsize=8)
    page.insert_text((492, 790), "[Turn over", fontsize=11)
    [(_, rect)] = page_segments(doc, (0, 290, 72), None, margin=48)
    assert rect.y1 < 320
    doc.close()


def test_the_footers_print_job_code_is_not_content():
    """ "03_0452_12_2026_1.21" beside the copyright line: 0452/12 FM26 Q4, last on its page,
    cropped down to it (user-reported)."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 300), "4 A cheque received by Tiberi was dishonoured.", fontsize=11)
    page.insert_text((50, 795), "© Cambridge University Press & Assessment 2026", fontsize=8)
    page.insert_text((258, 795), "03_0452_12_2026_1.21", fontsize=8)
    [(_, rect)] = page_segments(doc, (0, 290, 72), None, margin=40)
    assert rect.y1 < 320
    assert is_noise_line("06_0417_11_2021_1.8")
    doc.close()


def test_a_continuation_page_ignores_ink_reaching_down_from_the_header():
    """Cambridge's page number (y 38-49) and a filled corner mark (y 30-45) reach below the
    42pt top edge; every page a region carried onto came out with a sliver of them."""
    doc = pymupdf.open()
    first = doc.new_page(width=595, height=842)
    first.insert_text((72, 100), "(c) Describe the motion.", fontsize=11)
    second = doc.new_page(width=595, height=842)
    second.insert_text((295, 47), "4", fontsize=11)
    second.draw_rect(pymupdf.Rect(544, 30, 560, 45), color=None, fill=(0, 0, 0))
    second.insert_text((72, 300), "(d) State the energy change.", fontsize=11)
    segments = page_segments(doc, (0, 90, 72), (1, 290, 72), margin=48)
    assert [page for page, _ in segments] == [0]
    doc.close()


def test_a_cambridge_question_total_is_left_out_of_its_last_part():
    """"[Total: 7]" under the last part's mark is the source question's total, wrong in a
    composed paper that sets only some of its parts (user-reported)."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((89, 300), "(ii) State how the resultant force changes.", fontsize=11)
    page.insert_text((89, 330), "....................................", fontsize=11)
    page.insert_text((530, 330), "[1]", fontsize=11)
    page.insert_text((504, 360), "[Total: 7]", fontsize=11)
    [(_, rect)] = page_segments(doc, (0, 290, 89), None, margin=48)
    assert rect.y1 < 345
    doc.close()


def test_slanted_figure_text_is_not_a_margin_strip():
    """Fig. 2.2's "CAR", set at an angle at x 352, clipped every region on its page there
    (0625/43 ON23, user-reported). The strip is vertical text at the page's edge."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((360, 560), "CAR", fontsize=9, morph=(pymupdf.Point(360, 560), pymupdf.Matrix(27)))
    assert _margin_strip_clip(page) == (0.0, 595.0)
    doc.close()


def test_the_papers_last_part_runs_on_to_its_mark():
    # user-reported (0478/21 MJ26 Q12): the last question's answer lines run over two more
    # pages to its "[15]", and its region stopped at its first page; a spare lined page after
    # the mark is not taken in
    doc = pymupdf.open()
    for n in range(4):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "12 Write a program." if n == 0 else "." * 120, fontsize=11)
        if n == 2:
            page.insert_text((520, 700), "[15]", fontsize=11)
    assert [page for page, _ in page_segments(doc, (0, 90, 72), None, margin=48)] == [0, 1, 2]
    doc.close()


def test_the_newer_copyright_notice_is_cut_from_the_last_crop():
    # user-reported: since 2021 the block opens "To avoid the issue of disclosure...", a
    # paragraph above "Permission to reproduce", and the last question's crop took it in
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "40 Which statement is correct?", fontsize=11)
    page.insert_text((72, 130), "A   B   C   D", fontsize=11)
    page.insert_text((50, 700), "To avoid the issue of disclosure of answer-related information to candidates, all", fontsize=8)
    page.insert_text((50, 710), "copyright acknowledgements are reproduced online in our Copyright", fontsize=8)
    page.insert_text((50, 740), "Permission to reproduce items where third-party owned material protected by copyright", fontsize=8)
    [(_, rect)] = page_segments(doc, (0, 90, 72), None, margin=48)
    assert rect.y1 < 690
    doc.close()
