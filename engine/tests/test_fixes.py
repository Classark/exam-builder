"""Fixes to an extracted paper (qbx/fixes.py) and Claude reviewing issues (qbx/ai_fix.py)."""

import sys

import pymupdf
import pytest
from fastapi.testclient import TestClient

from qbx import ai_fix, fixes, llm
from qbx.anchors import decode_shifted_text
from qbx.fixes import Fix, Step
from qbx.schema import MarkSchemeEntry, Paper, Question, QuestionPart, QuestionPartMarkScheme
from qbx.validate import validate_paper


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


def test_a_fix_is_tried_on_a_copy_and_judged_by_the_checks():
    paper = _paper()
    before = _keys(paper)
    trial, clears, raises = fixes.verify(paper, GOOD, set())
    assert validate_paper(trial).passed and set(clears) == before and raises == []
    assert paper.questions[0].parts[1].marks == 0  # the paper itself is untouched

    bad = Fix(steps=[Step(op="set_marks", part_id="1.a", value=9)])
    _, clears, raises = fixes.verify(paper, bad, set())
    assert clears == [] and not fixes.safe_to_apply(bad, clears, raises)  # fixes nothing

    assert fixes.verify(paper, Fix(steps=[Step(op="set_marks", part_id="9.z", value=1)]), set())[0] is None


def test_a_dismissal_never_applies_itself():
    note = Fix(steps=[Step(op="dismiss")], issues=sorted(_keys(_paper())))
    _, clears, raises = fixes.verify(_paper(), note, set())
    assert clears and not fixes.safe_to_apply(note, clears, raises)


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


def _fake(review: dict):
    return llm.FakeLLM(lambda prompt, schema: review)


def _review(paper_dir):
    keys = sorted(_keys(_paper()))
    return {
        "fixes": [
            {"issues": keys, "reason": "the row labelled (c) answers (b); its codes M1 M1 A1 make 3",
             "steps": [{"op": "relabel_entry", "entry_id": "ms.c", "path": "b"},
                       {"op": "set_marks", "part_id": "1.b", "value": 3}]},
            {"issues": [], "reason": "wrong", "steps": [{"op": "set_marks", "part_id": "1.a", "value": 9}]},
        ],
        "notes": [{"issues": [keys[0]], "kind": "needs_person", "reason": "check the figure"}],
    }


@pytest.mark.parametrize("mode", ["auto", "review"])
def test_claudes_fixes_apply_themselves_only_in_auto_mode_and_only_when_checked(tmp_path, mode):
    paper_dir = tmp_path / "T_1"
    paper_dir.mkdir()
    (paper_dir / "paper.json").write_text(_paper().model_dump_json(), encoding="utf-8")
    saved = []
    counts = ai_fix.review_paper(_fake(_review(paper_dir)), paper_dir, mode, save=saved.append)
    recorded = fixes.read_fixes(paper_dir)
    if mode == "auto":
        assert counts == {"applied": 1, "suggested": 1, "notes": 1}
        assert validate_paper(saved[-1]).passed
    else:
        assert counts == {"applied": 0, "suggested": 2, "notes": 1} and not saved
    assert [f.status for f in recorded][-1] == "suggested"  # the note waits for a person
    assert fixes.read_base(paper_dir) is not None


def test_the_prompt_shows_the_rows_codes_and_what_is_linked():
    paper = _paper()
    prompt = ai_fix.build_prompt(paper, fixes.open_issues(paper, set()), [])
    assert "row id=ms.c Q1 label=c marks=3 (linked to nothing)" in prompt
    assert "M1 for" in prompt and "part id=1.b path=b marks=0 rows=[none]" in prompt


def test_the_prompt_shows_a_row_filed_under_the_next_question():
    """A row the parser filed under the wrong question number is shown with the question
    it's next to - only its own question's rows shown, Claude answered "no row was shown"."""
    paper = _paper()
    paper.mark_scheme.append(
        MarkSchemeEntry(id="ms.2b", paper_key="T_1", question_number=2, part_path="b", answer_text="y = 7", marks=3)
    )
    prompt = ai_fix.build_prompt(paper, fixes.open_issues(paper, set()), [])
    assert "row id=ms.2b Q2 label=b marks=3 (linked to nothing)" in prompt


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


def test_a_suggestion_is_accepted_or_rejected(client):
    http, paper_dir = client
    suggestion = GOOD.model_copy(update={"status": "suggested", "source": "ai"})
    fixes.write_fixes(paper_dir, [suggestion])
    assert http.post(f"/api/papers/T_1/fixes/{suggestion.id}/accept").json()["status"] == "applied"
    assert http.get("/api/papers/T_1/report").json()["passed"]


def test_apply_all_applies_every_waiting_suggestion_and_leaves_notes_for_a_person(client):
    http, paper_dir = client
    suggestion = GOOD.model_copy(update={"status": "suggested", "source": "ai"})
    note = Fix(steps=[Step(op="needs_person")], issues=suggestion.issues, reason="", source="ai", status="suggested")
    fixes.write_fixes(paper_dir, [suggestion, note])
    assert http.post("/api/fixes/accept_all").json() == {"applied": 1, "didnt_fit": 0, "busy": [], "notes": 1}
    assert [f.status for f in fixes.read_fixes(paper_dir)] == ["applied", "suggested"]
    assert http.get("/api/papers/T_1/report").json()["passed"]


def test_shifted_font_text_is_decoded():
    # 4MA0 1F January 2018 Q21: "(a) Solve the equation" stored 29 code points down
    blocks = [{"lines": [{"spans": [
        {"text": "\x0bD\x0c\x036ROYH\x03WKH\x03HTXDWLRQ"},
        {"text": "Answer\x07(2)"},  # ordinary prose with a control character stays
    ]}]}]
    spans = decode_shifted_text(blocks)[0]["lines"][0]["spans"]
    assert spans[0]["text"] == "(a) Solve the equation"
    assert spans[1]["text"] == "Answer\x07(2)"


def _run_together() -> Paper:
    """Q21 as 4MA1 extracted it: (a) missed and run into (b), whose row it also answers.
    Printed total 7: (a) 3, (b) 2, (c) 2."""
    from qbx.schema import BBox

    parts = [
        QuestionPart(id="21.b", question_id="21", path="b", depth=1, image_path="", marks=2,
                     text="Solve the equation ... y = (3) (b) Write down the inequality (2)",
                     segments=[BBox(page=10, x0=70, y0=60, x1=500, y1=410)]),
        QuestionPart(id="21.c", question_id="21", path="c", depth=1, image_path="", marks=2,
                     text="(c) Solve the inequality (2)", segments=[BBox(page=10, x0=70, y0=415, x1=500, y1=590)]),
    ]
    rows = [MarkSchemeEntry(id=f"ms.{p}", paper_key="T_2", question_number=21, part_path=p, answer_text=a, marks=m)
            for p, a, m in (("a", "1.25", 3), ("b", "-3 < x <= 4", 2), ("c", "m >= -2.5", 2))]
    return Paper(
        paper_key="T_2", board="Edexcel", level="International GCSE", subject="4MA1", paper="1",
        variant="F", session="JUN", year=24, cover_total_marks=7,
        questions=[Question(paper_key="T_2", board="Edexcel", subject="4MA1", question_number=21,
                            stem_text="", stem_image_path="", total_marks=7, parts=parts)],
        mark_scheme=rows,
        joins=[QuestionPartMarkScheme(part_id="21.b", mark_scheme_entry_id="ms.b"),
               QuestionPartMarkScheme(part_id="21.c", mark_scheme_entry_id="ms.c")],
    )


def test_claude_splits_a_part_the_parser_ran_into_its_neighbour(tmp_path):
    paper = _run_together()
    keys = sorted(k for k in _keys(paper) if "|21|" in k)
    review = {"fixes": [{"issues": keys, "reason": "(a) is in 21.b above (b)'s line", "steps": [
        {"op": "split_part", "part_id": "21.b", "at": {"page": 10, "y": 243}, "path": "b", "value": 2,
         "fields": {"text": "(b) Write down the inequality (2)"}},
        {"op": "edit_part", "part_id": "21.b", "fields": {"path": "a", "marks": 3, "text": "Solve the equation (3)"}},
        {"op": "unlink", "part_id": "21.b", "entry_id": "ms.b"},
        {"op": "link", "part_id": "21.b", "entry_id": "ms.a"},
        {"op": "link", "part_id": "21.b-split", "entry_id": "ms.b"},
    ]}]}
    paper_dir = tmp_path / "T_2"
    paper_dir.mkdir()
    (paper_dir / "paper.json").write_text(paper.model_dump_json(), encoding="utf-8")
    saved = []
    assert ai_fix.review_paper(_fake(review), paper_dir, "auto", save=saved.append)["applied"] == 1
    fixed = saved[-1]
    assert not {k for k in _keys(fixed) if "|21|" in k}
    a, b, c = fixed.questions[0].parts
    assert [(p.path, p.marks) for p in (a, b, c)] == [("a", 3), ("b", 2), ("c", 2)]
    assert (a.segments[0].y1, b.segments[0].y0) == (243, 243)


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


def test_the_prompt_offers_every_edit_and_the_lines_to_place_them():
    paper = _run_together()
    lines = {21: ["p10 y60: 21 (a) Solve the equation", "p10 y243: (b) Write down the inequality"]}
    prompt = ai_fix.build_prompt(paper, fixes.open_issues(paper, set()), [], lines)
    assert "p10 y243: (b) Write down" in prompt and "region=[p10 x70-500 y60-410]" in prompt
    for op in ("split_part", "add_part", "delete_part", "set_entry_marks", "renumber_question"):
        assert op in prompt


def test_claudes_fix_for_a_mistake_the_parser_no_longer_makes_is_not_reapplied():
    """4MA0 3H January 2017: with 12(b)(i)'s scheme row misread, Claude set the part to 0;
    once the parser read the row right, re-applying that broke a correct question."""
    fixed = _paper()
    fixed = fixes.apply_fix(fixed, GOOD)  # the parser now gets this right on its own
    assert validate_paper(fixed).passed
    stale = Fix(steps=[Step(op="set_marks", part_id="1.b", value=0)], source="ai",
                issues=["part_sum_matches_total|1|None"])
    assert validate_paper(fixes.apply_all(fixed, [stale])).passed
    # still needed while the issue is there
    assert fixes.apply_all(_paper(), [Fix(steps=GOOD.steps, source="ai", issues=sorted(_keys(_paper())))]) \
        .questions[0].parts[1].marks == 3
    # a teacher's own edit always applies
    mine = Fix(steps=[Step(op="set_marks", part_id="1.b", value=0)], source="manual")
    assert fixes.apply_all(fixed, [mine]).questions[0].parts[1].marks == 0


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


def test_claude_can_link_a_part_to_a_stretch_of_the_mark_scheme(tmp_path):
    """A part whose row the parser lost: Claude names the stretch of the scheme page, and the
    row is read from there like a teacher's drawn box - never written by Claude."""
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


def test_the_prompt_shows_the_mark_scheme_page_itself(tmp_path):
    """The scheme's own lines, read from the PDF - what Claude needs to find a lost row."""
    paper = _paper()
    paper.source_ms_path = _scheme_pdf(tmp_path / "ms.pdf")
    lines = ai_fix.scheme_lines(paper, {1})
    prompt = ai_fix.build_prompt(paper, fixes.open_issues(paper, set()), [], {}, lines)
    assert "MARK SCHEME LINES" in prompt and "1(b)  y = 7" in prompt.replace("1(b) y", "1(b)  y")
    assert "link_region" in prompt


def test_claude_looks_again_after_a_round_that_applied_something(tmp_path):
    """One fix can make the next possible: after a round that applied something, a second
    look at what's still open, on the paper as it now is."""
    paper_dir = tmp_path / "T_1"
    paper_dir.mkdir()
    (paper_dir / "paper.json").write_text(_paper().model_dump_json(), encoding="utf-8")
    keys = fixes.open_issues(_paper(), set())
    first = [k for k, i in keys.items() if i["check"] in ("part_has_mark_scheme_entry", "no_orphaned_mark_scheme_entries")]
    rounds = [
        {"fixes": [{"issues": first, "reason": "(c) is (b)'s row",
                    "steps": [{"op": "relabel_entry", "entry_id": "ms.c", "path": "b"}]}]},
        {"fixes": [{"issues": [k for k in keys if k not in first], "reason": "its codes make 3",
                    "steps": [{"op": "set_marks", "part_id": "1.b", "value": 3}]}]},
    ]
    fake = llm.FakeLLM(lambda prompt, schema: rounds[len(fake.calls) - 1])
    saved = []
    counts = ai_fix.review_paper(fake, paper_dir, "auto", save=saved.append)
    assert len(fake.calls) == 2 and counts["applied"] == 2
    assert all(k not in fake.calls[1][0] for k in first)  # round two sees only what's left
    assert validate_paper(saved[-1]).passed


def test_fix_all_asks_again_about_an_older_fixers_notes_and_everything_when_told(client):
    """A note the fixer of now made answers its issues for "Fix all"; one an older fixer made
    (before it could see the scheme's pages) doesn't. "Everything" asks about all of them
    again - except fixes the teacher rejected."""
    import main as backend

    _, paper_dir = client
    model = llm.load_settings().fix_model
    keys = sorted(_keys(_paper()))
    note = Fix(steps=[Step(op="needs_person")], issues=keys, source="ai", status="suggested", model=model,
               fixer=ai_fix.FIXER_VERSION)
    fixes.write_fixes(paper_dir, [note])
    assert backend._papers_with_issues() == []
    assert backend._papers_with_issues(everything=True) == ["T_1"]
    fixes.write_fixes(paper_dir, [note.model_copy(update={"fixer": ai_fix.FIXER_VERSION - 1})])
    assert backend._papers_with_issues() == ["T_1"]
    fixes.write_fixes(paper_dir, [note.model_copy(update={"status": "rejected"})])
    assert backend._papers_with_issues(everything=True) == []
