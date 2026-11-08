"""Adjusts an exam that already exists from a typed instruction ("replace Q3 with an
electrolysis question", "take it down to 30 marks", "drop the multiple choice", "remove
part (c) of Q2"). As with a new request (exam_request.py), Claude only reads the sentence
into a plan - which questions to take out, what the new total is, what any new questions
should be on. Which questions come out to meet a total, and which new ones go in, is
decided here and by exam_builder.py, so the marks add up exactly and nothing is set twice.

Sent to Claude: the instruction, the exam's questions (printed number, source paper and
question, a short snippet of the stem, each part's label, marks and topic names) and the
subject's topic list. That's the same question text tagging already sends.

Questions are named as the builder lists and the paper prints them: written ones Q1..Qn,
then multiple choice MCQ1..MCQm, each section numbered from 1 (quiz_export._section_numbers).
"""

from dataclasses import dataclass, field

from pydantic import BaseModel

from qbx.exam_builder import ExamResult, ExamSpec, build_preferring_new
from qbx.exam_request import MAX_MARKS
from qbx.llm import LLM
from qbx.topics import Topic


class _PartRemoval(BaseModel):
    question: str  # "Q2", "MCQ1"
    parts: list[str] = []  # part labels as listed, e.g. "c", "b.ii"


class _AdjustReply(BaseModel):
    # plain types with empty defaults rather than Optional: simpler for structured output
    remove: list[str] = []
    remove_parts: list[_PartRemoval] = []
    keep: list[str] = []
    total_marks: int = 0
    marks_change: int = 0
    refill: bool = False
    topic_ids: list[str] = []
    question_style: str = ""
    mcq_count: int = 0
    year_from: int = 0
    year_to: int = 0
    papers: list[str] = []
    summary: str = ""
    unmatched: list[str] = []


class Adjustment(BaseModel):
    """`_AdjustReply` checked against the exam: only labels, parts and topics that exist."""

    remove: list[int] = []  # item indices
    remove_parts: dict[int, list[str]] = {}  # item index -> part paths
    keep: list[int] = []
    target: int | None = None  # the exam's new total; None: work it out
    marks_change: int = 0
    refill: bool = False
    topic_ids: list[str] = []
    question_style: str = ""  # "" : decided from what was taken out
    mcq_count: int | None = None
    year_from: int | None = None
    year_to: int | None = None
    papers: list[str] = []
    summary: str = ""
    unmatched: list[str] = []


@dataclass
class _Item:
    """An exam item with what the bank knows about it."""

    paper_key: str
    question_number: int
    part_paths: list[str]
    marks: dict[str, int]  # part path -> marks, for the parts set
    topics: dict[str, list[str]]  # part path -> topic ids
    depends_on: dict[str, list[str]]
    mcq: bool
    stem: str = ""
    label: str = ""

    @property
    def total(self) -> int:
        return sum(self.marks.get(p, 0) for p in self.part_paths)


@dataclass
class AdjustResult:
    items: list[dict] = field(default_factory=list)  # QuizItem-shaped
    total_marks: int = 0
    target: int = 0
    exact: bool = True
    removed: list[str] = field(default_factory=list)  # labels of questions taken out
    added: int = 0  # questions added
    missing_topics: list[str] = field(default_factory=list)


def _entry_question(entries: dict[str, dict], paper_key: str, n: int) -> tuple[dict | None, list[dict], bool]:
    entry = entries.get(paper_key)
    if entry is None:
        return None, [], False
    for i, q in enumerate(entry["questions"]):
        if q["n"] == n:
            return q, entry["parts"][i], entry["mcq"][i]
    return None, [], False


def exam_items(items: list[dict], entries: dict[str, dict]) -> list[_Item]:
    """The exam's items (QuizItem dicts), each labelled as printed. `entries`: paper key ->
    library entry."""
    result = []
    for item in items:
        q, parts, mcq = _entry_question(entries, item["paper_key"], item["question_number"])
        by_path = {p["path"]: p for p in parts}
        result.append(_Item(
            paper_key=item["paper_key"],
            question_number=item["question_number"],
            part_paths=list(item["part_paths"]),
            marks={p: by_path[p]["marks"] for p in item["part_paths"] if p in by_path},
            topics={p: by_path[p]["topics"] for p in item["part_paths"] if p in by_path},
            depends_on={p: by_path[p]["depends_on"] for p in item["part_paths"] if p in by_path},
            mcq=mcq,
            stem=(q or {}).get("stem", ""),
        ))
    written = [i for i in result if not i.mcq]
    mcqs = [i for i in result if i.mcq]
    for n, i in enumerate(written, 1):
        i.label = f"Q{n}"
    for n, i in enumerate(mcqs, 1):
        i.label = f"MCQ{n}"
    return result


def build_prompt(instruction: str, items: list[_Item], spec: ExamSpec, topics: list[Topic]) -> str:
    names = {t.id: t.name for t in topics}
    total = sum(i.total for i in items)
    lines = [
        "A teacher has an exam assembled from past-paper questions and wants it changed:",
        f'"{instruction.strip()}"',
        "",
        f"The exam now ({total} marks). Written questions are Q1, Q2...; multiple-choice "
        "questions are MCQ1, MCQ2... Each part is listed as label (marks): topics.",
    ]
    for i in sorted(items, key=lambda i: (i.mcq, int(i.label.lstrip("MCQ")))):
        lines.append(f"{i.label}: {i.total} marks, from {i.paper_key} question {i.question_number}. {i.stem}")
        for p in i.part_paths:
            label = p or "(whole question)"
            tnames = ", ".join(names.get(t, t) for t in i.topics.get(p, [])) or "untagged"
            lines.append(f"    {label} ({i.marks.get(p, 0)}): {tnames}")
    if topics:
        lines += ["", "Syllabus topics (id, name):"]
        lines += [f"{t.id}  {t.name}" for t in topics]
    asked = ", ".join(names.get(t, t) for t in spec.topic_ids) or "no particular topics"
    lines += [
        "",
        f"It was made for: {asked}; {spec.total_marks} marks.",
        "",
        "Reply with only what the instruction asks for:",
        "- remove: questions to take out entirely, by label (e.g. ['Q3', 'MCQ2']). Anything "
        "the instruction says to replace, swap or drop. 'Remove the multiple choice' lists every MCQ;",
        "- remove_parts: parts to take out of a question that otherwise stays, as "
        "{question: 'Q2', parts: ['c']} using the part labels listed;",
        "- keep: questions the instruction says must stay;",
        "- total_marks: the exam's new total if the instruction names one ('make it 30 marks'), else 0;",
        "- marks_change: a change in marks if given that way ('10 marks shorter' -> -10, "
        "'add 5 marks' -> 5), else 0;",
        "- refill: true if what is removed should be replaced by new questions (replace, swap, "
        "'something else instead'); false if the exam should simply get shorter;",
        "- topic_ids: ids from the topic list that any new questions should be on, if the "
        "instruction says ('replace Q3 with one on electrolysis'). For 'something similar' use "
        "the removed question's topics. Empty to use the exam's own topics;",
        "- question_style: for new questions, 'mcq', 'structured' or 'any' if the instruction "
        "says which kind, else empty;",
        "- mcq_count: how many of the new questions should be multiple choice, if a number is given, else 0;",
        "- year_from / year_to / papers: limits on where new questions come from, if given "
        "('a more recent one' -> year_from a few years after the removed question's), else 0 / empty;",
        "- unmatched: topics asked for that nothing in the list covers, in the instruction's words;",
        "- summary: one short sentence saying what you understood, e.g. 'Replace Q3 with an "
        "electrolysis question'.",
    ]
    return "\n".join(lines)


def _label_index(items: list[_Item]) -> dict[str, int]:
    return {i.label.upper(): n for n, i in enumerate(items)}


def _normal_path(path: str) -> str:
    return path.strip().strip("()").replace(")(", ".").replace("(", ".").replace(")", "").replace(" ", ".").lower()


def read_adjustment(llm: LLM, instruction: str, items: list[_Item], spec: ExamSpec, topics: list[Topic]) -> Adjustment:
    reply = llm.json(build_prompt(instruction, items, spec, topics), _AdjustReply)
    index = _label_index(items)

    def idx(label: str) -> int | None:
        return index.get(label.strip().upper().replace(" ", ""))

    remove = [n for n in dict.fromkeys(idx(l) for l in reply.remove) if n is not None]
    remove_parts: dict[int, list[str]] = {}
    for r in reply.remove_parts:
        n = idx(r.question)
        if n is None or n in remove:
            continue
        known = {_normal_path(p): p for p in items[n].part_paths}
        paths = [known[_normal_path(p)] for p in r.parts if _normal_path(p) in known]
        if paths:
            remove_parts.setdefault(n, []).extend(p for p in paths if p not in remove_parts.get(n, []))
    known_topics = {t.id for t in topics}
    total = reply.total_marks if 0 < reply.total_marks <= MAX_MARKS else None
    style = reply.question_style if reply.question_style in ("structured", "mcq", "any") else ""
    return Adjustment(
        remove=remove,
        remove_parts=remove_parts,
        keep=[n for n in dict.fromkeys(idx(l) for l in reply.keep) if n is not None and n not in remove],
        target=total,
        marks_change=reply.marks_change if abs(reply.marks_change) <= MAX_MARKS else 0,
        refill=reply.refill,
        topic_ids=[t for t in dict.fromkeys(reply.topic_ids) if t in known_topics],
        question_style=style,
        mcq_count=reply.mcq_count if reply.mcq_count > 0 else None,
        year_from=reply.year_from or None,
        year_to=reply.year_to or None,
        papers=[p.strip() for p in reply.papers if p.strip()],
        summary=" ".join(reply.summary.split())[:200],
        unmatched=[u for u in reply.unmatched if u.strip()][:10],
    )


def _without_parts(item: _Item, paths: list[str]) -> list[str]:
    """The item's parts after taking out `paths` and every part that needs one of them."""
    gone = set(paths)
    changed = True
    while changed:
        changed = False
        for p in item.part_paths:
            if p not in gone and gone.intersection(item.depends_on.get(p, [])):
                gone.add(p)
                changed = True
    return [p for p in item.part_paths if p not in gone]


def _drop_for(items: list[tuple[int, _Item]], excess: int) -> list[int]:
    """Indices of whole items to take out to lose `excess` marks: exactly, with as few
    questions as possible, from the end of the exam first; failing that, the fewest marks
    over (the gap is then filled with new questions)."""
    # best way to reach each total: (number of items, indices), later items preferred
    reach: dict[int, list[int]] = {0: []}
    for n, item in reversed(items):
        for total, chosen in list(reach.items()):
            t = total + item.total
            if t not in reach or len(chosen) + 1 < len(reach[t]):
                reach[t] = chosen + [n]
    if excess in reach:
        return reach[excess]
    over = [t for t in reach if t > excess]
    return reach[min(over)] if over else [n for n, _ in items]


def apply_adjustment(
    items: list[_Item],
    adjustment: Adjustment,
    spec: ExamSpec,
    entries: list[tuple[str, dict]],
    topics: list[Topic],
    duplicates: dict[tuple[str, int], int] | None = None,
    topics_by_subject: dict[str, list[Topic]] | None = None,
    clashes: dict[tuple[str, int, str], set[int]] | None = None,
    used: set[tuple[str, int]] | None = None,
    seen: set[tuple[str, int]] | None = None,
) -> AdjustResult:
    """The exam after the adjustment: removals first, then whole questions dropped (from
    the end, never one the teacher said to keep) to come down to a smaller total, then new
    questions from the builder for any gap - none already in the exam or just taken out,
    none set in another exam (`used`), and none whose answer clashes with one kept. Questions
    the exam held before (`seen`) are avoided while others fit (build_preferring_new)."""
    current = sum(i.total for i in items)
    removed = set(adjustment.remove)
    paths = {n: list(i.part_paths) for n, i in enumerate(items)}
    for n, gone in adjustment.remove_parts.items():
        paths[n] = _without_parts(items[n], gone)
        if not paths[n]:
            removed.add(n)

    def kept_total() -> int:
        return sum(items[n].marks.get(p, 0) for n in paths if n not in removed for p in paths[n])

    trimmed = current - kept_total()
    if adjustment.target is not None:
        target = adjustment.target
    elif adjustment.marks_change:
        target = current + adjustment.marks_change
    elif adjustment.refill:
        target = current
    else:
        target = kept_total()
    target = max(0, min(MAX_MARKS, target))

    if kept_total() > target:
        protected = set(adjustment.keep)
        droppable = [
            (n, _Item(**{**items[n].__dict__, "part_paths": paths[n]}))
            for n in range(len(items)) if n not in removed and n not in protected
        ]
        removed.update(_drop_for(droppable, kept_total() - target))

    kept = [n for n in range(len(items)) if n not in removed]
    gap = target - kept_total()
    new_items: list[dict] = []
    added_marks = 0
    missing: list[str] = []
    if gap > 0:
        taken_out = [items[n] for n in removed]
        style = adjustment.question_style
        if not style:
            if taken_out and all(i.mcq for i in taken_out):
                style = "mcq"
            elif taken_out and not any(i.mcq for i in taken_out):
                style = "structured"
            elif spec.mcq_count:
                style = "structured"  # the exam's set number of MCQs stays as it is
            else:
                style = spec.question_style
        fill_spec = spec.model_copy(update={
            "topic_ids": adjustment.topic_ids or spec.topic_ids,
            "total_marks": gap,
            "question_style": "any" if adjustment.mcq_count else style,
            "mcq_count": adjustment.mcq_count,
            "year_from": adjustment.year_from or spec.year_from,
            "year_to": adjustment.year_to or spec.year_to,
            "papers": adjustment.papers or spec.papers,
            "sources": [s.model_copy(update={"papers": adjustment.papers}) for s in spec.sources]
            if adjustment.papers else spec.sources,
        })
        exclude = {(i.paper_key, i.question_number) for i in items} | (used or set())
        taken = frozenset(
            c for n in kept for p in paths[n] for c in (clashes or {}).get((items[n].paper_key, items[n].question_number, p), ())
        )
        built: ExamResult = build_preferring_new(
            entries, fill_spec, topics, duplicates, topics_by_subject, clashes, exclude, taken, seen
        )
        new_items, added_marks = built.items, built.total_marks
        missing = built.missing_topics

    # new questions go where the first one taken out was, else at the end
    result: list[dict] = []
    placed = False
    for n, item in enumerate(items):
        if n in removed:
            if not placed:
                result.extend(new_items)
                placed = True
            continue
        result.append({"paper_key": item.paper_key, "question_number": item.question_number, "part_paths": paths[n]})
    if not placed:
        result.extend(new_items)

    total = kept_total() + added_marks
    return AdjustResult(
        items=result,
        total_marks=total,
        target=target,
        exact=total == target,
        removed=[items[n].label for n in sorted(removed)],
        added=len(new_items),
        missing_topics=missing,
    )
