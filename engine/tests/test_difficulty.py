"""Difficulty: Claude's 1-4 rating from tagging, an estimate where there's none, and the
exam builder aiming at a level (user-asked: Easy, Medium, Hard, Very hard)."""

from qbx import difficulty
from qbx.exam_builder import ExamSpec, build_exam
from qbx.tagging import _PaperTags


def test_the_estimate_reads_command_word_and_marks():
    assert difficulty.estimate("(a) State the name of the gas.", 1) == 1
    assert difficulty.estimate("(b) Explain why the rate increases.", 3) == 2
    assert difficulty.estimate("(c) Evaluate whether Peter should expand.", 12) == 4


def test_reprints_share_one_level():
    """Core and Extended papers share questions (user-reported): a question is as hard on one
    as on the other, so its copies get one level."""
    groups = {("CORE", 3): 7, ("EXT", 5): 7}
    shared = difficulty.shared_levels({("CORE", 3): 1, ("EXT", 5): 3, ("EXT", 6): 4}, groups)
    assert shared == {("CORE", 3): 2, ("EXT", 5): 2, ("EXT", 6): 4}


def test_the_builder_gives_reprinted_copies_one_level():
    from qbx.exam_builder import candidate_units

    entries = _entries([1]) + [("Q_24", _entries([3])[0][1])]
    pooled, _ = candidate_units(entries, ExamSpec(total_marks=5), [], duplicates={("P_24", 1): 0, ("Q_24", 1): 0})
    assert len(pooled) == 1 and {u.difficulty for u in pooled[0]} == {2.0}


def test_claude_s_rating_wins_over_the_estimate():
    assert difficulty.part_level({"difficulty": 4}, 1) == 4
    assert difficulty.part_level({}, 2) == 2
    assert difficulty.part_level({"difficulty": 9}, 3) == 3


def test_levels_by_name():
    assert difficulty.parse("Very hard") == 4 and difficulty.parse("easy") == 1
    assert difficulty.parse("") is None and difficulty.parse(0) is None


def test_question_level_is_weighted_by_marks():
    assert difficulty.question_level([{"difficulty": 1, "marks": 1}, {"difficulty": 4, "marks": 6}]) == 4
    assert difficulty.question_level([]) is None


def test_the_reply_schema_requires_a_rating():
    schema = _PaperTags.model_json_schema()
    assert "difficulty" in schema["$defs"]["_PartTags"]["required"]


def _entries(levels: list[int]) -> list[tuple[str, dict]]:
    """One paper whose question n (1-based) has one 5-mark part at levels[n-1]."""
    return [("P_24", {
        "summary": {"year": 24, "subject": "0620", "paper": "4", "board": "Cambridge"},
        "questions": [{"n": n} for n in range(1, len(levels) + 1)],
        "parts": [[{"path": "a", "marks": 5, "topics": [], "depends_on": [], "tagged": True, "difficulty": d}] for d in levels],
        "mcq": [False] * len(levels),
    })]


def test_the_builder_aims_at_the_level_asked_for():
    entries = _entries([1, 1, 1, 1, 4, 4, 4, 4])
    hard = build_exam(entries, ExamSpec(total_marks=10, difficulty=4), [])
    easy = build_exam(entries, ExamSpec(total_marks=10, difficulty=1), [])
    assert hard.exact and hard.difficulty == 4
    assert easy.exact and easy.difficulty == 1


def test_the_total_comes_before_the_level():
    entries = _entries([1, 1, 4])
    hard = build_exam(entries, ExamSpec(total_marks=15, difficulty=4), [])
    assert hard.exact and hard.total_marks == 15
