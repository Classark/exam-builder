"""Edexcel International GCSE papers: naming, pairing, marks, mark schemes and keeping
exams to one board. Synthetic PDFs throughout, laid out the way the real ones measured -
never the real, copyrighted papers."""

import sys

import pymupdf
import pytest
from fastapi.testclient import TestClient

from qbx import edexcel, markscheme_edexcel
from qbx.anchors import AnchorSpan, resolve_tiers
from qbx.pages import PageType, classify_page
from qbx.pairing import identify
from qbx.scan import plan_scan
from qbx.tree import TextSpan
from tests.test_main import _make_paper, _make_source_pdf, _write_paper


# --- names: the sitting from the folder and file names ---------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        # the user's own filing: "<Month> <Year>\<kind>\<file>"
        ("June 2025/Question-paper/Questionpaper-Paper1C-June2025.pdf", ("question paper", "JUN", 2025, "1C", "folder")),
        ("November 2020/Mark-scheme/MarkScheme-Paper2CR-November2020.pdf", ("mark scheme", "NOV", 2020, "2CR", "folder")),
        ("2014 Jan/Mark-scheme/Markscheme-Paper1C-January2014.pdf", ("mark scheme", "JAN", 2014, "1C", "folder")),
        # flat, as an upload leaves it: the file name says
        ("Markscheme-Paper1C-January2012.pdf", ("mark scheme", "JAN", 2012, "1C", "filename")),
        # the rescheduled 2020 series, sat in the autumn
        ("IG-Edx.-Chemistry-May-Oct.-2020-1C.pdf", (None, "NOV", 2020, "1C", "filename")),
        # a question paper's 8-digit date is the exam's
        ("4CH0_1C_que_20120521.pdf", ("question paper", "JUN", 2012, "1C", "filename")),
        # the digit dropped from the name: half a paper code, for the cover to complete
        ("January 2020/Mark-scheme/Markscheme-PaperCR-January2020.pdf", ("mark scheme", "JAN", 2020, "CR", "folder")),
    ],
)
def test_the_sitting_comes_from_the_folder_and_file_names(path, expected):
    reading = edexcel.read_name(path)
    assert (reading.kind, reading.session, reading.year, reading.paper, reading.source) == expected


def test_a_mark_schemes_date_is_when_it_was_published_so_it_is_never_read():
    # _rms_20120823 is August: reading it would file a June scheme under November
    reading = edexcel.read_name("4CH0_1C_rms_20120823.pdf")
    assert (reading.kind, reading.session, reading.year) == ("mark scheme", None, None)


def test_the_cover_decides_the_paper_and_a_disagreeing_name_is_reported():
    # June 2013's "Markscheme-Paper1C" is the 1CR scheme by its own cover
    cover = "Mark Scheme (Results)\nSummer 2013\nInternational GCSE\nChemistry (4CH0) Paper 1CR"
    found = edexcel.identify("June 2013/Mark-scheme/Markscheme-Paper1C-June2013.pdf", cover)
    assert (found.unit, found.paper, found.session, found.year) == ("4CH0", "1CR", "JUN", 2013)
    assert "the cover says 1CR" in found.problems[0]


def test_co_listed_units_and_a_typo_still_give_the_chemistry_unit():
    assert edexcel.read_cover("Chemistry (4CHO) Paper 1C\nChemistry (KCH0) Paper 1C").unit == "4CH0"
    assert edexcel.read_cover("Unit: KCH0/4CH0\nPaper: 2C").unit == "4CH0"
    assert edexcel.read_cover("4CH1/1C 4SD0/1C\nUNIT: 4CH1\nPAPER: 1C").paper == "1C"


def test_a_unit_code_is_read_by_its_shape_not_a_list_of_subjects():
    # International A Level: the QP's "WMA11/01" box, the scheme's "(WST01) Paper 01", the
    # June 2025 "A" papers, and a scheme printing only its publications code
    assert (lambda c: (c.unit, c.paper))(edexcel.read_cover("Morning\nWMA11/01\nPaper\nreference")) == ("WMA11", "01")
    assert (lambda c: (c.unit, c.paper))(edexcel.read_cover("In Statistics S1 (WST01) Paper 01")) == ("WST01", "01")
    assert edexcel.read_cover("WMA11/01A\nPaper\nreference").paper == "01A"
    assert (lambda c: (c.unit, c.paper))(edexcel.read_cover("Pearson Edexcel IAL Mathematics (WMA12)\nWMA12_01_1906_MS")) == ("WMA12", "01")
    # UK A level and GCSE codes need nothing added either; an equation's 6CO2 isn't a unit
    assert edexcel.read_cover("Paper reference 9MA0/01").unit == "9MA0"
    assert edexcel.read_cover("6CO2 + 6H2O").unit is None


def test_an_international_a_level_paper_keys_by_its_unit_and_paper_number():
    cover = "Pearson Edexcel International Advanced Level\nThursday 9 May 2024\nWMA11/01R\nPaper\nreference"
    fp = edexcel.fingerprint("June 2024/Question-paper/Questionpaper-Unit1R(WMA11)-June2024.pdf", cover, "")
    assert fp.paper_key == "WMA11_01R_JUN_24"
    assert fp.level == "International Advanced Level"


# --- pairing and scanning ---------------------------------------------------------------


def _pdf(path, *pages):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for text in pages:
        doc.new_page(width=595, height=842).insert_textbox(pymupdf.Rect(50, 50, 545, 800), text, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _edexcel_qp(path, paper="1C", pref="P78950A", unit="4CH1"):
    return _pdf(
        path,
        f"Candidate surname\nCandidate Number\nPearson Edexcel International GCSE (9-1)\nMonday 19 May 2025\n"
        f"Chemistry\nUNIT: {unit}\nPAPER: {paper}\nAnswer all questions.\nThe total mark for this paper is 110.\n{pref}",
        "1 Some question text. (2)",
    )


def _edexcel_ms(path, paper="1C", pref="P78950A", unit="4CH1"):
    return _pdf(
        path,
        f"Mark Scheme (Results)\nSummer 2025\nPearson Edexcel International GCSE\nIn Chemistry ({unit}) Paper {paper}\n{pref}",
    )


def test_a_folder_filed_by_sitting_pairs_by_folder_and_cover(tmp_path):
    root = tmp_path / "Chemistry Edexcel"
    _edexcel_qp(root / "June 2025" / "Question-paper" / "Questionpaper-Paper1C-June2025.pdf")
    _edexcel_ms(root / "June 2025" / "Mark-scheme" / "Markscheme-Paper1C-June2025.pdf")
    # the same paper again, under its exam-date name: a second copy, not a second paper
    _edexcel_qp(root / "June 2025" / "Question-paper" / "4CH1_1C_que_20250519.pdf")
    _pdf(root / "June 2025" / "Examiner-report" / "Examiner-report-Paper1C-June2025.pdf", "Examiner report")

    plan = plan_scan(tmp_path)

    assert [p.paper_key for p in plan.pairs] == ["4CH1_1C_JUN_25"]
    reasons = {u.path.name: u.reason for u in plan.unpaired}
    assert "second copy" in reasons["Questionpaper-Paper1C-June2025.pdf"] or "second copy" in reasons["4CH1_1C_que_20250519.pdf"]
    assert "examiner report" in reasons["Examiner-report-Paper1C-June2025.pdf"]


def test_edexcel_documents_are_identified_with_their_board_and_reference(tmp_path):
    qp = identify(_edexcel_qp(tmp_path / "June 2025" / "Question-paper" / "Questionpaper-Paper1C-June2025.pdf"))
    assert (qp.board, qp.kind, qp.code, qp.session, qp.year, qp.pref) == (
        "Edexcel", "question paper", "4CH1/1C", "JUN", 2025, "P78950A",
    )


# --- question papers: Edexcel's marks ---------------------------------------------------


def _span(text, page=0, y=100.0, x0=60.0):
    return TextSpan(text=text, page=page, y0=y, x0=x0)


def test_marks_at_the_margin_and_question_totals_become_the_trees_own_form():
    spans = [
        _span("(a) State two observations.", y=80),
        _span("(2)", y=100, x0=496),
        _span("(b) Give the formula.", y=130),
        _span("(1)", y=150, x0=496),
        _span("(3)", y=170, x0=120),  # a list's "(3)" in the text, nowhere near the margin
        _span("(Total for Question 1 = 3 marks)", y=200, x0=342),
    ]
    texts = [s.text.strip() for s in edexcel.normalise_marks(spans)]
    assert texts == ["(a) State two observations.", "[2]", "(b) Give the formula.", "[1]", "(3)", "[Total: 3]"]


def test_a_page_set_off_from_the_rest_is_measured_back_by_its_marks():
    # 4CH0 2C June 2015: one page 11.5pt left of every other
    spans = [_span("(2)", page=p, y=100 + 30 * i, x0=496) for p in (0, 1, 3) for i in range(3)]
    spans += [_span("(2)", page=2, y=100 + 30 * i, x0=484.5) for i in range(3)]
    assert edexcel.page_shifts(spans) == {2: -11.5}
    assert [s.text.strip() for s in edexcel.normalise_marks(spans) if s.page == 2] == ["[2]"] * 3


def _anchor(text, x0, page, y, tiers, follows=False):
    return AnchorSpan(text=text, x0=x0, y0=y, page=page, pattern_tiers=tiers, bbox=(x0, y, x0 + 10, y + 10), follows_marker=follows)


def test_a_marker_after_its_question_number_does_not_set_its_tiers_position():
    # "5 (a) The diagram..." one space apart: the (a) sits at 49, every other at 61
    spans = [_anchor(str(n), 42, n, 60, ["question"]) for n in range(1, 6)]
    spans += [_anchor(f"({c})", 61, p, 200 + i, ["part"]) for p in range(1, 5) for i, c in enumerate("ab")]
    spans.append(_anchor("(a)", 49, 5, 60, ["part"], follows=True))
    parts = next(r for r in resolve_tiers(spans) if r.tier == "part")
    assert round(parts.x0_center) == 61
    assert len(parts.spans) == 9 and parts.rejected == []


def test_a_cluster_on_one_page_does_not_beat_one_running_through_the_paper():
    spans = [_anchor(f"({c})", 61, p, 100 + i, ["part"]) for p in range(1, 6) for i, c in enumerate("abc")]
    spans.append(_anchor("(b)", 49, 3, 400, ["part"]))  # one page's own indentation
    parts = next(r for r in resolve_tiers(spans) if r.tier == "part")
    assert round(parts.x0_center) == 61
    assert [s.x0 for s in parts.rejected] == [49]


def test_edexcel_pages_are_classified():
    table = "The Periodic Table of the Elements\n" + "\n".join(
        "hydrogen helium lithium beryllium boron carbon nitrogen oxygen fluorine neon sodium magnesium "
        "aluminium silicon phosphorus sulfur chlorine argon potassium calcium scandium titanium vanadium "
        "chromium manganese iron cobalt nickel copper zinc gallium germanium arsenic selenium bromine "
        "krypton rubidium strontium yttrium zirconium".split()
    )
    assert classify_page(table, 2, 28) == PageType.PERIODIC_TABLE
    # a last page holding a question's end above the copyright notice is a question page
    last = "(d) Outline the steps.\n(5)\n(Total for Question 6 = 14 marks)\nCopyright Pearson Education"
    assert classify_page(last, 20, 20) == PageType.QUESTION


# --- mark schemes -----------------------------------------------------------------------

_COLUMNS = [40, 140, 360, 480, 550]


def _scheme(path):
    """Two table pages, ruled the Edexcel way: column rules on every page, a header on the
    first only, labels split over spans, a label run into its answer, a mark centred above
    its label, and a "Total" row."""
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).insert_text((72, 100), "Mark Scheme (Results)")
    for page_number in range(2):
        page = doc.new_page(width=595, height=842)
        for x in _COLUMNS:
            page.draw_line((x, 60), (x, 780), width=0.5)
        page.draw_line((_COLUMNS[0], 60), (_COLUMNS[-1], 60), width=0.5)
        if page_number == 0:
            page.draw_line((_COLUMNS[0], 90), (_COLUMNS[-1], 90), width=0.5)
            for x, word in ((45, "Question number"), (220, "Answer"), (400, "Notes"), (500, "Marks")):
                page.insert_text((x, 80), word, fontname="hebo", fontsize=9)
            rows = [
                (110, [(45, "1"), (65, "(a)"), (95, "(i)")], "sodium chloride", "ALLOW NaCl", [(512, 110, "1")]),
                # its mark centred in the cell, printed above its label
                (150, [(95, "(ii)")], "it conducts electricity when molten", "", [(512, 140, "2")]),
                (200, [(95, "(iii) An explanation that links two points")], "", "", [(512, 200, "1")]),
            ]
            for y, labels, answer, notes, marks in rows:
                for x, label in labels:
                    page.insert_text((x, y), label, fontname="hebo", fontsize=10)
                if answer:
                    page.insert_text((150, y), answer, fontname="helv", fontsize=10)
                if notes:
                    page.insert_text((370, y), notes, fontname="helv", fontsize=10)
                for x, my, m in marks:
                    page.insert_text((x, my), m, fontname="hebo", fontsize=10)
            page.insert_text((150, 240), "Total for Question 1 = 4 marks", fontname="hebo", fontsize=10)
            page.insert_text((512, 240), "4", fontname="hebo", fontsize=10)
        else:
            # a continuation page: no header, the columns are the last page's
            page.insert_text((45, 100), "2", fontname="hebo", fontsize=10)
            page.insert_text((65, 100), "(a)", fontname="hebo", fontsize=10)
            page.insert_text((150, 100), "carbon dioxide", fontname="helv", fontsize=10)
            page.insert_text((512, 100), "1", fontname="hebo", fontsize=10)
            page.insert_text((65, 140), "(b)", fontname="hebo", fontsize=10)
            page.insert_text((150, 140), "M1 add acid M2 effervescence", fontname="helv", fontsize=10)
            page.insert_text((512, 140), "2", fontname="hebo", fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def test_edexcel_mark_scheme_rows_are_read_by_their_running_label(tmp_path):
    entries = markscheme_edexcel.parse_mark_scheme(_scheme(tmp_path / "ms.pdf"))
    got = {e.id: (e.marks, e.answer_text) for e in entries}
    assert got == {
        "1.a.i": (1, "sodium chloride"),
        "1.a.ii": (2, "it conducts electricity when molten"),
        "1.a.iii": (1, "An explanation that links two points"),
        "2.a": (1, "carbon dioxide"),
        "2.b": (2, "M1 add acid M2 effervescence"),
    }
    assert next(e for e in entries if e.id == "1.a.i").guidance_text == "Notes: ALLOW NaCl"


def test_edexcel_mark_scheme_rows_have_regions_to_copy(tmp_path):
    layout = markscheme_edexcel.parse_mark_scheme_layout(_scheme(tmp_path / "ms.pdf"))
    assert set(layout.rows) == {"1.a.i", "1.a.ii", "1.a.iii", "2.a", "2.b"}
    first = layout.rows["1.a.i"]
    assert (first.segments[0].page, first.segments[0].x0, first.segments[0].x1) == (1, 40, 550)
    # the label's own box covers its pieces, and the glued one stops short of its answer
    assert first.label.x0 < 50 and first.label.x1 < 120
    assert layout.rows["1.a.iii"].label.x1 < 130
    # one row never reaches into the next
    assert first.segments[0].y1 < layout.rows["1.a.ii"].segments[0].y0 + 20
    assert layout.columns == _COLUMNS and layout.marks_column == 3


def _maths_scheme(path, star=""):
    """Mathematics A's layout: Question | Working | Answer | Mark | Notes, a header on the
    first page only, and each question boxed in a table of its own with its own columns
    (4MA0 3H January 2017). On page 2, 12(b)'s content is printed 12pt above its label,
    just under the rule that opens its row."""
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).insert_text((72, 100), "Mark Scheme (Results)")

    def table(page, xs, top, bottom, separators):
        for x in xs:
            page.draw_line((x, top), (x, bottom), width=0.5)
        for y in (top, *separators, bottom):
            page.draw_line((xs[0], y), (xs[-1], y), width=0.5)

    def text(page, x, y, words, bold=False):
        page.insert_text((x, y), words, fontname="hebo" if bold else "helv", fontsize=10)

    first = doc.new_page(width=595, height=842)
    table(first, [40, 100, 250, 330, 370, 550], 60, 140, [90, 120])
    for x, word in ((45, "Question"), (110, "Working"), (260, "Answer"), (335, "Mark"), (380, "Notes")):
        text(first, x, 80, word, bold=True)
    text(first, 45, 105, "11" + star, bold=True)  # "11*": working must be shown
    text(first, 260, 105, "5")
    text(first, 340, 105, "1")
    text(first, 380, 105, "B1")
    text(first, 480, 134, "Total 1 mark", bold=True)

    second = doc.new_page(width=595, height=842)
    # 12: Answer | Mark | Notes at 300 | 420 | 460, unlike the header page's
    table(second, [40, 100, 300, 420, 460, 550], 60, 205, [95, 130, 165, 185])
    text(second, 45, 80, "12", bold=True)
    text(second, 65, 80, "(a)", bold=True)
    text(second, 310, 80, "96")
    text(second, 435, 80, "1")
    text(second, 470, 80, "B1")
    text(second, 110, 142, "180 - 48")  # (b)'s content, above its label
    text(second, 310, 142, "132")
    text(second, 435, 142, "1")
    text(second, 470, 142, "B1")
    text(second, 65, 154, "(b)", bold=True)
    text(second, 65, 178, "(c)", bold=True)
    text(second, 310, 178, "cyclic quadrilateral")
    text(second, 435, 178, "1")
    text(second, 470, 178, "B1")
    text(second, 480, 199, "Total 3 marks", bold=True)
    # 13: a table of its own further down, with Mark at 300-345
    table(second, [40, 100, 200, 300, 345, 550], 230, 300, [280])
    text(second, 45, 250, "13", bold=True)
    text(second, 110, 250, "0.0275 x 4000")
    text(second, 210, 250, "4339.16")
    text(second, 320, 250, "3")
    text(second, 360, 250, "M1 A1 B1")
    text(second, 480, 294, "Total 3 marks", bold=True)
    doc.save(str(path))
    doc.close()
    return path


def test_a_maths_scheme_reads_each_tables_own_columns_and_rows_from_their_rules(tmp_path):
    entries = markscheme_edexcel.parse_mark_scheme(_maths_scheme(tmp_path / "ms.pdf"))
    got = {e.id: (e.marks, e.answer_text) for e in entries}
    assert got == {
        "11.stem": (1, "5"),
        "12.a": (1, "96"),
        "12.b": (1, "180 - 48 132"),
        "12.c": (1, "cyclic quadrilateral"),
        "13.stem": (3, "0.0275 x 4000 4339.16"),
    }


def test_a_maths_scheme_row_region_runs_from_rule_to_rule(tmp_path):
    layout = markscheme_edexcel.parse_mark_scheme_layout(_maths_scheme(tmp_path / "ms.pdf"))
    region = layout.rows["12.b"].segments[0]
    assert (region.page, round(region.y0), round(region.y1)) == (2, 130, 165)
    above = layout.rows["12.a"].segments[0]
    assert above.y1 <= region.y0


# --- one board per exam -----------------------------------------------------------------


@pytest.fixture
def two_boards(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path))
    monkeypatch.setenv("QBX_QUIZZES_DIR", str(tmp_path / "quizzes"))
    sys.modules.pop("main", None)
    import main as backend

    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    _write_paper(tmp_path, _make_paper(source))  # TEST_1, Cambridge
    edexcel_paper = _make_paper(source).model_copy(
        update={"paper_key": "4CH1_1C_JUN_25", "board": "Edexcel", "subject": "4CH1"}
    )
    _write_paper(tmp_path, edexcel_paper)
    return TestClient(backend.app)


def test_an_exam_refuses_questions_from_two_boards(two_boards):
    two_boards.post("/api/quizzes", json={"name": "Mixed"})
    items = [
        {"paper_key": "TEST_1", "question_number": 1, "part_paths": ["a"]},
        {"paper_key": "4CH1_1C_JUN_25", "question_number": 1, "part_paths": ["a"]},
    ]
    response = two_boards.put("/api/quizzes/Mixed", json={"items": items})
    assert response.status_code == 400
    assert "one exam board" in response.json()["detail"]
    assert "Cambridge and Edexcel" in response.json()["detail"]
    # one board's questions on their own are fine
    assert two_boards.put("/api/quizzes/Mixed", json={"items": items[1:]}).status_code == 200


def test_each_paper_and_subject_says_its_board(two_boards):
    boards = {p["paper_key"]: p["board"] for p in two_boards.get("/api/papers").json()}
    assert boards == {"TEST_1": "Cambridge", "4CH1_1C_JUN_25": "Edexcel"}
    assert {s["subject"]: s["board"] for s in two_boards.get("/api/subjects").json()} == {
        "0000": "Cambridge", "4CH1": "Edexcel",
    }


def test_a_numbered_answer_line_is_not_a_question_number():
    # "Give two reasons": answer lines "1....." and "2....." at the question number's indent
    # (4CH1 1C Nov 2021 read the "2" as question 2, a page before it starts)
    from qbx.anchors import find_anchor_spans

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((42, 100), "1", fontname="hebo", fontsize=11)
    page.insert_text((60, 100), "Give two reasons why E is a mixture.", fontsize=11)
    page.insert_text((42, 200), "1", fontsize=11)
    page.insert_text((49, 200), "." * 60, fontsize=11)
    page.insert_text((42, 240), "2", fontsize=11)
    page.insert_text((49, 240), "." * 60, fontsize=11)
    numbers = [s.text for s in find_anchor_spans(doc) if s.pattern_tiers == ["question"]]
    doc.close()
    assert numbers == ["1"]


def test_a_part_on_a_line_opening_with_spaces_is_still_found():
    # 4MA0 1F January 2018 Q17: the line is a span of spaces at 42, then " (a) Work out..."
    # at 49 with its "(a)" at 61 - measured from the span boxes, (a) wasn't at its line's start
    from qbx.anchors import find_anchor_spans

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((42, 100), "17", fontname="hebo", fontsize=11)
    page.insert_text((61, 100), "Pasquale has a map with a scale of 1:125 000", fontsize=11)
    page.insert_text((42, 140), "  ", fontname="tiro", fontsize=11)
    page.insert_text((49, 140), "   (a) Work out the distance on the map.", fontsize=11)
    page.insert_text((61, 300), "(b) Work out the value of n.", fontsize=11)
    found = {s.text: round(s.x0) for s in find_anchor_spans(doc)}
    doc.close()
    assert "(a)" in found and abs(found["(a)"] - found["(b)"]) <= 3


# --- linking a part to its mark scheme answer by hand -----------------------------------


def test_a_part_is_linked_to_the_answer_drawn_round_on_the_mark_scheme(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path))
    monkeypatch.setenv("QBX_QUIZZES_DIR", str(tmp_path / "quizzes"))
    sys.modules.pop("main", None)
    import main as backend

    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    scheme = _scheme(tmp_path / "ms.pdf")
    paper = _make_paper(source)
    # its part joins nothing: the kind of paper the Reviewer flags
    paper = paper.model_copy(update={"source_ms_path": str(scheme), "board": "Edexcel", "mark_scheme": [], "joins": []})
    _write_paper(tmp_path, paper)
    client = TestClient(backend.app)
    assert client.get("/api/papers/TEST_1/report").json()["passed"] is False

    # a box round "1 (a) (i) sodium chloride", drawn over the answer column only
    box = {"page": 1, "x0": 145, "y0": 98, "x1": 350, "y1": 114}
    response = client.put("/api/papers/TEST_1/parts/1.a/ms_link", json={"segments": [box]})
    assert response.status_code == 200
    entry = response.json()["entry"]
    assert entry["answer_text"] == "sodium chloride" and entry["manual"] is True
    # widened to the table, so a composed scheme copies the whole row, label and all
    assert (entry["segments"][0]["x0"], entry["segments"][0]["x1"]) == (40, 550)
    assert entry["label"]["x0"] < 50
    assert client.get("/api/papers/TEST_1/report").json()["passed"] is True
    assert "manual.1.a" in client.get("/api/papers/TEST_1/ms_rows").json()["rows"]

    # kept beside the paper, so a re-extraction applies it again; removable
    assert (tmp_path / "TEST_1" / "ms_links.json").exists()
    assert client.delete("/api/papers/TEST_1/parts/1.a/ms_link").json()["entry"] is None
    assert client.get("/api/papers/TEST_1/report").json()["passed"] is False


def test_a_parts_crop_stops_above_its_questions_total_line():
    # "(Total for Question 2 = 8 marks)" and the rule under it belong to the whole question
    from qbx.crops import page_segments

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((61, 100), "(d) What flame colour indicates lithium ions?", fontsize=11)
    page.insert_text((496, 130), "(1)", fontsize=11)
    page.insert_text((342, 200), "(Total for Question 2 = 8 marks)", fontname="hebo", fontsize=11)
    page.draw_line((42, 210), (553, 210), width=0.5)
    (_, rect), = page_segments(doc, (0, 90, 0), None, 48)
    doc.close()
    assert rect.y1 < 190


def test_a_mark_printed_once_for_a_part_is_shared_by_its_rows_codes(tmp_path):
    """4MA0 4H June 2017 Q14: "2" printed on (a)(ii) below a blank (a)(i), each row B1 -
    1 and 1, never shared with (b)(i), the next part's row. "B1oe" counts as B1."""
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).insert_text((72, 100), "Mark Scheme (Results)")
    page = doc.new_page(width=595, height=842)
    xs = [40, 100, 250, 330, 370, 550]
    for x in xs:
        page.draw_line((x, 60), (x, 200), width=0.5)
    for y in (60, 90, 110, 130, 150, 170, 200):
        page.draw_line((xs[0], y), (xs[-1], y), width=0.5)
    for x, word in ((45, "Question"), (110, "Working"), (260, "Answer"), (335, "Mark"), (380, "Notes")):
        page.insert_text((x, 80), word, fontname="hebo", fontsize=9)
    rows = [
        (104, "14 (a)(i)", "52", None, "B1"),
        (124, "(a)(ii)", "same segment", "2", "B1 Dep on B1 in (ai)"),
        (144, "(b)(i)", "104", None, "B1 accept 256"),
        (164, "(b)(ii)", "angle at centre", "2", "B1oe Dep on B1 in (bi)"),
    ]
    for y, label, answer, mark, notes in rows:
        page.insert_text((45, y), label, fontname="hebo", fontsize=9)
        page.insert_text((260, y), answer, fontname="helv", fontsize=9)
        if mark:
            page.insert_text((345, y), mark, fontname="helv", fontsize=9)
        page.insert_text((380, y), notes, fontname="helv", fontsize=9)
    page.insert_text((480, 190), "Total 4 marks", fontname="hebo", fontsize=9)
    doc.save(str(tmp_path / "ms.pdf"))
    doc.close()
    got = {e.id: e.marks for e in markscheme_edexcel.parse_mark_scheme(tmp_path / "ms.pdf")}
    assert got == {"14.a.i": 1, "14.a.ii": 1, "14.b.i": 1, "14.b.ii": 1}


def test_an_international_a_level_scheme_reads_its_tables_and_skips_their_notes(tmp_path):
    """International A Level (WMA11, WFM03, WST01 2024-25): Question Number | Scheme | Marks,
    each question boxed in a table of its own, codes in the Marks column ("M1 A1") under a
    "(3)" printed once, closed by "(5 marks)". Notes follow each table, labelled with the
    question's parts again: below the box, under a "Notes" heading, or in a box of their
    own - never rows, and never the next question."""
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).insert_text((72, 100), "Mark Scheme (Results)")
    page = doc.new_page(width=595, height=842)
    xs = [40, 100, 450, 550]

    def box(top, bottom, rules=()):
        for x in xs:
            page.draw_line((x, top), (x, bottom), width=0.5)
        for y in (top, *rules, bottom):
            page.draw_line((xs[0], y), (xs[-1], y), width=0.5)

    def text(x, y, words, bold=False):
        page.insert_text((x, y), words, fontname="hebo" if bold else "helv", fontsize=9)

    box(60, 210, rules=(90, 140))
    text(45, 72, "Question", True), text(45, 84, "Number", True)
    text(250, 78, "Scheme", True), text(470, 78, "Marks", True)
    for y, label, answer, marks in (
        (104, "1. (a)", "x = 2", "M1"), (118, "", "so y = 3", "A1"), (132, "", "", "(2)"),
        (150, "(b) (i)", "x > 1", "B1"), (168, "(ii)", "x < 4", "M1 A1"), (182, "", "", "(3)"),
        (198, "", "", "(5 marks)"),
    ):
        if label:
            text(45, y, label, True)
        if answer:
            text(110, y, answer)
        text(470, y, marks)
    # notes below the box, at the label column
    text(45, 230, "(a)", True), text(110, 230, "M1: any complete method")
    text(45, 250, "(b)", True), text(110, 250, "B1: both inequalities")
    box(280, 340)
    text(45, 300, "2. (a)", True), text(110, 300, "y = 5"), text(470, 300, "B1  (1)")
    text(470, 330, "(1 mark)")
    # notes boxed under a heading
    box(350, 420)
    text(45, 365, "Notes", True)
    text(45, 380, "(a)", True), text(110, 380, "B1: correct value only")
    box(430, 500)
    text(45, 445, "3. (a)", True), text(110, 445, "p = 0.2"), text(470, 445, "B1  (1)")
    text(45, 470, "(b)", True), text(110, 470, "q = 0.8"), text(470, 470, "B1  (1)")
    # notes boxed with no heading, walking the parts again
    box(510, 560)
    text(45, 525, "(a)", True), text(110, 525, "B1: awrt 0.2")
    text(45, 545, "(b)", True), text(110, 545, "B1: ft their p")
    box(570, 620)
    text(45, 590, "4. (a)", True), text(110, 590, "z = 1"), text(470, 590, "B1  (1)")
    text(470, 610, "(1 mark)")
    doc.save(str(tmp_path / "ms.pdf"))
    doc.close()

    got = {e.id: e.marks for e in markscheme_edexcel.parse_mark_scheme(tmp_path / "ms.pdf")}
    assert got == {"1.a": 2, "1.b.i": 1, "1.b.ii": 2, "2.a": 1, "3.a": 1, "3.b": 1, "4.a": 1}


def test_a_starred_question_number_is_still_its_label(tmp_path):
    """Mathematics A stars a question whose working must be shown ("4*"); 4MA1 2H June 2025
    lost 21 of its 26 questions' scheme rows to it."""
    entries = markscheme_edexcel.parse_mark_scheme(_maths_scheme(tmp_path / "ms.pdf", star="*"))
    assert {e.id: e.marks for e in entries}["11.stem"] == 1


def test_a_code_that_finishes_the_line_befores_sentence_is_not_a_mark():
    """4MA0 4HR June 2013 20(d)(ii): "... and dep on" / "M1" refers back to a mark."""
    from qbx.markscheme_edexcel import _Row, _row_codes

    row = _Row([20, "d", "ii"], 0, 0.0, [])
    row.guidance = {"Notes": ["M1 from x = 1 to at least x = 3", "A1 ft if at least 1 mark in (b) and dep on", "M1"]}
    assert _row_codes(row) == 2


def test_label_pieces_stacked_in_one_cell_are_one_label():
    """4MA0 3H January 2013 wraps "20." / "(a)" / "(ii)" down a narrow Question cell: one
    label, each piece a level deeper - never "(a)" then "(b)"."""
    from qbx.markscheme_edexcel import _deeper

    assert _deeper([20, "a", None], [20, None, None])
    assert _deeper([20, "a", "ii"], [20, "a", None])
    assert not _deeper([20, "b", None], [20, "a", None])


def test_a_part_letter_run_into_its_numeral_is_a_label_piece():
    """"ai", "aii" (4MA1 1FR January 2021): no brackets, no space - still a label."""
    from qbx.markscheme_edexcel import _label_pieces

    assert _label_pieces("ai") and _label_pieces("aii") and _label_pieces("2 biv")
    assert not _label_pieces("air")


def test_a_questions_total_row_corrects_misread_marks_by_their_codes():
    """Rows read 3, 0, 0 under "Total 4 marks", coded B1, B1, B2: the codes add up to the
    total and the read marks don't, so a mark was misread (4MA0 2F January 2013 Q5)."""
    from qbx.markscheme_edexcel import _Row, _marks_from_codes

    rows = []
    for path, marks, code in (([5, None, "i"], [3], "B1"), ([5, None, "ii"], [], "B1"), ([5, None, "iii"], [], "B2")):
        row = _Row(path, 0, 0.0, [])
        row.marks, row.answer, row.guidance = marks, ["x"], {"Notes": [code]}
        rows.append(row)
    _marks_from_codes(rows, {5: 4})
    assert [r.marks for r in rows] == [[1], [1], [2]]


def test_scheme_labels_nest_letters_inside_numeral_parts():
    """"7(i)(a)", "(b)", "7(ii)(a)" (WFM01 January 2025) - and "4" over an answer's "3" in
    the label cell is working, never a question 43 (WFM03 January 2025)."""
    from qbx.markscheme_edexcel import _label_steps

    path = [6, "c", None]
    got = []
    for label in ("7(i)(a)", "(b)", "7(ii)(a)", "(b)"):
        path = _label_steps(label, path)
        got.append(list(path))
    assert got == [[7, "i", "a"], [7, "i", "b"], [7, "ii", "a"], [7, "ii", "b"]]
    assert _label_steps("8(i)", [7, "ii", "b"]) == [8, None, "i"]
    assert _label_steps("4 3", [3, None, "ii"]) is None
    assert _label_steps("5(b)", [5, "a", None]) == [5, "b", None]
    # "1(h)(i)": the numeral after (h) in one label (4BI1 2B November 2020 read it as part i)
    assert _label_steps("1(h)(i)", [1, "g", None]) == [1, "h", "i"]
    assert _label_steps("(i)", [1, "h", None]) == [1, "i", None]
    assert _label_steps("(i)", [5, "b", None]) == [5, "b", "i"]


def test_a_mark_beside_a_stray_full_stop_is_still_a_mark():
    """WFM01 June 2024 1(i)(b): "(2)" at the margin shares its line with a lone "." - and
    "(Total forQuestion 1 is 6 marks)", the space between two spans lost, is the total."""
    from qbx.tree import TextSpan

    spans = [
        TextSpan(text="(a) Find k.", page=0, y0=100, x0=77), TextSpan(text="(2)", page=0, y0=114, x0=496),
        TextSpan(text=".", page=0, y0=150, x0=43), TextSpan(text="(2)", page=0, y0=150, x0=496),
        TextSpan(text="(Total for", page=1, y0=760, x0=342), TextSpan(text="Question 1 is 6 marks)", page=1, y0=760, x0=380),
    ]
    texts = [s.text.strip() for s in edexcel.normalise_marks(spans)]
    assert texts[1] == "[2]" and texts[3] == "[2]" and "[Total: 6]" in texts


def test_biology_mark_tags_are_not_the_mark_column_s_content():
    # 4BI1 2B November 2020: "1" over "comp", "3" over "exp" - half the Mark column was words,
    # and the page took the Guidance column the page before had used as its Mark column
    from qbx.markscheme import MSSpan
    from qbx.markscheme_edexcel import _numeric_column

    def cell(text):
        return MSSpan(text=text, page=0, x=0, y=0)

    lines = [{1: [cell("A a circle of DNA")], 2: [cell("1")]}, {2: [cell("comp")]},
             {1: [cell("An explanation")], 2: [cell("3")]}, {2: [cell("exp")]}]
    assert _numeric_column(lines, 3) == 2
