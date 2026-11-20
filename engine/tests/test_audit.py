"""qbx audit: checks of extracted papers against their own PDFs (synthetic PDFs only)."""

import pymupdf

from qbx.audit import audit_paper, diff_snapshots, snapshot
from qbx.schema import BBox, Paper, Question, QuestionPart


def _pdf(tmp_path, lines):
    """A two-page PDF (cover + one question page); `lines` are (x, y, text) on page 2."""
    doc = pymupdf.open()
    doc.new_page()
    page = doc.new_page()
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=11)
    path = tmp_path / "qp.pdf"
    doc.save(path)
    doc.close()
    return path


def _paper(path, parts):
    """parts: (path, marks, BBox list)."""
    return Paper(
        paper_key="0620_41_MJ_24", board="Cambridge", level="IGCSE", subject="0620", paper="4",
        variant="1", session="M/J", year=24, source_qp_path=str(path),
        questions=[Question(
            paper_key="0620_41_MJ_24", board="Cambridge", subject="0620", question_number=1,
            stem_text="", stem_image_path="", total_marks=sum(m for _, m, _ in parts),
            parts=[
                QuestionPart(id=f"p{i}", question_id="q1", path=p, depth=1, text="", image_path="",
                             marks=m, segments=segs)
                for i, (p, m, segs) in enumerate(parts)
            ],
        )],
    )


LINES = [
    (50, 100, "1 (a) State what is meant by an isotope."),
    (520, 130, "[2]"),
    (60, 200, "(b) Explain why the two atoms react in the same way."),
    (520, 230, "[3]"),
]


def test_a_paper_whose_regions_cover_its_page_is_clean(tmp_path):
    path = _pdf(tmp_path, LINES)
    paper = _paper(path, [
        ("a", 2, [BBox(page=1, x0=45, y0=85, x1=545, y1=135)]),
        ("b", 3, [BBox(page=1, x0=45, y0=185, x1=545, y1=235)]),
    ])
    assert audit_paper(paper).findings == []


def test_a_crop_cut_short_on_the_right_loses_its_mark(tmp_path):
    # 0625/41 MJ21 Q7(a): regions ending at x 306 left the "[3]" at x 533 out
    path = _pdf(tmp_path, LINES)
    paper = _paper(path, [
        ("a", 2, [BBox(page=1, x0=45, y0=85, x1=545, y1=135)]),
        ("b", 3, [BBox(page=1, x0=45, y0=185, x1=306, y1=235)]),
    ])
    findings = audit_paper(paper).findings
    assert [f.check for f in findings] == ["marks_in_region", "marks_question"]
    assert "region prints 0, part has 3" in findings[0].detail


def test_text_in_no_region_and_overlapping_parts_are_found(tmp_path):
    path = _pdf(tmp_path, LINES)
    paper = _paper(path, [
        ("a", 5, [BBox(page=1, x0=45, y0=85, x1=545, y1=235)]),  # runs over (b)
        ("b", 3, [BBox(page=1, x0=45, y0=185, x1=545, y1=235)]),
    ])
    checks = [f.check for f in audit_paper(paper).findings]
    assert "overlap" in checks


def test_a_snapshot_diff_names_what_changed():
    seg = [BBox(page=1, x0=45, y0=85, x1=545, y1=135)]
    old = snapshot(_paper("x", [("a", 2, seg), ("b", 3, seg)]))
    new = snapshot(_paper("x", [("a", 2, [BBox(page=1, x0=45, y0=85, x1=306, y1=135)]), ("b", 4, seg)]))
    lines = diff_snapshots(old, new)
    assert "Q1 b marks 3 -> 4" in lines
    assert any(line.startswith("Q1 a region moved") for line in lines)
    assert diff_snapshots(old, old) == []


def test_a_mark_outside_every_region_leaves_its_question_short(tmp_path):
    # 0452/21 MJ26 1(a): its "[11]" never read, so the part and its region agree on 0
    path = _pdf(tmp_path, LINES)
    paper = _paper(path, [
        ("a", 2, [BBox(page=1, x0=45, y0=85, x1=400, y1=135)]),
        ("b", 3, [BBox(page=1, x0=45, y0=185, x1=545, y1=235)]),
    ])
    paper.questions[0].parts[0].marks_printed = 0
    checks = [f.check for f in audit_paper(paper).findings]
    assert checks == ["marks_question"]


def test_wording_after_a_part_s_mark_is_the_next_part_s(tmp_path):
    # 0452/21 MJ26: (b)'s opening paragraph inside (a)'s region, after (a)'s mark
    path = _pdf(tmp_path, LINES + [
        (60, 160, "The amount of cash drawn from the bank account was omitted."),
    ])
    paper = _paper(path, [
        ("a", 2, [BBox(page=1, x0=45, y0=85, x1=545, y1=170)]),
        ("b", 3, [BBox(page=1, x0=45, y0=185, x1=545, y1=235)]),
    ])
    paper.questions[0].parts[0].marks_printed = 2
    findings = audit_paper(paper).findings
    assert [f.check for f in findings] == ["text_after_mark"]
    assert "The amount of cash" in findings[0].detail


def test_a_marker_no_part_owns_is_found(tmp_path):
    # 0625/32 FM16: "(b)" read as nobody's label, its (i) and (ii) under (a)
    from qbx.schema import LabelRef

    lines = LINES + [(60, 300, "(c) Suggest one more reason."), (520, 330, "[1]")]
    path = _pdf(tmp_path, lines)
    paper = _paper(path, [
        ("a", 2, [BBox(page=1, x0=45, y0=85, x1=545, y1=135)]),
        ("b", 4, [BBox(page=1, x0=45, y0=185, x1=545, y1=335)]),
    ])
    # (a) and (b) own their labels; the (c) at y 300 is nobody's
    for part, y in zip(paper.questions[0].parts, (100, 200)):
        part.label_bbox = LabelRef(page=1, x0=60, y0=y - 9, x1=75, y1=y + 2, size=11, bold=False)
    checks = [f.check for f in audit_paper(paper).findings]
    assert "marker_unused" in checks


def test_scheme_marks_against_the_paper_s():
    from qbx.audit import _check_scheme_marks
    from qbx.schema import MarkSchemeEntry, QuestionPartMarkScheme

    seg = [BBox(page=1, x0=45, y0=85, x1=545, y1=135)]
    paper = _paper("x", [("a", 2, seg), ("b", 3, seg)])
    entries = {
        "1.a": MarkSchemeEntry(id="1.a", paper_key="k", question_number=1, part_path="a", answer_text="", marks=2),
        "1.b": MarkSchemeEntry(id="1.b", paper_key="k", question_number=1, part_path="b", answer_text="", marks=1),
    }
    paper.joins = [QuestionPartMarkScheme(part_id="p0", mark_scheme_entry_id="1.a"),
                   QuestionPartMarkScheme(part_id="p1", mark_scheme_entry_id="1.b")]
    found = []
    _check_scheme_marks(paper, entries, lambda *a: found.append(a))
    assert found == [("ms_marks", "Q1 b: paper 3 marks, scheme 1")]
