"""Topic tagging, part dependencies, the exam builder and typed requests. Every model call
goes to a FakeLLM - nothing here touches the network."""

import sys

import pytest
from fastapi.testclient import TestClient

from qbx import llm as llm_mod
from qbx.exam_builder import ExamSpec, build_exam
from qbx.exam_request import read_request
from qbx.llm import FakeLLM
from qbx.schema import ContainerHeading, Paper, Question, QuestionPart
from qbx.tagging import (
    merge_tags,
    question_needs_tagging,
    rule_dependencies,
    tag_key,
    tag_question,
    tag_questions,
)
from qbx.topics import Topic, clean_topics, descendants

TOPICS = [
    Topic(id="4", name="Electrochemistry"),
    Topic(id="4.1", name="Electrolysis", parent="4"),
    Topic(id="7", name="Chemical reactions"),
    Topic(id="7.2", name="Rate of reaction", parent="7"),
    Topic(id="9", name="Metals"),
]


def _part(n: int, path: str, text: str, marks: int = 2) -> QuestionPart:
    return QuestionPart(
        id=f"{n}.{path}", question_id=str(n), path=path, depth=path.count(".") + 1,
        text=text, image_path="", marks=marks,
    )


def _question(n: int, parts: list[QuestionPart], stem: str = "", headings=None) -> Question:
    return Question(
        paper_key="P", board="Cambridge", subject="0620", question_number=n, stem_text=stem,
        stem_image_path="", total_marks=sum(p.marks for p in parts), parts=parts,
        container_headings=headings or [],
    )


def _paper(questions: list[Question]) -> Paper:
    return Paper(
        paper_key="P", board="Cambridge", level="IGCSE", subject="0620", paper="4", variant="1",
        session="M/J", year=24, questions=questions,
    )


# -- dependencies by rule ----------------------------------------------------------------


def test_rules_catch_answer_references_and_hence():
    q = _question(3, [
        _part(3, "a", "(a) Calculate the moles of copper. [2]"),
        _part(3, "b", "(b) Using your answer to (a), calculate the mass. [2]"),
        _part(3, "c", "(c) Hence deduce the formula. [1]"),
        _part(3, "d", "(d) Give your answer to 3 significant figures. [2]"),
        _part(3, "e", "(e) Use your observations from (a), (c) and (d) to identify X. [2]"),
    ])
    deps = rule_dependencies(q)
    assert deps["a"] == set()
    assert deps["b"] == {"a"}
    assert deps["c"] == {"a", "b"}  # hence -> (b), and (b) needs (a)
    assert deps["d"] == set()  # "give your answer to 3 s.f." is not a reference
    assert deps["e"] == {"a", "b", "c", "d"}  # every label in the list, and what they need


def test_rules_track_where_a_figure_is_first_shown():
    q = _question(
        2,
        [
            _part(2, "a", "2 Fig. 2.1 shows a cell. (a) Name the anode. [1]"),
            _part(2, "b", "(b) Table 2.2 shows results. Complete Table 2.2. [2]"),
            _part(2, "c", "(c) Use Fig. 2.1 to explain. [2]"),
            _part(2, "d", "(d) Use Table 2.2 to find the rate. [2]"),
        ],
        stem="2 Fig. 2.1 shows a cell.",
    )
    deps = rule_dependencies(q)
    assert deps["c"] == set()  # Fig. 2.1 is in the stem, printed with every part
    assert deps["d"] == {"b"}  # Table 2.2 is first shown in (b)


def test_rules_resolve_a_lone_numeral_to_a_sibling():
    q = _question(5, [
        _part(5, "a.i", "(a) (i) Write the equation. [2]"),
        _part(5, "a.ii", "(ii) Use the equation in (i) to find x. [2]"),
    ], headings=[ContainerHeading(path="a", text="(a) Iron rusts.")])
    assert rule_dependencies(q)["a.ii"] == {"a.i"}


# -- tagging -----------------------------------------------------------------------------


def _reply(number: int, parts: list[dict]) -> dict:
    """A tagging reply for one question, in the per-paper shape."""
    return {"questions": [{"question_number": number, "parts": parts}]}


def test_tag_question_keeps_known_topics_and_adds_rule_dependencies():
    q = _question(3, [
        _part(3, "a", "(a) Describe electrolysis of brine. [3]"),
        _part(3, "b", "(b) Using your answer to (a), name the product at the cathode. [1]"),
        _part(3, "c", "(c) State one use of chlorine. [1]"),
    ])
    llm = FakeLLM(lambda prompt, schema: _reply(3, [
        {"path": "a", "topic_ids": ["4.1", "99.9"], "depends_on": []},
        {"path": "b", "topic_ids": ["4.1"], "depends_on": []},  # the rule adds (a)
        {"path": "c", "topic_ids": ["9"], "depends_on": ["b", "z"]},  # (b) brings (a) too
    ]))
    tags = tag_question(llm, _paper([q]), q, TOPICS)
    assert tags[tag_key(3, "a")]["topic_ids"] == ["4.1"]  # the made-up id is dropped
    assert tags[tag_key(3, "b")]["depends_on"] == ["a"]
    assert tags[tag_key(3, "c")]["depends_on"] == ["a", "b"]
    assert "Electrolysis" in llm.calls[0][0]  # the topic list is in the prompt


def test_a_part_never_depends_on_a_later_one():
    q = _question(1, [_part(1, "a", "(a) x [1]"), _part(1, "b", "(b) y [1]")])
    llm = FakeLLM(lambda p, s: _reply(1, [
        {"path": "a", "topic_ids": ["9"], "depends_on": ["b"]},
        {"path": "b", "topic_ids": ["9"], "depends_on": []},
    ]))
    assert tag_question(llm, _paper([q]), q, TOPICS)[tag_key(1, "a")]["depends_on"] == []


def test_a_whole_paper_is_tagged_in_one_request_and_a_missing_answer_stays_untagged():
    qs = [_question(n, [_part(n, "a", f"({n}) question text [1]")]) for n in (1, 2, 3)]
    llm = FakeLLM(lambda p, s: {"questions": [
        {"question_number": 1, "parts": [{"path": "a", "topic_ids": ["9"], "depends_on": []}]},
        {"question_number": 3, "parts": [{"path": "a", "topic_ids": ["4"], "depends_on": []}]},
    ]})
    tags = tag_questions(llm, _paper(qs), qs, TOPICS)
    assert len(llm.calls) == 1
    assert set(tags) == {tag_key(1, "a"), tag_key(3, "a")}  # Q2 is asked again next run


def test_retagging_is_skipped_until_the_text_changes_and_manual_tags_stay():
    q = _question(1, [_part(1, "a", "(a) x [1]")])
    llm = FakeLLM(lambda p, s: _reply(1, [{"path": "a", "topic_ids": ["9"], "depends_on": []}]))
    tags = tag_question(llm, _paper([q]), q, TOPICS)
    assert not question_needs_tagging(q, tags)
    changed = _question(1, [_part(1, "a", "(a) x, re-extracted [1]")])
    assert question_needs_tagging(changed, tags)

    manual = {tag_key(1, "a"): {"topic_ids": ["4"], "depends_on": [], "text_hash": "", "source": "manual"}}
    assert not question_needs_tagging(changed, manual)
    assert merge_tags(manual, tags)[tag_key(1, "a")]["topic_ids"] == ["4"]


def test_topic_lists_are_cleaned_and_expand_to_subtopics():
    cleaned = clean_topics([Topic(id="1", name=" A "), Topic(id="1", name="dup"), Topic(id="2.1", name="B", parent="2")])
    assert [(t.id, t.name, t.parent) for t in cleaned] == [("1", "A", None), ("2.1", "B", None)]
    assert descendants(TOPICS, ["4"]) == {"4", "4.1"}


# -- the exam builder --------------------------------------------------------------------


def _entry(paper_key: str, year: int, questions: list[list[tuple[str, int, list[str], list[str]]]], paper="4", mcq=None, subject="0620"):
    """questions: per question, its parts as (path, marks, topics, depends_on)."""
    return (paper_key, {
        "summary": {"paper_key": paper_key, "year": year, "paper": paper, "subject": subject},
        "questions": [{"n": i + 1, "marks": sum(p[1] for p in parts)} for i, parts in enumerate(questions)],
        "parts": [
            [{"path": p, "marks": m, "topics": t, "depends_on": d, "tagged": True} for p, m, t, d in parts]
            for parts in questions
        ],
        "mcq": mcq or [False] * len(questions),
    })


BANK = [
    _entry("A_24", 24, [
        [("a", 3, ["4.1"], []), ("b", 2, ["4.1"], ["a"]), ("c", 4, ["9"], [])],
        [("a", 5, ["7.2"], []), ("b", 3, ["7.2"], [])],
    ]),
    _entry("B_23", 23, [
        [("a", 6, ["4.1"], []), ("b", 4, ["4.1"], [])],
        [("a", 2, ["7.2"], []), ("b", 7, ["7.2"], ["a"])],
        [("a", 4, ["9"], [])],
    ]),
]


def test_builder_draws_on_several_subject_codes_each_with_its_own_papers():
    """0620 and 0971 are the same chemistry under two codes, with their own topic ids: a
    topic asked for by its 0620 id is found in 0971 by name."""
    from qbx.exam_builder import ExamSource

    other_topics = [Topic(id="C4", name="Electrochemistry"), Topic(id="C4.1", name="Electrolysis", parent="C4")]
    bank = [
        _entry("A_24", 24, [[("a", 3, ["4.1"], [])]], paper="4"),
        _entry("A2_24", 24, [[("a", 3, ["4.1"], [])]], paper="2"),  # 0620 paper 2: not picked
        _entry("K_24", 24, [[("a", 4, ["C4.1"], [])]], paper="6", subject="0971"),
        _entry("P_24", 24, [[("a", 5, ["4.1"], [])]], paper="4", subject="0625"),  # not picked at all
    ]
    spec = ExamSpec(
        subject="0620", topic_ids=["4"], total_marks=7, seed=1,
        sources=[ExamSource(subject="0620", papers=["4"]), ExamSource(subject="0971")],
    )
    result = build_exam(bank, spec, TOPICS, topics_by_subject={"0620": TOPICS, "0971": other_topics})
    assert result.exact
    assert sorted(i["paper_key"] for i in result.items) == ["A_24", "K_24"]


def test_builder_hits_the_exact_total_on_the_requested_topics():
    spec = ExamSpec(subject="0620", topic_ids=["4", "7"], total_marks=20, seed=1)
    result = build_exam(BANK, spec, TOPICS)
    assert result.exact and result.total_marks == 20
    assert result.missing_topics == []
    assert result.marks_by_topic["4"] > 0 and result.marks_by_topic["7"] > 0
    keys = [(i["paper_key"], i["question_number"]) for i in result.items]
    assert len(keys) == len(set(keys))  # a question at most once


def test_builder_never_takes_a_part_without_what_it_needs():
    for seed in range(20):
        result = build_exam(BANK, ExamSpec(subject="0620", topic_ids=["4", "7"], total_marks=15, seed=seed), TOPICS)
        for item in result.items:
            parts = {p["path"]: p for p in dict(BANK)[item["paper_key"]]["parts"][item["question_number"] - 1]}
            for path in item["part_paths"]:
                assert set(parts[path]["depends_on"]) <= set(item["part_paths"])
            # off-topic parts don't come along: A_24 Q1's (c) is on metals
            assert not (item["paper_key"] == "A_24" and item["question_number"] == 1 and "c" in item["part_paths"])


def test_builder_is_reproducible_and_filters_years_and_mcq():
    spec = ExamSpec(subject="0620", topic_ids=["4"], total_marks=5, seed=3)
    assert build_exam(BANK, spec, TOPICS).items == build_exam(BANK, spec, TOPICS).items
    recent = build_exam(BANK, ExamSpec(subject="0620", topic_ids=["4", "7"], total_marks=8, year_from=2024, seed=0), TOPICS)
    assert {i["paper_key"] for i in recent.items} == {"A_24"}
    mcq_bank = [_entry("M_24", 24, [[("", 1, ["4.1"], [])]] * 3, mcq=[True] * 3)]
    assert build_exam(mcq_bank, ExamSpec(subject="0620", topic_ids=["4"], total_marks=2), TOPICS).items == []
    assert build_exam(mcq_bank, ExamSpec(subject="0620", topic_ids=["4"], total_marks=2, question_style="mcq"), TOPICS).exact


def test_builder_reports_an_unreachable_total_and_missing_topics():
    result = build_exam(BANK, ExamSpec(subject="0620", topic_ids=["4.1", "3"], total_marks=500), TOPICS)
    assert not result.exact and result.total_marks < 500
    assert result.missing_topics == ["3"]


# -- reading a request -------------------------------------------------------------------


def test_read_request_validates_the_reply():
    llm = FakeLLM(lambda p, s: {
        "subject": "9999", "topic_ids": ["4", "nope"], "total_marks": 0, "question_style": "odd",
        "name": "  Electrolysis   quiz ", "unmatched": ["organic"],
    })
    read = read_request(llm, "electrolysis quiz", [{"subject": "0620", "name": "Chemistry", "papers": []}], {"0620": TOPICS})
    assert read.spec.subject == "0620"  # the only subject there is
    assert read.spec.topic_ids == ["4"]
    assert read.spec.total_marks == 40  # none given: the default
    assert read.spec.question_style == "structured"
    assert read.name == "Electrolysis quiz"
    assert read.unmatched == ["organic"]


# -- endpoints ---------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("QBX_QUIZZES_DIR", str(tmp_path / "quizzes"))
    monkeypatch.setattr(llm_mod, "CONFIG_DIR", tmp_path / "config")
    sys.modules.pop("main", None)
    import main as backend

    q1 = _question(1, [
        _part(1, "a", "1 Brine is electrolysed. (a) Name the anode product. [3]", 3),
        _part(1, "b", "(b) Using your answer to (a), write the half-equation. [2]", 2),
    ], stem="1 Brine is electrolysed.")
    q2 = _question(2, [_part(2, "a", "2 (a) Explain how temperature affects rate. [4]", 4)])
    paper = _paper([q1, q2])
    (tmp_path / "out" / "P").mkdir(parents=True)
    (tmp_path / "out" / "P" / "paper.json").write_text(paper.model_dump_json(), encoding="utf-8")

    def respond(prompt, schema):
        if schema.__name__ == "_PaperTags":
            return {"questions": [
                {"question_number": 1, "parts": [{"path": "a", "topic_ids": ["4.1"], "depends_on": []},
                                                 {"path": "b", "topic_ids": ["4.1"], "depends_on": []}]},
                {"question_number": 2, "parts": [{"path": "a", "topic_ids": ["7.2"], "depends_on": []}]},
            ]}
        return {"subject": "0620", "topic_ids": ["4", "7"], "total_marks": 9, "name": "Electrolysis and rates"}

    fake = FakeLLM(respond)
    llm_mod.set_llm_override(fake)
    yield TestClient(backend.app), backend, fake
    llm_mod.set_llm_override(None)


def test_claude_is_off_until_switched_on(api):
    client, _, _ = api
    settings = client.get("/api/settings").json()
    assert settings["enabled"] is False and settings["tag_model"] == "claude-haiku-5-5" and settings["request_model"] == "claude-sonnet-5-5"
    llm_mod.set_llm_override(None)
    assert llm_mod.get_llm() is None
    client.put("/api/settings", json={"enabled": True, "tag_model": "sonnet", "provider": "claude_code"})
    assert isinstance(llm_mod.get_llm("tag"), llm_mod.ClaudeLLM) and llm_mod.get_llm("tag").model == "claude-sonnet-5-5"  # an old alias is pinned on load


def test_tag_then_build_an_exam_from_a_typed_request(api):
    client, backend, fake = api
    client.put("/api/topics/0620", json={"topics": [t.model_dump() for t in TOPICS]})
    assert client.get("/api/subjects").json()[0]["n_topics"] == len(TOPICS)

    client.post("/api/tagging/start")
    for _ in range(200):
        if not client.get("/api/state").json()["tagging"]["running"]:
            break
        import time
        time.sleep(0.02)
    state = client.get("/api/state").json()["tagging"]
    assert state["done"] == 2 and state["failed"] == []
    assert sum(1 for _, schema in fake.calls if schema.__name__ == "_PaperTags") == 1  # one paper, one request
    question = client.get("/api/papers/P/questions/1").json()
    assert question["parts"][1]["tags"]["depends_on"] == ["a"]

    # a hand correction sticks
    client.put("/api/papers/P/tags", json={"question_number": 2, "path": "a", "topic_ids": ["7.2", "bogus"]})
    assert client.get("/api/papers/P/questions/2").json()["parts"][0]["tags"]["source"] == "manual"

    made = client.post("/api/exam/from_request", json={"text": "electrolysis and rates, 9 marks"}).json()
    assert made["exact"] and made["total_marks"] == 9
    assert made["name"] == "Electrolysis and rates – 9 marks"
    quiz = client.get(f"/api/quizzes/{made['name']}").json()
    assert quiz["request"]["text"] == "electrolysis and rates, 9 marks"
    assert {(i["question_number"], tuple(p["part_path"] for p in i["parts"])) for i in quiz["items"]} == {
        (1, ("a", "b")), (2, ("a",)),
    }
    again = client.post(f"/api/quizzes/{made['name']}/reroll").json()
    assert again["total_marks"] == 9


def test_ticked_papers_are_the_only_ones_an_exam_draws_on(api):
    """Papers ticked in the builder win over whatever the sentence says about papers."""
    client, backend, fake = api
    client.put("/api/topics/0620", json={"topics": [t.model_dump() for t in TOPICS]})
    client.post("/api/tagging/start")
    import time
    for _ in range(200):
        if not client.get("/api/state").json()["tagging"]["running"]:
            break
        time.sleep(0.02)

    # the only paper in the bank is paper 4, so ticking paper 2 alone leaves nothing
    none = client.post(
        "/api/exam/from_request", json={"text": "electrolysis and rates, 9 marks", "subject": "0620", "papers": ["2"]}
    )
    assert none.status_code == 400 and "No questions" in none.json()["detail"]

    made = client.post(
        "/api/exam/from_request", json={"text": "electrolysis and rates, 9 marks", "subject": "0620", "papers": ["4", "2"]}
    ).json()
    assert made["total_marks"] == 9
    quiz = client.get(f"/api/quizzes/{made['name']}").json()
    # kept for "Try different questions"
    assert quiz["request"]["spec"]["sources"] == [{"subject": "0620", "papers": ["4", "2"]}]
    # every question in the bank is now in that exam, and a new one never reuses them
    again = client.post("/api/exam/from_request", json={"text": "electrolysis and rates, 9 marks", "subject": "0620"})
    assert again.status_code == 400 and "already in your other exams" in again.json()["detail"]
    client.delete(f"/api/quizzes/{made['name']}")

    other = client.post("/api/exam/from_request", json={"text": "anything", "subject": "9702", "papers": ["4"]})
    assert other.status_code == 400 and "9702" in other.json()["detail"]

    # the builder's own form: a list of subject codes, each with its papers
    listed = client.post(
        "/api/exam/from_request",
        json={"text": "electrolysis and rates, 9 marks", "sources": [{"subject": "0620", "papers": ["4"]}]},
    ).json()
    assert listed["total_marks"] == 9
    both = client.post(
        "/api/exam/from_request",
        json={"text": "anything", "sources": [{"subject": "0620", "papers": []}, {"subject": "9702", "papers": []}]},
    )
    assert both.status_code == 400 and "9702" in both.json()["detail"]


def test_ai_endpoints_say_when_claude_is_off(api):
    client, _, _ = api
    llm_mod.set_llm_override(None)
    response = client.post("/api/exam/from_request", json={"text": "anything"})
    assert response.status_code == 400 and "Switch on Claude" in response.json()["detail"]


def test_reaching_the_usage_limit_pauses_tagging(api):
    client, _, _ = api

    def limited(prompt, schema):
        raise llm_mod.UsageLimitError("Your Claude usage limit is reached. It resets at 18:00.", 1_900_000_000)

    llm_mod.set_llm_override(FakeLLM(limited))
    client.put("/api/topics/0620", json={"topics": [t.model_dump() for t in TOPICS]})
    client.post("/api/tagging/start")
    import time
    for _ in range(200):
        state = client.get("/api/state").json()["tagging"]
        if not state["running"]:
            break
        time.sleep(0.02)
    assert state["stage"] == "Paused"
    assert "resets at 18:00" in state["paused"] and state["done"] == 0


# -- reprinted questions -----------------------------------------------------------------

from qbx.similar import duplicate_groups, question_signatures  # noqa: E402

MCQ = "Which statement about the element bromine is correct? A It displaces chlorine B It is a gas C It is brown D It is a metal"


def test_a_reprinted_question_is_found_across_papers_but_not_within_one():
    groups = duplicate_groups([
        ("P1", question_signatures("", [MCQ])),
        ("P2", question_signatures("", ["© UCLES 2020 0620/12/M/J/20 [Turn over " + MCQ])),  # footer noise
        ("P3", question_signatures("", ["Which gas is made when powdered zinc is added to dilute hydrochloric acid? A carbon dioxide"])),
        ("P1", question_signatures("", [MCQ + " extra"])),  # same paper: never a reprint
    ])
    assert groups[0] == groups[1]
    assert groups[2] not in (groups[0], groups[3])


def test_a_shared_long_part_makes_questions_reprints_but_a_stock_phrase_does_not():
    setup = ("You are going to investigate the rate of reaction between solution S and solution T at "
             "different temperatures. Read all the instructions carefully before starting the experiment "
             "and record every result in the table provided below.")
    stock = "(i) Complete the dot-and-cross diagram to show the electron arrangement in a molecule."
    groups = duplicate_groups([
        ("P1", question_signatures("", [setup, "(b) Plot the results on the grid. [3]"])),
        ("P2", question_signatures("", [setup, "(b) Explain why the rate increases. [2] Suggest one improvement. [1]"])),
        ("P3", question_signatures("", [stock + " of methane", "(ii) Name the type of bonding. [1]"])),
        ("P4", question_signatures("", [stock + " of ammonia", "(ii) Explain the boiling point. [2]"])),
    ])
    assert groups[0] == groups[1]
    assert groups[2] != groups[3]


def test_answers_clash_when_the_mark_scheme_says_the_same_thing_in_other_words():
    """Real pairs from 0620/41: one definition asked in two papers, its mark scheme answer
    reworded; and two different dot-and-cross answers, which only share stock words."""
    from qbx.similar import answer_clashes, answer_signature

    isotopes_23 = "M1 different atoms of the same element with the same number of protons (1) M2 different numbers of neutrons (1)"
    isotopes_25 = "M1 atoms of the same element that have the same number of protons M2 but different numbers of neutrons"
    dots_24 = "M1 one shared pair of dot and cross M2 non-bonding electrons complete on fluorine, either dots or crosses"
    dots_25 = "M1 three bonding pairs between N and H atoms M2 lone pair of dots on nitrogen, crosses on hydrogen"
    sigs = [answer_signature(t) for t in (isotopes_23, isotopes_25, dots_24, dots_25)]
    assert answer_signature("C4H10O2 (1)") is None and answer_signature("B") is None
    owners = [("A", 2), ("B", 1), ("C", 3), ("D", 4)]
    assert answer_clashes(list(zip(owners, sigs))) == [(0, 1)]
    # two parts of one question are never a clash
    assert answer_clashes([(("A", 2), sigs[0]), (("A", 2), sigs[1])]) == []


def test_builder_never_sets_two_parts_with_the_same_answer():
    bank = [
        _entry("R1", 24, [[("a", 2, ["4.1"], [])]]),
        _entry("R2", 24, [[("a", 2, ["4.1"], [])]]),
        _entry("R3", 24, [[("a", 2, ["4.1"], [])]]),
    ]
    clashes = {("R1", 1, "a"): {0}, ("R2", 1, "a"): {0}}  # R1 1(a) and R2 1(a) share an answer
    for seed in range(10):
        spec = ExamSpec(subject="0620", topic_ids=["4"], total_marks=4, seed=seed)
        result = build_exam(bank, spec, TOPICS, clashes=clashes)
        assert result.exact and len({i["paper_key"] for i in result.items} & {"R1", "R2"}) == 1


def test_builder_leans_towards_recent_papers_without_requiring_them():
    years = [15, 16, 17, 18, 24, 25]
    bank = [_entry(f"Y{y}", y, [[("a", 2, ["4.1"], [])]]) for y in years]
    picks = {y: 0 for y in years}
    for seed in range(60):
        result = build_exam(bank, ExamSpec(subject="0620", topic_ids=["4"], total_marks=4, seed=seed), TOPICS)
        for item in result.items:
            picks[int(item["paper_key"][1:])] += 1
    assert picks[24] + picks[25] > 2 * (picks[15] + picks[16])
    # only old papers on a topic: still used
    assert build_exam(bank[:2], ExamSpec(subject="0620", topic_ids=["4"], total_marks=4), TOPICS).exact


def test_builder_never_sets_two_copies_of_a_question():
    bank = [_entry(k, 24, [[("", 1, ["4.1"], [])]], mcq=[True]) for k in ("R1", "R2", "R3")]
    duplicates = {("R1", 1): 7, ("R2", 1): 7}  # R1 Q1 and R2 Q1 are the same question
    for seed in range(10):
        result = build_exam(bank, ExamSpec(subject="0620", topic_ids=["4"], total_marks=3, question_style="mcq", seed=seed), TOPICS, duplicates)
        assert len({i["paper_key"] for i in result.items} & {"R1", "R2"}) <= 1
        assert result.total_marks == 2 and not result.exact


@pytest.mark.skipif(sys.platform != "win32", reason="console windows are a Windows matter")
def test_claude_cli_is_started_without_a_console_window(monkeypatch):
    """The packaged app has no console, so each Claude call used to flash a terminal."""
    import subprocess

    import anyio

    from qbx import llm

    seen = {}

    async def fake_open_process(*args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(anyio, "open_process", fake_open_process)
    monkeypatch.setattr(llm, "_windows_hidden", False)
    llm._hide_cli_windows()
    anyio.run(lambda: anyio.open_process(["claude", "-v"]))
    assert seen["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_tagging_runs_without_thinking(monkeypatch, tmp_path):
    """Haiku thought ~10,500 tokens a paper before a ~1,300-token answer, running the
    user's subscription limit out twice; tagging picks from a given list, so no thinking."""
    from qbx import llm

    monkeypatch.setattr(llm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(llm, "_override", None)
    llm.save_settings(llm.Settings(enabled=True))

    tagger = llm.get_llm("tag")
    assert tagger.think is False
    assert llm.get_llm("request").think is True


def test_builder_sets_exactly_the_number_of_multiple_choice_questions_asked_for():
    """Real finding: "20 marks, only 4 multiple choice" came back all multiple choice."""
    bank = BANK + [
        _entry(f"M_{i}", 24, [[("", 1, ["4.1"], [])] for _ in range(10)], paper="2", mcq=[True] * 10)
        for i in range(2)
    ]
    spec = ExamSpec(subject="0620", topic_ids=["4", "7"], total_marks=20, question_style="any", mcq_count=4, seed=3)
    result = build_exam(bank, spec, TOPICS)
    mcq = [i for i in result.items if i["paper_key"].startswith("M_")]
    assert len(mcq) == 4
    assert result.exact and result.total_marks == 20


def test_request_with_a_number_of_mcqs_reads_as_that_many_and_the_rest_written():
    def reply(prompt, schema):
        return schema(subject="0620", topic_ids=["4"], total_marks=20, question_style="mcq", mcq_count=4, name="States")

    read = read_request(FakeLLM(reply), "states of matter, 20 marks, only 4 MCQs", [{"subject": "0620"}], {"0620": TOPICS})
    assert read.spec.mcq_count == 4 and read.spec.question_style == "any"


# -- adjusting an exam that already exists -----------------------------------------------

from qbx.exam_adjust import Adjustment, apply_adjustment, exam_items, read_adjustment  # noqa: E402

ADJUST_BANK = [
    _entry("X_24", 24, [
        [("a", 4, ["4.1"], [])],
        [("a", 4, ["7.2"], [])],
        [("a", 2, ["7.2"], []), ("b", 2, ["7.2"], ["a"])],
    ]),
    _entry("Y_23", 23, [
        [("a", 4, ["4.1"], [])],
        [("a", 3, ["9"], [])],
    ]),
]
ADJUST_SPEC = ExamSpec(subject="0620", topic_ids=["4", "7"], total_marks=8, seed=1)


def _exam(*keys):
    return exam_items(
        [{"paper_key": k, "question_number": n, "part_paths": paths} for k, n, paths in keys], dict(ADJUST_BANK)
    )


def _adjust(items, **kw):
    return apply_adjustment(items, Adjustment(**kw), ADJUST_SPEC, ADJUST_BANK, TOPICS)


def _keys(result):
    return [(i["paper_key"], i["question_number"]) for i in result.items]


def test_replacing_a_question_puts_a_new_one_on_the_topic_asked_for_in_its_place():
    items = _exam(("X_24", 1, ["a"]), ("X_24", 2, ["a"]))
    assert [i.label for i in items] == ["Q1", "Q2"]
    result = _adjust(items, remove=[1], refill=True, topic_ids=["4"])
    assert _keys(result) == [("X_24", 1), ("Y_23", 1)]
    assert result.exact and result.total_marks == 8 and result.removed == ["Q2"]


def test_a_smaller_total_drops_whole_questions_from_the_end_unless_told_to_keep_them():
    items = _exam(("X_24", 1, ["a"]), ("X_24", 2, ["a"]))
    assert _keys(_adjust(items, target=4)) == [("X_24", 1)]
    assert _keys(_adjust(items, target=4, keep=[1])) == [("X_24", 2)]
    assert _keys(_adjust(items, marks_change=-4)) == [("X_24", 1)]


def test_removing_a_part_takes_the_parts_that_need_it_too():
    items = _exam(("X_24", 3, ["a", "b"]), ("X_24", 1, ["a"]))
    assert _keys(_adjust(items, remove_parts={0: ["a"]})) == [("X_24", 1)]  # b needs a: nothing left
    kept = _adjust(items, remove_parts={0: ["b"]})
    assert kept.items[0]["part_paths"] == ["a"] and kept.total_marks == 6 and kept.exact


def test_added_marks_never_repeat_a_question_or_its_reprint():
    items = _exam(("X_24", 1, ["a"]), ("X_24", 2, ["a"]))
    grown = _adjust(items, marks_change=4)
    assert grown.exact and grown.total_marks == 12
    assert len(set(_keys(grown))) == len(grown.items)
    # Y_23 Q1 is a reprint of X_24 Q1: replacing Q2 with electrolysis finds nothing new
    reprint = apply_adjustment(
        items, Adjustment(remove=[1], refill=True, topic_ids=["4"]), ADJUST_SPEC, ADJUST_BANK, TOPICS,
        duplicates={("X_24", 1): 0, ("Y_23", 1): 0},
    )
    assert _keys(reprint) == [("X_24", 1)] and not reprint.exact


def test_changing_a_question_again_never_cycles_back_while_new_ones_fit():
    """User-reported: replacing a question over and over came back to ones already
    swapped out, since only the exam's current questions were excluded."""
    bank = ADJUST_BANK + [_entry("Z_22", 22, [[("a", 4, ["4.1"], [])]])]
    items = _exam(("X_24", 2, ["a"]), ("X_24", 1, ["a"]))
    seen = {("X_24", 1), ("X_24", 2), ("Y_23", 1)}  # Y_23 Q1 was in the exam before
    swap = Adjustment(remove=[1], refill=True, topic_ids=["4"])
    result = apply_adjustment(items, swap, ADJUST_SPEC, bank, TOPICS, seen=seen)
    assert _keys(result) == [("X_24", 2), ("Z_22", 1)]
    # with nothing new left, an old one is better than a gap: a preference, not a rule
    seen.add(("Z_22", 1))
    again = apply_adjustment(items, swap, ADJUST_SPEC, bank, TOPICS, seen=seen)
    assert again.exact and _keys(again)[0] == ("X_24", 2) and _keys(again)[1] in {("Y_23", 1), ("Z_22", 1)}


def test_read_adjustment_keeps_only_what_the_exam_has():
    items = _exam(("X_24", 3, ["a", "b"]), ("X_24", 1, ["a"]))
    llm = FakeLLM(lambda p, s: {
        "remove": ["q2", "Q9"], "remove_parts": [{"question": "Q1", "parts": ["(b)", "z"]}],
        "topic_ids": ["4", "nope"], "total_marks": 999, "question_style": "odd", "summary": " Swap  Q2 ",
    })
    adj = read_adjustment(llm, "swap Q2 for electrolysis", items, ADJUST_SPEC, TOPICS)
    assert adj.remove == [1] and adj.remove_parts == {0: ["b"]}
    assert adj.topic_ids == ["4"] and adj.target is None and adj.question_style == "" and adj.summary == "Swap Q2"
    assert "Q1: 4 marks, from X_24 question 3" in llm.calls[0][0]


def test_adjusting_a_saved_exam_by_instruction(api):
    client, backend, fake = api
    client.put("/api/topics/0620", json={"topics": [t.model_dump() for t in TOPICS]})
    client.post("/api/tagging/start")
    import time
    for _ in range(200):
        if not client.get("/api/state").json()["tagging"]["running"]:
            break
        time.sleep(0.02)
    made = client.post("/api/exam/from_request", json={"text": "electrolysis and rates, 9 marks"}).json()
    quiz = client.get(f"/api/quizzes/{made['name']}").json()
    rates = next(n for n, i in enumerate(quiz["items"], 1) if i["question_number"] == 2)

    llm_mod.set_llm_override(FakeLLM(lambda p, s: {"remove": [f"Q{rates}"], "summary": "Remove the rates question"}))
    out = client.post(f"/api/quizzes/{made['name']}/adjust", json={"text": "remove the rates question"}).json()
    assert out["total_marks"] == 5 and out["exact"] and out["removed"] == [f"Q{rates}"]
    quiz = client.get(f"/api/quizzes/{made['name']}").json()
    assert [i["question_number"] for i in quiz["items"]] == [1]
    assert quiz["request"]["spec"]["total_marks"] == 5
    assert quiz["request"]["adjustments"] == ["remove the rates question"]
    # the exam remembers the question it took out, so a later change picks others first
    seen = backend.Quiz.model_validate_json(backend._quiz_path(made["name"]).read_text(encoding="utf-8")).seen
    assert ("P", 2) in seen and ("P", 1) in seen

    # an exam put together by hand can be adjusted too
    client.post("/api/quizzes", json={"name": "Hand made"})
    client.put("/api/quizzes/Hand made", json={"items": [{"paper_key": "P", "question_number": 1, "part_paths": ["a", "b"]}]})
    llm_mod.set_llm_override(FakeLLM(lambda p, s: {"marks_change": 4, "topic_ids": ["7"]}))
    out = client.post("/api/quizzes/Hand made/adjust", json={"text": "add 4 marks on rates"}).json()
    assert out["total_marks"] == 9 and out["added"] == 1
    assert client.post("/api/quizzes/Hand made/adjust", json={"text": " "}).status_code == 400


def _tag_bank(client):
    client.put("/api/topics/0620", json={"topics": [t.model_dump() for t in TOPICS]})
    client.post("/api/tagging/start")
    import time
    for _ in range(200):
        if not client.get("/api/state").json()["tagging"]["running"]:
            break
        time.sleep(0.02)


def _add_paper(backend, key: str, paper: str, questions: list[Question]) -> None:
    """Another paper in the fixture's bank, numbered `paper`."""
    qs = [q.model_copy(update={"paper_key": key}) for q in questions]
    folder = backend.OUT_DIR / key
    folder.mkdir(parents=True)
    copy = _paper(qs).model_copy(update={"paper_key": key, "paper": paper})
    (folder / "paper.json").write_text(copy.model_dump_json(), encoding="utf-8")


def test_new_questions_never_come_from_another_exam_or_another_paper(api):
    """User-reported: an exam asked for from papers 2 and 4 came back with paper 5
    questions after changing it, and a new exam could repeat one already set."""
    client, backend, fake = api
    # numbered 2, so the fixture's tagging marks it as rates too
    _add_paper(backend, "P5", "5", [_question(2, [_part(2, "a", "2 (a) How does a catalyst change the rate? [4]", 4)])])
    _tag_bank(client)

    # a hand-made exam from paper 4: a change adds rates questions from paper 4 only
    client.post("/api/quizzes", json={"name": "Hand made"})
    client.put("/api/quizzes/Hand made", json={"items": [{"paper_key": "P", "question_number": 1, "part_paths": ["a", "b"]}]})
    llm_mod.set_llm_override(FakeLLM(lambda p, s: {"marks_change": 4, "refill": True, "topic_ids": ["7"]}))
    client.post("/api/quizzes/Hand made/adjust", json={"text": "add 4 marks on rates"})
    keys = {i["paper_key"] for i in client.get("/api/quizzes/Hand made").json()["items"]}
    assert keys == {"P"}

    # a request ticked to paper 5 draws on paper 5 alone
    llm_mod.set_llm_override(fake)
    made = client.post("/api/exam/from_request", json={"text": "rates, 4 marks", "sources": [{"subject": "0620", "papers": ["5"]}]}).json()
    assert {i["paper_key"] for i in client.get(f"/api/quizzes/{made['name']}").json()["items"]} == {"P5"}

    # and a new exam on rates finds nothing: P's rates question is in "Hand made", P5's in the other
    again = client.post("/api/exam/from_request", json={"text": "rates", "subject": "0620"})
    assert again.status_code == 400


def test_chat_talks_then_makes_then_changes_an_exam(api):
    from qbx.exam_chat import ChatTurn  # noqa: F401 - the module the endpoint uses
    client, backend, fake = api
    _tag_bank(client)
    turns = iter([
        {"reply": "Which topics should it cover?", "action": "none"},
        {"reply": "Making a 9-mark exam on electrolysis and rates.", "action": "make", "instruction": "0620, electrolysis and rates, 9 marks"},
        {"subject": "0620", "topic_ids": ["4", "7"], "total_marks": 9, "name": "Electrolysis and rates"},
        {"reply": "Taking out the rates question.", "action": "change", "instruction": "remove Q2"},
        {"remove": ["Q2"], "summary": "Remove Q2"},
    ])
    chat = FakeLLM(lambda p, s: next(turns))
    llm_mod.set_llm_override(chat)

    messages = [{"role": "teacher", "text": "I need a short chemistry test"}]
    out = client.post("/api/chat", json={"messages": messages}).json()
    assert out["action"] == "none" and out["quiz"] is None and "topics" in out["message"]["text"]

    messages += [out["message"], {"role": "teacher", "text": "electrolysis and rates, 9 marks"}]
    out = client.post("/api/chat", json={"messages": messages}).json()
    assert out["action"] == "make" and out["message"]["outcome"]["total_marks"] == 9
    name = out["quiz"]
    # the conversation is saved with the exam it made, and Claude saw it all
    assert len(client.get(f"/api/quizzes/{name}").json()["chat"]) == 4
    assert "I need a short chemistry test" in chat.calls[1][0]

    messages += [out["message"], {"role": "teacher", "text": "drop the rates one"}]
    out = client.post("/api/chat", json={"messages": messages, "quiz": name}).json()
    assert out["action"] == "change" and out["message"]["outcome"]["total_marks"] == 5
    assert "Q1: 5 marks" in chat.calls[3][0]  # the open exam is in the prompt
    assert client.post("/api/chat", json={"messages": []}).status_code == 400


def test_chat_reports_an_action_that_could_not_run():
    from qbx.exam_chat import ChatMessage, read_turn
    llm = FakeLLM(lambda p, s: {"reply": "Building it again.", "action": "reroll"})
    # nothing open: nothing to reroll, so it's just a reply
    turn = read_turn(llm, [ChatMessage(role="teacher", text="again")], [], {}, None)
    assert turn.action == "none" and turn.reply == "Building it again."
    llm = FakeLLM(lambda p, s: {"reply": "Making it.", "action": "make", "instruction": ""})
    assert read_turn(llm, [ChatMessage(role="teacher", text="go")], [], {}, None).action == "none"
