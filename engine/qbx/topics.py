"""Per-subject syllabus topic lists - what parts are tagged with and exams are asked for.

Stored as CONFIG_DIR/topics/<subject>.json, outside out/ so re-extraction never touches
them. Filled from a syllabus PDF the user uploads and then editable by hand; the list is the
only vocabulary tagging may use.

Claude reads the syllabus, not a parser (user-decided). Cambridge and Edexcel lay theirs out
differently - Cambridge's "4.1 Fluid mosaic membranes / Learning outcomes / 1 describe...",
sometimes in Core and Supplement columns; Edexcel's "1.1 Integers / Students should be
taught to: / A understand...", printed once per tier with the same numbering - and the next
board will differ again. Two steps (`read_syllabus`): the topic tree, with the pages each
top-level topic is printed on, then each top-level topic's learning outcomes from just those
pages, a few at a time. The outcomes are what tagging matches a question against
(`topics_prompt_list(outcomes=True)`): a heading like "Stoichiometry" says little about
whether a part on the volume of a gas belongs there.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor

import pymupdf
from pydantic import BaseModel

from qbx import llm as llm_mod
from qbx.llm import LLM

_SUBJECT_RE = re.compile(r"^[A-Za-z0-9]{1,12}$")


class Topic(BaseModel):
    id: str  # the syllabus's own numbering: "4", "4.1"
    name: str
    parent: str | None = None
    # what a student must know or do, condensed from the syllabus's learning outcomes, one
    # per item; a tier's or column's own ones prefixed ("Supplement:", "Higher:")
    outcomes: list[str] = []


class TopicList(BaseModel):
    subject: str
    topics: list[Topic] = []


def _path(subject: str):
    if not _SUBJECT_RE.match(subject):
        raise ValueError(f"not a subject code: {subject!r}")
    return llm_mod.CONFIG_DIR / "topics" / f"{subject}.json"


def load_topics(subject: str) -> TopicList:
    """The subject's topic list: on the Classark provider the server's (`_served`) when it
    has one, otherwise this computer's own file."""
    path = _path(subject)  # a bad subject code raises, rather than reading as "no topics"
    served = _served(subject)
    if served is not None:
        return served
    try:
        return TopicList.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TopicList(subject=subject)


# -- the Classark server's lists ---------------------------------------------------------
# On the Classark provider every teacher tags against the server's list for the subject
# (GET /v1/classark/topics/<subject>, uploaded by an admin from these same files), used
# exactly as served: the server's shared tag cache hits only when two teachers' prompts
# match byte for byte. A copy is kept in config/topics/classark/ with its ETag and checked
# at most every SERVER_CHECK_SECONDS; without the internet, the copy is used.

SERVER_CHECK_SECONDS = 3600
_checked: dict[str, float] = {}


def _served_path(subject: str):
    return llm_mod.CONFIG_DIR / "topics" / "classark" / f"{subject}.json"


def _served(subject: str) -> TopicList | None:
    import time

    if llm_mod._override is not None or llm_mod.active_settings().provider != "classark":
        return None
    from qbx import account

    if not account.signed_in():
        return None
    path = _served_path(subject)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = None
    if time.time() - _checked.get(subject, 0.0) > SERVER_CHECK_SECONDS:
        try:
            status, etag, body = account.get(f"/v1/classark/topics/{subject}", (saved or {}).get("etag"))
        except account.AccountError:
            status = None  # offline: the copy, if any
        if status is not None:
            _checked[subject] = time.time()
        if status == 200 and isinstance(body, dict):
            saved = {"etag": etag, "topics": body.get("topics") or []}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
        elif status == 404:  # the server has no list for this subject (any more)
            saved = None
            path.unlink(missing_ok=True)
    if not saved:
        return None
    try:
        return TopicList(subject=subject, topics=[Topic.model_validate(t) for t in saved["topics"]])
    except (ValueError, KeyError, TypeError):
        return None


def clean_topics(topics: list[Topic]) -> list[Topic]:
    """Drops blank and repeated ids, and parents that aren't in the list (a topic whose
    parent is missing becomes top-level rather than being lost)."""
    seen: dict[str, Topic] = {}
    for t in topics:
        tid, name = t.id.strip(), " ".join(t.name.split())
        if tid and name and tid not in seen:
            outcomes = [o for o in (" ".join(o.split()) for o in t.outcomes) if o]
            seen[tid] = Topic(id=tid, name=name, parent=(t.parent or "").strip() or None, outcomes=outcomes)
    for t in seen.values():
        if t.parent not in seen or t.parent == t.id:
            t.parent = None
    return list(seen.values())


def save_topics(subject: str, topics: list[Topic]) -> TopicList:
    result = TopicList(subject=subject, topics=clean_topics(topics))
    path = _path(subject)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def descendants(topics: list[Topic], ids: list[str]) -> set[str]:
    """The ids asked for plus every topic under them - asking for "4" means all of 4.x."""
    children: dict[str, list[str]] = {}
    for t in topics:
        if t.parent:
            children.setdefault(t.parent, []).append(t.id)
    out, stack = set(), list(ids)
    while stack:
        tid = stack.pop()
        if tid not in out:
            out.add(tid)
            stack.extend(children.get(tid, []))
    return out


def topics_prompt_list(topics: list[Topic], outcomes: bool = False) -> str:
    """One "id  name" line per topic; with `outcomes`, its learning outcomes indented under
    it (tagging - reading a request only needs the names)."""
    lines = []
    for t in topics:
        lines.append(f"{t.id}  {t.name}")
        if outcomes:
            lines += [f"    - {o}" for o in t.outcomes]
    return "\n".join(lines)


# -- reading a syllabus ------------------------------------------------------------------

# characters: a whole syllabus is 80-150K (Edexcel 4MA1 83K, Cambridge 9700 143K) and goes in
# one request; this only stops a mistaken upload of something enormous
_SYLLABUS_TEXT_LIMIT = 400_000
# top-level topics whose outcomes one request reads, and requests at once
_OUTCOME_GROUP = 2
_OUTCOME_WORKERS = 4
# topics a second-chance request asks about (each reads the whole syllabus)
_RETRY_GROUP = 6

_BOARD_NOTES = (
    "Syllabuses differ by board. Cambridge numbers topics '4' and subtopics '4.1', each with "
    "numbered learning outcomes ('Candidates should be able to: 1 describe...'), sometimes in "
    "Core and Supplement columns. Edexcel (Pearson) numbers them '1' and '1.1' under assessment "
    "objective headings ('AO1 Numbers and algebra' is not a topic), with lettered outcomes "
    "('Students should be taught to: A understand and use...'), and may print the whole content "
    "twice, once per tier (Foundation, Higher), with the same numbering - that is one topic per "
    "id, never one per tier. Other boards may differ again: follow the document's own numbering."
)


class _SyllabusTopic(BaseModel):
    id: str
    name: str
    parent: str | None = None
    # top-level topics: every page its content, or its subtopics', is printed on
    pages: list[int] = []


class _SyllabusReply(BaseModel):
    topics: list[_SyllabusTopic]


class _TopicOutcomes(BaseModel):
    id: str
    outcomes: list[str]


class _OutcomesReply(BaseModel):
    topics: list[_TopicOutcomes]


def syllabus_pages(pdf_bytes: bytes) -> list[str]:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        return [page.get_text("text") for page in doc]


def _with_page_numbers(pages: list[str], numbers: list[int] | None = None) -> str:
    chosen = numbers if numbers else list(range(1, len(pages) + 1))
    text = "\n".join(f"=== page {n} ===\n{pages[n - 1]}" for n in chosen if 1 <= n <= len(pages))
    return text[:_SYLLABUS_TEXT_LIMIT]


def syllabus_text(pdf_bytes: bytes) -> str:
    return _with_page_numbers(syllabus_pages(pdf_bytes))


def _tree_prompt(subject: str, text: str) -> str:
    return (
        f"This is the text of an exam syllabus (specification) for subject {subject}, page by "
        "page. List its subject-content topics as a tree, using the syllabus's own numbering as "
        "each id (e.g. '4' for a top-level topic and '4.1' for a subtopic under it, with parent "
        "'4'). Include the top-level topics and their numbered subtopics only - not individual "
        "learning outcomes, assessment objectives, practical skills lists or administrative "
        "sections. Use the syllabus's own short heading as each name. For each top-level topic "
        "also give `pages`: every page number on which its content or its subtopics' content is "
        "printed (every tier or column of it); leave `pages` empty for subtopics.\n\n"
        + _BOARD_NOTES
        + "\n\n---\n"
        + text
    )


def _outcomes_prompt(subject: str, wanted: list[Topic], text: str) -> str:
    listing = "\n".join(f"{t.id}  {t.name}" for t in wanted)
    return (
        f"These pages are from the syllabus for subject {subject}. For each topic below, list "
        "the learning outcomes printed under it - what students must know or be able to do. One "
        "item per outcome, condensed: drop 'Candidates should be able to' and the like, but keep "
        "every technical term, named example, formula, quantity and limit (\"not required\", "
        "\"limited to\"). A tier's or column's extra outcomes go in the same topic, each prefixed "
        "with that tier or column's name (\"Supplement:\", \"Extended:\", \"Higher:\"); where a "
        "tier only says \"see Foundation Tier\", add nothing for it. Leave out worked examples "
        "and teacher notes unless they say what is or isn't assessed. A topic with subtopics gets "
        "only the outcomes printed directly under it, if any. Use the ids exactly as given. "
        "Outcomes printed under deeper numbered headings that aren't listed (1.5.1, 1.5.2 under "
        "1.5) belong to the listed topic they fall under: give them under that topic's id. "
        "Where the syllabus lists content items rather than outcome sentences (a numbered item "
        "beside a \"Know and understand\" / \"Be able to\" column), each item is an outcome: give "
        "it with what it covers and what students must be able to do (\"Harvard architecture: "
        "memory structure, difference from von Neumann; complete a diagram of it\").\n\n"
        + _BOARD_NOTES
        + "\n\nTopics:\n"
        + listing
        + "\n\n---\n"
        + text
    )


def read_syllabus(llm: LLM, subject: str, pages: list[str] | str) -> list[Topic]:
    """The syllabus's topic tree with each topic's learning outcomes, read by Claude: one
    request for the tree, then one per couple of top-level topics, from their pages only."""
    if isinstance(pages, str):
        pages = [pages]
    tree = llm.json(_tree_prompt(subject, _with_page_numbers(pages)), _SyllabusReply).topics
    topics = clean_topics([Topic(id=t.id, name=t.name, parent=t.parent) for t in tree])
    by_id = {t.id: t for t in topics}
    on_pages = {t.id.strip(): {n for n in t.pages if 1 <= n <= len(pages)} for t in tree}

    top = [t for t in topics if t.parent is None]
    groups = [top[i : i + _OUTCOME_GROUP] for i in range(0, len(top), _OUTCOME_GROUP)]

    def read_group(group: list[Topic], whole: bool = False) -> list[_TopicOutcomes]:
        ids = set(descendants(topics, [t.id for t in group])) if not whole else {t.id for t in group}
        wanted = [t for t in topics if t.id in ids]
        # a page either side: a topic can start at the foot of the page before. A topic
        # given no pages means the whole syllabus.
        numbers = []
        if not whole and all(on_pages.get(t.id) for t in group):
            numbers = sorted(
                {m for t in group for n in on_pages[t.id] for m in (n - 1, n, n + 1) if 1 <= m <= len(pages)}
            )
        return llm.json(_outcomes_prompt(subject, wanted, _with_page_numbers(pages, numbers)), _OutcomesReply).topics

    def listed(topic_id: str) -> Topic | None:
        # a syllabus numbered deeper than the tree (0625's 1.5.1 under 1.5, Business's
        # 1.1.1): Claude may still answer by the deeper id - its outcomes go to the listed
        # topic it falls under. Dropping them left 0625 with 11 of 30 topics read.
        parts = topic_id.strip().rstrip(".").split(".")
        while parts:
            if (found := by_id.get(".".join(parts))) is not None:
                return found
            parts.pop()
        return None

    read: dict[str, list[str]] = {}

    def take(reply: list[_TopicOutcomes]) -> None:
        for got in reply:
            topic = listed(got.id)
            if topic is not None:
                read.setdefault(topic.id, []).extend(o for o in got.outcomes if o not in read.get(topic.id, []))

    with ThreadPoolExecutor(max_workers=_OUTCOME_WORKERS) as pool:
        for reply in pool.map(read_group, groups):
            take(reply)
        # every lowest-level topic still without outcomes is asked again, on its own, from
        # the whole syllabus: the tree's pages can be wrong (0648's topics 7-8, printed on
        # page 14, read empty twice), and a long group can come back half answered (9488
        # read 19 of 23 topics once and 8 of 23 the next time, the syllabus unchanged)
        parents = {t.parent for t in topics}
        missing = [t for t in topics if t.id not in parents and not read.get(t.id)]
        retry = [missing[i : i + _RETRY_GROUP] for i in range(0, len(missing), _RETRY_GROUP)]
        for reply in pool.map(lambda g: read_group(g, whole=True), retry):
            take(reply)
    for topic_id, outcomes in read.items():
        if not by_id[topic_id].outcomes:
            by_id[topic_id].outcomes = outcomes
    return clean_topics(topics)


def as_json(topic_list: TopicList) -> dict:
    return json.loads(topic_list.model_dump_json())
