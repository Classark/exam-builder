"""Chooses questions and parts from the bank to make an exam on given topics, out of an
exact number of marks. Pure code, no model: the choice is reproducible from its seed, and
the total is added up here rather than trusted to anyone.

What can be chosen ("units"), per question:
- the parts on the requested topics, together with every earlier part they depend on
  (tags.json `depends_on`) - so a part is never set without what it needs;
- or one of those parts alone, again with what it depends on, when that helps hit the
  total. A unit holding every part of the question is the whole question.
A question not fully tagged can only be taken whole. A question is used at most once, and
so is a question reprinted in several papers (similar.py): its copies are pooled as one
choice. Nor are two parts set whose mark scheme answers are the same (similar.py's answer
clashes) - one thing asked twice in different words.

The choice: a randomised greedy pass fills the exam topic by topic (whichever requested
topic has the fewest marks so far goes next), preferring whole questions and recent papers;
a knapsack over the remaining questions, looked through newest-leaning first, then makes up
the exact remainder. Many such attempts are scored - exact total, then balance across
topics, then fewest cut-up questions - and the best kept. Age isn't scored: among equally
good exams that would always pick the newest, and "Try different questions" would keep
coming back to the same papers.

Recent papers are preferred, never required: a paper's pull falls by a quarter for each
year it is older than the newest in the pool (RECENT_DECAY), so last year's questions come
up far more often than a decade-old one's, but an old question still fills a topic the
recent papers don't cover.
"""

import random
from dataclasses import dataclass, field

from pydantic import BaseModel

from qbx.topics import Topic, descendants

ATTEMPTS = 40
# questions the exact-remainder knapsack looks through per attempt - plenty to find a fill,
# and keeps an attempt fast on a bank of thousands
KNAPSACK_POOL = 300
MAX_BACKTRACK = 4
# the pull towards recent papers: RECENT_WEIGHT x RECENT_DECAY ** (years older than the
# newest paper in the pool), against a random 0-1 and 0.6 for a whole question
RECENT_WEIGHT = 0.9
RECENT_DECAY = 0.75


class ExamSource(BaseModel):
    """One subject code an exam draws on, and which of its papers (empty: any)."""

    subject: str
    papers: list[str] = []


class ExamSpec(BaseModel):
    # the subject whose topic list the request was read against
    subject: str | None = None
    topic_ids: list[str] = []
    total_marks: int = 40
    year_from: int | None = None
    year_to: int | None = None
    papers: list[str] = []  # paper numbers, e.g. ["4"]; empty means any
    # "structured" (written answers), "mcq" (multiple choice) or "any"
    question_style: str = "structured"
    # Exactly this many multiple-choice questions, the rest of the marks written ("20 marks,
    # only 4 multiple choice"). None: `question_style` alone decides.
    mcq_count: int | None = None
    seed: int = 0
    # Subject codes picked in the builder, each with its own papers - an exam can draw on
    # several (0620 and 0971 are the same chemistry). Empty: `subject` with `papers`.
    sources: list[ExamSource] = []
    # The exam board every question comes from: an exam never mixes boards (their papers,
    # mark schemes and periodic tables differ). None: the board of `subject`.
    board: str | None = None

    def source_list(self) -> list[ExamSource]:
        if self.sources:
            return self.sources
        return [ExamSource(subject=self.subject, papers=self.papers)] if self.subject else []


@dataclass
class _Unit:
    paper_key: str
    question_number: int
    part_paths: list[str]
    marks: int
    groups: frozenset[int]  # indices of the requested topics it covers
    whole: bool
    year: int
    mcq: bool = False
    # answer clash ids of its parts: a unit sharing one with an already chosen unit is out
    clashes: frozenset[int] = frozenset()


@dataclass
class ExamResult:
    items: list[dict] = field(default_factory=list)  # QuizItem-shaped
    total_marks: int = 0
    exact: bool = False
    marks_by_topic: dict[str, int] = field(default_factory=dict)
    missing_topics: list[str] = field(default_factory=list)


def _full_year(year: int) -> int:
    return year + 2000 if year < 100 else year


def _closure(paths: list[str], parts: list[dict]) -> list[str]:
    by_path = {p["path"]: p for p in parts}
    need, stack = set(paths), list(paths)
    while stack:
        for d in by_path.get(stack.pop(), {}).get("depends_on", []):
            if d not in need and d in by_path:
                need.add(d)
                stack.append(d)
    return [p["path"] for p in parts if p["path"] in need]


def _normal(name: str) -> str:
    return " ".join(name.lower().split())


def _wanted_in(spec: ExamSpec, topics: list[Topic], subject_topics: list[Topic]) -> list[set[str]]:
    """The requested topics (ids from `spec.subject`'s list) as another subject's own ids,
    matched by name - two codes of one subject (0620, 0971) name their topics alike. A topic
    with no match there asks for nothing from that subject."""
    names = {t.id: _normal(t.name) for t in topics}
    by_name: dict[str, list[str]] = {}
    for t in subject_topics:
        by_name.setdefault(_normal(t.name), []).append(t.id)
    return [
        descendants(subject_topics, by_name.get(names.get(tid, ""), [])) for tid in spec.topic_ids
    ]


def candidate_units(
    entries: list[tuple[str, dict]],
    spec: ExamSpec,
    topics: list[Topic],
    duplicates: dict[tuple[str, int], int] | None = None,
    topics_by_subject: dict[str, list[Topic]] | None = None,
    clashes: dict[tuple[str, int, str], set[int]] | None = None,
    exclude: set[tuple[str, int]] | None = None,
) -> tuple[list[list[_Unit]], list[set[str]]]:
    """Units grouped by question - a question contributes at most one, and reprints of one
    question (`duplicates`: (paper key, number) -> group) share a single group - and each
    requested topic expanded to itself plus its subtopics. `clashes`: (paper key, number,
    part path) -> answer clash ids (Library.answer_clashes). `exclude`: (paper key, number)
    of questions already in the exam, or just taken out of it - neither they nor their
    reprints are offered."""
    clashes = clashes or {}
    exclude = exclude or set()

    def clashes_of(paper_key: str, n: int, paths: list[str]) -> frozenset[int]:
        return frozenset(c for path in paths for c in clashes.get((paper_key, n, path), ()))

    wanted = [descendants(topics, [t]) for t in spec.topic_ids] if spec.topic_ids else []
    sources = {src.subject: src.papers for src in spec.source_list()}
    wanted_by_subject: dict[str, list[set[str]]] = {}

    def wanted_for(subject: str) -> list[set[str]]:
        if subject == spec.subject or not spec.topic_ids:
            return wanted
        if subject not in wanted_by_subject:
            other = (topics_by_subject or {}).get(subject, [])
            wanted_by_subject[subject] = _wanted_in(spec, topics, other)
        return wanted_by_subject[subject]

    by_question: list[list[_Unit]] = []
    for paper_key, entry in entries:
        s = entry["summary"]
        year = _full_year(s["year"])
        if spec.year_from and year < spec.year_from or spec.year_to and year > spec.year_to:
            continue
        if sources and s["subject"] not in sources:
            continue
        papers = sources.get(s["subject"], spec.papers) if sources else spec.papers
        if papers and str(s["paper"]) not in papers:
            continue
        subject_wanted = wanted_for(s["subject"])
        for q, parts, mcq in zip(entry["questions"], entry["parts"], entry["mcq"]):
            if not parts or spec.question_style == "structured" and mcq or spec.question_style == "mcq" and not mcq:
                continue
            if (paper_key, q["n"]) in exclude:
                continue

            def groups_of(topic_ids) -> frozenset[int]:
                if not subject_wanted:
                    return frozenset([0])  # no topics asked for: everything counts
                return frozenset(i for i, w in enumerate(subject_wanted) if w.intersection(topic_ids))

            all_paths = [p["path"] for p in parts]
            if not all(p["tagged"] for p in parts):
                g = groups_of({t for p in parts for t in p["topics"]}) if wanted else frozenset([0])
                if g:
                    by_question.append([_Unit(
                        paper_key, q["n"], all_paths, q["marks"], g, True, year, mcq,
                        clashes_of(paper_key, q["n"], all_paths),
                    )])
                continue
            matching = [p for p in parts if groups_of(p["topics"])]
            if not matching:
                continue
            options: dict[tuple[str, ...], _Unit] = {}

            def add(paths: list[str]) -> None:
                chosen = [p for p in parts if p["path"] in set(paths)]
                key = tuple(paths)
                if key not in options:
                    options[key] = _Unit(
                        paper_key, q["n"], paths, sum(p["marks"] for p in chosen),
                        groups_of({t for p in chosen for t in p["topics"]}), paths == all_paths, year, mcq,
                        clashes_of(paper_key, q["n"], paths),
                    )

            add(_closure([p["path"] for p in matching], parts))
            if len(matching) > 1:
                for p in matching:
                    add(_closure([p["path"]], parts))
            units = [u for u in options.values() if u.marks > 0 and u.groups]
            if units:
                by_question.append(units)
    # pool reprints: one choice among all their copies
    pooled: dict[int, list[_Unit]] = {}
    merged: list[list[_Unit]] = []
    banned = {(duplicates or {}).get(key) for key in exclude} - {None}
    for units in by_question:
        group = (duplicates or {}).get((units[0].paper_key, units[0].question_number))
        if group in banned:
            continue  # a reprint of a question the exam already has (or just dropped)
        if group is None:
            merged.append(units)
        elif group in pooled:
            pooled[group].extend(units)
        else:
            pooled[group] = list(units)
            merged.append(pooled[group])
    return merged, wanted


def _knapsack(questions: list[list[_Unit]], target: int, taken: frozenset[int] = frozenset()) -> list[_Unit] | None:
    """At most one unit per question, adding up to exactly `target`, none clashing with
    `taken` or each other; None if not found (one way is kept per total)."""
    if target == 0:
        return []
    reach: dict[int, tuple[list[_Unit], frozenset[int]]] = {0: ([], taken)}
    for units in questions:
        new = dict(reach)
        for total, (chosen, blocked) in reach.items():
            for u in units:
                t = total + u.marks
                if t <= target and t not in new and not u.clashes & blocked:
                    new[t] = (chosen + [u], blocked | u.clashes)
        reach = new
        if target in reach:
            return reach[target][0]
    return None


def _recency(year: int, newest: int) -> float:
    return RECENT_WEIGHT * RECENT_DECAY ** max(0, newest - year)


def _attempt(
    questions: list[list[_Unit]], n_groups: int, target: int, rng: random.Random, taken: frozenset[int] = frozenset()
) -> list[_Unit]:
    """`taken`: answer clash ids already used elsewhere in the exam."""
    newest = max((u.year for units in questions for u in units), default=0)
    # a random order leaning newest-first - what the knapsack below looks through
    weight = [rng.random() + _recency(max(u.year for u in units), newest) for units in questions]
    order = sorted(range(len(questions)), key=lambda qi: -weight[qi])
    used: set[int] = set()
    chosen: list[tuple[int, _Unit]] = []
    marks_by_group = [0] * n_groups
    exhausted: set[int] = set()
    remaining = target
    blocked = set(taken)

    while remaining > 0 and len(exhausted) < n_groups:
        g = min((i for i in range(n_groups) if i not in exhausted), key=lambda i: (marks_by_group[i], rng.random()))
        best, best_score = None, None
        for qi in order:
            if qi in used:
                continue
            for u in questions[qi]:
                if g not in u.groups or u.marks > remaining or u.clashes & blocked:
                    continue
                score = rng.random() + (0.6 if u.whole else 0) + _recency(u.year, newest)
                if best_score is None or score > best_score:
                    best, best_score = (qi, u), score
        if best is None:
            exhausted.add(g)
            continue
        used.add(best[0])
        chosen.append(best)
        blocked |= best[1].clashes
        remaining -= best[1].marks
        for gi in best[1].groups:
            marks_by_group[gi] += best[1].marks

    # make up the exact remainder, giving back the last few picks if that's what it takes
    for give_back in range(0, min(MAX_BACKTRACK, len(chosen)) + 1):
        kept = chosen[: len(chosen) - give_back]
        gap = target - sum(u.marks for _, u in kept)
        kept_q = {qi for qi, _ in kept}
        rest = [questions[qi] for qi in order if qi not in kept_q][:KNAPSACK_POOL]
        fill = _knapsack(rest, gap, taken.union(*(u.clashes for _, u in kept)))
        if fill is not None:
            return [u for _, u in kept] + fill
    return [u for _, u in chosen]


def _attempt_with_mcq(
    questions: list[list[_Unit]], n_groups: int, target: int, mcq_count: int, rng: random.Random,
    taken: frozenset[int] = frozenset(),
) -> list[_Unit]:
    """`mcq_count` multiple-choice questions, then the rest of the marks from written ones.

    Real finding: "20 marks, only 4 multiple choice" came back entirely multiple choice -
    there was no way to say how many, and one-mark questions are the easiest way to land an
    exact total. So the two kinds are chosen separately: the multiple-choice questions first
    (a one-mark question per mark, balanced across topics as usual), then the written
    questions to make up the exact remainder. With fewer on-topic multiple-choice questions
    than asked for, it takes them all and the written ones cover the difference."""
    mcq_qs = [[u for u in units if u.mcq] for units in questions]
    mcq_qs = [units for units in mcq_qs if units]
    written_qs = [[u for u in units if not u.mcq] for units in questions]
    written_qs = [units for units in written_qs if units]
    per_question = min((u.marks for units in mcq_qs for u in units), default=1) or 1
    mcq_target = min(mcq_count * per_question, target)
    chosen_mcq = _attempt(mcq_qs, n_groups, mcq_target, rng, taken)[:mcq_count]
    written_target = target - sum(u.marks for u in chosen_mcq)
    taken = taken.union(*(u.clashes for u in chosen_mcq))
    chosen_written = _attempt(written_qs, n_groups, written_target, rng, taken) if written_target > 0 else []
    return chosen_written + chosen_mcq


def _score(units: list[_Unit], n_groups: int, target: int) -> float:
    total = sum(u.marks for u in units)
    by_group = [0] * n_groups
    for u in units:
        for g in u.groups:
            by_group[g] += u.marks
    share = target / n_groups if n_groups else 0
    imbalance = sum(abs(m - share) for m in by_group) / max(target, 1)
    missing = sum(1 for m in by_group if m == 0)
    fragments = sum(1 for u in units if not u.whole)
    same_paper = len(units) - len({u.paper_key for u in units})
    return abs(target - total) * 100 + missing * 20 + imbalance * 10 + fragments * 1.5 + same_paper * 0.5


def build_exam(
    entries: list[tuple[str, dict]],
    spec: ExamSpec,
    topics: list[Topic],
    duplicates: dict[tuple[str, int], int] | None = None,
    topics_by_subject: dict[str, list[Topic]] | None = None,
    clashes: dict[tuple[str, int, str], set[int]] | None = None,
    exclude: set[tuple[str, int]] | None = None,
    taken: frozenset[int] = frozenset(),
) -> ExamResult:
    """`exclude` and `taken` are for adding to an exam that already has questions
    (exam_adjust.py): the questions it holds, and the answer clash ids of their parts."""
    counted = spec.mcq_count is not None and spec.mcq_count > 0
    # a set number of multiple-choice questions needs both kinds in the pool
    pool_spec = spec.model_copy(update={"question_style": "any"}) if counted else spec
    questions, wanted = candidate_units(entries, pool_spec, topics, duplicates, topics_by_subject, clashes, exclude)
    n_groups = max(len(wanted), 1)
    covered = set().union(*(u.groups for units in questions for u in units)) if questions else set()
    missing = [spec.topic_ids[i] for i in range(len(wanted)) if i not in covered]

    best, best_score = [], None
    for i in range(ATTEMPTS):
        rng = random.Random(spec.seed * 7919 + i)
        if counted:
            units = _attempt_with_mcq(questions, n_groups, spec.total_marks, spec.mcq_count, rng, taken)
        else:
            units = _attempt(questions, n_groups, spec.total_marks, rng, taken)
        score = _score(units, n_groups, spec.total_marks)
        if counted:
            # the number of multiple-choice questions asked for outweighs everything but the total
            score += abs(sum(1 for u in units if u.mcq) - spec.mcq_count) * 50
        if best_score is None or score < best_score:
            best, best_score = units, score

    # grouped by topic as asked, then oldest paper first within a topic
    best.sort(key=lambda u: (min(u.groups) if u.groups else 0, u.year, u.paper_key, u.question_number))
    by_topic = {}
    for i, tid in enumerate(spec.topic_ids):
        by_topic[tid] = sum(u.marks for u in best if i in u.groups)
    total = sum(u.marks for u in best)
    return ExamResult(
        items=[{"paper_key": u.paper_key, "question_number": u.question_number, "part_paths": u.part_paths} for u in best],
        total_marks=total,
        exact=total == spec.total_marks,
        marks_by_topic=by_topic,
        missing_topics=missing,
    )


def build_preferring_new(
    entries: list[tuple[str, dict]],
    spec: ExamSpec,
    topics: list[Topic],
    duplicates: dict[tuple[str, int], int] | None = None,
    topics_by_subject: dict[str, list[Topic]] | None = None,
    clashes: dict[tuple[str, int, str], set[int]] | None = None,
    exclude: set[tuple[str, int]] | None = None,
    taken: frozenset[int] = frozenset(),
    avoid: set[tuple[str, int]] | None = None,
) -> ExamResult:
    """build_exam, keeping out `avoid` too while that still makes as good an exam.
    `avoid` is every question this exam has shown before. Without it, changing a question
    again and again cycled back to ones the teacher had already swapped out, since only
    the exam's current questions were excluded (user-reported). It's a preference, not a
    rule: when no other questions hit the total and topics, it falls back to old ones."""
    exclude = exclude or set()
    fresh_avoid = (avoid or set()) - exclude
    if not fresh_avoid:
        return build_exam(entries, spec, topics, duplicates, topics_by_subject, clashes, exclude, taken)
    fresh = build_exam(entries, spec, topics, duplicates, topics_by_subject, clashes, exclude | fresh_avoid, taken)
    if fresh.exact and len(fresh.missing_topics) == 0 and fresh.items:
        return fresh
    fallback = build_exam(entries, spec, topics, duplicates, topics_by_subject, clashes, exclude, taken)

    def quality(r: ExamResult) -> tuple:
        return (bool(r.items), r.exact, -len(r.missing_topics), -abs(spec.total_marks - r.total_marks))

    return fresh if quality(fresh) >= quality(fallback) else fallback
