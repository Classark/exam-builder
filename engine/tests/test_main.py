import sys
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from qbx.schema import BBox, MarkSchemeEntry, Paper, Question, QuestionPart, QuestionPartMarkScheme
from qbx.validate import validate_paper


def _make_source_pdf(path):
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    doc.new_page(width=595, height=842)
    doc.save(str(path))
    doc.close()


def _make_paper(source_pdf_path, extra_parts: list[QuestionPart] | None = None) -> Paper:
    part = QuestionPart(
        id="1.a", question_id="1", path="a", depth=1, text="Explain X. [2]",
        image_path="assets/q1a.svg", marks=2,
        segments=[BBox(page=0, x0=0, y0=100, x1=595, y1=200)],
    )
    parts = [part, *(extra_parts or [])]
    question = Question(
        paper_key="TEST_1", board="Cambridge", subject="0000", question_number=1,
        stem_text="", stem_image_path="", total_marks=sum(p.marks for p in parts), parts=parts,
    )
    # a mark scheme entry + join per part, so the fixture is a genuinely clean paper -
    # validate_paper's part_has_mark_scheme_entry check would otherwise always fail here.
    mark_scheme = [
        MarkSchemeEntry(
            id=f"ms.{p.id}", paper_key="TEST_1", question_number=1, part_path=p.path,
            answer_text="", marks=p.marks,
        )
        for p in parts
    ]
    joins = [
        QuestionPartMarkScheme(part_id=p.id, mark_scheme_entry_id=f"ms.{p.id}") for p in parts
    ]
    return Paper(
        paper_key="TEST_1", board="Cambridge", level="IGCSE", subject="0000", paper="1",
        variant="1", session="M/J", year=24, source_qp_path=str(source_pdf_path),
        questions=[question], mark_scheme=mark_scheme, joins=joins,
    )


def _write_paper(tmp_path, paper: Paper) -> None:
    paper_dir = tmp_path / paper.paper_key
    paper_dir.mkdir(exist_ok=True)
    (paper_dir / "paper.json").write_text(paper.model_dump_json(indent=2), encoding="utf-8")
    (paper_dir / "report.json").write_text(
        validate_paper(paper).model_dump_json(indent=2), encoding="utf-8"
    )
    (paper_dir / "assets").mkdir(exist_ok=True)
    (paper_dir / "assets" / "q1a.svg").write_text("<svg></svg>", encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path))
    monkeypatch.setenv("QBX_QUIZZES_DIR", str(tmp_path / "quizzes"))
    sys.modules.pop("main", None)
    import main as backend

    source_pdf_path = tmp_path / "source.pdf"
    _make_source_pdf(source_pdf_path)
    _write_paper(tmp_path, _make_paper(source_pdf_path))

    return TestClient(backend.app)


@pytest.fixture
def client_with_two_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path))
    sys.modules.pop("main", None)
    import main as backend

    source_pdf_path = tmp_path / "source.pdf"
    _make_source_pdf(source_pdf_path)
    second_part = QuestionPart(
        id="1.b", question_id="1", path="b", depth=1, text="Explain Y. [3]",
        image_path="", marks=3, segments=[],
    )
    _write_paper(tmp_path, _make_paper(source_pdf_path, extra_parts=[second_part]))

    return TestClient(backend.app)


def test_list_papers(client):
    response = client.get("/api/papers")
    assert response.status_code == 200
    [summary] = response.json()
    assert {k: summary[k] for k in ("paper_key", "passed", "n_issues", "n_open", "n_questions")} == {
        "paper_key": "TEST_1", "passed": True, "n_issues": 0, "n_open": 0, "n_questions": 1
    }


def test_get_paper(client):
    response = client.get("/api/papers/TEST_1")
    assert response.status_code == 200
    assert response.json()["questions"][0]["parts"][0]["marks"] == 2


def test_get_paper_404(client):
    assert client.get("/api/papers/NOPE").status_code == 404


def test_get_page_image(client):
    response = client.get("/api/papers/TEST_1/page/0")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_get_page_image_out_of_range(client):
    assert client.get("/api/papers/TEST_1/page/99").status_code == 404


def test_get_asset(client):
    response = client.get("/api/papers/TEST_1/assets/q1a.svg")
    assert response.status_code == 200
    assert "<svg" in response.text


def test_get_asset_path_traversal_rejected(client):
    response = client.get("/api/papers/TEST_1/assets/../../paper.json")
    assert response.status_code in (403, 404)


def test_update_part_marks(client):
    response = client.patch("/api/papers/TEST_1/parts/1.a", json={"marks": 5})
    assert response.status_code == 200
    assert response.json()["marks"] == 5

    paper = client.get("/api/papers/TEST_1").json()
    assert paper["questions"][0]["total_marks"] == 5


def test_update_part_404(client):
    response = client.patch("/api/papers/TEST_1/parts/nope", json={"marks": 5})
    assert response.status_code == 404


def test_merge_parts(client_with_two_parts):
    client = client_with_two_parts
    response = client.post("/api/papers/TEST_1/parts/1.a/merge")
    assert response.status_code == 200
    merged = response.json()
    assert merged["marks"] == 5
    assert "Explain X" in merged["text"] and "Explain Y" in merged["text"]

    paper = client.get("/api/papers/TEST_1").json()
    assert len(paper["questions"][0]["parts"]) == 1


def _make_uploadable_qp(path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "9702/42/M/J/23")  # fingerprint footer code
    page.insert_text((50, 200), "1")
    page.insert_text((72, 220), "(a)")
    page.insert_text((72, 240), "Explain X. [2]")
    doc.save(str(path))
    doc.close()


def _make_uploadable_ms(path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "1(a)")
    page.insert_text((150, 100), "Explanation of X")
    page.insert_text((500, 100), "2")
    doc.save(str(path))
    doc.close()


def test_open_papers_folder_creates_and_opens_it(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("QBX_PAPERS_DIR", str(tmp_path / "papers"))
    sys.modules.pop("main", None)
    import main as backend

    opened = []
    monkeypatch.setattr(backend.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(backend.subprocess, "run", lambda cmd, **kw: opened.append(Path(cmd[-1])))

    response = TestClient(backend.app).post("/api/papers/open_folder")

    assert response.status_code == 200
    assert (tmp_path / "papers").is_dir()
    assert [Path(p) for p in opened] == [(tmp_path / "papers").resolve()]


def test_upload_runs_extraction_and_lists_the_new_paper(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("QBX_PAPERS_DIR", str(tmp_path / "papers"))
    sys.modules.pop("main", None)
    import main as backend

    client = TestClient(backend.app)

    qp_path = tmp_path / "incoming_qp.pdf"
    ms_path = tmp_path / "incoming_ms.pdf"
    _make_uploadable_qp(qp_path)
    _make_uploadable_ms(ms_path)

    with open(qp_path, "rb") as qp_f, open(ms_path, "rb") as ms_f:
        response = client.post(
            "/api/papers/upload",
            files={
                "qp_file": ("incoming_qp.pdf", qp_f, "application/pdf"),
                "ms_file": ("incoming_ms.pdf", ms_f, "application/pdf"),
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["paper_key"] == "9702_42_MJ_23"
    assert body["n_questions"] == 1

    listed = client.get("/api/papers").json()
    assert any(p["paper_key"] == "9702_42_MJ_23" for p in listed)

    # the uploaded files land in papers/, sanitised, so future page-image requests work
    assert (tmp_path / "papers" / "incoming_qp.pdf").exists()


def test_upload_rejects_scanned_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("QBX_PAPERS_DIR", str(tmp_path / "papers"))
    sys.modules.pop("main", None)
    import main as backend

    client = TestClient(backend.app)

    blank_pdf = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)  # no text layer at all
    doc.save(str(blank_pdf))
    doc.close()

    with open(blank_pdf, "rb") as qp_f, open(blank_pdf, "rb") as ms_f:
        response = client.post(
            "/api/papers/upload",
            files={
                "qp_file": ("blank.pdf", qp_f, "application/pdf"),
                "ms_file": ("blank.pdf", ms_f, "application/pdf"),
            },
        )

    assert response.status_code == 400


def test_split_part(client):
    response = client.post(
        "/api/papers/TEST_1/parts/1.a/split", json={"page": 0, "y": 150}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["first"]["segments"][0]["y1"] == 150
    assert body["second"]["segments"][0]["y0"] == 150
    assert body["second"]["id"] == "1.a-split"


def test_list_questions_groups_parts_by_question(client):
    response = client.get("/api/questions")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["paper_key"] == "TEST_1"
    assert body[0]["question_number"] == 1
    assert body[0]["parts"] == [
        {"path": "a", "marks": 2, "text": "Explain X. [2]", "image_path": "assets/q1a.svg"}
    ]


def test_list_questions_text_search(client):
    assert len(client.get("/api/questions", params={"q": "Explain X"}).json()) == 1
    assert len(client.get("/api/questions", params={"q": "nonexistent phrase"}).json()) == 0


def test_list_questions_subject_filter(client):
    assert len(client.get("/api/questions", params={"subject": "0000"}).json()) == 1
    assert len(client.get("/api/questions", params={"subject": "9999"}).json()) == 0


def test_quiz_create_list_get(client):
    create = client.post("/api/quizzes", json={"name": "My Quiz"})
    assert create.status_code == 200
    assert create.json() == {"name": "My Quiz", "items": [], "request": None, "duration_minutes": None, "chat": [], "seen": []}

    listed = client.get("/api/quizzes").json()
    assert listed == [{"name": "My Quiz", "n_items": 0, "from_request": False, "has_chat": False, "questions": []}]

    fetched = client.get("/api/quizzes/My Quiz").json()
    assert fetched == {"name": "My Quiz", "items": [], "total_marks": 0, "request": None, "duration_minutes": None, "chat": []}


def test_quiz_duplicate_name_rejected(client):
    client.post("/api/quizzes", json={"name": "Dup"})
    response = client.post("/api/quizzes", json={"name": "Dup"})
    assert response.status_code == 400


def test_quiz_update_resolves_items_with_fresh_marks(client):
    client.post("/api/quizzes", json={"name": "Q1"})
    update = client.put(
        "/api/quizzes/Q1",
        json={"items": [{"paper_key": "TEST_1", "question_number": 1, "part_paths": ["a"]}]},
    )
    assert update.status_code == 200

    fetched = client.get("/api/quizzes/Q1").json()
    assert fetched["total_marks"] == 2
    assert fetched["items"][0]["parts"][0]["text"] == "Explain X. [2]"
    assert fetched["items"][0]["missing"] is False
    assert fetched["items"][0]["parts"][0]["missing"] is False


def test_quiz_item_referencing_missing_part_is_flagged_not_crashed(client):
    client.post("/api/quizzes", json={"name": "Q2"})
    client.put(
        "/api/quizzes/Q2",
        json={"items": [{"paper_key": "TEST_1", "question_number": 1, "part_paths": ["zzz"]}]},
    )

    fetched = client.get("/api/quizzes/Q2").json()
    assert fetched["items"][0]["missing"] is False  # the question itself resolved fine
    assert fetched["items"][0]["parts"][0]["missing"] is True
    assert fetched["total_marks"] == 0


def test_quiz_item_referencing_deleted_paper_is_flagged_not_crashed(client):
    client.post("/api/quizzes", json={"name": "Q3"})
    client.put(
        "/api/quizzes/Q3",
        json={"items": [{"paper_key": "NOPE", "question_number": 1, "part_paths": ["a"]}]},
    )

    response = client.get("/api/quizzes/Q3")
    assert response.status_code == 200
    assert response.json()["items"][0]["missing"] is True


def test_quiz_get_404(client):
    assert client.get("/api/quizzes/nope").status_code == 404


def test_quiz_delete(client):
    client.post("/api/quizzes", json={"name": "ToDelete"})
    response = client.delete("/api/quizzes/ToDelete")
    assert response.status_code == 200
    assert client.get("/api/quizzes/ToDelete").status_code == 404


def test_quiz_items_say_which_are_multiple_choice(client, tmp_path):
    """The builder lists multiple-choice questions in a section of their own, after the
    written ones, the way the paper prints them - so each item says which it is."""
    source = tmp_path / "source.pdf"
    mcq = _make_paper(source).model_copy(
        update={"paper_key": "MCQ_1", "paper_title": "Paper 1 Multiple Choice (Core)"}
    )
    question = mcq.questions[0]
    part = question.parts[0].model_copy(update={"id": "1.", "path": "", "marks": 1, "text": "Which? A B C D"})
    mcq = mcq.model_copy(update={"questions": [question.model_copy(update={"paper_key": "MCQ_1", "parts": [part]})]})
    _write_paper(tmp_path, mcq)

    client.post("/api/quizzes", json={"name": "Mixed"})
    client.put(
        "/api/quizzes/Mixed",
        json={"items": [
            {"paper_key": "MCQ_1", "question_number": 1, "part_paths": [""]},
            {"paper_key": "TEST_1", "question_number": 1, "part_paths": ["a"]},
        ]},
    )
    items = client.get("/api/quizzes/Mixed").json()["items"]
    assert [(i["paper_key"], i["mcq"]) for i in items] == [("MCQ_1", True), ("TEST_1", False)]


def test_quiz_list_names_each_exams_questions(client):
    """So the builder can warn that a question was already used in another exam."""
    for name in ("Mock 1", "Mock 2"):
        client.post("/api/quizzes", json={"name": name})
    client.put(
        "/api/quizzes/Mock 1",
        json={"items": [{"paper_key": "TEST_1", "question_number": 1, "part_paths": ["a"]}]},
    )
    listed = {q["name"]: q["questions"] for q in client.get("/api/quizzes").json()}
    assert listed == {"Mock 1": ["TEST_1::1"], "Mock 2": []}


def test_quiz_sources_list_downloads_as_its_own_pdf(client):
    client.post("/api/quizzes", json={"name": "Mock"})
    client.put(
        "/api/quizzes/Mock",
        json={"items": [{"paper_key": "TEST_1", "question_number": 1, "part_paths": ["a"]}]},
    )
    response = client.get("/api/quizzes/Mock/export_sources.pdf")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert 'filename="Mock_sources.pdf"' in response.headers["content-disposition"]
    assert client.get("/api/quizzes/Missing/export_sources.pdf").status_code == 404


def test_quiz_time_allowance_is_saved_with_the_exam(client):
    client.post("/api/quizzes", json={"name": "Timed"})
    assert client.get("/api/quizzes/Timed").json()["duration_minutes"] is None
    assert client.put("/api/quizzes/Timed/duration", json={"minutes": 75}).json()["duration_minutes"] == 75
    # editing the questions keeps it
    client.put("/api/quizzes/Timed", json={"items": [{"paper_key": "TEST_1", "question_number": 1, "part_paths": ["a"]}]})
    assert client.get("/api/quizzes/Timed").json()["duration_minutes"] == 75
    # and the question paper's cover prints it without being told again
    import pymupdf
    pdf = client.get("/api/quizzes/Timed/export_qp.pdf").content
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        assert "1 hour 15 minutes" in doc[0].get_text()
    assert client.put("/api/quizzes/Timed/duration", json={"minutes": ""}).json()["duration_minutes"] is None


def test_deleting_a_paper_removes_it_and_its_pdfs_from_the_papers_folder(client, tmp_path):
    import shutil
    import sys

    backend = sys.modules["main"]
    papers = tmp_path / "papers"
    papers.mkdir()
    backend.PAPERS_DIR = papers
    qp, ms = papers / "t_qp.pdf", papers / "t_ms.pdf"
    shutil.copy(tmp_path / "source.pdf", qp)
    shutil.copy(tmp_path / "source.pdf", ms)
    outside = tmp_path / "elsewhere.pdf"
    shutil.copy(tmp_path / "source.pdf", outside)
    paper = _make_paper(qp).model_copy(update={"paper_key": "DEL_1", "source_ms_path": str(outside)})
    _write_paper(tmp_path, paper)

    client.post("/api/quizzes", json={"name": "Uses it"})
    client.put("/api/quizzes/Uses it", json={"items": [{"paper_key": "DEL_1", "question_number": 1, "part_paths": ["a"]}]})
    assert client.get("/api/papers/DEL_1/used_in").json() == {"exams": ["Uses it"]}

    result = client.delete("/api/papers/DEL_1").json()
    assert result["removed_pdfs"] == ["t_qp.pdf"]
    assert not (tmp_path / "DEL_1").exists() and not qp.exists()
    assert outside.exists()  # never deletes a PDF outside the papers folder
    assert "DEL_1" not in [p["paper_key"] for p in client.get("/api/papers").json()]
    # the exam keeps working, with the question shown as missing
    assert client.get("/api/quizzes/Uses it").json()["items"][0]["missing"] is True
    assert client.delete("/api/papers/DEL_1").status_code == 404


def test_deleting_a_paper_can_keep_its_pdfs(client, tmp_path):
    import shutil
    import sys

    backend = sys.modules["main"]
    backend.PAPERS_DIR = tmp_path
    kept = tmp_path / "keep_qp.pdf"
    shutil.copy(tmp_path / "source.pdf", kept)
    _write_paper(tmp_path, _make_paper(kept).model_copy(update={"paper_key": "KEEP_1"}))
    assert client.delete("/api/papers/KEEP_1?delete_pdfs=false").json()["removed_pdfs"] == []
    assert kept.exists() and not (tmp_path / "KEEP_1").exists()


def test_quiz_rename_keeps_its_questions(client):
    client.post("/api/quizzes", json={"name": "Old"})
    client.put(
        "/api/quizzes/Old",
        json={"items": [{"paper_key": "TEST_1", "question_number": 1, "part_paths": ["a"]}]},
    )
    response = client.post("/api/quizzes/Old/rename", json={"name": "Year 10 mock"})
    assert response.status_code == 200

    assert client.get("/api/quizzes/Old").status_code == 404
    renamed = client.get("/api/quizzes/Year 10 mock").json()
    assert renamed["name"] == "Year 10 mock"
    assert renamed["items"][0]["paper_key"] == "TEST_1"
    assert [q["name"] for q in client.get("/api/quizzes").json()] == ["Year 10 mock"]


def test_quiz_rename_refuses_a_taken_or_empty_name(client):
    client.post("/api/quizzes", json={"name": "A"})
    client.post("/api/quizzes", json={"name": "B"})
    assert client.post("/api/quizzes/A/rename", json={"name": "B"}).status_code == 400
    assert client.post("/api/quizzes/A/rename", json={"name": "  "}).status_code == 400
    assert client.post("/api/quizzes/Missing/rename", json={"name": "C"}).status_code == 404
    # a change of case alone is fine, though on Windows it's the same file
    assert client.post("/api/quizzes/A/rename", json={"name": "a"}).status_code == 200
    assert sorted(q["name"] for q in client.get("/api/quizzes").json()) == ["B", "a"]


def test_quiz_export_produces_separate_qp_and_ms_pdfs(client):
    client.post("/api/quizzes", json={"name": "ExportMe"})
    client.put(
        "/api/quizzes/ExportMe",
        json={"items": [{"paper_key": "TEST_1", "question_number": 1, "part_paths": ["a"]}]},
    )

    qp_response = client.get("/api/quizzes/ExportMe/export_qp.pdf")
    ms_response = client.get("/api/quizzes/ExportMe/export_ms.pdf")

    assert qp_response.status_code == 200
    assert qp_response.headers["content-type"] == "application/pdf"
    assert "ExportMe_qp.pdf" in qp_response.headers["content-disposition"]
    assert qp_response.content.startswith(b"%PDF")

    assert ms_response.status_code == 200
    assert "ExportMe_ms.pdf" in ms_response.headers["content-disposition"]
    assert ms_response.content.startswith(b"%PDF")
    # two distinct files, not the same combined document twice
    assert qp_response.content != ms_response.content


def test_quiz_export_404_for_missing_quiz(client):
    assert client.get("/api/quizzes/nope/export_qp.pdf").status_code == 404
    assert client.get("/api/quizzes/nope/export_ms.pdf").status_code == 404


def test_preview_renders_an_unsaved_selection_as_page_images(client):
    item = {"paper_key": "TEST_1", "question_number": 1, "part_paths": ["a"]}

    full = client.post("/api/preview/qp", json={"items": [item]}).json()["pages"]
    single = client.post(
        "/api/preview/qp", json={"items": [item], "cover": False, "first_number": 3}
    ).json()["pages"]

    assert all(p.startswith("data:image/png;base64,") for p in full)
    # the single-question preview is the same content without the cover page
    assert len(single) == len(full) - 1


def test_preview_of_empty_selection_has_no_pages(client):
    item = {"paper_key": "TEST_1", "question_number": 1, "part_paths": []}
    assert client.post("/api/preview/qp", json={"items": [item]}).json() == {"pages": []}


def test_bank_search_and_one_question(client):
    bank = client.get("/api/bank").json()
    [paper] = bank["papers"]
    assert paper["paper_key"] == "TEST_1"
    assert paper["questions"] == [{"n": 1, "marks": 2, "stem": "Explain X. [2]", "n_parts": 1, "topics": []}]
    assert client.get("/api/search", params={"q": "explain x"}).json() == [{"paper_key": "TEST_1", "question_number": 1}]
    assert client.get("/api/search", params={"q": "nothing like it"}).json() == []
    question = client.get("/api/papers/TEST_1/questions/1").json()
    assert question["parts"] == [{"path": "a", "marks": 2, "text": "Explain X. [2]", "tags": None}]


def test_an_edit_bumps_the_version_the_ui_polls(client):
    before = client.get("/api/state").json()["version"]
    client.patch("/api/papers/TEST_1/parts/1.a", json={"marks": 5})
    assert client.get("/api/state").json()["version"] > before


def test_issues_can_be_marked_checked(client):
    client.patch("/api/papers/TEST_1/parts/1.a", json={"marks": 0})  # now flagged: marks_unknown
    [issue] = [i for i in client.get("/api/papers/TEST_1/report").json()["issues"] if i["check"] == "marks_unknown"]
    assert issue["checked"] is False
    assert client.get("/api/papers").json()[0]["n_open"] >= 1

    client.post("/api/papers/TEST_1/review", json={"keys": [issue["key"]], "checked": True})
    report = client.get("/api/papers/TEST_1/report").json()
    assert next(i for i in report["issues"] if i["key"] == issue["key"])["checked"] is True
    summary = client.get("/api/papers").json()[0]
    assert summary["n_open"] == summary["n_issues"] - 1


def test_ms_rows_without_a_source_scheme_is_empty(client):
    assert client.get("/api/papers/TEST_1/ms_rows").json() == {"rows": {}, "page_count": 0}


def test_one_paper_can_be_reprocessed_from_its_own_pdfs(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("QBX_PAPERS_DIR", str(tmp_path / "papers"))
    sys.modules.pop("main", None)
    import main as backend

    client = TestClient(backend.app)
    qp_path, ms_path = tmp_path / "incoming_qp.pdf", tmp_path / "incoming_ms.pdf"
    _make_uploadable_qp(qp_path)
    _make_uploadable_ms(ms_path)
    with open(qp_path, "rb") as qp_f, open(ms_path, "rb") as ms_f:
        client.post(
            "/api/papers/upload",
            files={"qp_file": ("incoming_qp.pdf", qp_f, "application/pdf"),
                   "ms_file": ("incoming_ms.pdf", ms_f, "application/pdf")},
        )
    # a hand edit since: re-processing goes back to the PDFs, then re-applies it
    part_id = client.get("/api/papers/9702_42_MJ_23").json()["questions"][0]["parts"][0]["id"]
    assert client.patch(f"/api/papers/9702_42_MJ_23/parts/{part_id}", json={"marks": 3}).status_code == 200

    response = client.post("/api/papers/9702_42_MJ_23/reprocess")
    assert response.status_code == 200
    assert response.json()["paper_key"] == "9702_42_MJ_23"
    assert client.get("/api/papers/9702_42_MJ_23").json()["questions"][0]["parts"][0]["marks"] == 3

    assert client.post("/api/papers/NOPE_1/reprocess").status_code == 404
    (tmp_path / "papers" / "incoming_ms.pdf").unlink()
    assert client.post("/api/papers/9702_42_MJ_23/reprocess").status_code == 409


def test_a_scan_extracts_papers_in_parallel_worker_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("QBX_PAPERS_DIR", str(tmp_path / "papers"))
    monkeypatch.setenv("QBX_SCAN_WORKERS", "2")
    sys.modules.pop("main", None)
    import main as backend

    papers = tmp_path / "papers"
    papers.mkdir()
    for code in ("9702/42/M/J/23", "9702/41/M/J/23"):
        name = code.replace("/", "_")
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 90), code)
        page.insert_text((50, 200), "1")
        page.insert_text((72, 220), "(a)")
        page.insert_text((72, 240), "Explain X. [2]")
        doc.save(str(papers / f"{name}_qp.pdf"))
        doc.close()
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 60), f"{code} Mark Scheme")
        page.insert_text((50, 100), "1(a)")
        page.insert_text((150, 100), "Explanation of X")
        page.insert_text((500, 100), "2")
        doc.save(str(papers / f"{name}_ms.pdf"))
        doc.close()

    # paired by hand: these synthetic files don't carry the cover wording pairing reads
    from qbx.scan import PlannedPair, ScanPlan

    plan = ScanPlan(pairs=[
        PlannedPair(paper_key=f"9702_{v}_MJ_23", qp_path=papers / f"9702_{v}_M_J_23_qp.pdf",
                    ms_path=papers / f"9702_{v}_M_J_23_ms.pdf")
        for v in ("41", "42")
    ])
    monkeypatch.setattr(backend, "plan_scan", lambda folder, progress=None, cache_path=None: plan)
    backend._scan_state.update(done=0, processed=[], failed=[], skipped=[], unpaired=[])
    backend._run_scan(False)
    state = backend._scan_state
    assert state["workers"] == 2, {k: state.get(k) for k in ("error", "skipped", "unpaired", "total", "stage")}
    assert sorted(p["paper_key"] for p in state["processed"]) == ["9702_41_MJ_23", "9702_42_MJ_23"], state
    assert state["done"] == 2 and not state["failed"]
    assert (tmp_path / "out" / "9702_41_MJ_23" / "paper.json").exists()


def test_a_scan_that_died_does_not_block_the_next(tmp_path, monkeypatch):
    # a scan thread killed mid-run left "running" set, and every later Scan press did
    # nothing until the server was restarted
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("QBX_PAPERS_DIR", str(tmp_path / "papers"))
    sys.modules.pop("main", None)
    import main as backend
    from fastapi.testclient import TestClient

    backend._scan_state.update(running=True, stage="Pairing the PDFs in the papers folder")
    backend._scan_thread = None
    started = []
    monkeypatch.setattr(backend, "_run_scan", lambda reprocess: started.append(reprocess))

    state = TestClient(backend.app).post("/api/papers/scan").json()

    assert state["stage"] == "Starting"
    assert started == [False]
