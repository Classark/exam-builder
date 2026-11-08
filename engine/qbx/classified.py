"""Classified (topical) past-paper booklets: every past question on a topic, gathered
together, one booklet per topic. Not an exam - no marks target, no avoiding questions set
in other exams, no answer-clash checks, and no model: it reads the topic tags the parts
already carry (tags.json, through the library index).

What goes under a topic, per question, is exam_builder's unit logic, reused rather than
copied: the parts tagged with the topic (or any of its subtopics), together with every
earlier part they depend on (`depends_on`). A question with parts on two topics therefore
appears under both, each time with only the parts that topic needs. A question not fully
tagged is taken whole when any of its tagged parts is on the topic; one with no tags at all
can't be classified, and `untagged_parts` counts those parts for the teacher.

A question reprinted in several papers (Library.duplicates) is printed once per topic, from
the newest paper; the other papers are listed as "also in" on the booklet's source list.

Order within a topic: newest sitting first, then paper and variant, then question number,
with multiple-choice questions last (the composer sets them as a section of their own).
"""

from dataclasses import dataclass, field

from pydantic import BaseModel

from qbx.exam_builder import ExamSource, ExamSpec, candidate_units
from qbx.fingerprint import session_name
from qbx.topics import Topic

# months into the year each session sits, so sittings sort in time across both boards
_SESSION_ORDER = {"F/M": 3, "M/J": 6, "O/N": 11, "JAN": 1, "JUN": 6, "NOV": 11, "SP": 0}


class ClassifiedSpec(BaseModel):
    board: str | None = None
    # the subject whose topic list `topic_ids` come from - the first picked, by default
    subject: str | None = None
    sources: list[ExamSource] = []
    topic_ids: list[str] = []
    year_from: int | None = None
    year_to: int | None = None
    # "any", "mcq" (multiple choice only) or "structured" (written only)
    question_style: str = "any"

    def subject_of_topics(self) -> str | None:
        return self.subject or (self.sources[0].subject if self.sources else None)


@dataclass
class ClassifiedItem:
    paper_key: str
    question_number: int
    part_paths: list[str]
    marks: int
    mcq: bool
    whole: bool
    # the other papers printing the same question (reprints), newest first
    also_in: list[str] = field(default_factory=list)

    def ref(self) -> dict:
        """QuizItem-shaped, for the composer."""
        return {"paper_key": self.paper_key, "question_number": self.question_number, "part_paths": self.part_paths}


@dataclass
class TopicGroup:
    topic_id: str
    name: str
    items: list[ClassifiedItem] = field(default_factory=list)

    @property
    def total_marks(self) -> int:
        return sum(i.marks for i in self.items)


def _full_year(year: int) -> int:
    return year + 2000 if year < 100 else year


def sitting_key(summary: dict) -> tuple[int, int]:
    return _full_year(summary["year"]), _SESSION_ORDER.get(summary.get("session", ""), 0)


def _paper_order(summary: dict) -> tuple:
    """Newest first, then paper and variant, numerically where they're numbers."""
    def number(text) -> tuple:
        text = str(text or "")
        return (0, int(text), "") if text.isdigit() else (1, 0, text)

    year, month = sitting_key(summary)
    return (-year, -month, summary.get("subject", ""), number(summary.get("paper")), number(summary.get("variant")))


def _exam_spec(spec: ClassifiedSpec, topic_id: str) -> ExamSpec:
    return ExamSpec(
        subject=spec.subject_of_topics(), topic_ids=[topic_id], year_from=spec.year_from,
        year_to=spec.year_to, question_style=spec.question_style, sources=spec.sources,
        board=spec.board,
    )


def in_scope(entries: list[tuple[str, dict]], spec: ClassifiedSpec) -> list[tuple[str, dict]]:
    """The papers the spec draws on: its board, its subjects and their papers, its years."""
    sources = {s.subject: s.papers for s in spec.sources}
    kept = []
    for key, entry in entries:
        s = entry["summary"]
        if spec.board and s.get("board", spec.board) != spec.board:
            continue
        if sources and s["subject"] not in sources:
            continue
        papers = sources.get(s["subject"], [])
        if papers and str(s["paper"]) not in papers:
            continue
        year = _full_year(s["year"])
        if spec.year_from and year < spec.year_from or spec.year_to and year > spec.year_to:
            continue
        kept.append((key, entry))
    return kept


def untagged_parts(entries: list[tuple[str, dict]], spec: ClassifiedSpec) -> int:
    """Parts in the spec's papers (and question style) with no tags at all - they can't be
    put under any topic."""
    count = 0
    for _, entry in in_scope(entries, spec):
        for parts, mcq in zip(entry["parts"], entry["mcq"]):
            if spec.question_style == "mcq" and not mcq or spec.question_style == "structured" and mcq:
                continue
            count += sum(1 for p in parts if not p["tagged"])
    return count


def classify(
    entries: list[tuple[str, dict]],
    spec: ClassifiedSpec,
    topics: list[Topic],
    duplicates: dict[tuple[str, int], int] | None = None,
    topics_by_subject: dict[str, list[Topic]] | None = None,
) -> list[TopicGroup]:
    """One group per requested topic, in the order asked, each holding every question on
    it (as the parts that topic needs), reprints printed once from the newest paper."""
    entries = in_scope(entries, spec)
    by_key = dict(entries)
    names = {t.id: t.name for t in topics}
    duplicates = duplicates or {}
    groups = []
    for topic_id in spec.topic_ids:
        # no duplicates passed: one list of units per question, reprints pooled below
        questions, _ = candidate_units(entries, _exam_spec(spec, topic_id), topics, None, topics_by_subject)
        items: list[tuple[dict, ClassifiedItem]] = []
        for units in questions:
            # the first unit candidate_units makes is the closure of every matching part -
            # the others (one matching part each, with what it needs) are subsets of it
            unit = max(units, key=lambda u: len(u.part_paths))
            items.append((by_key[unit.paper_key]["summary"], ClassifiedItem(
                unit.paper_key, unit.question_number, unit.part_paths, unit.marks, unit.mcq, unit.whole,
            )))
        # reprints: kept once, from the newest paper (the first in order)
        items.sort(key=lambda pair: (_paper_order(pair[0]), pair[1].question_number))
        kept: list[tuple[dict, ClassifiedItem]] = []
        first_of_group: dict[int, ClassifiedItem] = {}
        for summary, item in items:
            group = duplicates.get((item.paper_key, item.question_number))
            if group is not None and group in first_of_group:
                first_of_group[group].also_in.append(item.paper_key)
                continue
            if group is not None:
                first_of_group[group] = item
            kept.append((summary, item))
        # multiple choice last, as the composer prints it
        kept.sort(key=lambda pair: pair[1].mcq)
        groups.append(TopicGroup(topic_id, names.get(topic_id, topic_id), [item for _, item in kept]))
    return groups


def paper_label(summary: dict) -> str:
    """"0620/42", as the papers name themselves."""
    return f"{summary['subject']}/{summary['paper']}{summary.get('variant') or ''}"


def sitting_label(summary: dict) -> str:
    return f"{session_name(summary.get('session', ''))} {_full_year(summary['year'])}"


def parts_label(item: ClassifiedItem) -> str:
    if item.whole or not any(item.part_paths):
        return "All"
    return ", ".join("".join(f"({piece})" for piece in path.split(".")) for path in item.part_paths)
