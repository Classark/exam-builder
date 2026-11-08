"""Measures the tagger against parts a teacher tagged by hand (`qbx tag-eval`).

Tagging changes are judged on numbers, not on reading a few replies: the hand tags of a
handful of papers (TopicChips in the question bank, `source: "manual"` in tags.json) are the
answer key, and each prompt style tags the same parts without saving anything. Styles:

- headings: topic ids and names only (the tagger before learning outcomes and answers)
- outcomes: plus each topic's learning outcomes
- answers:  plus each part's mark scheme answer
- full:     both (what tagging uses)

Scores per hand-tagged part, over the parts the style got a reply for:
- first:   Claude's first topic is one of the hand-tagged ones
- exact:   Claude's topics are exactly the hand-tagged ones
- overlap: they share at least one topic
- area:    they share a top-level topic (the right chapter, if not the right section)
"""

from dataclasses import dataclass, field
from pathlib import Path

from qbx.llm import LLM
from qbx.schema import Paper
from qbx.tagging import read_tags, tag_key, tag_questions
from qbx.topics import Topic

STYLES = {
    "headings": {"outcomes": False, "answers": False},
    "outcomes": {"outcomes": True, "answers": False},
    "answers": {"outcomes": False, "answers": True},
    "full": {"outcomes": True, "answers": True},
}


@dataclass
class Score:
    parts: int = 0
    replied: int = 0
    first: int = 0
    exact: int = 0
    overlap: int = 0
    area: int = 0
    # (paper, part key, hand-tagged, Claude's) for every part whose first topic missed
    misses: list[tuple[str, str, list[str], list[str]]] = field(default_factory=list)


def hand_tagged_papers(out_dir: Path, subject: str) -> list[tuple[Paper, dict[str, dict]]]:
    """The subject's papers with at least one hand-tagged part, and those parts' tags."""
    found = []
    for paper_dir in sorted(p for p in out_dir.iterdir() if p.is_dir() and p.name.startswith(subject)):
        manual = {k: v for k, v in read_tags(paper_dir).items() if v.get("source") == "manual" and v.get("topic_ids")}
        if manual and (paper_dir / "paper.json").exists():
            paper = Paper.model_validate_json((paper_dir / "paper.json").read_text(encoding="utf-8"))
            if paper.subject == subject:
                found.append((paper, manual))
    return found


def _top(topic_id: str, parents: dict[str, str | None]) -> str:
    seen = set()
    while parents.get(topic_id) and topic_id not in seen:
        seen.add(topic_id)
        topic_id = parents[topic_id]  # type: ignore[assignment]
    return topic_id


def score(papers: list[tuple[Paper, dict[str, dict]]], llm: LLM, topics: list[Topic], style: str) -> Score:
    parents = {t.id: t.parent for t in topics}
    result = Score()
    for paper, manual in papers:
        questions = [
            q for q in paper.questions if any(tag_key(q.question_number, p.path) in manual for p in q.parts)
        ]
        got = tag_questions(llm, paper, questions, topics, **STYLES[style])
        for key, hand in manual.items():
            want = hand["topic_ids"]
            result.parts += 1
            claude = got.get(key, {}).get("topic_ids")
            if claude is None:
                continue
            result.replied += 1
            hit_first = bool(claude) and claude[0] in want
            result.first += hit_first
            result.exact += set(claude) == set(want)
            result.overlap += bool(set(claude) & set(want))
            result.area += bool({_top(t, parents) for t in claude} & {_top(t, parents) for t in want})
            if not hit_first:
                result.misses.append((paper.paper_key, key, want, claude))
    return result
