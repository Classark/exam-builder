"""Fixes to an extracted paper (qbx/fixes.py): the Reviewer's edits."""

import sys

import pymupdf
import pytest
from fastapi.testclient import TestClient

from qbx import fixes
from qbx.anchors import decode_shifted_text
from qbx.fixes import Fix, Step
from qbx.schema import MarkSchemeEntry, Paper, Question, QuestionPart, QuestionPartMarkScheme


def _paper() -> Paper:
    """Q1, printed total 5: (a) 2 marks, linked; (b) no mark and no row; and a row printed
    under the wrong label (c) holding (b)'s answer, worth 3."""
    parts = [
        QuestionPart(id="1.a", question_id="1", path="a", depth=1, text="(a) Find x. (2)", image_path="", marks=2),
        QuestionPart(id="1.b", question_id="1", path="b", depth=1, text="(b) Find y.", image_path="", marks=0),
    ]
    return Paper(
        paper_key="T_1", board="Edexcel", level="International GCSE", subject="4MA1", paper="1",
        variant="F", session="JUN", year=24, cover_total_marks=5,
        questions=[Question(paper_key="T_1", board="Edexcel", subject="4MA1", question_number=1,
                            stem_text="", stem_image_path="", total_marks=5, parts=parts)],
        mark_scheme=[
            MarkSchemeEntry(id="ms.a", paper_key="T_1", question_number=1, part_path="a", answer_text="x = 4", marks=2),
            MarkSchemeEntry(id="ms.c", paper_key="T_1", question_number=1, part_path="c", answer_text="y = 7", marks=3,
                            guidance_text="M1 for ... M1 ... A1 cao"),
        ],
        joins=[QuestionPartMarkScheme(part_id="1.a", mark_scheme_entry_id="ms.a")],
    )


def _keys(paper: Paper) -> set[str]:
    return set(fixes.open_issues(paper, set()))


GOOD = Fix(steps=[Step(op="relabel_entry", entry_id="ms.c", path="b"), Step(op="set_marks", part_id="1.b", value=3)])


def test_parts_merge_with_their_marks_and_rows():
    paper = _paper()
    merged = fixes.apply_fix(paper, Fix(steps=[Step(op="merge_parts", part_ids=["1.a", "1.b"], path="a")]))
    (part,) = merged.questions[0].parts
    assert (part.id, part.marks) == ("1.a", 2)
    assert [(j.part_id, j.mark_scheme_entry_id) for j in merged.joins] == [("1.a", "ms.a")]


def test_applied_fixes_are_replayed_on_the_base_and_one_that_no_longer_fits_is_skipped(tmp_path):
    fixes.write_fixes(tmp_path, [
        Fix(steps=[Step(op="set_marks", part_id="1.gone", value=1)]),
        GOOD,
        Fix(steps=[Step(op="set_marks", part_id="1.a", value=7)], status="undone"),
    ])
    paper = fixes.rebuild(tmp_path, _paper(), lambda p: p)
    assert [p.marks for p in paper.questions[0].parts] == [2, 3]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path))
    monkeypatch.setenv("QBX_QUIZZES_DIR", str(tmp_path / "quizzes"))
    sys.modules.pop("main", None)
    import main as backend

    paper_dir = tmp_path / "T_1"
    paper_dir.mkdir()
    (paper_dir / "paper.json").write_text(_paper().model_dump_json(), encoding="utf-8")
    return TestClient(backend.app), paper_dir


def test_a_reviewer_edit_is_kept_as_a_fix_and_can_be_undone(client):
    http, paper_dir = client
    assert http.patch("/api/papers/T_1/parts/1.b", json={"marks": 3}).status_code == 200
    (fix,) = http.get("/api/papers/T_1/fixes").json()
    assert fix["source"] == "manual" and fix["status"] == "applied"
    assert (paper_dir / "paper.base.json").exists()
    http.post(f"/api/papers/T_1/fixes/{fix['id']}/undo")
    marks = [p["marks"] for p in http.get("/api/papers/T_1").json()["questions"][0]["parts"]]
    assert marks == [2, 0]


def test_shifted_font_text_is_decoded():
    # 4MA0 1F January 2018 Q21: "(a) Solve the equation" stored 29 code points down
    blocks = [{"lines": [{"spans": [
        {"text": "\x0bD\x0c\x036ROYH\x03WKH\x03HTXDWLRQ"},
        {"text": "Answer\x07(2)"},  # ordinary prose with a control character stays
    ]}]}]
    spans = decode_shifted_text(blocks)[0]["lines"][0]["spans"]
    assert spans[0]["text"] == "(a) Solve the equation"
    assert spans[1]["text"] == "Answer\x07(2)"


def test_parts_rows_and_questions_can_be_added_deleted_and_corrected():
    paper = _paper()
    added = fixes.apply_fix(paper, Fix(steps=[
        Step(op="add_part", question_number=1, path="c", value=1, part_id="1.b",
             fields={"text": "(c) Why?", "segments": [{"page": 0, "x0": 0, "y0": 0, "x1": 9, "y1": 9}]}),
    ]))
    assert [p.path for p in added.questions[0].parts] == ["a", "c", "b"]
    assert added.questions[0].parts[1].segments[0].x1 == 9

    gone = fixes.apply_fix(paper, Fix(steps=[Step(op="delete_part", part_id="1.a"), Step(op="delete_entry", entry_id="ms.c")]))
    assert [p.id for p in gone.questions[0].parts] == ["1.b"] and gone.joins == []
    assert [e.id for e in gone.mark_scheme] == ["ms.a"]

    other = fixes.apply_fix(paper, Fix(steps=[
        Step(op="set_entry_marks", entry_id="ms.c", value=4), Step(op="set_cover_total", value=9),
        Step(op="renumber_question", question_number=1, value=2),
    ]))
    assert (other.mark_scheme[1].marks, other.cover_total_marks, other.questions[0].question_number) == (4, 9, 2)
    assert fixes.apply_fix(paper, Fix(steps=[Step(op="delete_question", question_number=1)])).questions == []
    with pytest.raises(fixes.FixError):
        fixes.apply_fix(paper, Fix(steps=[Step(op="add_part", question_number=1, path="a")]))


def _scheme_pdf(path) -> str:
    """A mark scheme page whose 1(b) row the parser 'lost': "1(b)" at y 200, its answer and
    codes beside it, then a notes line - y in upright page points."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 120), "1(a)  x = 4   B1 B1", fontsize=10)
    page.insert_text((60, 210), "1(b)  y = 7   M1 A1", fontsize=10)
    page.insert_text((60, 300), "Notes  M1: any method", fontsize=10)
    doc.save(str(path))
    doc.close()
    return str(path)


def test_a_part_links_to_a_stretch_of_the_mark_scheme(tmp_path):
    """A part whose row the parser lost: a stretch of the scheme page is named, and the row is
    read from there like a teacher's drawn box."""
    paper = _paper()
    paper.source_ms_path = _scheme_pdf(tmp_path / "ms.pdf")
    paper.mark_scheme = [e for e in paper.mark_scheme if e.id != "ms.c"]  # (b)'s row lost
    fixed = fixes.apply_fix(paper, Fix(steps=[Step(op="link_region", part_id="1.b", at={"page": 0, "y0": 195, "y1": 290})]))
    entry = next(e for e in fixed.mark_scheme if e.id == "region.1.b")
    assert "y = 7" in entry.answer_text and "M1: any method" not in entry.answer_text
    assert entry.marks == 2  # nothing printed on the paper: its codes, M1 A1
    assert any(j.part_id == "1.b" and j.mark_scheme_entry_id == "region.1.b" for j in fixed.joins)
    with pytest.raises(fixes.FixError):
        fixes.apply_fix(paper, Fix(steps=[Step(op="link_region", part_id="1.b", at={"page": 0, "y0": 500, "y1": 600})]))


def test_the_reviewer_links_a_row_with_one_edit_and_undoes_it(client):
    """The rebuilt Reviewer links a scheme row the parser found to a part with one click:
    one `link` step through /edit, which clears both "no answer" and "orphaned row"."""
    http, _ = client
    open_before = {i["check"] for i in http.get("/api/papers/T_1/report").json()["issues"]}
    assert {"part_has_mark_scheme_entry", "no_orphaned_mark_scheme_entries"} <= open_before

    r = http.post("/api/papers/T_1/edit", json={"steps": [{"op": "link", "part_id": "1.b", "entry_id": "ms.c"}]})
    assert r.status_code == 200 and r.json()["fix_id"]
    checks = {i["check"] for i in http.get("/api/papers/T_1/report").json()["issues"]}
    assert not checks & {"part_has_mark_scheme_entry", "no_orphaned_mark_scheme_entries"}

    http.post(f"/api/papers/T_1/fixes/{r.json()['fix_id']}/undo")
    checks = {i["check"] for i in http.get("/api/papers/T_1/report").json()["issues"]}
    assert "no_orphaned_mark_scheme_entries" in checks


def test_claudes_old_fixes_no_longer_apply(client):
    """Claude stopped fixing papers (user-decided): a fixes.json still holding its fixes loads,
    but they're neither applied nor listed. The teacher's own edits still are."""
    http, paper_dir = client
    fixes.write_fixes(paper_dir, [GOOD.model_copy(update={"source": "ai"})])
    assert [p.marks for p in fixes.rebuild(paper_dir, _paper(), lambda p: p).questions[0].parts] == [2, 0]
    assert http.get("/api/papers/T_1/fixes").json() == []
    fixes.write_fixes(paper_dir, [GOOD])
    assert [p.marks for p in fixes.rebuild(paper_dir, _paper(), lambda p: p).questions[0].parts] == [2, 3]


def test_the_reviewer_cannot_send_notes_or_unknown_edits(client):
    http, _ = client
    assert http.post("/api/papers/T_1/edit", json={"steps": [{"op": "dismiss"}]}).status_code == 400
    assert http.post("/api/papers/T_1/edit", json={"steps": [{"op": "nonsense"}]}).status_code == 400
    assert http.post("/api/papers/T_1/edit", json={"steps": []}).status_code == 400
    # a step that doesn't fit the paper is refused, not half-applied
    bad = {"steps": [{"op": "link", "part_id": "9.z", "entry_id": "ms.c"}]}
    assert http.post("/api/papers/T_1/edit", json=bad).status_code == 400


def test_a_question_the_parser_missed_is_added_by_hand(tmp_path):
    """0452/11 ON16: the scheme had Q6 rows and the paper as read had no Q6 - the Reviewer
    adds it from a box drawn on the question paper, then links its rows."""
    box = {"page": 18, "x0": 50, "y0": 60, "x1": 540, "y1": 700}
    add = Fix(steps=[Step(op="add_question", question_number=6, value=13, fields={"segments": [box]})])
    paper = fixes.apply_fix(_paper(), add)
    assert [q.question_number for q in paper.questions] == [1, 6]
    (part,) = paper.questions[1].parts
    assert (part.id, part.path, part.marks, part.segments[0].page) == ("6.", "", 13, 18)

    with pytest.raises(fixes.FixError):  # a number already used
        fixes.apply_fix(paper, Fix(steps=[Step(op="add_question", question_number=6, fields={"segments": [box]})]))
    with pytest.raises(fixes.FixError):  # nothing drawn
        fixes.apply_fix(_paper(), Fix(steps=[Step(op="add_question", question_number=2)]))

    fixes.write_fixes(tmp_path, [add])  # kept across re-extraction
    rebuilt = fixes.rebuild(tmp_path, _paper(), lambda p: p)
    assert [q.question_number for q in rebuilt.questions] == [1, 6]
