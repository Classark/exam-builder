"""Classification mode: topical booklets from the tags parts already carry. Synthetic
fixtures only - no real papers, no model."""

import io
import json
import sys
import time
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient

from qbx import llm as llm_mod
from qbx.classified import ClassifiedSpec, classify, untagged_parts
from qbx.exam_builder import ExamSource
from qbx.schema import BBox, MarkSchemeEntry, Paper, Question, QuestionPart, QuestionPartMarkScheme
from qbx.tagging import TAGS_FORMAT, tag_key
from qbx.topics import Topic

TOPICS = [
    Topic(id="4", name="Electrochemistry"),
    Topic(id="4.1", name="Electrolysis", parent="4"),
    Topic(id="7", name="Chemical reactions"),
    Topic(id="7.2", name="Rate of reaction", parent="7"),
    Topic(id="9", name="Metals"),
]


def _entry(paper_key, year, questions, paper="4", session="M/J", mcq=None, subject="0620", board="Cambridge"):
    """questions: per question, its parts as (path, marks, topics, depends_on); a part with
    topics None is untagged."""
    return (paper_key, {
        "summary": {
            "paper_key": paper_key, "year": year, "paper": paper, "variant": "1", "session": session,
            "subject": subject, "board": board,
        },
        "questions": [{"n": i + 1, "marks": sum(p[1] for p in parts)} for i, parts in enumerate(questions)],
        "parts": [
            [
                {"path": p, "marks": m, "topics": t or [], "depends_on": d, "tagged": t is not None}
                for p, m, t, d in parts
            ]
            for parts in questions
        ],
        "mcq": mcq or [False] * len(questions),
    })


def _spec(*topic_ids, **kw) -> ClassifiedSpec:
    return ClassifiedSpec(
        board="Cambridge", subject="0620", sources=[ExamSource(subject="0620")], topic_ids=list(topic_ids), **kw
    )


def _keys(group) -> list[tuple[str, int, list[str]]]:
    return [(i.paper_key, i.question_number, i.part_paths) for i in group.items]


def test_each_topic_gets_its_questions_newest_first():
    bank = [
        _entry("OLD", 19, [[("a", 3, ["4.1"], [])]]),
        _entry("NEW_MJ", 24, [[("a", 2, ["7.2"], [])], [("a", 4, ["4.1"], [])]]),
        _entry("NEW_ON", 24, [[("a", 5, ["4.1"], [])]], session="O/N"),
    ]
    electro, rates = classify(bank, _spec("4.1", "7.2"), TOPICS)
    assert electro.name == "Electrolysis" and rates.topic_id == "7.2"
    # O/N 2024 is newer than M/J 2024, which is newer than 2019
    assert _keys(electro) == [("NEW_ON", 1, ["a"]), ("NEW_MJ", 2, ["a"]), ("OLD", 1, ["a"])]
    assert electro.total_marks == 12
    assert _keys(rates) == [("NEW_MJ", 1, ["a"])]


def test_a_parent_topic_takes_in_its_subtopics():
    bank = [_entry("A", 24, [[("a", 3, ["4.1"], [])], [("a", 2, ["4"], [])], [("a", 1, ["9"], [])]])]
    (group,) = classify(bank, _spec("4"), TOPICS)
    assert [i.question_number for i in group.items] == [1, 2]


def test_a_part_comes_with_the_parts_it_depends_on():
    bank = [_entry("A", 24, [[("a", 2, ["9"], []), ("b", 3, ["9"], []), ("c", 4, ["4.1"], ["a"])]])]
    (group,) = classify(bank, _spec("4.1"), TOPICS)
    (item,) = group.items
    assert item.part_paths == ["a", "c"] and item.marks == 6 and not item.whole


def test_a_question_on_two_topics_is_split_between_them():
    bank = [_entry("A", 24, [[("a", 3, ["4.1"], []), ("b", 2, ["7.2"], []), ("c", 1, ["7.2"], ["b"])]])]
    electro, rates = classify(bank, _spec("4", "7"), TOPICS)
    assert _keys(electro) == [("A", 1, ["a"])]
    assert _keys(rates) == [("A", 1, ["b", "c"])]


def test_a_reprint_is_printed_once_from_the_newest_paper():
    bank = [
        _entry("P3_23", 23, [[("a", 3, ["4.1"], [])]], paper="3"),
        _entry("P4_24", 24, [[("a", 3, ["4.1"], [])]], paper="4"),
        _entry("P5_22", 22, [[("a", 3, ["4.1"], [])]], paper="5"),
    ]
    duplicates = {("P3_23", 1): 7, ("P4_24", 1): 7, ("P5_22", 1): 7}
    (group,) = classify(bank, _spec("4.1"), TOPICS, duplicates)
    (item,) = group.items
    assert item.paper_key == "P4_24"
    assert item.also_in == ["P3_23", "P5_22"]


def test_question_style_filters_and_multiple_choice_goes_last():
    bank = [_entry("A", 24, [[("", 1, ["4.1"], [])], [("a", 3, ["4.1"], [])]], mcq=[True, False])]
    assert _keys(classify(bank, _spec("4.1", question_style="structured"), TOPICS)[0]) == [("A", 2, ["a"])]
    assert _keys(classify(bank, _spec("4.1", question_style="mcq"), TOPICS)[0]) == [("A", 1, [""])]
    newer_mcq = [_entry("M", 25, [[("", 1, ["4.1"], [])]], mcq=[True]), *bank]
    assert [i.mcq for i in classify(newer_mcq, _spec("4.1"), TOPICS)[0].items] == [False, True, True]


def test_scope_keeps_one_board_its_papers_and_years_and_counts_untagged_parts():
    bank = [
        _entry("C4", 24, [[("a", 3, ["4.1"], []), ("b", 2, None, [])]], paper="4"),
        _entry("C2", 24, [[("a", 3, ["4.1"], [])]], paper="2"),
        _entry("OLD", 15, [[("a", 3, ["4.1"], [])]], paper="4"),
        _entry("E", 24, [[("a", 3, ["4.1"], [])]], board="Edexcel"),
    ]
    spec = ClassifiedSpec(
        board="Cambridge", subject="0620", sources=[ExamSource(subject="0620", papers=["4"])],
        topic_ids=["4.1"], year_from=2018,
    )
    # a question not fully tagged is taken whole, as the exam builder takes it
    assert _keys(classify(bank, spec, TOPICS)[0]) == [("C4", 1, ["a", "b"])]
    assert untagged_parts(bank, spec) == 1


# -- endpoints ---------------------------------------------------------------------------

_PROSE = (
    "Aqueous copper sulfate is electrolysed using inert electrodes and the products at the "
    "anode and cathode are collected and tested carefully by the students. Name each product."
)


def _source_pdf(path, lines: list[str]) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for i, text in enumerate(lines):
        page.insert_text((72, 100 + 60 * i), text)
    doc.save(str(path))
    doc.close()


def _write(out, key, *, year, paper, board="Cambridge", subject="0620", questions):
    """questions: (number, [(path, marks, text, topics)]); a part's region is its own line
    of a one-page source PDF."""
    lines = [text for _, parts in questions for _, _, text, _ in parts]
    source = out / f"{key}.pdf"
    _source_pdf(source, lines)
    qs, entries, joins, tags, row = [], [], [], {}, 0
    for number, parts in questions:
        qparts = []
        for path, marks, text, topics in parts:
            qparts.append(QuestionPart(
                id=f"{number}.{path}", question_id=str(number), path=path, depth=1, text=text,
                image_path="", marks=marks, segments=[BBox(page=0, x0=60, y0=85 + 60 * row, x1=560, y1=110 + 60 * row)],
            ))
            entries.append(MarkSchemeEntry(
                id=f"ms.{number}.{path}", paper_key=key, question_number=number, part_path=path,
                answer_text=f"answer {number}{path}", marks=marks,
            ))
            joins.append(QuestionPartMarkScheme(part_id=f"{number}.{path}", mark_scheme_entry_id=f"ms.{number}.{path}"))
            if topics is not None:
                tags[tag_key(number, path)] = {"topic_ids": topics, "depends_on": [], "source": "manual"}
            row += 1
        qs.append(Question(
            paper_key=key, board=board, subject=subject, question_number=number, stem_text="",
            stem_image_path="", total_marks=sum(p.marks for p in qparts), parts=qparts,
        ))
    paper_obj = Paper(
        paper_key=key, board=board, level="IGCSE", subject=subject, subject_name="Chemistry", paper=paper,
        variant="1", session="M/J", year=year, source_qp_path=str(source), questions=qs,
        mark_scheme=entries, joins=joins,
    )
    (out / key).mkdir(parents=True)
    (out / key / "paper.json").write_text(paper_obj.model_dump_json(), encoding="utf-8")
    (out / key / "tags.json").write_text(json.dumps({"format": TAGS_FORMAT, "parts": tags}), encoding="utf-8")


@pytest.fixture
def api(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("QBX_OUT_DIR", str(out))
    monkeypatch.setenv("QBX_QUIZZES_DIR", str(tmp_path / "quizzes"))
    monkeypatch.setattr(llm_mod, "CONFIG_DIR", tmp_path / "config")
    sys.modules.pop("main", None)
    import main as backend

    _write(out, "0620_42_MJ_24", year=24, paper="4", questions=[
        (1, [("a", 3, _PROSE, ["4.1"]), ("b", 2, "Explain how temperature changes the rate of reaction.", ["7.2"])]),
        (2, [("a", 4, "Describe how the rate of reaction depends on concentration.", ["7.2"])]),
        (3, [("a", 2, "An untagged question about metals.", None)]),
    ])
    # question 1(a) again, reprinted in an older paper
    _write(out, "0620_32_MJ_23", year=23, paper="3", questions=[(5, [("a", 3, _PROSE, ["4.1"])])])
    _write(out, "4CH1_1C_JUN_24", year=24, paper="1C", board="Edexcel", subject="4CH1", questions=[
        (1, [("a", 2, "Electrolysis of brine.", ["4.1"])]),
    ])
    client = TestClient(backend.app)
    client.put("/api/topics/0620", json={"topics": [t.model_dump() for t in TOPICS]})
    return client


def _body(**kw) -> dict:
    return {"board": "Cambridge", "sources": [{"subject": "0620", "papers": []}], "topic_ids": ["4", "7"], **kw}


def test_preview_lists_each_topics_questions_with_reprints_and_untagged_parts(api):
    data = api.post("/api/classified/preview", json=_body()).json()
    electro, rates = data["topics"]
    assert (electro["name"], electro["n_questions"], electro["total_marks"]) == ("Electrochemistry", 1, 3)
    (item,) = electro["items"]
    assert (item["paper_key"], item["question_number"], item["parts"]) == ("0620_42_MJ_24", 1, "(a)")
    assert item["paper"] == "0620/41" and item["sitting"] == "May/June 2024"
    assert item["also_in"] == ["0620/31 May/June 2023"]
    assert [(i["question_number"], i["parts"]) for i in rates["items"]] == [(1, "(b)"), (2, "All")]
    assert data["untagged_parts"] == 1


def test_subjects_from_two_boards_are_refused(api):
    api.put("/api/topics/4CH1", json={"topics": [t.model_dump() for t in TOPICS]})
    body = _body(sources=[{"subject": "0620"}, {"subject": "4CH1"}], board="")
    response = api.post("/api/classified/preview", json=body)
    assert response.status_code == 400 and "one exam board" in response.json()["detail"]


def _export(api, body) -> zipfile.ZipFile:
    started = api.post("/api/classified/export", json=body)
    assert started.status_code == 200, started.text
    for _ in range(500):
        state = api.get("/api/state").json()["classified"]
        if not state["running"]:
            break
        time.sleep(0.02)
    assert state["ready"] and state["done"] == state["total"], state
    response = api.get(f"/api/classified/download/{started.json()['id']}")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_export_makes_a_booklet_and_mark_scheme_per_topic_with_sources_at_the_end(api):
    archive = _export(api, _body())
    assert sorted(archive.namelist()) == [
        "4_Electrochemistry_ms.pdf", "4_Electrochemistry_qp.pdf", "7_Chemical_reactions_ms.pdf", "7_Chemical_reactions_qp.pdf",
    ]
    qp = pymupdf.open(stream=archive.read("4_Electrochemistry_qp.pdf"), filetype="pdf")
    cover, last = qp[0].get_text(), qp[-1].get_text()
    assert "Electrochemistry" in cover and "1 question" in cover and "3 marks" in cover
    assert "Candidate" not in cover and "CANDIDATE" not in cover
    assert "Question sources" in last and "Also in" in last and "0620/31 May/June 2023" in last
    qp.close()
    ms = pymupdf.open(stream=archive.read("7_Chemical_reactions_ms.pdf"), filetype="pdf")
    assert "Mark Scheme" in ms[0].get_text() and "Chemical reactions" in ms[0].get_text()
    ms.close()


def test_questions_left_out_in_the_preview_stay_out(api):
    body = _body(excluded=[{"topic_id": "7", "paper_key": "0620_42_MJ_24", "question_number": 2}])
    archive = _export(api, body)
    qp = pymupdf.open(stream=archive.read("7_Chemical_reactions_qp.pdf"), filetype="pdf")
    assert "1 question" in qp[0].get_text()
    qp.close()
    # leaving out every question of every topic leaves nothing to make
    everything = [
        {"paper_key": "0620_42_MJ_24", "question_number": n} for n in (1, 2)
    ]
    assert api.post("/api/classified/export", json=_body(excluded=everything)).status_code == 400


def test_composing_never_touches_saved_exams(api, tmp_path):
    api.post("/api/quizzes", json={"name": "Mine"})
    before = {p.name: p.read_bytes() for p in (tmp_path / "quizzes").glob("*.json")}
    _export(api, _body())
    assert {p.name: p.read_bytes() for p in (tmp_path / "quizzes").glob("*.json")} == before
