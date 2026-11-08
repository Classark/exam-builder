"""Claude reads a syllabus's topics and learning outcomes (topics.read_syllabus), tagging
matches against those outcomes and each part's mark scheme answer, and `qbx tag-eval`
scores prompt styles against hand tags."""

import json

from qbx import tag_eval
from qbx.llm import FakeLLM
from qbx.schema import MarkSchemeEntry, QuestionPartMarkScheme
from qbx.tagging import build_prompt, tag_key, write_tags
from qbx.topics import Topic, _OutcomesReply, _SyllabusReply, clean_topics, read_syllabus, topics_prompt_list

from tests.test_ai import TOPICS, _paper, _part, _question

PAGES = [f"page {n} text" for n in range(1, 11)]


def _syllabus_llm():
    def respond(prompt, schema):
        if schema is _SyllabusReply:
            return {"topics": [
                {"id": "1", "name": "Numbers", "pages": [3, 4]},
                {"id": "1.1", "name": "Integers", "parent": "1"},
                {"id": "1.2", "name": "Fractions", "parent": "1"},
                {"id": "2", "name": "Algebra", "pages": [8]},
                {"id": "3", "name": "Graphs", "pages": []},
            ]}
        assert schema is _OutcomesReply
        if "1.1  Integers" in prompt:
            return {"topics": [
                {"id": "1.1", "outcomes": ["understand and use integers", "Higher: use index notation"]},
                {"id": "1.2", "outcomes": ["  add fractions  "]},
                {"id": "2", "outcomes": ["form equations"]},
                {"id": "9.9", "outcomes": ["not a topic"]},
            ]}
        return {"topics": [{"id": "3", "outcomes": ["plot graphs"]}]}

    return FakeLLM(respond)


def test_claude_reads_the_tree_then_each_sections_outcomes_from_its_own_pages():
    llm = _syllabus_llm()
    topics = {t.id: t for t in read_syllabus(llm, "4MA1", PAGES)}

    assert list(topics) == ["1", "1.1", "1.2", "2", "3"]
    assert topics["1.1"].outcomes == ["understand and use integers", "Higher: use index notation"]
    assert topics["1.2"].outcomes == ["add fractions"]
    assert topics["2"].outcomes == ["form equations"] and topics["1"].outcomes == []

    tree_prompt, *outcome_prompts = [p for p, _ in llm.calls]
    assert "=== page 10 ===" in tree_prompt and "Foundation, Higher" in tree_prompt
    assert len(outcome_prompts) == 2  # two top-level topics a request
    first = next(p for p in outcome_prompts if "1.1  Integers" in p)
    # topics 1 and 2's pages (3-4, 8) and one either side, nothing else
    assert all(f"=== page {n} ===" in first for n in (2, 3, 4, 5, 7, 8, 9))
    assert "=== page 6 ===" not in first and "=== page 10 ===" not in first
    # a topic giving no pages reads the whole syllabus
    second = next(p for p in outcome_prompts if "3  Graphs" in p)
    assert "=== page 1 ===" in second and "=== page 10 ===" in second
    assert topics["3"].outcomes == ["plot graphs"]


def test_outcomes_survive_cleaning_and_show_only_when_asked():
    topics = clean_topics([Topic(id="4.1", name="Electrolysis", outcomes=["  ions move ", ""])])
    assert topics[0].outcomes == ["ions move"]
    assert topics_prompt_list(topics) == "4.1  Electrolysis"
    assert topics_prompt_list(topics, outcomes=True) == "4.1  Electrolysis\n    - ions move"


def _paper_with_answers():
    q = _question(1, [_part(1, "a", "(a) Explain this observation."), _part(1, "b", "(b) Name the gas.")])
    paper = _paper([q])
    paper.mark_scheme = [
        MarkSchemeEntry(id="m1", paper_key="P", question_number=1, part_path="a",
                        answer_text="ions are free to move in the molten electrolyte", marks=2),
    ]
    paper.joins = [QuestionPartMarkScheme(part_id="1.a", mark_scheme_entry_id="m1")]
    return paper, q


def test_the_tagging_prompt_carries_outcomes_and_mark_scheme_answers():
    paper, q = _paper_with_answers()
    topics = [t.model_copy(update={"outcomes": ["electrolysis of molten compounds"]}) if t.id == "4.1" else t for t in TOPICS]

    full = build_prompt(paper, [q], topics)
    assert "    - electrolysis of molten compounds" in full
    assert "Mark scheme answer: ions are free to move in the molten electrolyte" in full
    assert full.index("Syllabus topics") < full.index("Subject:")  # the shared list first

    bare = build_prompt(paper, [q], topics, outcomes=False, answers=False)
    assert "electrolysis of molten compounds" not in bare and "Mark scheme answer" not in bare


def test_tag_eval_scores_styles_against_hand_tags(tmp_path):
    paper, q = _paper_with_answers()
    paper_dir = tmp_path / "0620_41_MJ_24"
    paper_dir.mkdir()
    paper.paper_key = paper_dir.name
    (paper_dir / "paper.json").write_text(paper.model_dump_json(), encoding="utf-8")
    write_tags(paper_dir, {
        tag_key(1, "a"): {"topic_ids": ["4.1"], "depends_on": [], "source": "manual"},
        tag_key(1, "b"): {"topic_ids": ["7.2"], "depends_on": [], "source": "manual"},
    })

    def respond(prompt, schema):
        b = ["7.2"] if "Mark scheme answer" in prompt else ["7"]
        return {"questions": [{"question_number": 1, "parts": [
            {"path": "a", "topic_ids": ["4.1"], "depends_on": []},
            {"path": "b", "topic_ids": b, "depends_on": []},
        ]}]}

    papers = tag_eval.hand_tagged_papers(tmp_path, "0620")
    assert len(papers) == 1
    llm = FakeLLM(respond)
    headings = tag_eval.score(papers, llm, TOPICS, "headings")
    full = tag_eval.score(papers, llm, TOPICS, "full")
    assert (headings.parts, headings.first, headings.area) == (2, 1, 2)
    assert (full.first, full.exact) == (2, 2) and not full.misses
    assert headings.misses == [("0620_41_MJ_24", "1|b", ["7.2"], ["7"])]
    assert json.loads((paper_dir / "tags.json").read_text())["parts"]["1|b"]["source"] == "manual"  # nothing saved
