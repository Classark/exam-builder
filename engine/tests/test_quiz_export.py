import pymupdf

from qbx.anchors import find_anchor_spans
from qbx.quiz_export import PAGE_WIDTH, QuizGroup, compose_ms_pdf, compose_qp_pdf
from qbx.schema import (
    BBox,
    ContainerHeading,
    LabelRef,
    MarkSchemeEntry,
    Paper,
    Question,
    QuestionPart,
    QuestionPartMarkScheme,
)


def _content_text(doc) -> str:
    """Everything after the cover page - what the questions themselves say."""
    return "".join(p.get_text("text") for p in list(doc)[1:])


def _make_source_pdf(path, n_pages=1):
    doc = pymupdf.open()
    for _ in range(n_pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "Question content goes here.")
    doc.save(str(path))
    doc.close()


def _label_bbox_from_source(path, marker_text, page_index=0) -> LabelRef:
    """Runs the real anchor detector against a source PDF to get an authentic label ref
    (same one pipeline.py/tree.py would produce), rather than hand-computing coordinates."""
    doc = pymupdf.open(str(path))
    spans = find_anchor_spans(doc, content_pages={page_index})
    doc.close()
    span = next(s for s in spans if s.text == marker_text and s.page == page_index)
    x0, y0, x1, y1 = span.bbox
    return LabelRef(
        page=page_index, x0=x0, y0=y0, x1=x1, y1=y1, size=span.size, bold=span.bold
    )


def _write_paper(
    out_dir, paper_key, source_pdf_path, question_number, parts, mark_scheme, joins,
    number_bbox=None, container_headings=None,
):
    question = Question(
        paper_key=paper_key, board="Cambridge", subject="0000", question_number=question_number,
        stem_text="", stem_image_path="", total_marks=sum(p.marks for p in parts),
        parts=parts, number_bbox=number_bbox, container_headings=container_headings or [],
    )
    paper = Paper(
        paper_key=paper_key, board="Cambridge", level="IGCSE", subject="0000", paper="1",
        variant="1", session="M/J", year=24, source_qp_path=str(source_pdf_path),
        questions=[question], mark_scheme=mark_scheme, joins=joins,
    )
    paper_dir = out_dir / paper_key
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "paper.json").write_text(paper.model_dump_json(indent=2), encoding="utf-8")


def test_short_questions_pack_onto_one_page(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)

    parts = [
        QuestionPart(
            id=f"1.{label}", question_id="1", path=label, depth=1, text="x", image_path="", marks=1,
            segments=[BBox(page=0, x0=0, y0=90 + i * 20, x1=595, y1=105 + i * 20)],
        )
        for i, label in enumerate(["a", "b", "c"])
    ]
    _write_paper(tmp_path, "P1", source, 1, parts, [], [])

    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=[p.path for p in parts])]
    pdf_bytes = compose_qp_pdf(groups, tmp_path)

    assert pdf_bytes.startswith(b"%PDF")
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    # 3 tiny parts must pack onto one content page, not one page each (plus the cover)
    assert doc.page_count == 2
    doc.close()


def test_parts_are_relettered_a_b_c_regardless_of_original_path(tmp_path):
    source = tmp_path / "source.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "(c) First selected part. [2]")
    page.insert_text((72, 130), "(d) Second selected part. [1]")
    doc.save(str(source))
    doc.close()

    label_c = _label_bbox_from_source(source, "(c)")
    label_d = _label_bbox_from_source(source, "(d)")
    parts = [
        QuestionPart(
            id="1.c", question_id="1", path="c", depth=1, text="x", image_path="", marks=2,
            segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=115)], label_bbox=label_c,
        ),
        QuestionPart(
            id="1.d", question_id="1", path="d", depth=1, text="x", image_path="", marks=1,
            segments=[BBox(page=0, x0=0, y0=120, x1=595, y1=145)], label_bbox=label_d,
        ),
    ]
    # selection order is [d, c] - reversed from source order - to prove relettering follows
    # the *selection*, not the original paper's own lettering.
    _write_paper(tmp_path, "P1", source, 1, parts, [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["d", "c"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = _content_text(doc)
    doc.close()

    assert "(a)" in text and "(b)" in text


def test_original_label_glyph_is_removed_not_just_painted_over(tmp_path):
    # real-world finding: a downloaded composed PDF's get_text() (what Ctrl+A / copy-paste
    # sees) showed *both* the new label and the original one stacked right after it - a
    # white rect drawn on top of the copied content hides it visually but leaves the
    # original glyphs sitting in the text layer, fully selectable/extractable. The fix must
    # remove the original glyphs from the source before the region is copied, not just draw
    # over them afterward.
    source = tmp_path / "source.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "(c) Explain the reaction. [2]")
    doc.save(str(source))
    doc.close()

    label_bbox = _label_bbox_from_source(source, "(c)")
    part = QuestionPart(
        id="1.c", question_id="1", path="c", depth=1, text="x", image_path="", marks=2,
        segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=115)], label_bbox=label_bbox,
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["c"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = _content_text(doc)
    doc.close()

    # the new label was really drawn (not just a separate floating title)...
    assert "(a)" in text
    # ...and the original is gone from the text layer entirely, not just hidden visually.
    assert "(c)" not in text


def test_relabelling_a_source_page_does_not_mutate_the_file_on_disk(tmp_path):
    # the redaction that removes the original label must happen on the in-memory copy
    # opened for this export, never on the source PDF sitting in papers/ - that file is the
    # user's original upload and must stay untouched across repeated/future exports.
    source = tmp_path / "source.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "(c) Explain the reaction. [2]")
    doc.save(str(source))
    doc.close()
    original_bytes = source.read_bytes()

    label_bbox = _label_bbox_from_source(source, "(c)")
    part = QuestionPart(
        id="1.c", question_id="1", path="c", depth=1, text="x", image_path="", marks=2,
        segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=115)], label_bbox=label_bbox,
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["c"])]

    compose_qp_pdf(groups, tmp_path)

    assert source.read_bytes() == original_bytes


def test_unlettered_question_relabels_the_question_number(tmp_path):
    source = tmp_path / "source.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "4")
    page.insert_text((70, 100), "A single matching item worth its own marks. [2]")
    doc.save(str(source))
    doc.close()

    number_bbox = _label_bbox_from_source(source, "4")
    part = QuestionPart(
        id="4.", question_id="4", path="", depth=1, text="x", image_path="", marks=2,
        segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=115)],
    )
    _write_paper(tmp_path, "P1", source, 4, [part], [], [], number_bbox=number_bbox)
    groups = [QuizGroup(paper_key="P1", question_number=4, part_paths=[""])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = doc[0].get_text("text")
    doc.close()

    assert "1" in text  # renumbered to the quiz's sequential 1, not source question 4


def test_content_pages_carry_a_page_number_and_the_paper_name(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=110)],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path, title="My Practice Paper")
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    cover_text = doc[0].get_text("text")
    content_text = doc[1].get_text("text")
    n_pages = doc.page_count
    doc.close()

    # the page number sits at the top of a content page, the paper's name at the foot -
    # named with the document kind, since the QP and MS are handed out separately
    assert content_text.strip().startswith("2")
    assert "My Practice Paper - Question Paper" in content_text
    # the cover itself carries the plain name, without the suffix
    assert cover_text.strip().splitlines()[0] == "My Practice Paper"
    # the cover states the length, the way a real paper's front page does
    assert f"This document has {n_pages} pages, including this one." in cover_text


def test_oversized_segment_shrinks_to_fit_a_single_page(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    # a segment taller than a whole A4 page
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=0, y0=0, x1=595, y1=2000)],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    # must not raise, and must still produce a valid single-question page
    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    assert pdf_bytes.startswith(b"%PDF")


def test_mark_scheme_answer_included_and_relettered(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.c", question_id="1", path="c", depth=1, text="x", image_path="", marks=2,
        segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=110)],
    )
    entry = MarkSchemeEntry(id="ms1", paper_key="P1", question_number=1, part_path="c", answer_text="The correct answer is B", marks=2)
    join = QuestionPartMarkScheme(part_id="1.c", mark_scheme_entry_id="ms1")
    _write_paper(tmp_path, "P1", source, 1, [part], [entry], [join])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["c"])]

    pdf_bytes = compose_ms_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    full_text = "".join(page.get_text("text") for page in doc)
    doc.close()

    assert "The correct answer is B" in full_text
    assert "(a)" in full_text  # relettered, not the source's own "(c)"


def test_qp_and_ms_are_distinct_documents(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=2,
        segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=110)],
    )
    entry = MarkSchemeEntry(id="ms1", paper_key="P1", question_number=1, part_path="a", answer_text="Answer text", marks=2)
    join = QuestionPartMarkScheme(part_id="1.a", mark_scheme_entry_id="ms1")
    _write_paper(tmp_path, "P1", source, 1, [part], [entry], [join])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    qp_bytes = compose_qp_pdf(groups, tmp_path)
    ms_bytes = compose_ms_pdf(groups, tmp_path)

    assert qp_bytes.startswith(b"%PDF") and ms_bytes.startswith(b"%PDF")
    assert qp_bytes != ms_bytes

    ms_text = "".join(p.get_text("text") for p in pymupdf.open(stream=ms_bytes, filetype="pdf"))
    qp_text = "".join(p.get_text("text") for p in pymupdf.open(stream=qp_bytes, filetype="pdf"))
    assert "Answer text" in ms_text
    assert "Answer text" not in qp_text


def test_missing_part_does_not_crash(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1, segments=[])
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["nonexistent"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    assert pdf_bytes.startswith(b"%PDF")


def test_missing_paper_does_not_crash(tmp_path):
    groups = [QuizGroup(paper_key="NOPE", question_number=1, part_paths=["a"])]
    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    assert pdf_bytes.startswith(b"%PDF")


def _container_source(path):
    """A question laid out like a real one: a container part with two subparts beneath it."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "(c) All smartphones have a MAC address.")
    page.insert_text((92, 140), "(i) State what is meant by the term MAC address. [1]")
    page.insert_text((92, 180), "(ii) Describe the structure of a MAC address. [3]")
    doc.save(str(path))
    doc.close()


def _container_paper(tmp_path):
    source = tmp_path / "source.pdf"
    _container_source(source)
    heading = ContainerHeading(
        path="c", text="All smartphones have a MAC address.",
        segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=125)],
        label_bbox=_label_bbox_from_source(source, "(c)"),
    )
    parts = [
        QuestionPart(
            id="1.c.i", question_id="1", path="c.i", depth=2, text="x", image_path="", marks=1,
            # the first child absorbs the container intro (tree.py), so its own body region
            # is tracked separately - that's what a composed paper must use.
            segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=165)],
            label_bbox=_label_bbox_from_source(source, "(c)"),
            body_segments=[BBox(page=0, x0=80, y0=128, x1=560, y1=165)],
            body_label_bbox=_label_bbox_from_source(source, "(i)"),
        ),
        QuestionPart(
            id="1.c.ii", question_id="1", path="c.ii", depth=2, text="x", image_path="", marks=3,
            segments=[BBox(page=0, x0=80, y0=168, x1=560, y1=205)],
            label_bbox=_label_bbox_from_source(source, "(ii)"),
        ),
    ]
    _write_paper(tmp_path, "P1", source, 1, parts, [], [], container_headings=[heading])
    return source


def test_subparts_collapse_under_one_letter_with_roman_numerals(tmp_path):
    # user-reported: picking (c)(i) and (c)(ii) wrongly produced "(b)" then a new top-level
    # "(c)". Subparts of one container belong under a single new letter, as (i) and (ii).
    _container_paper(tmp_path)
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["c.i", "c.ii"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(p.get_text("text") for p in doc)
    doc.close()

    assert "(a)" in text  # the container, relettered
    assert "(i)" in text and "(ii)" in text  # its subparts, kept beneath it
    assert "(b)" not in text  # NOT promoted to a second top-level part
    assert "(c)" not in text  # and the original container letter is gone


def test_subparts_are_resequenced_in_selection_order(tmp_path):
    # picking (c)(ii) first means it becomes (a)(i) - output order, not source order.
    _container_paper(tmp_path)
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["c.ii", "c.i"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page_text = _content_text(doc)
    doc.close()

    assert page_text.index("Describe the structure") < page_text.index("State what is meant")


def test_lone_subpart_still_gets_its_container_heading(tmp_path):
    # user-reported: taking (c)(ii) on its own has to bring "(c) All smartphones have a MAC
    # address." with it, or the subpart has no context.
    _container_paper(tmp_path)
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["c.ii"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(p.get_text("text") for p in doc)
    doc.close()

    assert "All smartphones have a MAC address" in text
    assert "Describe the structure" in text


def test_container_intro_is_not_rendered_twice_when_first_child_is_selected(tmp_path):
    # the first child's own `segments` absorb the container intro; rendering the heading
    # *and* that absorbed copy would print the intro twice.
    _container_paper(tmp_path)
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["c.i"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(p.get_text("text") for p in doc)
    doc.close()

    assert text.count("All smartphones have a MAC address") == 1


def test_mark_scheme_labels_match_the_qp_hierarchy(tmp_path):
    _container_paper(tmp_path)
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["c.i", "c.ii"])]

    pdf_bytes = compose_ms_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(p.get_text("text") for p in doc)
    doc.close()

    assert "1(a)(i)" in text
    assert "1(a)(ii)" in text


def test_no_text_survives_from_outside_the_selected_region(tmp_path):
    # user-reported: Ctrl+A on a composed paper showed whole source pages - headers,
    # footers and unselected parts - hidden behind the visible content.
    source = tmp_path / "source.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 40), "PAGE HEADER MUST NOT LEAK")
    page.insert_text((72, 100), "(a) The selected part. [1]")
    page.insert_text((72, 300), "(b) An unselected part that must not leak. [2]")
    page.insert_text((72, 800), "0478/11/M/J/20 UCLES FOOTER MUST NOT LEAK")
    doc.save(str(source))
    doc.close()

    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=115)],
        label_bbox=_label_bbox_from_source(source, "(a)"),
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(p.get_text("text") for p in doc)
    doc.close()

    assert "The selected part" in text
    assert "MUST NOT LEAK" not in text
    assert "UCLES" not in text
    assert "unselected part" not in text


def test_barcode_inside_the_selected_region_is_removed(tmp_path):
    # user-reported: a barcode appeared in a composed paper. crops.py keeps it out of the
    # bounds it measures, but a barcode can overlap the band where content starts (real
    # paper: barcode at y=54-59, first body line at 58.3), so no trim can separate them -
    # inside the region, it has to be redacted away like any other page furniture.
    barcode = ",\x01\x01\x01\x01\t\x01\x01\x01\x01\x01\x01\x01\x02,"
    source = tmp_path / "source.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((66, 58), barcode, fontsize=5)
    page.insert_text((72, 100), "(a) The selected part. [1]")
    doc.save(str(source))
    doc.close()

    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=60, y0=50, x1=560, y1=115)],  # deliberately spans both
        label_bbox=_label_bbox_from_source(source, "(a)"),
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(p.get_text("text") for p in doc)
    doc.close()

    assert "The selected part" in text
    assert "\x01\x01\x01" not in text


def test_question_content_is_centred_not_flushed_left(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    # a narrow region - flush-left placement would put it at the page margin, centring
    # leaves roughly equal space either side, the way a real paper is laid out. The region
    # is taken from the text's real bbox: a segment that clipped into the glyphs would see
    # them redacted away as out-of-region content, which is correct but not what's tested
    # here.
    doc = pymupdf.open(str(source))
    text_bbox = doc[0].get_text("blocks")[0][:4]
    doc.close()
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[
            BBox(page=0, x0=text_bbox[0] - 4, y0=text_bbox[1] - 4,
                 x1=text_bbox[2] + 4, y1=text_bbox[3] + 4)
        ],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    blocks = [b for b in doc[1].get_text("blocks") if "content goes here" in b[4]]
    doc.close()

    assert blocks, "expected the copied content to be found on the page"
    left_gap = blocks[0][0]
    right_gap = PAGE_WIDTH - blocks[0][2]
    assert abs(left_gap - right_gap) < 20  # centred, not hard against the left margin


def test_stem_is_not_stranded_at_the_foot_of_a_page_without_its_first_part(tmp_path):
    # a real paper never prints a question's stem at the bottom of one page with the part
    # it introduces overleaf.
    source = tmp_path / "source.pdf"
    doc = pymupdf.open()
    filler_page = doc.new_page(width=595, height=842)
    filler_page.insert_text((72, 200), "(a) Filler content occupying most of the page.")
    question_page = doc.new_page(width=595, height=842)
    question_page.insert_text((72, 100), "1")
    question_page.insert_text((92, 100), "Consider the following.")
    question_page.insert_text((72, 200), "(a) A part tall enough to not fit beneath the stem.")
    doc.save(str(source))
    doc.close()

    tall = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=1, x0=60, y0=180, x1=560, y1=700)],
    )
    filler = QuestionPart(
        id="2.a", question_id="2", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=60, y0=180, x1=560, y1=560)],
    )
    question = Question(
        paper_key="P1", board="Cambridge", subject="0000", question_number=1,
        stem_text="", stem_image_path="", total_marks=1, parts=[tall],
        stem_segments=[BBox(page=1, x0=60, y0=88, x1=560, y1=112)],
        number_bbox=_label_bbox_from_source(source, "1", page_index=1),
    )
    filler_question = Question(
        paper_key="P1", board="Cambridge", subject="0000", question_number=2,
        stem_text="", stem_image_path="", total_marks=1, parts=[filler],
    )
    paper = Paper(
        paper_key="P1", board="Cambridge", level="IGCSE", subject="0000", paper="1",
        variant="1", session="M/J", year=24, source_qp_path=str(source),
        questions=[filler_question, question],
    )
    paper_dir = tmp_path / "P1"
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "paper.json").write_text(paper.model_dump_json(indent=2), encoding="utf-8")

    # the filler fills most of page 1, so question 2's stem would otherwise land at its foot
    groups = [
        QuizGroup(paper_key="P1", question_number=2, part_paths=["a"]),
        QuizGroup(paper_key="P1", question_number=1, part_paths=["a"]),
    ]
    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages = [p.get_text("text") for p in doc]
    doc.close()

    stem_page = next(i for i, t in enumerate(pages) if "Consider the following" in t)
    part_page = next(i for i, t in enumerate(pages) if "tall enough" in t)
    assert stem_page == part_page


def test_cover_states_the_teachers_duration_in_words(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=115)],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    with_duration = compose_qp_pdf(groups, tmp_path, duration_minutes=75)
    doc = pymupdf.open(stream=with_duration, filetype="pdf")
    assert "1 hour 15 minutes" in doc[0].get_text("text")
    doc.close()

    # no allowance set means none is printed - a made-up one would be worse than none
    without = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=without, filetype="pdf")
    cover = doc[0].get_text("text")
    doc.close()
    assert "minute" not in cover and "hour" not in cover


def test_cover_carries_no_source_paper_codes(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=115)],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path, title="Practice Set 1")
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    cover = doc[0].get_text("text")
    doc.close()

    assert "Compiled from" not in cover
    assert "P1" not in cover
    # the title stands on its own, and the paper is labelled for what it is
    assert cover.strip().startswith("Practice Set 1")
    assert "Question Paper" in cover
    # identification is a name box only - no centre or candidate number
    assert "CANDIDATE\nNAME" in cover
    assert "NUMBER" not in cover


def _mcq_paper(tmp_path):
    """A multiple-choice item: one unlettered part, worth one mark, options lettered A-D
    inside its own text and no "[n]" printed anywhere (pipeline.py backfills the mark)."""
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.", question_id="1", path="", depth=1, marks=1, image_path="",
        text="Which process describes diffusion? Aa liquid freezing Ba solid melting "
             "Ca substance diffusing through a liquid Da substance diffusing through air",
        segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=115)],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])


def test_cover_states_the_mcq_mark_when_the_paper_has_multiple_choice(tmp_path):
    _mcq_paper(tmp_path)
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=[""])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    cover = doc[0].get_text("text")
    doc.close()

    assert "Each multiple-choice question is worth 1 mark." in cover


def test_cover_omits_the_mcq_line_for_a_written_paper(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="Explain X. [2]", image_path="",
        marks=2, segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=115)],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    pdf_bytes = compose_qp_pdf(groups, tmp_path)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    cover = doc[0].get_text("text")
    doc.close()

    assert "multiple-choice" not in cover


def test_preview_without_cover_numbers_from_first_number(tmp_path):
    # the question bank's single-question preview: no cover page, and the question carries
    # the number it would get in the paper rather than always "1"
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    parts = [
        QuestionPart(
            id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
            segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=105)],
        )
    ]
    _write_paper(tmp_path, "P1", source, 1, parts, [], [])
    groups = [
        QuizGroup(paper_key="P1", question_number=1, part_paths=["a"]),
        QuizGroup(paper_key="GONE", question_number=1, part_paths=["a"]),
    ]

    with_cover = pymupdf.open(stream=compose_qp_pdf(groups, tmp_path), filetype="pdf")
    preview = pymupdf.open(
        stream=compose_qp_pdf(groups, tmp_path, include_cover=False, first_number=4),
        filetype="pdf",
    )

    assert preview.page_count == with_cover.page_count - 1
    assert "Question 5: (source paper not found)" in preview[0].get_text()
    with_cover.close()
    preview.close()


def test_only_stems_and_headings_keep_with_what_follows():
    # regression (0478 s20 Q7): a part insisting on fitting alongside the *next* part jumped
    # to a new page and stranded its container heading alone at the foot of the previous one.
    from qbx.quiz_export import ITEM_GAP, _Block, _keep_with_next

    seg = [BBox(page=0, x0=0, y0=0, x1=10, y1=10)]
    stem, heading = _Block(seg, introduces=True), _Block(seg, introduces=True)
    part_i, part_ii = _Block(seg), _Block(seg)
    blocks = [stem, heading, part_i, part_ii]
    heights = [50.0, 20.0, 80.0, 214.0]

    # a stem followed by a heading keeps with both, down to the first real part
    assert _keep_with_next(blocks, heights, 0) == 20 + 80 + 2 * ITEM_GAP
    assert _keep_with_next(blocks, heights, 1) == 80 + ITEM_GAP
    # parts break between each other freely
    assert _keep_with_next(blocks, heights, 2) == 0
    assert _keep_with_next(blocks, heights, 3) == 0


def test_heading_too_tall_to_share_a_page_does_not_keep():
    from qbx.quiz_export import CONTENT_BOTTOM, CONTENT_TOP, _Block, _keep_with_next

    seg = [BBox(page=0, x0=0, y0=0, x1=10, y1=10)]
    blocks = [_Block(seg, introduces=True), _Block(seg)]
    assert _keep_with_next(blocks, [20.0, CONTENT_BOTTOM - CONTENT_TOP], 0) == 0


def test_mark_scheme_rows_are_copied_from_the_source_and_relabelled(tmp_path):
    from tests.ms_fixtures import write_ruled_ms

    source = tmp_path / "P1_qp_11.pdf"
    _make_source_pdf(source)
    write_ruled_ms(tmp_path / "P1_ms_11.pdf", [("3(a)", "first answer", 2), ("3(b)", "second answer", 1)])
    part = QuestionPart(
        id="3.b", question_id="3", path="b", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=110)],
    )
    entry = MarkSchemeEntry(id="3.b", paper_key="P1", question_number=3, part_path="b", answer_text="second answer", marks=1)
    _write_paper(tmp_path, "P1", source, 3, [part], [entry], [QuestionPartMarkScheme(part_id="3.b", mark_scheme_entry_id="3.b")])

    doc = pymupdf.open(stream=compose_ms_pdf([QuizGroup("P1", 3, ["b"])], tmp_path), filetype="pdf")
    pages = [p.get_text() for p in doc]

    assert "Generic Marking Principles" in pages[1]  # the principles, copied
    assert doc[1].rect.width > doc[1].rect.height  # on landscape pages
    table = pages[2]
    assert "second answer" in table and "first answer" not in table
    assert "1(a)" in table  # relabelled...
    assert "3(b)" not in "".join(pages)  # ...with the original label really gone
    doc.close()


def test_ms_row_label_follows_how_the_scheme_splits_the_part():
    from qbx.quiz_export import _ms_row_label

    assert _ms_row_label(2, "(a)", "c", "c") == "2(a)"
    assert _ms_row_label(2, "(a)", "c", "c.i") == "2(a)(i)"  # the scheme goes deeper
    assert _ms_row_label(2, "(b)(ii)", "c.ii", "c") == "2(b)"  # one row for the container


def _paper_with_ruled_ms(tmp_path, key, question_number, ms_rows, narrow):
    from tests.ms_fixtures import write_ruled_ms

    source = tmp_path / f"{key}_qp_11.pdf"
    _make_source_pdf(source)
    write_ruled_ms(tmp_path / f"{key}_ms_11.pdf", ms_rows, narrow=narrow)
    part = QuestionPart(
        id=str(question_number), question_id=str(question_number), path="", depth=0, text="x",
        image_path="", marks=1, segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=110)],
    )
    entry_id = f"{question_number}.stem"
    entry = MarkSchemeEntry(id=entry_id, paper_key=key, question_number=question_number, part_path="", answer_text="x", marks=1)
    join = QuestionPartMarkScheme(part_id=str(question_number), mark_scheme_entry_id=entry_id)
    _write_paper(tmp_path, key, source, question_number, [part], [entry], [join])


def test_portrait_only_mark_scheme_stays_portrait_at_its_own_size(tmp_path):
    # a multiple-choice (or maths) scheme is portrait; a paper drawing only on such
    # schemes is set on portrait pages like theirs - nothing widened, nothing shrunk
    from qbx.quiz_export import PORTRAIT_MS

    _paper_with_ruled_ms(tmp_path, "P1", 8, [("7", "C", 1), ("8", "B", 1)], narrow=True)
    doc = pymupdf.open(stream=compose_ms_pdf([QuizGroup("P1", 8, [""])], tmp_path), filetype="pdf")
    table_page = doc[doc.page_count - 1]
    borders = [d["rect"] for d in table_page.get_drawings()]

    assert table_page.rect.width < table_page.rect.height
    assert min(r.x0 for r in borders) >= PORTRAIT_MS.table_x0 - 2
    assert max(r.x1 for r in borders) <= PORTRAIT_MS.table_x1 + 2
    doc.close()


def test_narrow_table_is_widened_beside_a_landscape_scheme(tmp_path):
    # real-paper finding: beside a landscape scheme, a portrait scheme's ~497pt table sat
    # narrow in the middle of the landscape page - it's re-set at full width instead
    from qbx.quiz_export import LANDSCAPE_MS

    _paper_with_ruled_ms(tmp_path, "P1", 3, [("3", "wide answer", 2)], narrow=False)
    _paper_with_ruled_ms(tmp_path, "P2", 8, [("7", "C", 1), ("8", "B", 1)], narrow=True)
    groups = [QuizGroup("P1", 3, [""]), QuizGroup("P2", 8, [""])]
    doc = pymupdf.open(stream=compose_ms_pdf(groups, tmp_path), filetype="pdf")
    table_page = doc[doc.page_count - 1]
    words = {w[4]: w for w in table_page.get_text("words")}
    borders = [d["rect"] for d in table_page.get_drawings()]

    assert table_page.rect.width > table_page.rect.height
    assert "B" in words and "C" not in words  # only the selected answer
    assert "2" in words  # relabelled question 8 -> 2
    # the grid spans the standard full width, not the source's 497pt
    assert min(r.x0 for r in borders) <= LANDSCAPE_MS.table_x0 + 1
    assert max(r.x1 for r in borders) >= LANDSCAPE_MS.table_x1 - 1
    # and the answer sits at the left of its widened cell, not stretched or centred
    assert words["B"][0] < LANDSCAPE_MS.table_x0 + 90
    doc.close()


def _source_with_periodic_table(path):
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).insert_text((72, 100), "Question content goes here.")
    table = doc.new_page(width=595, height=842)
    table.insert_text((250, 40), "16")  # the source's own page number: page furniture
    table.insert_text((72, 90), "The Periodic Table of Elements")
    for i, name in enumerate(
        "hydrogen helium lithium beryllium boron carbon nitrogen oxygen fluorine neon sodium "
        "magnesium aluminium silicon phosphorus sulfur chlorine argon potassium calcium scandium "
        "titanium vanadium chromium manganese iron cobalt nickel copper zinc gallium germanium "
        "arsenic selenium bromine krypton rubidium strontium yttrium zirconium niobium".split()
    ):
        table.insert_text((72 + (i % 6) * 80, 130 + (i // 6) * 30), name)
    table.insert_text((50, 805), "0000/11/M/J/24")
    doc.save(str(path))
    doc.close()


def test_periodic_table_is_appended_when_a_source_paper_prints_one(tmp_path):
    source = tmp_path / "source.pdf"
    _source_with_periodic_table(source)
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=110)],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    doc = pymupdf.open(stream=compose_qp_pdf(groups, tmp_path), filetype="pdf")
    last = doc[-1].get_text()
    assert "The Periodic Table of Elements" in last and "zirconium" in last
    # the standard table (qbx/assets), sideways title and gas-volume note included
    assert "The volume of one mole of any gas" in last
    assert "0000/11/M/J/24" not in last  # the source's footer stays behind
    assert "The Periodic Table is printed in the question paper." in doc[0].get_text()
    doc.close()

    # the single-question preview is that question alone
    preview = pymupdf.open(stream=compose_qp_pdf(groups, tmp_path, include_cover=False), filetype="pdf")
    assert "Periodic" not in "".join(p.get_text() for p in preview)
    preview.close()


def test_no_periodic_table_when_no_source_paper_prints_one(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=110)],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    doc = pymupdf.open(stream=compose_qp_pdf([QuizGroup("P1", 1, ["a"])], tmp_path), filetype="pdf")
    assert "Periodic" not in "".join(p.get_text() for p in doc)
    doc.close()


def test_question_number_without_a_stem_is_carried_by_its_first_block():
    # regression (0580 m25 qp 22): "12 (a) Write..." has no stem, so the number was never
    # rewritten - and picking only (b) lost it altogether
    from qbx.quiz_export import _Block, _place_question_number

    number = LabelRef(page=8, x0=49.6, y0=440.4, x1=61.1, y1=451.9, origin_x=49.6, origin_y=449.6)
    part_a = LabelRef(page=8, x0=72.0, y0=440.4, x1=86.0, y1=451.9, origin_x=72.0, origin_y=449.6)
    part_b = LabelRef(page=8, x0=72.3, y0=613.6, x1=86.4, y1=625.1, origin_x=72.3, origin_y=622.8)

    with_a = [_Block([BBox(page=8, x0=45.6, y0=440.4, x1=549.7, y1=603.1)], relabel=(part_a, "(a)"))]
    _place_question_number(with_a, number, 1)
    assert with_a[0].number == (number, "1")  # rewritten where it stands

    only_b = [_Block([BBox(page=8, x0=68.3, y0=613.6, x1=549.7, y1=759.1)], relabel=(part_b, "(a)"))]
    _place_question_number(only_b, number, 1)
    placed, text = only_b[0].number
    assert text == "1"
    assert placed.x0 == number.x0  # in the question-number column...
    assert placed.origin_y == part_b.origin_y  # ...on the block's own first line


def test_preamble_sections_shared_between_schemes_are_grouped():
    from qbx.quiz_export import _preamble_section

    # the generic principles, whether a page opens them or continues them
    assert _preamble_section("Generic Marking Principles") == _preamble_section("GENERIC MARKING PRINCIPLE 5:")
    # the annotations guidance and its continuation table
    assert _preamble_section("Annotations guidance for centres") == _preamble_section("Annotation Meaning")
    # subject-specific notes stay distinct
    assert _preamble_section("Mathematics-Specific Marking Principles") != _preamble_section(
        "Science-Specific Marking Principles"
    )
    assert _preamble_section("MARK SCHEME NOTES") == "mark scheme notes"


def test_four_column_table_widens_its_text_columns_only():
    # regression (0580 m25 MS 22): a maths scheme's Question | Answer | Marks | Partial
    # Marks table is portrait-width; beside a landscape scheme it widens to full width by
    # growing the text columns, never by scaling anything down
    from qbx.quiz_export import LANDSCAPE_MS, _MSFlow

    flow = _MSFlow(pymupdf.open(), LANDSCAPE_MS)
    source = [49.3, 109.3, 328.0, 372.9, 545.9]  # as measured
    out = flow.widened_columns(source, marks_column=2)

    assert out[0] == LANDSCAPE_MS.table_x0 and abs(out[-1] - LANDSCAPE_MS.table_x1) < 1e-6
    assert abs((out[1] - out[0]) - (source[1] - source[0])) < 1e-6  # Question: own width
    assert abs((out[3] - out[2]) - (source[3] - source[2])) < 1e-6  # Marks: own width
    assert out[2] - out[1] > source[2] - source[1]  # Answer grows
    assert out[4] - out[3] > source[4] - source[3]  # Partial Marks grows


def _text_sizes(page) -> dict[str, float]:
    return {
        span["text"].strip(): span["size"]
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        if span["text"].strip()
    }


def test_a3_source_is_set_at_the_same_size_as_an_a4_one(tmp_path):
    """Real-paper finding (0620 s17 qp 11): an A3 download laid out beside A4 papers took
    the paper's one scale from its own wide crops - every A4 question shrank to 74% and the
    A3 one came out oversized. Each source is brought to A4 before the layout is shared."""
    a4, a3 = tmp_path / "a4.pdf", tmp_path / "a3.pdf"
    for path, (width, height), size in ((a4, (595, 842), 10), (a3, (842, 1191), 10 * 842 / 595)):
        doc = pymupdf.open()
        page = doc.new_page(width=width, height=height)
        page.insert_text((72 * width / 595, 100), f"Body from {path.stem}", fontsize=size)
        doc.save(str(path))
        doc.close()
    for key, path, width in (("A4", a4, 595), ("A3", a3, 842)):
        part = QuestionPart(
            id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
            # a real paper's body band, x=60-540 on A4, scaled with the page
            segments=[BBox(page=0, x0=60 * width / 595, y0=80, x1=540 * width / 595, y1=120)],
        )
        _write_paper(tmp_path, key, path, 1, [part], [], [])
    groups = [QuizGroup(paper_key=k, question_number=1, part_paths=["a"]) for k in ("A4", "A3")]

    doc = pymupdf.open(stream=compose_qp_pdf(groups, tmp_path, title="T"), filetype="pdf")
    sizes = _text_sizes(doc[1])
    doc.close()

    assert abs(sizes["Body from a4"] - 10) < 0.2  # A4 content copies at full size
    assert abs(sizes["Body from a3"] - sizes["Body from a4"]) < 0.2


def test_footer_links_the_builder_and_carries_the_teachers_note(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=0, y0=90, x1=595, y1=110)],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    for pdf_bytes in (
        compose_qp_pdf(groups, tmp_path, title="T", footer_note="Year 10 - Set 2"),
        compose_ms_pdf(groups, tmp_path, title="T", footer_note="Year 10 - Set 2"),
    ):
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        page = doc[doc.page_count - 1]
        text = page.get_text("text")
        uris = [link.get("uri") for link in page.get_links()]
        doc.close()
        assert "Classark Exam Builder" in text
        assert "Year 10 - Set 2" in text
        assert "https://classark.com" in uris


def _answer_sheet_rows(doc) -> list[str]:
    """The question numbers down the answer sheet's rows, and the options on each."""
    rows = []
    for page in doc:
        if "Multiple-choice Answer Sheet" not in page.get_text("text"):
            continue
        words = page.get_text("words")
        for x0, y0, x1, y1, text, *_ in words:
            if text.isdigit() and y0 > 150 and x0 > 200:  # clear of the page number and panel
                options = sorted(
                    (w[0], w[4]) for w in words
                    if abs(w[1] - y0) < 8 and w[0] > x1 and w[0] < x1 + 130 and not w[4].isdigit()
                )
                rows.append(text + ":" + "".join(o for _, o in options))
    return rows


def test_answer_sheet_opens_the_multiple_choice_section(tmp_path):
    _mcq_paper(tmp_path)
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=[""])] * 3

    doc = pymupdf.open(stream=compose_qp_pdf(groups, tmp_path, title="T"), filetype="pdf")
    rows = _answer_sheet_rows(doc)
    pages = [page.get_text("text") for page in doc]
    doc.close()

    # one row per multiple-choice question, options A-D only
    assert rows == ["1:ABCD", "2:ABCD", "3:ABCD"]
    # with no written questions, the sheet comes straight after the cover - no blank page
    assert "Multiple-choice Answer Sheet" in pages[1] and "Instructions" in pages[1]
    assert "Candidate name" not in pages[1]
    assert "Question content" in pages[2]
    assert "answer sheet printed just before them" in pages[0]


def test_answer_sheet_numbers_only_the_multiple_choice_questions(tmp_path):
    _mcq_paper(tmp_path)
    source = tmp_path / "source.pdf"
    written = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="Explain X. [2]", image_path="",
        marks=2, segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=115)],
    )
    _write_paper(tmp_path, "W1", source, 1, [written], [], [])
    groups = [
        QuizGroup(paper_key="W1", question_number=1, part_paths=["a"]),
        QuizGroup(paper_key="P1", question_number=1, part_paths=[""]),
    ]

    doc = pymupdf.open(stream=compose_qp_pdf(groups, tmp_path, title="T"), filetype="pdf")
    rows = _answer_sheet_rows(doc)
    doc.close()

    # the multiple-choice section numbers its own questions from 1
    assert rows == ["1:ABCD"]


def test_written_paper_has_no_answer_sheet(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="Explain X. [2]", image_path="",
        marks=2, segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=115)],
    )
    _write_paper(tmp_path, "P1", source, 1, [part], [], [])
    groups = [QuizGroup(paper_key="P1", question_number=1, part_paths=["a"])]

    doc = pymupdf.open(stream=compose_qp_pdf(groups, tmp_path, title="T"), filetype="pdf")
    text = "".join(page.get_text("text") for page in doc)
    doc.close()

    assert "Answer Sheet" not in text


def test_each_section_numbers_from_one():
    from qbx.quiz_export import _ResolvedGroup, _section_numbers

    def group(mcq):
        return _ResolvedGroup(paper=None, question=None, has_multiple_choice=mcq)

    resolved = [group(False), group(False), group(True), group(True), group(True)]
    assert _section_numbers(resolved) == [1, 2, 1, 2, 3]
    # the single-question preview: numbered as the question it would become
    assert _section_numbers([group(True)], first_number=4) == [4]


def test_answer_sheet_splits_into_balanced_columns_then_pages():
    from qbx.quiz_export import _mcq_sheet_columns

    assert _mcq_sheet_columns(list(range(1, 4))) == [[[1, 2, 3]]]
    def shape(n):
        return [[len(c) for c in page] for page in _mcq_sheet_columns(list(range(1, n + 1)))]

    assert shape(20) == [[20]]
    assert shape(25) == [[13, 12]]
    assert shape(47) == [[16, 16, 15]]
    assert shape(70) == [[20, 20, 20], [10]]


def test_mcq_whose_options_are_not_in_its_text_is_found_by_its_answer_letter():
    """Real-paper finding (0620 s17 qp 11 Q6): options glued together in extraction
    ("ACl2BCH4CH2O DHCl"), so only the mark scheme's single-letter answer says it's MCQ."""
    from qbx.quiz_export import is_multiple_choice

    part = QuestionPart(
        id="6.", question_id="6", path="", depth=1, marks=1, image_path="",
        text="Which molecule contains exactly two single covalent bonds? ACl2BCH4CH2O DHCl",
    )
    entry = MarkSchemeEntry(id="ms6", paper_key="P1", question_number=6, part_path="", answer_text="C", marks=1)
    paper = Paper(
        paper_key="P1", board="Cambridge", level="IGCSE", subject="0620", paper="1",
        variant="1", session="M/J", year=17, source_qp_path="",
        questions=[], mark_scheme=[entry],
        joins=[QuestionPartMarkScheme(part_id="6.", mark_scheme_entry_id="ms6")],
    )
    assert not is_multiple_choice(part)
    assert is_multiple_choice(part, paper)
    # the cover's own title is enough, with no mark scheme at all
    titled = paper.model_copy(update={"mark_scheme": [], "joins": [], "paper_title": "Paper 1 Multiple Choice (Core)"})
    assert is_multiple_choice(part, titled)
    # a one-mark written answer is not multiple choice
    written = paper.model_copy(update={"mark_scheme": [entry.model_copy(update={"answer_text": "covalent bonding"})]})
    assert not is_multiple_choice(part, written)


def test_multiple_choice_comes_last_on_new_pages_after_its_answer_sheet(tmp_path):
    _mcq_paper(tmp_path)  # P1 Q1, multiple choice; its source page reads "Question content"
    written_source = tmp_path / "written.pdf"
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).insert_text((72, 100), "Explain osmosis.")
    doc.save(str(written_source))
    doc.close()
    written = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="Explain osmosis. [2]", image_path="",
        marks=2, segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=115)],
    )
    _write_paper(tmp_path, "W1", written_source, 1, [written], [], [])
    # the teacher put a multiple-choice question first
    groups = [
        QuizGroup(paper_key="P1", question_number=1, part_paths=[""]),
        QuizGroup(paper_key="W1", question_number=1, part_paths=["a"]),
        QuizGroup(paper_key="P1", question_number=1, part_paths=[""]),
    ]

    doc = pymupdf.open(stream=compose_qp_pdf(groups, tmp_path, title="T"), filetype="pdf")
    rows = _answer_sheet_rows(doc)
    pages = [page.get_text("text") for page in doc]
    doc.close()

    # the written question is 1, and the MCQ section numbers again from 1
    assert rows == ["1:ABCD", "2:ABCD"]
    mcq_text = "".join(text for text in pages if "Question content" in text)
    assert "Multiple-choice questions" in mcq_text  # the restarted numbering is headed
    written_pages = [i for i, text in enumerate(pages) if "osmosis" in text]
    sheet_pages = [i for i, text in enumerate(pages) if "Multiple-choice Answer Sheet" in text]
    mcq_pages = [i for i, text in enumerate(pages) if "Question content" in text]
    assert written_pages and sheet_pages and mcq_pages
    # written, then the answer sheet, then the multiple-choice questions - each on its own pages
    assert max(written_pages) < min(sheet_pages) and max(sheet_pages) < min(mcq_pages)


def test_mark_scheme_follows_the_multiple_choice_last_order(tmp_path):
    _mcq_paper(tmp_path)
    source = tmp_path / "source.pdf"
    written = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="Explain X. [2]", image_path="",
        marks=2, segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=115)],
    )
    entry = MarkSchemeEntry(id="ms1", paper_key="W1", question_number=1, part_path="a", answer_text="Osmosis answer", marks=2)
    _write_paper(
        tmp_path, "W1", source, 1, [written], [entry],
        [QuestionPartMarkScheme(part_id="1.a", mark_scheme_entry_id="ms1")],
    )
    groups = [
        QuizGroup(paper_key="P1", question_number=1, part_paths=[""]),
        QuizGroup(paper_key="W1", question_number=1, part_paths=["a"]),
    ]

    doc = pymupdf.open(stream=compose_ms_pdf(groups, tmp_path, title="T"), filetype="pdf")
    text = "".join(page.get_text("text") for page in doc)
    doc.close()

    # the written question moved ahead of the MCQ, so its answer is labelled 1(a); each
    # section numbers from 1, so each is headed
    assert "1(a)" in text
    assert text.index("Written questions") < text.index("Multiple-choice questions")


def test_sources_list_names_where_each_question_came_from_in_paper_order(tmp_path):
    from qbx.quiz_export import compose_sources_pdf

    _mcq_paper(tmp_path)  # P1 Q1: multiple choice, M/J 2024
    source = tmp_path / "source.pdf"
    parts = [
        QuestionPart(
            id=f"4.{path}", question_id="4", path=path, depth=1, text="Explain. [2]", image_path="",
            marks=2, segments=[BBox(page=0, x0=60, y0=88, x1=560, y1=115)],
        )
        for path in ("a", "b")
    ]
    _write_paper(tmp_path, "W1", source, 4, parts, [], [])
    groups = [
        QuizGroup(paper_key="P1", question_number=1, part_paths=[""]),
        QuizGroup(paper_key="W1", question_number=4, part_paths=["b"]),
    ]

    doc = pymupdf.open(stream=compose_sources_pdf(groups, tmp_path, title="Mock"), filetype="pdf")
    words = doc[0].get_text("text")
    doc.close()

    # the written question is Q1 of the paper, the MCQ Q2 - as the paper numbers them
    written, mcq = words.index("Written questions"), words.index("Multiple choice")
    assert written < mcq
    assert "May/June 2024" in words
    assert "Q4" in words[written:mcq] and "(b)" in words[written:mcq]
    assert "Q1" in words[mcq:] and "All" in words[mcq:]


def test_a_question_whose_source_pdf_has_gone_prints_a_placeholder_not_an_error(tmp_path):
    """Real finding: a paper still in the bank whose PDF was no longer in the papers folder
    made every preview and export that included it fail with a 500."""
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="x", image_path="", marks=1,
        segments=[BBox(page=0, x0=60, y0=85, x1=540, y1=115)],
    )
    _write_paper(tmp_path, "GONE", tmp_path / "moved_away.pdf", 1, [part], [], [])
    _write_paper(tmp_path, "HERE", source, 1, [part], [], [])
    groups = [QuizGroup("GONE", 1, ["a"]), QuizGroup("HERE", 1, ["a"])]

    doc = pymupdf.open(stream=compose_qp_pdf(groups, tmp_path, title="T"), filetype="pdf")
    text = _content_text(doc)
    doc.close()
    assert "moved_away.pdf) is missing from the papers folder" in text
    assert "Question content goes here." in text  # the other question still prints
