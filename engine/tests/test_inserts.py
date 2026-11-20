"""Inserts - a paper's case study booklet (Business Studies 0450 Paper 2), printed in a
composed paper just before the first question taken from it (user-decided)."""

import pymupdf

from qbx import inserts
from qbx.quiz_export import _insert_labels
from qbx.scan import plan_scan
from tests.test_main import client  # noqa: F401 - the fixture
from tests.test_pairing import _ms, _pdf, _qp


def _insert(path, code="0450/21", sitting="May/June 2018"):
    return _pdf(
        path,
        f"Cambridge IGCSE\nBUSINESS STUDIES {code}\nPaper 2\nINSERT\n{sitting}\n"
        "READ THESE INSTRUCTIONS FIRST\nThis Insert contains the case study material.",
        "Value Gym (VG)\nPeter is a qualified fitness instructor.\nAppendix 1",
    )


def test_an_insert_goes_with_its_question_paper(tmp_path):
    _qp(tmp_path / "qp.pdf", code="0450/21", sitting="May/June 2018")
    _ms(tmp_path / "ms.pdf", code="0450/21", sitting="May/June 2018")
    _insert(tmp_path / "in.pdf")
    _insert(tmp_path / "lonely_in.pdf", code="0450/22")

    plan = plan_scan(tmp_path)

    assert [p.paper_key for p in plan.pairs] == ["0450_21_MJ_18"]
    assert {k: p.name for k, p in plan.inserts.items()} == {"0450_21_MJ_18": "in.pdf"}
    reasons = {u.path.name: u.reason for u in plan.unpaired}
    assert reasons == {"lonely_in.pdf": "insert for 0450_22_MJ_18 - its question paper isn't in the folder"}


def test_the_index_finds_an_insert_by_its_paper(tmp_path):
    pdf = _insert(tmp_path / "in.pdf")
    inserts.save_index(tmp_path / "out", {"0450_21_MJ_18": pdf})
    assert inserts.insert_for(tmp_path / "out", "0450_21_MJ_18") == str(pdf)
    assert inserts.insert_for(tmp_path / "out", "0450_22_MJ_18") is None


def test_an_insert_prints_its_material_not_its_cover(tmp_path):
    pdf = _insert(tmp_path / "in.pdf")
    with pymupdf.open(pdf) as doc:
        page = doc.new_page(width=595, height=842)
        page.insert_text((250, 100), "BLANK PAGE", fontsize=10)
        doc.saveIncr()
    regions = inserts.content_pages(str(pdf))
    assert [r.page for r in regions] == [1]


def test_one_copy_before_the_questions_that_share_it():
    a, b = "a_insert.pdf", "b_insert.pdf"
    labels = _insert_labels([a, a, None, b, a], [1, 2, 3, 4, 5])
    assert labels == [
        "Insert for Questions 1-2", None, None, "Insert for Question 4", "Insert for Question 5",
    ]


def test_the_cover_says_when_a_paper_comes_with_an_insert(tmp_path):
    with_one = _pdf(tmp_path / "p2.pdf", "BUSINESS STUDIES 0450/21\nYou will need: Insert (enclosed)\nThe insert contains the case study.")
    older = _pdf(tmp_path / "p2_2016.pdf", "This document consists of 9 printed pages, 3 blank pages and 1 Insert.\nThe Insert contains the case study.")
    without = _pdf(tmp_path / "p1.pdf", "BUSINESS STUDIES 0450/11\nAnswer all questions.")
    assert inserts.cover_needs_insert(with_one) and inserts.cover_needs_insert(older)
    assert not inserts.cover_needs_insert(without)
    # listed but with nothing to print: 0620 Paper 5's filler page (0620/51 MJ18), 0455
    # Paper 2's answer booklet
    practical = _pdf(tmp_path / "p5.pdf", "CHEMISTRY 0620/51\nThis document consists of 11 printed pages, 1 blank page and 1 Insert.")
    enclosed = _pdf(tmp_path / "p5_2023.pdf", "CHEMISTRY 0620/52\nInsert (enclosed)")
    assert not inserts.cover_needs_insert(practical) and not inserts.cover_needs_insert(enclosed)


def _upload(client, tmp_path, insert_file):
    from tests.test_main import _make_uploadable_ms, _make_uploadable_qp

    qp, ms = tmp_path / "incoming_qp.pdf", tmp_path / "incoming_ms.pdf"
    _make_uploadable_qp(qp)
    _make_uploadable_ms(ms)
    with open(qp, "rb") as q, open(ms, "rb") as m, open(insert_file, "rb") as i:
        return client.post(
            "/api/papers/upload",
            files={
                "qp_file": ("incoming_qp.pdf", q, "application/pdf"),
                "ms_file": ("incoming_ms.pdf", m, "application/pdf"),
                "insert_file": (insert_file.name, i, "application/pdf"),
            },
        )


def test_an_insert_uploaded_with_its_paper_is_attached(client, tmp_path):
    response = _upload(client, tmp_path, _insert(tmp_path / "case_study.pdf", code="9702/42", sitting="May/June 2023"))
    assert response.status_code == 200
    listed = {p["paper_key"]: p for p in client.get("/api/papers").json()}
    assert listed["9702_42_MJ_23"]["has_insert"] is True


def test_another_paper_s_insert_is_refused(client, tmp_path):
    response = _upload(client, tmp_path, _insert(tmp_path / "other.pdf", code="9702/41", sitting="May/June 2023"))
    assert response.status_code == 400
    assert "9702_41_MJ_23" in response.json()["detail"]
    listed = {p["paper_key"]: p for p in client.get("/api/papers").json()}
    assert listed["9702_42_MJ_23"]["has_insert"] is False  # the paper itself stays
